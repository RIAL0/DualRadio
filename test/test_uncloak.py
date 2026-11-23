"""Survey.handle: cloaked beacon stays hidden; assoc / reassoc / directed probe / probe
response reveal the name; broadcast probes and other APs are untouched."""
import os, struct, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import wifimgr as wm
wm.log = lambda *a, **k: None
import moncore

def rt(freq=2437, sig=-40):
    # radiotap: present = CHANNEL(bit3) + DBM_ANTSIGNAL(bit5); channel field 2-aligned at off 8
    present = (1 << 3) | (1 << 5)
    body = struct.pack("<HH", freq, 0x00a0) + struct.pack("<b", sig) + b"\x00"
    return struct.pack("<BBHI", 0, 0, 8 + len(body), present) + body

def mac(s): return bytes(int(x, 16) for x in s.split(":"))
AP, STA, BC = mac("aa:bb:cc:00:00:01"), mac("02:11:22:33:44:55"), b"\xff" * 6
def ie_ssid(n): return bytes([0, len(n)]) + n.encode()
DS6 = bytes([3, 1, 6])

def mgmt(sub, a1, a2, a3, body):
    return struct.pack("<H", sub << 4) + b"\x00\x00" + a1 + a2 + a3 + b"\x00\x00" + body

def beacon(ssid_ie, sub=8):
    return mgmt(sub, BC, AP, AP, b"\x00" * 8 + struct.pack("<HH", 100, 0x0411) + ssid_ie + DS6)

s = moncore.Survey.__new__(moncore.Survey)
import threading
s.lock, s.aps, s.band, s.mode, s.cur_chan = threading.Lock(), {}, "ALL", "monitor", 6
s.band_frames = {"2G": 0, "5G": 0, "6G": 0}
res = []
def check(label, ok): res.append((label, ok)); print(("PASS " if ok else "FAIL ") + label)
k = "aa:bb:cc:00:00:01"

s.handle(rt() + beacon(bytes([0, 0])))
check("cloaked beacon -> hidden, no name", s.aps[k]["hidden"] and s.aps[k]["ssid"] == "")
s.handle(rt() + mgmt(4, BC, STA, BC, ie_ssid("Nope")))
check("broadcast probe request ignored", s.aps[k]["ssid"] == "")
s.handle(rt() + mgmt(0, AP, STA, AP, struct.pack("<HH", 0x0411, 10) + ie_ssid("HomeMesh")))
check("assoc request reveals name", s.aps[k]["ssid"] == "HomeMesh" and s.aps[k]["hidden"])
s.handle(rt() + beacon(bytes([0, 8]) + b"\x00" * 8))
check("later null-padded beacon keeps revealed name + hidden flag", s.aps[k]["ssid"] == "HomeMesh" and s.aps[k]["hidden"])

s.aps.clear()
s.handle(rt() + beacon(bytes([0, 0])))
s.handle(rt() + mgmt(2, AP, STA, AP, struct.pack("<HH", 0x0411, 10) + STA + ie_ssid("Backhaul")))
check("reassoc request reveals name", s.aps[k]["ssid"] == "Backhaul")
s.aps.clear()
s.handle(rt() + beacon(bytes([0, 0])))
s.handle(rt() + mgmt(4, AP, STA, AP, ie_ssid("Directed")))
check("directed probe request reveals name", s.aps[k]["ssid"] == "Directed")
s.aps.clear()
s.handle(rt() + beacon(bytes([0, 0])))
s.handle(rt() + beacon(ie_ssid("ProbeResp"), sub=5))
check("probe response reveals name, still flagged hidden", s.aps[k]["ssid"] == "ProbeResp" and s.aps[k]["hidden"])
s.aps.clear()
s.handle(rt() + beacon(ie_ssid("Open")))
check("normal beacon not hidden", s.aps[k]["ssid"] == "Open" and not s.aps[k]["hidden"])
s.handle(rt() + mgmt(0, mac("aa:bb:cc:00:00:09"), STA, mac("aa:bb:cc:00:00:09"), b"\x00" * 4 + ie_ssid("Other")))
check("assoc to unknown AP creates nothing", len(s.aps) == 1)

# --- hidden BSS on the same router as a named one
def ap(b, ssid, ch=36): return {"bssid": b, "ssid": ssid, "chan": ch}
named = [ap("3c:58:5d:a0:aa:90", "HomeNet"), ap("10:20:30:40:50:60", "Other")]
check("sibling: last octet +1", moncore.bss_sibling(ap("3c:58:5d:a0:aa:91", ""), named)["ssid"] == "HomeNet")
check("sibling: LAA first octet", moncore.bss_sibling(ap("3e:58:5d:a0:aa:90", ""), named)["ssid"] == "HomeNet")
check("no sibling on another channel", moncore.bss_sibling(ap("3c:58:5d:a0:aa:91", "", 11), named) is None)
check("no sibling for unrelated BSSID", moncore.bss_sibling(ap("aa:bb:cc:dd:ee:ff", ""), named) is None)
check("no sibling when middle octets differ", moncore.bss_sibling(ap("3c:58:5d:a1:aa:91", ""), named) is None)
s2 = moncore.Survey.__new__(moncore.Survey); s2.lock = threading.Lock()
s2.aps = {"3c:58:5d:a0:aa:90": dict(ap("3c:58:5d:a0:aa:90", "HomeNet"), rssi=-40, clients={}),
          "3c:58:5d:a0:aa:92": dict(ap("3c:58:5d:a0:aa:92", ""), rssi=-41, clients={}, hidden=True)}
snap = {x["bssid"]: x for x in s2.snapshot()}
tel = [ap("be:12:34:56:78:96", "NeighbourNet", 6)]
check("ISP-style LAA variant (be->ae, +1) matches", moncore.bss_sibling(ap("ae:12:34:56:78:97", "", 6), tel) is not None)
check("multicast-bit flip never matches", moncore.bss_sibling(ap("bf:fb:0f:a0:aa:97", "", 6), tel) is None)
check("global OUIs differing in octet0 don't match", moncore.bss_sibling(ap("10:fb:0f:a0:aa:97", "", 6), [ap("20:fb:0f:a0:aa:96", "X", 6)]) is None)
check("snapshot tags hidden sibling", snap["3c:58:5d:a0:aa:92"]["sibling"] == "HomeNet")

import netcore
_v = netcore.vendor
netcore.vendor = lambda b: "Smart Innovation LLC"
check("vendor hint trimmed", moncore._vendor_hint("10:2c:b1:80:b2:c3") == "Smart Innovation")
netcore.vendor = lambda b: "randomised / locally administered"
check("no hint for randomised MAC", moncore._vendor_hint("ae:12:34:56:78:97") == "")
netcore.vendor = lambda b: "unknown (install ieee-data)"
check("no hint when OUI db missing", moncore._vendor_hint("10:2c:b1:80:b2:c3") == "")
netcore.vendor = _v

fails = [l for l, ok in res if not ok]
print(f"\n{len(res) - len(fails)}/{len(res)} checks passed")
sys.exit(1 if fails else 0)
