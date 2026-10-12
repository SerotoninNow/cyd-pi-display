# ESP32-2432S028 as a Raspberry Pi display

The CYD plugs into a Pi USB port. That one cable powers it and carries the picture:
the Pi captures its screen, scales it to 320x240, and sends JPEG frames over USB serial;
the ESP32 decodes and draws them. Touches are sent back and can drive the Pi's pointer.

## 1. Flash the ESP32 (from any computer)

PlatformIO lives in a virtual environment in this folder (many distros have no system `pip`):

```bash
python3 -m venv .venv && .venv/bin/pip install platformio
```

```bash
.venv/bin/pio run -d firmware -t upload
```

If your board has two USB ports (micro-USB and USB-C), add `-e cyd2usb` to that command.
If the upload can't connect, hold the BOOT button while it starts.

After flashing, the screen shows "Waiting for the Pi...".

## 2. Run the sender on the Pi

```bash
sudo apt install python3-serial python3-pil python3-evdev grim
```

```bash
python3 pi/cyd_display.py --source test
```

That shows a test pattern with a clock, which confirms the link. Then drop `--source test`
to mirror the real screen. The capture method is chosen automatically:

| Pi setup | Method | Notes |
| --- | --- | --- |
| Desktop on Wayland (Pi OS Bookworm default) | `grim` | run it from a terminal inside the desktop session |
| KDE Plasma on Wayland | `spectacle` | a few frames per second |
| Desktop on X11 | `x11` | needs `pip install mss` |
| Text console / no desktop | `fb` | reads `/dev/fb0`; user must be in the `video` group |

Useful options: `--touch` (CYD touchscreen moves and clicks the Pi pointer; needs write access
to `/dev/uinput`, simplest with `sudo`), `--stretch`, `--quality`, `--port`.

## Tuning

- **Speed**: the link runs at 921600 baud. For more frames per second, change `LINK_BAUD` in
  `firmware/platformio.ini` to `2000000`, reflash, and pass `--baud 2000000`.
- **Colours inverted**: add `-DTFT_INVERSION_ON=1` to the build flags of your env.
- **Touch lands off-target**: adjust the `TOUCH_*_MIN/MAX` values in `firmware/src/main.cpp`.
- **Readable text**: the panel is only 320x240, so set the Pi to a low resolution
  (640x480 scales down cleanly).
