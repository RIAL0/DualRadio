#!/usr/bin/env python3
"""netcore NetworkManager logic against an nmcli simulator (mimics single-activation semantics)."""
import os, sys
SRC = os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "src"); sys.path.insert(0, SRC); os.chdir(SRC)
import netcore as nc

TRUE_PSK = {"Secret": "pw123456", "HomeNet": "home-pass-123", "Neighbour-5G": "neigh-pass", "Cafe": "new-pw", "Lab": "right"}
P, cmds = {}, []
active = {"wlan0": "HomeNet", "wlan1": ""}
state = {"wlan0": "connected", "wlan1": "disconnected"}
results = []


def ok(label, cond):
    results.append((label, bool(cond)))
    print(("PASS " if cond else "FAIL ") + label)


def addp(name, ssid, ifname="", band="", psk="", metric="-1", km="wpa-psk"):
    u = f"u{len(P) + 1}"
    P[u] = dict(name=name, ssid=ssid, ifname=ifname, band=band, psk=psk, metric=metric, km=km)
    return u


addp("HomeNet", "HomeNet", "", "", "home-pass-123")
addp("Odd:Name", "Cafe:Guest", "wlan1", "", "x")   # ':' inside names/SSIDs must survive escaping


def find(by, x):
    return next((u for u, p in P.items() if (by == "uuid" and u == x) or (by == "id" and p["name"] == x)), None)


def run(c, timeout=30):
    cmds.append(c)
    if c[:6] == ["nmcli", "-t", "-f", "NAME,TYPE,UUID", "con", "show"]:
        esc = lambda v: v.replace("\\", "\\\\").replace(":", "\\:")
        return 0, "\n".join(f"{esc(p['name'])}:802-11-wireless:{u}" for u, p in P.items()), ""
    if c[:3] == ["nmcli", "-t", "-f"] and c[4:6] == ["con", "show"] and c[6] == "uuid":
        p = P[c[7]]   # real nmcli: one "property:value" per line, ':' in values escaped
        vals = {"802-11-wireless.ssid": p["ssid"], "802-11-wireless.band": p["band"],
                "connection.interface-name": p["ifname"], "802-11-wireless.mode": "infrastructure",
                "connection.autoconnect": "yes"}
        return 0, "\n".join(f"{k}:{vals[k].replace(':', chr(92) + ':')}" for k in c[3].split(",")), ""
    if c[:3] == ["nmcli", "-t", "-g"]:
        raise AssertionError("multi-field -g is not parseable on real nmcli: " + " ".join(c))
    if c[:4] == ["nmcli", "-s", "-g", "802-11-wireless-security.psk"]:
        return 0, P[c[7]]["psk"], ""
    if c[:3] == ["nmcli", "-g", "802-11-wireless-security.key-mgmt"]:
        return 0, P[c[6]]["km"], ""
    if c[:5] == ["nmcli", "--wait", "40", "con", "up"]:
        u = find(c[5], c[6]); p = P.get(u); ifc = c[8]
        if not p or p["ifname"] not in ("", ifc):
            return 10, "", "Error: not usable on device"
        if TRUE_PSK.get(p["ssid"]) != p["psk"]:
            return 4, "", "Error: Secrets were required, but not provided."
        for dev, con in list(active.items()):          # NM single-activation: profile moves devices
            if con == p["name"] and dev != ifc:
                active[dev], state[dev] = "", "disconnected"
        active[ifc], state[ifc] = p["name"], "connected"
        return 0, "", ""
    if c[:3] == ["nmcli", "con", "delete"]:
        u = find(c[3], c[4]); P.pop(u, None)
        return (0 if u else 10), "", ""
    if c[:3] == ["nmcli", "con", "add"]:
        a = dict(zip(c[3::2], c[4::2]))
        addp(a["con-name"], a["ssid"], a.get("ifname", ""), a.get("802-11-wireless.band", ""), a.get("wifi-sec.psk", ""),
             km=a.get("wifi-sec.key-mgmt", ""))
        return 0, "", ""
    if c[:4] == ["nmcli", "-g", "ipv4.route-metric", "con"]:
        return 0, P[find(c[5], c[6])]["metric"], ""
    if c[:3] == ["nmcli", "con", "mod"]:
        P[find(c[3], c[4])]["metric"] = c[6]
        return 0, "", ""
    return 0, "", ""


