# DualRadio — publish record

| Field | Value |
|---|---|
| Package | `dualradio` (name verified free in CardputerZero/packages pool, 2026-11-23) |
| Version / arch | 1.3.2 / arm64 |
| .deb | `dist/dualradio_1.3.2_arm64.deb` |
| App name | DualRadio |
| Category | System Tools |
| Icon | `store/icon.png` (256×256) |
| Screenshots | `store/screenshots/1_wifi.png`, `2_networks.png`, `3_monitor.png`, `4_bluetooth.png` (320×170) |
| Licence | MIT |
| Source repo | TODO: create `https://github.com/RIAL0/DualRadio` and push this folder |
| Maintainer field | `Ricardo (RIAL0) <RIAL0@users.noreply.github.com>` — TODO: confirm your GitHub noreply address |
| Prepublish check | `prepublish_check.py --deb dist/dualradio_1.3.2_arm64.deb --app-dir .` → passed |
| On-device test | TODO (see checklist below) |
| Submission | TODO |

## Before submitting
1. Install on the CardputerZero and run the on-device checklist.
2. Replace the screenshots with real captures if you like (must be exactly 320×170, 4 images).
3. Push the source to GitHub.
4. Submit either way:
   - Web: https://dev.cardputer.cc/#/upload → upload the .deb, fill title/summary/description from `app-builder.json`.
   - CLI: `./czdev login` then `./czdev publish --deb dist/dualradio_1.3.2_arm64.deb` from this folder.

## On-device checklist
- [ ] Appears in APPLaunch with icon; launches; Esc back; hold Esc exits to launcher
- [ ] Both radios show; scan on wlan1 with B band filter; detail screen fills
- [ ] Join a network on wlan1 that wlan0 already knows → no password asked, wlan0 stays up
- [ ] P on each radio → `ip route` shows that radio's default first
- [ ] Unplug dongle while only it is connected → wlan0 rejoins a saved network
- [ ] M → survey lists 2.4 + 5 GHz APs; L lock; R record → pcap in ~/captures opens in Wireshark
- [ ] Hotspot on wlan0 → phone joins and gets internet via wlan1
- [ ] T test → ping + Mbit/s
- [ ] Bluetooth: both adapters listed; scan hci1; pair a phone (code confirm); hunt
