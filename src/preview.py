#!/usr/bin/env python3
"""Render DualRadio screens with mock data to PNG (no hardware). Also used by tests."""
import os
import time

import btcore
import dualradio as dr
import moncore
import netcore as nc

IFS = [
    dict(name="wlan0", mode="managed", mac="f0:40:af:90:77:da", phy="phy0", channel=11, freq=2462, width=20,
         driver="brcmfmac", bus="sdio", usb_id="", chip="", up=True, mon_ok=False, nm_state="connected", connection="HomeNet",
         ssid="HomeNet", signal=-46, txrate="72M", ip="192.168.1.40", ssh=True, role="INT", ap_ok=True,
         is_main=True, hotspot=False),
    dict(name="wlan1", mode="managed", mac="98:ba:5f:d2:0f:c2", phy="phy3", channel=36, freq=5180, width=80,
         driver="rtw88_8821cu", bus="usb", usb_id="0bda:c820", chip="RTL8821CU", up=True, mon_ok=True, nm_state="connected",
         connection="wlan1-HomeNet", ssid="HomeNet", signal=-38, txrate="325M", ip="192.168.1.39",
         ssh=False, role="USB", ap_ok=True, is_main=False, hotspot=False),
]
ADAPTERS = [
    dict(path="/org/bluez/hci0", hci="hci0", address="B8:27:EB:4C:21:9A", name="CardputerZero",
         alias="CardputerZero", powered=True, discovering=False, discoverable=False, bus="uart", role="INT", devices=2),
    dict(path="/org/bluez/hci1", hci="hci1", address="98:BA:5F:D2:0F:C3", name="CardputerZero #2",
         alias="CardputerZero #2", powered=True, discovering=True, discoverable=False, bus="usb", role="USB", devices=0),
]
IW_SCAN = open(os.path.join(os.path.dirname(os.path.realpath(__file__)), "preview_scan.txt")).read() \
    if os.path.exists(os.path.join(os.path.dirname(os.path.realpath(__file__)), "preview_scan.txt")) else ""


def _dev(path, adapter, addr, name, rssi, cls=None, app=None, paired=False, conn=False, mfr=None, uuids=(),
         atype="public", tx=None):
    return {"path": path, "adapter": adapter, "address": addr, "address_type": atype, "name": name,
            "alias": name or addr.replace(":", "-"), "class": cls, "appearance": app, "icon": "",
            "paired": paired, "bonded": paired, "trusted": paired, "blocked": False, "connected": conn,
            "legacy_pairing": False, "rssi": rssi, "tx_power": tx, "uuids": list(uuids),
            "manufacturer": mfr or {}, "service_data": {}}


DEVICES = [
    _dev("/d/1", "/org/bluez/hci1", "AC:80:0A:11:22:33", "WH-1000XM4", -54, cls=0x240404, paired=True, conn=True,
         uuids=["0000110b-0000-1000-8000-00805f9b34fb", "0000111e-0000-1000-8000-00805f9b34fb",
                "0000110e-0000-1000-8000-00805f9b34fb"]),
    _dev("/d/2", "/org/bluez/hci1", "F4:12:FA:9C:01:02", "Alex's iPhone", -61, app=0x0040, atype="public",
         mfr={0x004C: bytes.fromhex("1005031c")}),
    _dev("/d/3", "/org/bluez/hci1", "5A:11:22:33:44:55", "", -71, tx=-12, atype="random",
         mfr={0x004C: bytes.fromhex("0215f7826da64fa24e988024bc5b71e0893e00010002c5")}),
    _dev("/d/4", "/org/bluez/hci1", "E8:9F:6D:00:AA:01", "ESP32-S3 Sensor", -77, atype="public",
         uuids=["0000181a-0000-1000-8000-00805f9b34fb"]),
    _dev("/d/5", "/org/bluez/hci1", "C0:28:8D:01:02:03", "Garmin Instinct", -82, app=0x00C1),
]


class FakeKB:
    shift = caps = False
    fds = []

    def __init__(self, script=None):
        self.script = list(script or [])
        self.esc = None
        self.idle = 0

    def poll(self, t):
        time.sleep(0.01)
        if self.script:
            k = self.script.pop(0)
            if callable(k):
                k()
                return []
            return [k]
        self.idle += 1
        return []

    def esc_long(self):
        return self.esc is not None

    def close(self):
        pass


