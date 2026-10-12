#!/usr/bin/env python3
"""Mirror the Raspberry Pi screen onto an ESP32-2432S028 (CYD) over USB serial.

Run this on the Pi with the CYD plugged into one of its USB ports:

    python3 cyd_display.py                 # auto-detect capture method
    python3 cyd_display.py --touch         # also use the CYD as a touch pointer
    python3 cyd_display.py --source test   # moving test pattern, no capture

See ../firmware/src/main.cpp for the link protocol.
"""
import argparse
import io
import os
import shutil
import struct
import subprocess
import sys
import threading
import time
import zlib

import serial
from PIL import Image, ImageDraw

W, H = 320, 240
MAGIC = b"CYDF"
MAX_FRAME = 60000
ACK_TIMEOUT = 1.5


class FbCapture:
    """Linux framebuffer: the text console, or a desktop without Wayland/KMS overlays."""

    def __init__(self, dev):
        sysdir = f"/sys/class/graphics/{os.path.basename(dev)}"
        with open(f"{sysdir}/virtual_size") as f:
            self.size = tuple(int(v) for v in f.read().split(","))
        with open(f"{sysdir}/bits_per_pixel") as f:
            bpp = int(f.read())
        with open(f"{sysdir}/stride") as f:
            self.stride = int(f.read())
        try:
            self.rawmode = {32: "BGRX", 24: "BGR", 16: "BGR;16"}[bpp]
        except KeyError:
            raise SystemExit(f"{dev}: unsupported depth {bpp} bpp")
        self.dev = dev

    def grab(self):
        with open(self.dev, "rb") as f:
            data = f.read(self.stride * self.size[1])
        return Image.frombuffer("RGB", self.size, data, "raw", self.rawmode, self.stride, 1)


class GrimCapture:
    """Wayland (labwc / wayfire, the Raspberry Pi OS Bookworm desktop)."""

    def grab(self):
        out = subprocess.run(["grim", "-c", "-t", "ppm", "-"], check=True, capture_output=True).stdout
        return Image.open(io.BytesIO(out)).convert("RGB")


class SpectacleCapture:
    """KDE Plasma on Wayland, where grim is not supported. Slower: a few frames per second."""

    def __init__(self):
        self.path = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), f"cyd-{os.getpid()}.bmp")

    def grab(self):
        subprocess.run(["spectacle", "-b", "-n", "-m", "-o", self.path], check=True, capture_output=True)
        with Image.open(self.path) as img:
            return img.convert("RGB")


class X11Capture:
    def __init__(self):
        import mss

        self.sct = mss.mss()

    def grab(self):
        shot = self.sct.grab(self.sct.monitors[1])
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


class TestCapture:
    def grab(self):
        img = Image.new("RGB", (W, H), "black")
        d = ImageDraw.Draw(img)
        for i, c in enumerate(("red", "green", "blue", "white")):
            d.rectangle((i * 80, 0, i * 80 + 79, 60), fill=c)
        d.rectangle((0, 0, W - 1, H - 1), outline="yellow")
        d.text((100, 110), time.strftime("%H:%M:%S"), fill="white")
        x = int(time.time() * 40) % (W - 20)
        d.rectangle((x, 180, x + 20, 200), fill="orange")
        return img


def make_capture(source, fbdev):
    if source == "auto":
        wayland = os.environ.get("WAYLAND_DISPLAY")
        if wayland and "KDE" in os.environ.get("XDG_CURRENT_DESKTOP", "") and shutil.which("spectacle"):
            source = "spectacle"
        elif wayland and shutil.which("grim"):
            source = "grim"
        elif os.environ.get("DISPLAY"):
            source = "x11"
        else:
            source = "fb"
        print(f"capture source: {source}")
    if source == "grim":
        return GrimCapture()
    if source == "spectacle":
        return SpectacleCapture()
    if source == "x11":
        return X11Capture()
    if source == "test":
        return TestCapture()
    return FbCapture(fbdev)


def fit(img, stretch):
    """Scale to the panel. Returns the 320x240 frame and the box the image occupies in it."""
    if stretch:
        return img.resize((W, H), Image.BILINEAR), (0, 0, W, H)
    scale = min(W / img.width, H / img.height)
    w, h = max(1, round(img.width * scale)), max(1, round(img.height * scale))
    x, y = (W - w) // 2, (H - h) // 2
    frame = Image.new("RGB", (W, H), "black")
    frame.paste(img.resize((w, h), Image.BILINEAR), (x, y))
    return frame, (x, y, w, h)


