#!/bin/sh
# Started with the desktop (see setup-autostart.sh). Mirrors the desktop onto the CYD with touch,
# and restarts the mirror if it ever stops, e.g. while the CYD is unplugged.
cd "$(dirname "$0")/.." || exit 1
while true; do
    python3 pi/cyd_display.py --source grim --touch >> "$HOME/cyd-display.log" 2>&1
    sleep 3
done