class FakeBt:
    def __init__(self):
        self.prompts = __import__("queue").Queue()
        self.scanning = set()
        self.agent_ok = True

    def adapters(self):
        return [dict(a) for a in ADAPTERS]

    def devices(self, path, paired_only=False):
        return [dict(d) for d in DEVICES if d["adapter"] == path and (d["paired"] or not paired_only)]

    def device(self, path):
        return dict(next(d for d in DEVICES if d["path"] == path))

    def rssi(self, path):
        return self.device(path)["rssi"] + int(3 * (time.time() % 2))

    def start_scan(self, p):
        self.scanning.add(p)

    def stop_scan(self, p):
        self.scanning.discard(p)

    def pair(self, path):
        import queue
        ans = queue.Queue()
        self.prompts.put({"kind": "confirm", "device": self.device(path)["alias"], "value": "482913",
                          "ok": lambda: ans.put(True), "err": lambda e: ans.put(False)})
        if ans.get(timeout=30):
            next(d for d in DEVICES if d["path"] == path).update(paired=True, trusted=True)
            return "paired + trusted"
        raise btcore.BtError("pairing failed: rejected")

    def connect(self, p):
        next(d for d in DEVICES if d["path"] == p)["connected"] = True
        return "connected"

    def disconnect(self, p):
        next(d for d in DEVICES if d["path"] == p)["connected"] = False
        return "disconnected"

    def trust(self, p, on):
        return "trusted" if on else "untrusted"

    def remove(self, a, p):
        DEVICES[:] = [d for d in DEVICES if d["path"] != p]
        return "removed"

    def power(self, p, on):
        return "ok"

    @staticmethod
    def reply(prompt, accept, value=None):
        (prompt["ok"]() if accept else prompt["err"](None))

    def close(self):
        pass


def install_mocks():
    nc.collect = lambda: [dict(i) for i in IFS]
    btcore.Bt = FakeBt


class Sink:
    def __init__(self):
        self.frames = []

    def show(self, img):
        self.frames.append(img)

    def close(self):
        pass


def make_app(script=None):
    install_mocks()
    app = dr.App(Sink(), FakeKB(script))
    deadline = time.time() + 3
    while app.bt is None and time.time() < deadline:
        time.sleep(0.05)
    app.refresh()
    return app


def survey_with_aps(ifc="wlan1", cls=None):
    s = (cls or moncore.Survey)(ifc, [(1, 2412), (6, 2437), (11, 2462), (36, 5180), (44, 5220), (149, 5745)])
    now = time.time()
    data = [("00:1a:2b:3c:4d:06", "HomeNet", 36, "5G", -39, "WPA2/WPA3", "ax", 4),
            ("00:1a:2b:3c:4d:05", "HomeNet", 11, "2G", -45, "WPA2", "n", 2),
            ("10:20:30:40:50:60", "Neighbour-5G", 149, "5G", -63, "WPA3", "ac", 1),
            ("9c:3d:cf:00:11:22", "NETGEAR42", 6, "2G", -71, "WPA2", "n", 0),
            ("00:11:22:33:44:55", "", 6, "2G", -74, "WPA2", "n", 0),
            ("4a:1a:2b:3c:4d:07", "", 36, "5G", -40, "WPA3", "ax", 0),
            ("5a:1a:2b:3c:4d:08", "", 36, "5G", -40, "OPEN", "ax", 0),
            ("aa:bb:cc:dd:ee:ff", "Cafe Free WiFi", 1, "2G", -80, "OPEN", "g", 3),
            ("c4:e9:84:12:34:56", "TP-Link_5G_8E", 44, "5G", -83, "WPA2", "ac", 0)]
    for b, ssid, ch, band, rssi, sec, std, cl in data:
        s.aps[b] = {"bssid": b, "ssid": ssid, "hidden": not ssid, "chan": ch, "freq": None, "band": band,
                    "rssi": rssi, "rssi_max": rssi + 3, "beacons": 120, "security": sec, "std": std,
                    "clients": {f"a2:00:00:00:00:{n:02x}": {"rssi": rssi - 6 - n, "frames": 40 - n, "last": now}
                                for n in range(cl)}, "first_seen": now - 40, "rssi_min": rssi - 7,
                    "data_frames": 380 * cl, "data_bytes": 210000 * cl, "country": "AU", "stations": cl or None,
                    "utilisation": 18, "wps": {"manufacturer": "Generic", "model": "R1"} if "HomeNet" in ssid else {},
                    "width": 80 if band == "5G" else 20, "rate_max": 54.0, "beacon_int": 100,
                    "last_seen": now - 0.4, "rsn": {"akm": ["PSK"], "pairwise": ["CCMP"], "group": "CCMP",
                                                    "pmf": "capable"} if sec != "OPEN" else None}
    s.cur_chan = 36
    return s


