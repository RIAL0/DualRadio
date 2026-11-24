#!/bin/sh
# DualRadio one-time setup (run by hand, over SSH):
#
#     sudo sh /usr/share/APPLaunch/apps/dualradio/setup-sudo.sh
#
# Lets APPLaunch start DualRadio as root without a password prompt, which it needs to
# control the radios. Most CardputerZeros already have passwordless sudo for the device
# user (set when the image is flashed) and never need this; it's for devices where that
# wasn't set up (e.g. a custom username). The CardputerZero store forbids an app from
# installing a sudoers file, so this is a script you run yourself, not part of the package.
#
# Safe: scoped to the 'gpio' group and to exactly the two commands run-dualradio uses, and
# validated with `visudo -c` before it's installed.
set -e

RULE='%gpio ALL=(root) NOPASSWD: /usr/bin/true, /usr/bin/python3 /usr/share/APPLaunch/apps/dualradio/dualradio.py'
DEST=/etc/sudoers.d/dualradio

if [ "$(id -u)" -ne 0 ]; then
    echo "Run it with sudo:  sudo sh $0" >&2
    exit 1
fi

TMP=$(mktemp)
printf '%s\n' "$RULE" > "$TMP"
if visudo -cf "$TMP" >/dev/null 2>&1; then
    install -m 0440 -o root -g root "$TMP" "$DEST"
    rm -f "$TMP"
    echo "OK: $DEST installed. Relaunch DualRadio from the menu - it will stay working across reboots."
else
    rm -f "$TMP"
    echo "ERROR: sudoers rule failed validation; nothing was changed." >&2
    exit 1
fi