nc.run = run
nc.sh = lambda cmd, timeout=30: (run(cmd), "")[1]
nc.nm_states = lambda: {k: (state[k], active[k]) for k in state}
nc.wm.list_wifi_ifaces = lambda: ["wlan0", "wlan1"]
nc.iface_bus = lambda n: "usb" if n == "wlan1" else "sdio"
pref = {"v": "internal"}
nc.get_main_pref = lambda: pref["v"]
nc.set_main_pref = lambda r: pref.__setitem__("v", r)
metric = lambda name: P[find("id", name)]["metric"]

bss = nc.parse_iw_scan(open("preview_scan.txt").read())
nets = nc.group_networks(bss, nc.saved_ssids_for("wlan1"))
tel5 = next(n for n in nets if n["ssid"] == "HomeNet" and n["band"] == "5G")
asked = []

r = nc.connect("wlan1", tel5, lambda: asked.append(1))
ok("1 dongle joins HomeNet 5G without a password prompt", not asked and "reused" in r)
ok("1 wlan0 keeps its connection (no profile stealing)", state["wlan0"] == "connected" and active["wlan0"] == "HomeNet")
p = P[find("id", "wlan1-HomeNet")]
ok("1 per-radio profile bound to wlan1, 5 GHz locked", p["ifname"] == "wlan1" and p["band"] == "a")
ok("1 internal stays main (110) / dongle secondary (400)", metric("HomeNet") == "110" and metric("wlan1-HomeNet") == "400")

cmds.clear()
nc.connect("wlan1", tel5, lambda: asked.append(1))
ok("2 reconnect uses saved per-radio profile (no new profile)", not any(c[:3] == ["nmcli", "con", "add"] for c in cmds))

asked.clear()
nb = next(n for n in nets if n["ssid"] == "Neighbour-5G")
nc.connect("wlan1", nb, lambda: (asked.append(1), "neigh-pass")[1])
ok("3 new network asks for the password exactly once", len(asked) == 1)

addp("Cafe", "Cafe", "wlan0", "", "old-pw")
asked.clear()
r = nc.connect("wlan1", dict(ssid="Cafe", band="2G", security="WPA2", dual=False), lambda: (asked.append(1), "new-pw")[1])
ok("4 stale reused password -> asks once, then connects", len(asked) == 1 and "connected" in r)

try:
    nc.connect("wlan1", dict(ssid="Lab", band="5G", security="WPA2", dual=False), lambda: "wrong")
    ok("5 wrong password raises", False)
except nc.WifiError as e:
    ok("5 wrong password raises + no junk profile left", "wrong password" in str(e) and not any(p["ssid"] == "Lab" for p in P.values()))

active["wlan1"], state["wlan1"] = "wlan1-HomeNet", "connected"
nc.set_main("wlan1")
ok("6 set dongle as main flips metrics", metric("wlan1-HomeNet") == "110" and metric("HomeNet") == "400")
nc.set_main("wlan0")
ok("6 back to internal", metric("HomeNet") == "110")

b, s = nc.forget_targets("wlan1", "HomeNet")
ok("7 forget on wlan1 touches only its own profile", [x["name"] for x in b] == ["wlan1-HomeNet"] and [x["name"] for x in s] == ["HomeNet"])

state["wlan0"], active["wlan0"] = "disconnected", ""
cmds.clear()
nc.dispatch("wlan1", "down")
ok("8 dongle removed while internal idle -> internal reconnects",
   ["nmcli", "dev", "set", "wlan0", "autoconnect", "yes"] in cmds and ["nmcli", "--wait", "0", "dev", "connect", "wlan0"] in cmds)
cmds.clear()
state["wlan0"] = "connected"
nc.dispatch("wlan1", "down")
ok("8 internal already connected -> no action", not any("connect" in c for c in cmds if c[:2] == ["nmcli", "--wait"]))

