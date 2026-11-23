"""MonitorScreen._check_24_silent: monitor survey deaf on 2.4 GHz -> live switch to scan-sweep."""
import os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import preview as pv
pv.install_mocks()
import dualradio as dr, moncore
saved, toasts, res = {}, [], []
moncore.save_caps = lambda k, c: saved.update({k: c})
dr.wm.log = lambda *a, **k: None

class App:
    survey = None
    def toast(self, m, c=None): toasts.append(m)

class S:
    def __init__(self, band, f24=0, f5=300, mode="monitor", locked=None):
        self.band, self.mode, self.locked = band, mode, locked
        self.band_frames = {"2G": f24, "5G": f5, "6G": 0}
        self.stopped = False
    def stop_capture(self): return None
    def stop(self): self.stopped = True

def screen(s, age):
    m = dr.MonitorScreen.__new__(dr.MonitorScreen)
    m.app, m.i = App(), {"name": "wlan1", "driver": "rtw88_8821cu", "usb_id": "0bda:c820"}
    m.bands, m.caps, m.fell_back, m.prior = ["ALL", "2G", "5G"], {"mon24": True, "sweep": False}, False, "test"
    m.survey, m.started, m.pending, m.rows = s, time.time() - age, None, []
    m.reset_tried = m.reset_used = m.reset_next = False
    return m

def check(l, ok): res.append(ok); print(("PASS " if ok else "FAIL ") + l)

m = screen(S("ALL"), 5); m._check_24_silent(m.survey)
check("ALL: no switch before a full pass", m.pending is None)
m = screen(S("ALL"), 20); m._check_24_silent(m.survey)
check("ALL: silent 2.4 after 18 s -> restart as sweep", m.pending == "ALL" and m.mode_for("ALL") == "sweep")
check("caps saved mon24=False sweep=True", list(saved.values())[-1]["mon24"] is False and list(saved.values())[-1]["sweep"])
check("toast shown", "scan-sweep" in toasts[-1])
m = screen(S("2G", f24=40), 30); m._check_24_silent(m.survey)
check("2G frames heard -> stays monitor", m.pending is None)
m = screen(S("5G"), 60); m._check_24_silent(m.survey)
check("5G survey never falls back", m.pending is None)
m = screen(S("ALL", locked=36), 60); m._check_24_silent(m.survey)
check("locked on a 5 GHz channel -> no fallback", m.pending is None)
m = screen(S("2G", mode="sweep"), 60); m._check_24_silent(m.survey)
check("already sweep -> no loop", m.pending is None)
m = screen(S("2G"), 13); m._check_24_silent(m.survey); p1 = m.pending; m.pending = None
m.survey = S("2G"); m._check_24_silent(m.survey)
check("falls back only once per screen", p1 == "2G" and m.pending is None)
# --- USB dongle: first silence -> USB reset retry, not sweep
u = screen(S("ALL"), 20); u.i["bus"] = "usb"; u.reset_tried = False; u.reset_used = False; u.reset_next = False
u._check_24_silent(u.survey)
check("usb: silent -> reset + retry monitor (no sweep yet)", u.reset_next and u.pending == "ALL" and u.mode_for("ALL") == "monitor")
u.pending = None; u.reset_used = True; u.survey = S("ALL", f24=50); u._check_24_silent(u.survey)
check("usb: 2.4 heard after reset -> reset24 learned", u.caps.get("reset24") is True)
u.caps = {"mon24": True}; u.reset_used, u.fell_back = True, False; u.survey = S("ALL"); u.started = time.time() - 20
u._check_24_silent(u.survey)
check("usb: still silent after reset -> sweep, reset24 off", u.mode_for("ALL") == "sweep" and u.caps["reset24"] is False)

# --- usb_reset against a fake sysfs
import tempfile, threading as th
w = dr.wm
root = tempfile.mkdtemp(); w.SYSFS = root
dev = os.path.join(root, "devices/usb1/1-1"); intf = os.path.join(dev, "1-1:1.0")
os.makedirs(intf); open(os.path.join(dev, "authorized"), "w").write("1"); open(os.path.join(dev, "idVendor"), "w").write("0bda")
os.makedirs(os.path.join(root, "class/net"))
def mk(name):
    d = os.path.join(root, "class/net", name); os.makedirs(os.path.join(d, "phy80211"), exist_ok=True)
    os.symlink(intf, os.path.join(d, "device"))
mk("wlan1")
names = ["wlan1"]
w.list_wifi_ifaces = lambda: list(names)
def replug():   # the kernel brings the dongle back under a new name after authorized=1
    time.sleep(0.3)
    import shutil; shutil.rmtree(os.path.join(root, "class/net/wlan1")); names.clear()
    time.sleep(1.6); mk("wlan2"); names.append("wlan2")
th.Thread(target=replug, daemon=True).start()
check("usb_device_dir finds the device", w.usb_device_dir("wlan1") == dev)
nn = w.usb_reset("wlan1", timeout=8)
check("usb_reset waits for the renamed interface", nn == "wlan2" and open(os.path.join(dev, "authorized")).read() == "1")
try:
    w.usb_reset("nope", timeout=1); check("non-usb raises", False)
except w.WifiError:
    check("non-usb raises", True)
# --- known rtw88_8821cu dongle: reset preset, no probe needed
dr.moncore.caps_for = lambda k: None
dr.wm.phy_channels = lambda phy: [(1, 2412), (36, 5180)]
ms = dr.MonitorScreen(App(), {"name": "wlan1", "driver": "rtw_8821cu", "bus": "usb", "usb_id": "0bda:c811", "phy": "phy1"})
check("rtw8821cu preset: reset24 on, monitor on 2.4", ms.caps.get("reset24") and ms.mode_for("ALL") == "monitor")
dr.moncore.caps_for = lambda k: {"mon24": False, "sweep": True, "reset24": False}
ms = dr.MonitorScreen(App(), {"name": "wlan1", "driver": "rtw_8821cu", "bus": "usb", "usb_id": "0bda:c811", "phy": "phy1"})
check("learned reset24=False is respected", ms.mode_for("ALL") == "sweep")
dr.moncore.caps_for = lambda k: None
ms = dr.MonitorScreen(App(), {"name": "wlan2", "driver": "mt7921u", "bus": "usb", "usb_id": "0e8d:7961", "phy": "phy2"})
check("other chips: no preset (probe runs)", ms.caps is None)
print(f"\n{sum(res)}/{len(res)} checks passed"); sys.exit(0 if all(res) else 1)
