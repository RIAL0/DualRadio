#!/usr/bin/env python3
"""Drive the real DualRadio App loop with scripted keys against mocked engines."""
import os
import sys
import threading
import time

SRC = os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "src")
sys.path.insert(0, SRC)
os.chdir(SRC)

import dualradio as dr  # noqa: E402
import moncore  # noqa: E402
import netcore as nc  # noqa: E402
import preview as pv  # noqa: E402
import wifimgr as wm  # noqa: E402

K = dr.Key
calls = []


def key(code=0, ch=None):
    return K(code, ch)


UP, DOWN, LEFT, RIGHT = key(103), key(108), key(105), key(106)
ENTER, ESC, BKSP = key(28), key(1), key(14)


def ch(c):
    return key(0, c)


# ---------------- mocks
bss = nc.parse_iw_scan(open("preview_scan.txt").read())
nc.scan_full = lambda ifc: (calls.append(("scan", ifc)), time.sleep(0.2), bss)[2]
nc.saved_ssids_for = lambda ifc: {"HomeNet"}


def fake_connect(ifc, net, ask):
    pw = ask()
    calls.append(("connect", ifc, net["ssid"], net["band"], pw))
    if pw is None:
        raise wm.WifiError("cancelled")
    return f"{ifc}: connected to {net['ssid']}"


nc.connect = fake_connect
PROFS = [{"name": "wlan1-HomeNet", "uuid": "u1", "ssid": "HomeNet", "band": "a", "ifname": "wlan1",
          "mode": "infrastructure", "autoconnect": "yes"},
         {"name": "HomeNet", "uuid": "u2", "ssid": "HomeNet", "band": "", "ifname": "",
          "mode": "infrastructure", "autoconnect": "yes"}]
nc.profiles_for = lambda ifc: list(PROFS)
nc.forget = lambda profs: (calls.append(("forget", [p["uuid"] for p in profs])), "forgot")[1]
nc.forget_targets = lambda ifc, ssid: ([p for p in PROFS if p["ifname"] == ifc and p["ssid"] == ssid],
                                       [p for p in PROFS if p["ifname"] == "" and p["ssid"] == ssid])
wm.disconnect = lambda ifc: (calls.append(("disconnect", ifc)), f"{ifc}: disconnected")[1]
nc.disconnect_all = lambda: (calls.append(("disconnect_all",)), "disconnected wlan0, wlan1")[1]
nc.set_main = lambda ifc: (calls.append(("main", ifc)), f"{ifc} is now the main network")[1]


def mon_on(ifc, channel=None):
    calls.append(("mon_on", ifc))
    return f"{ifc}: MONITOR"


wm.mon_on = mon_on
wm.mon_off = lambda ifc, autoconnect=True: (calls.append(("mon_off", ifc)), f"{ifc}: MANAGED")[1]
wm.phy_channels = lambda phy: [(1, 2412), (6, 2437), (36, 5180), (149, 5745)]


REAL_SURVEY = moncore.Survey


class FakeSurvey:
    def __init__(self, ifc, chans, band="ALL", mode="monitor", phy=None):
        self.s = pv.survey_with_aps(ifc, REAL_SURVEY)
        self.mode, self.phy, self.cap_if = mode, phy, (ifc if mode == "monitor" else ifc + "mon")
        calls.append(("survey_mode", mode, band))
        self.all_channels, self.band, self.locked, self.cur_chan = chans, band, None, 36
        self.pcap, self.pcap_bytes, self.pcap_path = None, 0, ""
        self.kicking, self.started, self.dwell = False, time.time(), 0.3
        self.band_frames = {"2G": 120, "5G": 900, "6G": 0}

    def channels(self):
        return [c for c, _ in self.all_channels]

    def kick_24(self):
        calls.append(("kick",))

    def start(self):
        calls.append(("survey_start",))

    def stop(self):
        calls.append(("survey_stop",))

    def snapshot(self):
        return [a for a in self.s.snapshot() if self.band == "ALL" or a["band"] == self.band]

    def set_band(self, b):
        self.band = b
        calls.append(("band", b))

    def lock_channel(self, c):
        self.locked = c
        calls.append(("lock", c))

    def start_capture(self):
        self.pcap, self.pcap_path = True, "/home/rial/captures/x.pcap"
        calls.append(("rec_start",))

    def stop_capture(self):
        if self.pcap:
            self.pcap = None
            calls.append(("rec_stop",))
            return self.pcap_path, 42, 4096
        return None


moncore.Survey = FakeSurvey
moncore.caps_for = lambda driver: {"mon24": False, "sweep": True, "reset24": False}   # a chip that needs scan-sweep on 2.4 GHz


# ---------------- keyboard that waits while a job is running
class ScriptKB(pv.FakeKB):
    def __init__(self, script):
        super().__init__(script)
        self.app = None
        self.trace = []

    def poll(self, t):
        time.sleep(0.005)
        if self.app and self.app.stack and isinstance(self.app.stack[-1], dr.BusyScreen):
            return []
        if self.script:
            k = self.script.pop(0)
            top = type(self.app.stack[-1]).__name__ if self.app and self.app.stack else "-"
            if callable(k):
                k()
                return []
            self.trace.append((top, k.code, k.char))
            return [k]
        self.idle += 1
        if self.idle > 60:
            self.esc = time.time() - 1
        return []