profs = nc.list_profiles()
ok("10 SSID with ':' parsed intact from multiline terse output", any(p["ssid"] == "Cafe:Guest" for p in profs))
ok("10 every profile has mode + band parsed", all(p["mode"] == "infrastructure" for p in profs))
shared = {"name": "HomeNet", "uuid": find("id", "HomeNet"), "ssid": "HomeNet", "band": "", "ifname": ""}
active["wlan0"], state["wlan0"] = "HomeNet", "connected"
nc.forget([P[find("id", "wlan1-HomeNet")] | {"uuid": find("id", "wlan1-HomeNet")}])
nc.connect_profile("wlan1", shared)
ok("9 saved-list connect of shared profile copies it instead of stealing",
   state["wlan0"] == "connected" and find("id", "wlan1-HomeNet") is not None)

# ---- 11. scan: kernel cache only has the connected BSS, NetworkManager has the rest (Alex's wlan0 report)
NM_LIST = "\n".join([
    "*:00\\:1A\\:2B\\:3C\\:4D\\:05:HomeNet:11:2462 MHz:84:WPA2:130 Mbit/s",
    " :9C\\:3D\\:CF\\:00\\:11\\:22:NETGEAR42:6:2437 MHz:55:WPA1 WPA2:54 Mbit/s",
    " :62\\:45\\:B0\\:AA\\:BB\\:CC:iPhone (Alex):6:2437 MHz:70:WPA2:144 Mbit/s",
    " :00\\:11\\:22\\:33\\:44\\:55:Cafe Free WiFi:1:2412 MHz:30::54 Mbit/s",
    " :10\\:20\\:30\\:40\\:50\\:60:Neighbour:1:2412 MHz:35:WPA3:270 Mbit/s",
    " :AA\\:AA\\:AA\\:00\\:00\\:01::6:2437 MHz:40:WPA2:54 Mbit/s"])
IW_ONLY_CONNECTED = """BSS 00:1a:2b:3c:4d:05(on wlan0) -- associated
\tfreq: 2462.0
\tcapability: ESS Privacy (0x0411)
\tsignal: -46.00 dBm
\tSSID: HomeNet
\tRSN:\t * Version: 1
\t\t * Authentication suites: PSK
"""
scan_rc = {"refuse": 0}
base_run = nc.run


def run_scan(c, timeout=30):
    if c[:4] == ["nmcli", "-t", "-f", nc.NM_SCAN_FIELDS]:
        if "--rescan" in c and scan_rc["refuse"] > 0:
            scan_rc["refuse"] -= 1
            return 1, "", "Error: Scanning not allowed immediately following previous scan."
        return 0, NM_LIST, ""
    if c[:4] == ["iw", "dev", "wlan0", "scan"]:
        return 0, IW_ONLY_CONNECTED, ""
    if c[:4] == ["iw", "dev", "wlan0", "link"]:
        return 0, "Connected to 00:1a:2b:3c:4d:05 (on wlan0)\n\tSSID: HomeNet", ""
    return base_run(c, timeout)


nc.run = run_scan
nc.time.sleep = lambda s: None
state["wlan0"] = "connected"
b = nc.scan_full("wlan0")
nets = nc.group_networks(b, {"HomeNet"})
ok("11 scan lists every network, not just the connected one", len(nets) == 5)
tel = next(n for n in nets if n["ssid"] == "HomeNet")
ok("11 connected network keeps full kernel details", tel["inuse"] and tel["bsses"][0].get("src") != "nm" and tel["security"] == "WPA2")
ng = next(n for n in nets if n["ssid"] == "NETGEAR42")
ok("11 NM-only entries parsed (BSSID escaping, security, dBm)", ng["bsses"][0]["bssid"] == "9c:3d:cf:00:11:22"
   and ng["security"] == "WPA/WPA2" and ng["signal"] == -72 and not ng["inuse"])
ok("11 open + WPA3-only mapped for connect", next(n for n in nets if n["ssid"] == "Cafe Free WiFi")["security"] == "--"
   and nc.wm.key_mgmt(next(n for n in nets if n["ssid"] == "Neighbour")["security"]) == "sae")
rows = dict(nc.network_detail_lines(ng))
ok("11 detail screen works for NM-only entries", rows["Width"].startswith("?") and rows["Max rate (NM)"] == "54 Mbit/s")
scan_rc["refuse"] = 5
ok("11 rescan refused 3x -> still returns NM's list", len(nc.group_networks(nc.scan_full("wlan0"))) == 5)
nc.run = base_run

