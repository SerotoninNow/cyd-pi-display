#!/usr/bin/env python3
"""Drive an ESP32-2432S028 (CYD) as a small USB display for a Raspberry Pi or Linux PC.

Run this with the CYD plugged into a USB port:

    python3 cyd_display.py --source dashboard              # system dashboard (CPU, temperature, memory...)
    python3 cyd_display.py --source apps --run lxterminal  # a separate 320x240 screen for apps, with touch
    python3 cyd_display.py                                 # mirror the main screen
    python3 cyd_display.py --touch                         # mirror, and use the CYD as a touch pointer
    python3 cyd_display.py --source test                   # moving test pattern

See ../firmware/src/main.cpp for the link protocol.
"""
import argparse
import ctypes
import ctypes.util
import io
import os
import select
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time

import serial
from PIL import Image, ImageChops, ImageDraw, ImageFont

W, H = 320, 240
_children = []  # processes to stop on exit
TILE = 16  # partial updates are aligned to JPEG blocks so their edges don't show
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


def load_font(size, bold=False):
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    for folder in ("/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/TTF", "/usr/share/fonts/dejavu"):
        try:
            return ImageFont.truetype(os.path.join(folder, name), size)
        except OSError:
            pass
    return ImageFont.load_default(size)


class DashboardCapture:
    """Draws a live system dashboard at the panel's native size."""

    BG, FG, DIM, TRACK = (12, 14, 20), (235, 238, 245), (140, 146, 160), (40, 44, 56)

    def __init__(self):
        self.big, self.font, self.small = load_font(26, True), load_font(15), load_font(12)
        self.cpu_prev = self._cpu_times()
        self.frame, self.updated = None, 0.0
        self.thermal = self._find_thermal()

    @staticmethod
    def _cpu_times():
        with open("/proc/stat") as f:
            v = [int(x) for x in f.readline().split()[1:]]
        return sum(v), v[3] + v[4]  # total, idle + iowait

    @staticmethod
    def _find_thermal():
        zones = sorted(p for p in os.listdir("/sys/class/thermal") if p.startswith("thermal_zone"))
        preferred = ("cpu-thermal", "x86_pkg_temp", "k10temp", "coretemp")
        for want in preferred:
            for z in zones:
                try:
                    with open(f"/sys/class/thermal/{z}/type") as f:
                        if f.read().strip() == want:
                            return f"/sys/class/thermal/{z}/temp"
                except OSError:
                    pass
        return f"/sys/class/thermal/{zones[0]}/temp" if zones else None

    def _stats(self):
        total, idle = self._cpu_times()
        dt, di = total - self.cpu_prev[0], idle - self.cpu_prev[1]
        self.cpu_prev = (total, idle)
        cpu = 100.0 * (1 - di / dt) if dt else 0.0

        temp = None
        if self.thermal:
            try:
                with open(self.thermal) as f:
                    temp = int(f.read()) / 1000
            except OSError:
                pass

        mem = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":")
                mem[k] = int(v.split()[0]) * 1024
        used = mem["MemTotal"] - mem["MemAvailable"]

        st = os.statvfs("/")
        disk_total = st.f_blocks * st.f_frsize
        disk_used = disk_total - st.f_bavail * st.f_frsize

        with open("/proc/uptime") as f:
            up = int(float(f.read().split()[0]))
        try:
            # Picks the outgoing interface's address; a UDP connect sends nothing.
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sk:
                sk.connect(("192.0.2.1", 9))
                ip = sk.getsockname()[0]
        except OSError:
            ip = "offline"
        return dict(cpu=cpu, temp=temp, mem=(used, mem["MemTotal"]), disk=(disk_used, disk_total),
                    up=up, load=os.getloadavg()[0], ip=ip)

    @staticmethod
    def _level(frac):
        return (80, 200, 120) if frac < 0.6 else (240, 180, 60) if frac < 0.85 else (235, 80, 80)

    def _row(self, d, y, label, value, frac):
        d.text((14, y), label, font=self.font, fill=self.DIM)
        d.text((W - 14, y), value, font=self.font, fill=self.FG, anchor="ra")
        frac = min(max(frac, 0.0), 1.0)
        d.rounded_rectangle((14, y + 22, W - 14, y + 28), 3, fill=self.TRACK)
        if frac > 0:
            d.rounded_rectangle((14, y + 22, 14 + max(6, int((W - 28) * frac)), y + 28), 3, fill=self._level(frac))

    def grab(self):
        now = time.monotonic()
        if self.frame is not None and now - self.updated < 1.0:
            return self.frame
        self.updated = now
        s = self._stats()
        img = Image.new("RGB", (W, H), self.BG)
        d = ImageDraw.Draw(img)
        clock = time.strftime("%H:%M")
        name, room = socket.gethostname(), W - 28 - d.textlength(clock, font=self.big) - 12
        while len(name) > 1 and d.textlength(name, font=self.big) > room:
            name = name[:-2] + "\u2026"
        d.text((14, 8), name, font=self.big, fill=self.FG)
        d.text((W - 14, 8), clock, font=self.big, fill=self.FG, anchor="ra")
        d.line((14, 46, W - 14, 46), fill=self.TRACK)

        gb = 1024**3
        self._row(d, 56, "CPU", f"{s['cpu']:.0f}%", s["cpu"] / 100)
        if s["temp"] is not None:
            self._row(d, 92, "Temperature", f"{s['temp']:.0f}\u00b0C", (s["temp"] - 30) / 55)
        self._row(d, 128, "Memory", f"{s['mem'][0] / gb:.1f} / {s['mem'][1] / gb:.1f} GB", s["mem"][0] / s["mem"][1])
        self._row(d, 164, "Disk", f"{s['disk'][0] / gb:.0f} / {s['disk'][1] / gb:.0f} GB", s["disk"][0] / s["disk"][1])

        days, rem = divmod(s["up"], 86400)
        uptime = (f"{days}d " if days else "") + f"{rem // 3600}h {rem % 3600 // 60}m"
        d.text((14, 214), s["ip"], font=self.small, fill=self.DIM)
        d.text((W - 14, 214), f"up {uptime}  load {s['load']:.2f}", font=self.small, fill=self.DIM, anchor="ra")
        self.frame = img
        return img