def run(outdir):
    os.makedirs(outdir, exist_ok=True)
    app = make_app()

    def snap(screen, name, push=True):
        if push:
            app.push(screen)
        screen.tick()
        img = screen.render()
        img.save(os.path.join(outdir, name + ".png"))
        app.last_frame = img
        return img

    home = dr.HomeScreen(app)
    app.push(home)
    app.toast("wlan1: connected to HomeNet (password reused)", dr.OK)
    snap(home, "01_wifi_home", push=False)
    bss = nc.parse_iw_scan(IW_SCAN)
    nl = dr.NetListScreen(app, "wlan1", bss, {"HomeNet", "Home-Office"})
    snap(nl, "02_networks")
    nd = dr.NetDetailScreen(app, "wlan1", nl.rows[0])
    snap(nd, "03_network_detail")
    app.pop()
    app.pop()
    mon_if = dict(IFS[1], mode="monitor")
    ms = dr.MonitorScreen.__new__(dr.MonitorScreen)
    dr.ListScreen.__init__(ms, app, "")
    ms.i, ms.survey, ms.pending, ms.made_monitor, ms.band = mon_if, survey_with_aps(), None, False, "ALL"
    ms.survey.mode = "monitor"
    ms.bands, ms.caps, ms.fell_back, ms.prior = ["ALL", "2G", "5G"], {"mon24": True}, False, ""
    ms.reset_tried = ms.reset_used = ms.reset_next = False
    ms.started = time.time()
    ms.survey.band_frames = {"2G": 1840, "5G": 3120, "6G": 0}
    ms.survey.pcap, ms.survey.pcap_bytes = object(), 1_843_200
    app.push(ms)
    ms.rows = ms.survey.snapshot()
    snap(ms, "04_monitor_survey", push=False)
    ms.survey.pcap = None
    app.stack.remove(ms)
    home.tab = 1
    app.toast("hci1: 5 devices found", dr.OK)
    snap(home, "05_bt_home", push=False)
    bl = dr.BtListScreen(app, ADAPTERS[1], scan=True)
    snap(bl, "06_bt_scan")
    bd = dr.BtDetailScreen(app, ADAPTERS[1], "/d/3")
    snap(bd, "07_bt_detail")
    hunt = dr.BtHuntScreen(app, ADAPTERS[1], "/d/2", "Alex's iPhone")
    hunt.hist = [-80, -79, -78, -78, -76, -75, -74, -74, -72, -70, -69, -67, -66, -66, -64, -63, -62, -61]
    snap(hunt, "08_bt_hunt")
    conf = dr.ConfirmScreen(app, "Confirm pairing", "Does Alex's iPhone show this code?", None, None,
                            dr.ACCENT, big="482913", hint="Enter = matches    Esc = reject")
    snap(conf, "09_bt_pair_confirm")
    while len(app.stack) > 1:
        app.stack.pop()
    home.tab = 0
    menu = None
    dr.wifi_menu(app, app.ifs[1])
    menu = app.stack[-1]
    snap(menu, "10_wifi_menu", push=False)
    app.stack.pop()
    hs = dr.HotspotScreen.__new__(dr.HotspotScreen)
    dr.Screen.__init__(hs, app)
    hs.ifc, hs.conf = "wlan0", {"ssid": "CardputerZero-AP", "psk": "k7Qm2xTa9LpZ"}
    snap(hs, "11_hotspot")
    ts = dr.TestScreen.__new__(dr.TestScreen)
    dr.Screen.__init__(ts, app)
    ts.ifc, ts.phase, ts.err = "wlan1", "done", None
    ts.ping = {"host": "1.1.1.1", "loss": 0.0, "min": 9.8, "avg": 12.4, "max": 18.1, "jitter": 2.1}
    ts.speed = {"bytes": 10_000_000, "secs": 0.98, "mbps": 81.6}
    snap(ts, "12_speed_test")
    app.stop.set()
    print(f"preview written to {outdir}")