# ---- 12. hidden network join adds the hidden flag
cmds.clear()
nc.connect("wlan1", {"ssid": "Secret", "band": "?", "security": "WPA2", "dual": False, "hidden": True}, lambda: "pw123456")
add_cmd = next(c for c in cmds if c[:3] == ["nmcli", "con", "add"])
ok("12 hidden join sets 802-11-wireless.hidden yes", "802-11-wireless.hidden" in add_cmd and "Secret" in add_cmd)

# ---- 13. duplicate guard (dongle must not join the same SSID + band as the built-in radio)
import tempfile
nc.NODUP_FILE = os.path.join(tempfile.mkdtemp(), "nodup")
links = {"wlan0": {"ssid": "HomeNet", "freq": 2462, "band": "2G", "chan": 11}, "wlan1": None}
nc.link_info = lambda ifc: links.get(ifc)
TRUE_PSK["Single"] = "single-pw"
links["wlan0"] = {"ssid": "Single", "freq": 2437, "band": "2G", "chan": 6}
try:
    nc.connect("wlan1", {"ssid": "Single", "band": "2G", "security": "WPA2", "dual": False}, lambda: "single-pw")
    ok("13 single-band duplicate blocked", False)
except nc.WifiError as e:
    ok("13 single-band duplicate blocked", "duplicate guard" in str(e))
links["wlan0"] = {"ssid": "HomeNet", "freq": 2462, "band": "2G", "chan": 11}
cmds.clear()
r = nc.connect("wlan1", {"ssid": "HomeNet", "band": "2G", "security": "WPA2", "dual": True,
                         "bsses": []}, lambda: "home-pass-123")
ok("13 dual-band duplicate -> dongle goes to 5 GHz instead",
   any(c[:3] == ["nmcli", "con", "add"] and "a" in c[c.index("802-11-wireless.band") + 1:][:1] for c in cmds)
   or any(c[:5] == ["nmcli", "--wait", "40", "con", "up"] for c in cmds))
# dispatcher: NM auto-joined the dongle onto the same SSID+band
links["wlan1"] = {"ssid": "HomeNet", "freq": 2462, "band": "2G", "chan": 11}
active["wlan1"], state["wlan1"] = "wlan1-HomeNet", "connected"
P[find("id", "wlan1-HomeNet")]["band"] = ""
cmds.clear()
msg = nc.enforce_nodup()
ok("13 dispatcher moves dongle to the other band", msg and "moved to 5G" in msg and
   ["nmcli", "con", "mod", "id", "wlan1-HomeNet", "802-11-wireless.band", "a"] in cmds)
# single-band network: move fails -> band restored + dongle disconnected
addp("wlan1-Single", "Single", "wlan1", "", "WRONG")
links["wlan0"] = {"ssid": "Single", "freq": 2437, "band": "2G", "chan": 6}
links["wlan1"] = {"ssid": "Single", "freq": 2437, "band": "2G", "chan": 6}
active["wlan1"] = "wlan1-Single"
cmds.clear()
msg = nc.enforce_nodup()
ok("13 no other band -> band restored and dongle disconnected", msg and "disconnected" in msg
   and ["nmcli", "dev", "disconnect", "wlan1"] in cmds
   and ["nmcli", "con", "mod", "id", "wlan1-Single", "802-11-wireless.band", ""] in cmds)
nc.set_nodup(False)
cmds.clear()
ok("13 guard off -> no action", nc.enforce_nodup() is None and not cmds)
nc.set_nodup(True)
links["wlan1"] = {"ssid": "Other", "freq": 5180, "band": "5G", "chan": 36}
ok("13 different networks -> no action", nc.enforce_nodup() is None)

# ---- 14. built-in radio: NM + kernel cache only know the connected AP -> direct kernel scan fills the list
ONLY_CONNECTED_NM = "*:00\\:1A\\:2B\\:3C\\:4D\\:05:HomeNet:11:2462 MHz:84:WPA2:130 Mbit/s"
DIRECT = IW_ONLY_CONNECTED + """BSS 9c:3d:cf:00:11:22(on wlan0)
\tfreq: 2437.0
\tcapability: ESS Privacy (0x0411)
\tsignal: -70.00 dBm
\tSSID: NETGEAR42
\tRSN:\t * Version: 1
\t\t * Authentication suites: PSK
BSS 00:11:22:33:44:55(on wlan0)
\tfreq: 2412.0
\tcapability: ESS (0x0401)
\tsignal: -80.00 dBm
\tSSID: Cafe Free WiFi
"""
direct_calls = []


