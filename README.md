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

That shows a test pattern with a clock, which confirms the link.

### Its own screen

The CYD can show its own content instead of copying the main screen:

```bash
python3 pi/cyd_display.py --source dashboard
```

A live system dashboard: CPU, temperature, memory, disk, IP address, uptime and load.

```bash
python3 pi/cyd_display.py --source apps --run lxterminal
```

A separate, invisible 320x240 screen whose apps appear only on the CYD, at native size, with
touch working as the mouse on that screen (your main pointer is unaffected). Each new window
is sized to fill the panel. `--run` can be given several times; to start more apps later,
set `DISPLAY=:5` for them. Needs `sudo apt install xvfb`.

To type into apps on the CYD, press Ctrl+Alt+K: the keyboard then goes to the CYD (shown by a
blue frame around the panel) until you press it again. Change the combination with `--hotkey`.
This reads keyboards directly, so your user must be in the `input` group (it is by default on
Raspberry Pi OS; elsewhere `sudo usermod -aG input $USER`, then log out and back in).

### Mirroring

Without `--source`, the main screen is mirrored. The capture method is chosen automatically:

| Pi setup | Method | Notes |
| --- | --- | --- |
| Desktop on Wayland (Pi OS Bookworm default) | `grim` | run it from a terminal inside the desktop session |
| KDE Plasma on Wayland | `spectacle` | a few frames per second |
| Desktop on X11 | `x11` | needs `pip install mss` |
| Text console / no desktop | `fb` | reads `/dev/fb0`; user must be in the `video` group |

Useful options: `--touch` (CYD touchscreen moves and clicks the Pi pointer; needs write access
to `/dev/uinput`, simplest with `sudo`), `--stretch`, `--quality`, `--port`.

## Tuning

- **Speed**: only the part of the screen that changed is sent, so small changes (a clock, a
  terminal line) update at around 40 per second and full-screen changes at around 14. The link
  runs at 2 Mbaud; if frames get lost on your USB port, set `LINK_BAUD` in `firmware/platformio.ini`
  to `921600`, reflash, and pass `--baud 921600`. When mirroring, screen capture is often the
  limit instead (Spectacle on KDE manages about 4 per second).
- **Colours inverted**: add `-DTFT_INVERSION_ON=1` to the build flags of your env.
- **Touch lands off-target**: adjust the `TOUCH_*_MIN/MAX` values in `firmware/src/main.cpp`.
- **Readable text**: the panel is only 320x240, so set the Pi to a low resolution
  (640x480 scales down cleanly).

## Using the CYD as a microSD card reader

The CYD has a microSD slot, so with different firmware it can stand in for a card reader
when you have none. It is slow (an OS image with desktop took about 6.5 hours) but every
block is read back and checked after writing.

```bash
.venv/bin/pio run -d firmware -e sdcard2usb -t upload
```

Use `-e sdcard` for the single-USB board. Then, with the card in the CYD's slot:

```bash
python3 tools/cyd_sd.py info
```

That shows the card's partitions and whether the files a Pi 3 B needs to boot are present.
`write IMAGE.img.xz` writes an OS image (erasing the card; `--start SECTOR` resumes an interrupted
write), and `setup` sets the user, Wi-Fi and SSH on a Raspberry Pi OS (Trixie or later) card
through cloud-init, asking for passwords in the terminal. Reflash the display firmware afterwards.

The link runs at 1.5 Mbaud; faster settings corrupted data on the CH340 in testing.