def tops(app):
    return [type(s).__name__ for s in app.stack]


checks = []


def check(label, fn):
    def _c():
        try:
            ok = fn()
        except Exception as e:  # noqa
            ok = f"EXC {e!r}"
        checks.append((label, ok))
    return _c


script = [
    # --- duplicate guard: dongle -> HomeNet 2.4 GHz while wlan0 is already on it
    lambda: setattr(nc, "nodup_enabled", lambda: True),
    ch("x"), ch("s"), ch("x"), ENTER,
    check("detail is HomeNet 2G", lambda: app.stack[-1].net["ssid"] == "HomeNet" and app.stack[-1].net["band"] == "2G"),
    ENTER,
    check("duplicate guard offers the other band", lambda: isinstance(app.stack[-1], dr.ConfirmScreen)
          and "5G instead" in app.stack[-1].body),
    ESC, ESC, ESC, ch("f"),
    # --- Wi-Fi: scan on dongle, filter 5G, open Neighbour-5G, connect with password
    ch("x"), ch("s"),
    check("network list open (X = down selected wlan1)", lambda: isinstance(app.stack[-1], dr.NetListScreen) and app.stack[-1].ifc == "wlan1"),
    ENTER, ENTER,
    check("Enter on connected network -> options menu", lambda: isinstance(app.stack[-1], dr.MenuScreen) and
          [r[0] for r in app.stack[-1].rows][:2] == ["Reconnect", "Disconnect"]),
    ESC, ESC, ESC, ch("s"),
    ch("x"), ch("x"), ch("f"),
    check("F/X move in lists", lambda: app.stack[-1].idx == 1),
    ch("f"),
    ch("b"), ch("b"),
    check("band filter 5G", lambda: app.stack[-1].band == "5G" and all(n["band"] == "5G" for n in app.stack[-1].rows)),
    DOWN, ENTER,
    check("detail is Neighbour-5G", lambda: app.stack[-1].net["ssid"] == "Neighbour-5G"),
    DOWN, DOWN, UP,
    ENTER,
    check("password prompt shown", lambda: isinstance(app.stack[-1], dr.TextInputScreen)),
    ch("P"), ch("w"), ch("!"), ch("2"), ch("x"), BKSP, ch("3"), key(15), ENTER,
    check("connected + back home", lambda: tops(app) == ["HomeScreen"] and
          ("connect", "wlan1", "Neighbour-5G", "5G", "Pw!23") in calls),
    # --- saved networks on wlan1 -> forget bound profile
    ch("n"),
    check("saved list 2 rows", lambda: isinstance(app.stack[-1], dr.SavedScreen) and len(app.stack[-1].rows) == 2),
    ch("e"),
    check("forget confirm", lambda: isinstance(app.stack[-1], dr.ConfirmScreen)),
    ENTER,
    check("forgot only wlan1 profile", lambda: ("forget", ["u1"]) in calls),
    ESC,
    # --- disconnect wlan0 (carries SSH) -> SSH warning -> yes
    UP, ch("d"),
    check("ssh warning", lambda: isinstance(app.stack[-1], dr.ConfirmScreen) and "SSH" in app.stack[-1].title),
    ENTER,
    check("disconnected wlan0", lambda: ("disconnect", "wlan0") in calls),
    # --- disconnect both via menu, cancel
    ENTER,
    check("menu open", lambda: isinstance(app.stack[-1], dr.MenuScreen)),
    ch("a"),
    check("both-confirm mentions SSH", lambda: "SSH" in app.stack[-1].body),
    ESC,
    check("not executed", lambda: ("disconnect_all",) not in calls),
    # --- set main on wlan1
    DOWN, ch("p"),
    check("main set wlan1", lambda: ("main", "wlan1") in calls),
    # --- monitor on wlan1 -> survey screen -> band, lock, record, leave
    ch("m"),
    lambda: time.sleep(0.3),
    check("M opens survey in scan-sweep mode (no monitor mode switch)",
          lambda: isinstance(app.stack[-1], dr.MonitorScreen) and ("survey_mode", "sweep", "ALL") in calls
          and ("mon_on", "wlan1") not in calls),
    ch("b"),
    check("B -> 2.4 GHz stays in sweep", lambda: ("band", "2G") in calls and app.stack[-1].survey.mode == "sweep"),
    ch("b"), lambda: time.sleep(0.4),
    check("B -> 5 GHz switches to true monitor mode", lambda: ("mon_on", "wlan1") in calls
          and ("survey_mode", "monitor", "5G") in calls and app.stack[-1].survey.mode == "monitor"),
    check("survey 5G only", lambda: len(app.stack[-1].rows) >= 2 and all(a["band"] == "5G" for a in app.stack[-1].rows)),
    ch("l"), DOWN, ENTER,
    check("locked ch149", lambda: ("lock", 149) in calls),
    ch("r"),
    check("recording", lambda: ("rec_start",) in calls),
    ENTER, ESC,
    ESC,
    check("leaving 5 GHz survey asks to keep monitor mode", lambda: isinstance(app.stack[-1], dr.ConfirmScreen)
          and "monitor" in app.stack[-1].body),
    ESC, lambda: time.sleep(0.3),
    check("survey stopped + pcap saved", lambda: ("rec_stop",) in calls and ("survey_stop",) in calls),
    check("choosing Esc restores normal mode", lambda: ("mon_off", "wlan1") in calls and tops(app) == ["HomeScreen"]),
    lambda: calls.clear(),
    ch("m"), lambda: time.sleep(0.3), ch("b"), ch("b"), lambda: time.sleep(0.4), ESC, ENTER, lambda: time.sleep(0.3),
    check("choosing Enter keeps the dongle in monitor mode", lambda: ("mon_on", "wlan1") in calls
          and ("mon_off", "wlan1") not in calls and tops(app) == ["HomeScreen"]),
    # --- Bluetooth tab: scan on hci1, open WH-1000XM4? pick iPhone, pair w/ confirm, connect, hunt
    ch("c"), ch("x"), ch("s"),
    check("bt list hci1", lambda: isinstance(app.stack[-1], dr.BtListScreen) and app.stack[-1].a["hci"] == "hci1"),
    DOWN, ENTER,
    check("bt detail iPhone", lambda: isinstance(app.stack[-1], dr.BtDetailScreen)),
    ch("p"),
    lambda: time.sleep(0.3),
    check("pair confirm with code", lambda: isinstance(app.stack[-1], dr.ConfirmScreen) and app.stack[-1].big == "482913"),
    ENTER,
    lambda: time.sleep(0.3),
    check("paired", lambda: next(d for d in pv.DEVICES if d["path"] == "/d/2")["paired"]),
    ch("o"),
    check("connected", lambda: next(d for d in pv.DEVICES if d["path"] == "/d/2")["connected"]),
    ch("h"),
    check("hunt", lambda: isinstance(app.stack[-1], dr.BtHuntScreen)),
    lambda: time.sleep(1.2),
    check("hunt has samples", lambda: len(app.stack[-1].hist) >= 1),
    ESC, ch("e"), ENTER,
    check("removed + popped detail", lambda: all(d["path"] != "/d/2" for d in pv.DEVICES) and
          isinstance(app.stack[-1], dr.BtListScreen)),
    ESC,
    check("scan stopped", lambda: "/org/bluez/hci1" not in app.bt.scanning),
    ch("h"), ESC,
    check("home again", lambda: tops(app) == ["HomeScreen"]),
    ch("z"),
    check("Z switches tab", lambda: app.stack[0].tab == 0),
    ENTER, ch("h"),
    check("menu has Join hidden network", lambda: isinstance(app.stack[-1], dr.TextInputScreen) and "SSID" in app.stack[-1].label),
    ch("S"), ch("e"), ch("c"), ENTER,
    check("hidden: password prompt allows empty (open)", lambda: isinstance(app.stack[-1], dr.TextInputScreen) and app.stack[-1].allow_empty),
    ESC,
    ch("y"), ch("x"), ENTER,
    check("Settings: text size -> Large", lambda: dr.TEXT_SIZE == "large" and isinstance(app.stack[-1], dr.MenuScreen)),
    ENTER,
    check("Settings: back to Normal", lambda: dr.TEXT_SIZE == "normal"),
    ESC,
    ch("n"),
    check("saved list renders real-world names", lambda: isinstance(app.stack[-1], dr.SavedScreen)),
    lambda: app.stack[-1].rows.append({"name": "x", "uuid": "u9", "ssid": "Bad\nName", "band": "", "ifname": "wlan1",
                                       "mode": "infrastructure", "autoconnect": "yes"}),
    lambda: app.stack[-1].render(),
    check("SSID with newline doesn't crash render", lambda: isinstance(app.stack[-1], dr.SavedScreen)),
    lambda: setattr(app.stack[-1], "render", lambda: 1 / 0),
    lambda: setattr(app.stack[-1], "dirty", True),
    lambda: time.sleep(0.3),
    check("screen error is contained (popped + toast, app alive)", lambda: tops(app) == ["HomeScreen"] and "error" in app.toast_msg),
]

pv.install_mocks()
dr.save_size = dr.save_theme = lambda n: None
kb = ScriptKB(script)
app = dr.App(pv.Sink(), kb)
kb.app = app
deadline = time.time() + 3
while app.bt is None and time.time() < deadline:
    time.sleep(0.05)
t0 = time.time()
app.run(dr.HomeScreen(app))
app.shutdown()
fails = [(l, r) for l, r in checks if r is not True]
for l, r in checks:
    print(("PASS " if r is True else "FAIL ") + l + ("" if r is True else f"  -> {r}"))
print(f"\n{len(checks) - len(fails)}/{len(checks)} checks passed in {time.time() - t0:.1f}s; frames rendered: {len(app.fb.frames)}")
if fails:
    print("key trace:", kb.trace)
sys.exit(1 if fails else 0)