class _XImage(ctypes.Structure):
    _fields_ = [
        ("width", ctypes.c_int), ("height", ctypes.c_int), ("xoffset", ctypes.c_int), ("format", ctypes.c_int),
        ("data", ctypes.c_void_p), ("byte_order", ctypes.c_int), ("bitmap_unit", ctypes.c_int),
        ("bitmap_bit_order", ctypes.c_int), ("bitmap_pad", ctypes.c_int), ("depth", ctypes.c_int),
        ("bytes_per_line", ctypes.c_int), ("bits_per_pixel", ctypes.c_int),
    ]  # trailing fields are never touched from Python


class _XWindowAttributes(ctypes.Structure):
    _fields_ = [
        ("x", ctypes.c_int), ("y", ctypes.c_int), ("width", ctypes.c_int), ("height", ctypes.c_int),
        ("border_width", ctypes.c_int), ("depth", ctypes.c_int), ("visual", ctypes.c_void_p),
        ("root", ctypes.c_ulong), ("class_", ctypes.c_int), ("bit_gravity", ctypes.c_int),
        ("win_gravity", ctypes.c_int), ("backing_store", ctypes.c_int), ("backing_planes", ctypes.c_ulong),
        ("backing_pixel", ctypes.c_ulong), ("save_under", ctypes.c_int), ("colormap", ctypes.c_ulong),
        ("map_installed", ctypes.c_int), ("map_state", ctypes.c_int), ("all_event_masks", ctypes.c_long),
        ("your_event_mask", ctypes.c_long), ("do_not_propagate_mask", ctypes.c_long),
        ("override_redirect", ctypes.c_int), ("screen", ctypes.c_void_p),
    ]


