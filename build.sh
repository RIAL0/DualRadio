#!/usr/bin/env bash
# Build the DualRadio .deb (APPLaunch layout). Usage: ./build.sh  -> dist/dualradio_<ver>_arm64.deb
set -euo pipefail
cd "$(dirname "$0")"
VER=$(python3 -c "import re;print(re.search(r'VERSION = \"(.+?)\"', open('src/dualradio.py').read()).group(1))")
PKG=dualradio
STAGE=$(mktemp -d); trap 'rm -rf "$STAGE"' EXIT
chmod 0755 "$STAGE"
A=$STAGE/usr/share/APPLaunch
install -d "$STAGE/DEBIAN" "$A/apps/$PKG" "$A/bin" "$A/applications" "$A/share/images" \
           "$STAGE/usr/bin" \
           "$A/apps/$PKG/fonts"
for f in dualradio.py netcore.py wifimgr.py moncore.py btcore.py preview.py; do
    python3 -m py_compile "src/$f"
    install -m 0755 "src/$f" "$A/apps/$PKG/$f"
done
install -m 0644 src/preview_scan.txt "$A/apps/$PKG/"
install -m 0644 src/fonts/* "$A/apps/$PKG/fonts/"
install -m 0755 packaging/run-dualradio "$A/bin/run-dualradio"
install -m 0644 packaging/dualradio.desktop "$A/applications/dualradio.desktop"
install -m 0644 icon.png "$A/share/images/dualradio.png"
ln -s /usr/share/APPLaunch/apps/$PKG/wifimgr.py "$STAGE/usr/bin/dualradio-cli"
install -m 0755 packaging/postinst packaging/postrm "$STAGE/DEBIAN/"
cat > "$STAGE/DEBIAN/control" <<CTRL
Package: $PKG
Version: $VER
Architecture: arm64
Maintainer: Ricardo (RIAL0) <RIAL0@users.noreply.github.com>
Section: APPLaunch
Priority: optional
Homepage: https://github.com/RIAL0/DualRadio
Depends: python3, python3-pil, python3-numpy, python3-dbus, python3-gi, bluez, iw, network-manager, iproute2, iputils-ping, fonts-dejavu-core
Recommends: ieee-data
Description: DualRadio - Wi-Fi + Bluetooth manager for CardputerZero
 Manage the built-in radio and a USB Wi-Fi/BT dongle side by side:
 per-radio connect/disconnect, remembered networks, band filter, full
 network details, main-network selection, auto-fallback to built-in
 Wi-Fi, monitor-mode AP survey with pcap capture, hotspot, ping/speed
 test, and Bluetooth scan/details/pair/connect/hunt per adapter.
CTRL
mkdir -p dist
dpkg-deb --root-owner-group -b "$STAGE" "dist/${PKG}_${VER}_arm64.deb"
