#!/bin/sh
# Makes the CYD this Pi's display: on every boot the desktop logs in automatically
# and is mirrored onto the CYD, with the CYD's touchscreen working as the mouse.
#
#   sh pi/setup-autostart.sh          # set it up (reboot afterwards)
#   sh pi/setup-autostart.sh remove   # undo
#
# Run as your normal user on Raspberry Pi OS (Trixie or later); it uses sudo where needed.
set -e

HERE=$(cd "$(dirname "$0")/.." && pwd)
ENTRY="$HOME/.config/autostart/cyd-display.desktop"
UDEV=/etc/udev/rules.d/60-cyd-uinput.rules
CMDLINE=/boot/firmware/cmdline.txt
# With no monitor attached the desktop has no screen to draw on, so force the HDMI output on.
# 640x480 scales down exactly 2:1 onto the CYD's 320x240.
VIDEO=video=HDMI-A-1:640x480@60D

if [ "$1" = remove ]; then
    rm -f "$ENTRY"
    if [ -f "$HOME/.config/kanshi/config.before-cyd" ]; then
        mv "$HOME/.config/kanshi/config.before-cyd" "$HOME/.config/kanshi/config"
    else
        rm -f "$HOME/.config/kanshi/config"
    fi
    sudo rm -f "$UDEV" /etc/modules-load.d/cyd-uinput.conf
    sudo sed -i "s/ $VIDEO//" "$CMDLINE"
    echo "Removed. Desktop autologin was left on (change it with: sudo raspi-config)."
    exit 0
fi

# Desktop with automatic login, so there is a screen to show without anyone at a keyboard.
sudo raspi-config nonint do_boot_behaviour B4

grep -q "$VIDEO" "$CMDLINE" || sudo sed -i "1 s/\$/ $VIDEO/" "$CMDLINE"

# Touch input needs to create a virtual pointer through /dev/uinput.
echo 'KERNEL=="uinput", GROUP="input", MODE="0660"' | sudo tee "$UDEV" >/dev/null
echo uinput | sudo tee /etc/modules-load.d/cyd-uinput.conf >/dev/null

# The forced output otherwise comes up at 1024x768; the desktop's display manager (kanshi) sets the mode.
KANSHI="$HOME/.config/kanshi/config"
if [ -s "$KANSHI" ] && ! grep -q "cyd" "$KANSHI"; then
    cp "$KANSHI" "$KANSHI.before-cyd"
fi
mkdir -p "$(dirname "$KANSHI")"
printf 'profile cyd {\n\toutput HDMI-A-1 enable mode 640x480 position 0,0 transform normal\n}\n' > "$KANSHI"

mkdir -p "$(dirname "$ENTRY")"
cat > "$ENTRY" <<EOF
[Desktop Entry]
Type=Application
Name=CYD display
Comment=Show this desktop on the CYD screen
Exec=$HERE/pi/autostart.sh
EOF

echo "Done. Reboot to start using the CYD as the display: sudo reboot"