def _load_xlib():
    x = ctypes.cdll.LoadLibrary(ctypes.util.find_library("X11") or "libX11.so.6")
    vp, ul = ctypes.c_void_p, ctypes.c_ulong
    x.XOpenDisplay.restype, x.XOpenDisplay.argtypes = vp, [ctypes.c_char_p]
    x.XDefaultRootWindow.restype, x.XDefaultRootWindow.argtypes = ul, [vp]
    x.XGetImage.restype = ctypes.POINTER(_XImage)
    x.XGetImage.argtypes = [vp, ul, ctypes.c_int, ctypes.c_int, ctypes.c_uint, ctypes.c_uint, ul, ctypes.c_int]
    x.XDestroyImage.argtypes = [ctypes.POINTER(_XImage)]
    x.XQueryTree.argtypes = [vp, ul, ctypes.POINTER(ul), ctypes.POINTER(ul), ctypes.POINTER(ctypes.POINTER(ul)),
                             ctypes.POINTER(ctypes.c_uint)]
    x.XGetWindowAttributes.argtypes = [vp, ul, ctypes.POINTER(_XWindowAttributes)]
    x.XMoveResizeWindow.argtypes = [vp, ul, ctypes.c_int, ctypes.c_int, ctypes.c_uint, ctypes.c_uint]
    x.XFree.argtypes = [vp]
    x.XFlush.argtypes = [vp]
    return x


class AppsCapture:
    """A separate, invisible 320x240 X screen (Xvfb) whose contents go to the CYD.

    Apps started with --run (or with DISPLAY set to this screen) appear only on the
    CYD. There is no window manager, so each new window is sized to fill the panel."""

    def __init__(self, display, commands):
        self.display = display
        num = display.lstrip(":").split(".")[0]
        if not os.path.exists(f"/tmp/.X11-unix/X{num}"):
            if not shutil.which("Xvfb"):
                raise SystemExit(
                    "Xvfb is needed for --source apps. Install it with 'sudo apt install xvfb' "
                    "(Raspberry Pi OS, Debian) or 'sudo pacman -S xorg-server-xvfb' (Arch, CachyOS)."
                )
            self.server = subprocess.Popen(
                ["Xvfb", display, "-screen", "0", f"{W}x{H}x24", "-nolisten", "tcp"], stderr=subprocess.DEVNULL
            )
            _children.append(self.server)
            for _ in range(50):
                if os.path.exists(f"/tmp/.X11-unix/X{num}"):
                    break
                time.sleep(0.1)
        self.x = _load_xlib()
        self.d = self.x.XOpenDisplay(display.encode())
        if not self.d:
            raise SystemExit(f"Can't open X display {display}.")
        self.root = self.x.XDefaultRootWindow(self.d)
        env = dict(os.environ, DISPLAY=display, GDK_BACKEND="x11", QT_QPA_PLATFORM="xcb")
        env.pop("WAYLAND_DISPLAY", None)
        for cmd in commands:
            subprocess.Popen(cmd, shell=True, env=env, start_new_session=True)
        print(f"apps screen: {display} (start more apps with DISPLAY={display})")

    def _fit_windows(self):
        x, ul = self.x, ctypes.c_ulong
        root, parent, children, n = ul(), ul(), ctypes.POINTER(ul)(), ctypes.c_uint()
        if not x.XQueryTree(self.d, self.root, ctypes.byref(root), ctypes.byref(parent), ctypes.byref(children),
                            ctypes.byref(n)):
            return
        attrs = _XWindowAttributes()
        for i in range(n.value):
            w = children[i]
            if x.XGetWindowAttributes(self.d, w, ctypes.byref(attrs)) and attrs.map_state == 2:  # IsViewable
                big = attrs.width > W // 2 or attrs.height > H // 2
                if big and not attrs.override_redirect and (attrs.x, attrs.y, attrs.width, attrs.height) != (0, 0, W, H):
                    x.XMoveResizeWindow(self.d, w, 0, 0, W, H)
        if children:
            x.XFree(children)
        x.XFlush(self.d)

    keyboard_on = False  # set by KeyboardForwarder; shown as a coloured frame around the panel

    def grab(self):
        self._fit_windows()
        img = self.x.XGetImage(self.d, self.root, 0, 0, W, H, 0xFFFFFFFF, 2)  # ZPixmap
        if not img:
            raise RuntimeError("XGetImage failed")
        try:
            xi = img.contents
            data = ctypes.string_at(xi.data, xi.bytes_per_line * xi.height)
            frame = Image.frombuffer("RGB", (W, H), data, "raw", "BGRX", xi.bytes_per_line, 1).copy()
        finally:
            self.x.XDestroyImage(img)
        if self.keyboard_on:
            ImageDraw.Draw(frame).rectangle((0, 0, W - 1, H - 1), outline=(60, 160, 255), width=3)
        return frame