def encode(frame, quality):
    while True:
        buf = io.BytesIO()
        frame.save(buf, "JPEG", quality=quality)  # baseline; the ESP32 can't decode progressive
        if buf.tell() <= MAX_FRAME or quality <= 20:
            return buf.getvalue()
        quality -= 15


class TouchPointer:
    """Turns CYD touches into an absolute pointer (like a tablet) via uinput."""

    def __init__(self):
        from evdev import AbsInfo, UInput, ecodes as e

        self.e = e
        axis = AbsInfo(value=0, min=0, max=65535, fuzz=0, flat=0, resolution=0)
        self.ui = UInput(
            {e.EV_KEY: [e.BTN_LEFT, e.BTN_RIGHT], e.EV_ABS: [(e.ABS_X, axis), (e.ABS_Y, axis)]},
            name="cyd-touch",
        )
        self.box = (0, 0, W, H)
        self.down = False

    def event(self, x, y, pressed):
        e = self.e
        bx, by, bw, bh = self.box
        nx = min(max((x - bx) / bw, 0.0), 1.0)
        ny = min(max((y - by) / bh, 0.0), 1.0)
        self.ui.write(e.EV_ABS, e.ABS_X, int(nx * 65535))
        self.ui.write(e.EV_ABS, e.ABS_Y, int(ny * 65535))
        if pressed != self.down:
            self.ui.syn()  # move first so the click lands at the new position
            self.ui.write(e.EV_KEY, e.BTN_LEFT, int(pressed))
            self.down = pressed
        self.ui.syn()


def reader(port, ack, touch):
    """Background thread: parses lines coming back from the ESP32."""
    try:
        while True:
            line = port.readline().strip()
            if line == b"K":
                ack.set()
            elif line.startswith(b"T ") and touch:
                try:
                    x, y, p = (int(v) for v in line.split()[1:4])
                except ValueError:
                    continue
                touch.event(x, y, bool(p))
    except (serial.SerialException, OSError, TypeError):
        ack.set()  # port went away; unblock the sender so it can reconnect


def stream(port, cap, touch, args):
    ack = threading.Event()
    threading.Thread(target=reader, args=(port, ack, touch), daemon=True).start()
    period = 1.0 / args.fps
    shown = None  # checksum of the frame currently on the panel
    while True:
        start = time.monotonic()
        frame, box = fit(cap.grab(), args.stretch)
        if touch:
            touch.box = box
        digest = zlib.crc32(frame.tobytes())
        if digest != shown:
            data = encode(frame, args.quality)
            ack.clear()
            port.write(MAGIC + struct.pack("<I", len(data)) + data)
            # Unacked frames are simply sent again on the next pass.
            if ack.wait(ACK_TIMEOUT):
                shown = digest
        delay = period - (time.monotonic() - start)
        if delay > 0:
            time.sleep(delay)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", default="/dev/ttyUSB0")
    ap.add_argument("--baud", type=int, default=921600, help="must match LINK_BAUD in platformio.ini")
    ap.add_argument("--source", choices=("auto", "fb", "grim", "spectacle", "x11", "test"), default="auto")
    ap.add_argument("--fbdev", default="/dev/fb0")
    ap.add_argument("--fps", type=float, default=15, help="upper limit; the link usually caps it lower")
    ap.add_argument("--quality", type=int, default=70, help="JPEG quality 1-95")
    ap.add_argument("--stretch", action="store_true", help="fill the panel instead of letterboxing")
    ap.add_argument("--touch", action="store_true", help="expose the touchscreen as a pointer (needs /dev/uinput)")
    args = ap.parse_args()

    cap = make_capture(args.source, args.fbdev)
    touch = TouchPointer() if args.touch else None

    while True:
        try:
            with serial.Serial(args.port, args.baud, timeout=1, write_timeout=5) as port:
                print(f"connected to {args.port}")
                stream(port, cap, touch, args)
        except (serial.SerialException, OSError) as err:
            print(f"{args.port}: {err}; retrying", file=sys.stderr)
            time.sleep(2)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