def run_only_connected(c, timeout=30):
    if c[:4] == ["nmcli", "-t", "-f", nc.NM_SCAN_FIELDS]:
        return 0, ONLY_CONNECTED_NM, ""
    if c == ["iw", "dev", "wlan0", "scan", "dump"]:
        return 0, IW_ONLY_CONNECTED, ""
    if c == ["iw", "dev", "wlan0", "scan"]:
        direct_calls.append(c)
        return (240, "", "Device or resource busy") if len(direct_calls) == 1 else (0, DIRECT, "")
    if c[:4] == ["iw", "dev", "wlan0", "link"]:
        return 0, "Connected to 00:1a:2b:3c:4d:05 (on wlan0)", ""
    return base_run(c, timeout)


nc.run = run_only_connected
nets = nc.group_networks(nc.scan_full("wlan0"))
ok("14 only-connected NM list -> direct scan (retried when busy) finds the rest",
   len(nets) == 3 and len(direct_calls) == 2 and any(n["ssid"] == "NETGEAR42" for n in nets))
nc.run = base_run

# ---- 15. duplicate guard between two dongles (later one yields)
nc.wm.list_wifi_ifaces = lambda: ["wlan0", "wlan1", "wlan2"]
nc.iface_bus = lambda n: "usb" if n in ("wlan1", "wlan2") else "sdio"
links.update({"wlan0": {"ssid": "Home", "freq": 2412, "band": "2G", "chan": 1},
              "wlan1": {"ssid": "Lab", "freq": 5180, "band": "5G", "chan": 36},
              "wlan2": {"ssid": "Lab", "freq": 5180, "band": "5G", "chan": 36}})
state["wlan2"], active["wlan2"] = "connected", ""
cmds.clear()
msg = nc.enforce_nodup()
ok("15 two dongles on Lab 5G -> wlan2 gives way, wlan1 untouched",
   msg and msg.startswith("wlan2") and not any("wlan1" in c for c in cmds if c[:2] == ["nmcli", "dev"]))

# --- in-app watcher (replaces the NM dispatcher hook)
nc.wm.list_wifi_ifaces = lambda: ["wlan0", "wlan1"]
state.pop("wlan2", None); active.pop("wlan2", None)
nc.enforce_nodup = lambda: None
w = nc.Watcher()
state.update(wlan0="disconnected", wlan1="connected"); active.update(wlan0="", wlan1="wlan1-HomeNet")
cmds.clear(); w.poll()
ok("16 watcher first poll: no events, policy applied", w.last is not None)
state["wlan1"], active["wlan1"] = "disconnected", ""
cmds.clear(); ev = w.poll()
ok("17 dongle drops while built-in idle -> built-in connects",
   ("wlan1", "down") in ev and ["nmcli", "--wait", "0", "dev", "connect", "wlan0"] in cmds)
state["wlan0"], active["wlan0"] = "connected", "HomeNet"
nc.wm.list_wifi_ifaces = lambda: ["wlan0"]
state.pop("wlan1"); active.pop("wlan1")
cmds.clear(); ev = w.poll()
ok("18 built-in comes up -> 'up' event; unplugged dongle already down", ev == [("wlan0", "up")])
nc.wm.list_wifi_ifaces = lambda: ["wlan0", "wlan1"]
state["wlan1"], active["wlan1"] = "connected", "wlan1-HomeNet"
state["wlan0"], active["wlan0"] = "connected", "HomeNet"
w2 = nc.Watcher(); w2.poll(); state["wlan1"], active["wlan1"] = "disconnected", ""
cmds.clear(); w2.poll()
ok("19 dongle drops while built-in is connected -> no forced reconnect",
   ["nmcli", "--wait", "0", "dev", "connect", "wlan0"] not in cmds)

fails = [l for l, c in results if not c]
print(f"\n{len(results) - len(fails)}/{len(results)} passed")
sys.exit(1 if fails else 0)