class AppsTouch:
    """Sends CYD touches to the apps screen as mouse clicks (XTest), leaving the main pointer alone."""

    def __init__(self, display):
        self.x = _load_xlib()
        self.t = ctypes.cdll.LoadLibrary(ctypes.util.find_library("Xtst") or "libXtst.so.6")
        self.t.XTestFakeMotionEvent.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_ulong]
        self.t.XTestFakeButtonEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
        self.d = self.x.XOpenDisplay(display.encode())  # its own connection: this runs on the reader thread
        self.box = (0, 0, W, H)
        self.down = False

    def event(self, x, y, pressed):
        self.t.XTestFakeMotionEvent(self.d, -1, x, y, 0)
        if pressed != self.down:
            self.t.XTestFakeButtonEvent(self.d, 1, int(pressed), 0)
            self.down = pressed
        self.x.XFlush(self.d)


class KeyboardForwarder:
    """Pressing the hotkey sends the keyboard to the apps screen instead of the desktop, until it
    is pressed again. Reads keyboards directly (evdev), so it works under Wayland and X11 alike;
    needs read access to /dev/input (the 'input' group)."""

    MODIFIERS = {
        "ctrl": ("KEY_LEFTCTRL", "KEY_RIGHTCTRL"),
        "alt": ("KEY_LEFTALT", "KEY_RIGHTALT"),
        "shift": ("KEY_LEFTSHIFT", "KEY_RIGHTSHIFT"),
        "super": ("KEY_LEFTMETA", "KEY_RIGHTMETA"),
    }

    def __init__(self, display, hotkey, capture):
        import evdev
        from evdev import ecodes

        self.evdev, self.ecodes, self.capture = evdev, ecodes, capture
        *mods, key = hotkey.lower().split("+")
        try:
            self.mod_groups = [{ecodes.ecodes[k] for k in self.MODIFIERS[m]} for m in mods]
            self.key = ecodes.ecodes["KEY_" + key.upper()]
        except KeyError:
            raise SystemExit(f"Can't understand hotkey '{hotkey}'; use e.g. ctrl+alt+k.")
        self.hotkey = hotkey

        self.devices = []
        for path in evdev.list_devices():
            try:
                dev = evdev.InputDevice(path)
            except OSError:
                continue
            keys = dev.capabilities().get(ecodes.EV_KEY, [])
            if ecodes.KEY_A in keys and ecodes.KEY_ENTER in keys:
                self.devices.append(dev)
        if not self.devices:
            raise PermissionError

        self.x = _load_xlib()
        self.t = ctypes.cdll.LoadLibrary(ctypes.util.find_library("Xtst") or "libXtst.so.6")
        self.t.XTestFakeKeyEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
        self.d = self.x.XOpenDisplay(display.encode())
        self.active = self.toggle_pending = False
        self.pressed = set()
        threading.Thread(target=self._run, daemon=True).start()
        print(f"keyboard: press {hotkey} to type on the CYD, and again to return to the desktop")

    def _is_hotkey(self, code):
        return code == self.key and all(group & self.pressed for group in self.mod_groups)

    def _apply_toggle(self):
        # Only switched with every key up, so neither side is left with a key stuck down.
        self.active = not self.active
        for dev in list(self.devices):
            try:
                dev.grab() if self.active else dev.ungrab()
            except OSError:
                pass
        self.capture.keyboard_on = self.active
        print(f"keyboard: {'on the CYD' if self.active else 'back on the desktop'}")

    def _run(self):
        ecodes = self.ecodes
        while self.devices:
            ready, _, _ = select.select(self.devices, [], [])
            for dev in ready:
                try:
                    events = list(dev.read())
                except OSError:  # unplugged
                    self.devices.remove(dev)
                    continue
                for ev in events:
                    if ev.type != ecodes.EV_KEY:
                        continue
                    if ev.value == 1:
                        self.pressed.add(ev.code)
                    elif ev.value == 0:
                        self.pressed.discard(ev.code)
                    hot = ev.code == self.key and (self.toggle_pending or self._is_hotkey(ev.code))
                    if ev.value == 1 and hot:
                        self.toggle_pending = True
                    elif self.active and not hot:
                        # X keycodes are evdev codes + 8; autorepeat (value 2) is sent as another press.
                        self.t.XTestFakeKeyEvent(self.d, ev.code + 8, int(ev.value != 0), 0)
                        self.x.XFlush(self.d)
                    if self.toggle_pending and not self.pressed:
                        self.toggle_pending = False
                        self._apply_toggle()


def make_capture(source, fbdev, display=":5", commands=()):
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
    if source == "dashboard":
        return DashboardCapture()
    if source == "apps":
        return AppsCapture(display, commands)
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
                try:
                    touch.event(x, y, bool(p))
                except Exception as err:  # a failed touch must not stop the link
                    print(f"touch: {err!r}", file=sys.stderr)
    except (serial.SerialException, OSError, TypeError):
        ack.set()  # port went away; unblock the sender so it can reconnect


def _with_length(data):
    return struct.pack("<I", len(data)) + data


def changed_area(frame, shown):
    """The block-aligned box that differs from what's on the panel, or None."""
    box = ImageChops.difference(frame, shown).getbbox()
    if not box:
        return None
    x0, y0 = box[0] // TILE * TILE, box[1] // TILE * TILE
    x1, y1 = min(W, -(-box[2] // TILE) * TILE), min(H, -(-box[3] // TILE) * TILE)
    return x0, y0, x1, y1


def stream(port, cap, touch, args):
    ack = threading.Event()
    threading.Thread(target=reader, args=(port, ack, touch), daemon=True).start()
    period = 1.0 / args.fps
    shown = None  # the frame currently on the panel; None forces a full redraw
    while True:
        start = time.monotonic()
        frame, box = fit(cap.grab(), args.stretch)
        if touch:
            touch.box = box
        area = (0, 0, W, H) if shown is None else changed_area(frame, shown)
        if area:
            x0, y0, x1, y1 = area
            if (x1 - x0) * (y1 - y0) > W * H * 0.6:
                packet = b"CYDF" + _with_length(encode(frame, args.quality))
            else:
                data = encode(frame.crop(area), args.quality)
                packet = b"CYDR" + struct.pack("<HH", x0, y0) + _with_length(data)
            ack.clear()
            port.write(packet)
            # A lost frame leaves the panel in an unknown state, so the next one is a full redraw.
            shown = frame if ack.wait(ACK_TIMEOUT) else None
        delay = period - (time.monotonic() - start)
        if delay > 0:
            time.sleep(delay)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", default="/dev/ttyUSB0")
    ap.add_argument("--baud", type=int, default=2000000, help="must match LINK_BAUD in platformio.ini")
    ap.add_argument(
        "--source", choices=("auto", "dashboard", "apps", "fb", "grim", "spectacle", "x11", "test"), default="auto"
    )
    ap.add_argument("--run", action="append", default=[], metavar="CMD", help="with --source apps: start CMD on it")
    ap.add_argument("--display", default=":5", help="with --source apps: X display number for the apps screen")
    ap.add_argument("--hotkey", default="ctrl+alt+k", help="with --source apps: toggles typing on the CYD")
    ap.add_argument("--fbdev", default="/dev/fb0")
    ap.add_argument("--fps", type=float, default=30, help="upper limit; the link usually caps it lower")
    ap.add_argument("--quality", type=int, default=70, help="JPEG quality 1-95")
    ap.add_argument("--stretch", action="store_true", help="fill the panel instead of letterboxing")
    ap.add_argument("--touch", action="store_true", help="expose the touchscreen as a pointer (needs /dev/uinput)")
    args = ap.parse_args()

    cap = make_capture(args.source, args.fbdev, args.display, args.run)
    if args.source == "apps":
        touch = AppsTouch(args.display)
        try:
            KeyboardForwarder(args.display, args.hotkey, cap)
        except (PermissionError, ImportError):
            print(
                "keyboard: can't read keyboards, so typing on the CYD is off. Allow it with\n"
                "  sudo usermod -aG input $USER\nthen log out and back in (python3-evdev must be installed).",
                file=sys.stderr,
            )
    else:
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
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # so a plain kill also cleans up
    try:
        main()
    except KeyboardInterrupt:
        pass
    finally:
        for proc in _children:
            proc.terminate()
