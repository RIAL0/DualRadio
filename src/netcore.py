#!/usr/bin/env python3
"""
netcore - DualRadio Wi-Fi engine (extends wifimgr).

Adds: full network details from `iw scan dump`, per-radio NetworkManager
profiles (<ifname>-<ssid>, compatible with the WiFi USB app) with PSK reuse
across radios, forget-per-radio, disconnect-all, "main network" route policy,
auto-fallback helper for the NM dispatcher, hotspot, ping / speed tests.

CLI (used by the NetworkManager dispatcher script):
  netcore.py dispatch IFACE ACTION     apply main-route policy / failover
  netcore.py main [internal|usb]       show or set the main radio
  netcore.py apply-main                re-apply route metrics now
"""
import json
import os
import re
import secrets
import socket
import ssl
import string
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import wifimgr as wm  # noqa: E402
from wifimgr import WifiError, band_of, log, nm_states, run, sh, split_terse  # noqa: E402,F401

wm.LOG_FILE = "/var/log/dualradio.log"

STATE_DIR = "/etc/dualradio"
MAIN_FILE = os.path.join(STATE_DIR, "main")
HOTSPOT_FILE = os.path.join(STATE_DIR, "hotspot.json")
HOTSPOT_PROFILE = "dualradio-hotspot"
# Ethernet defaults to metric 100 in NetworkManager, so a plugged cable still wins.
METRIC_MAIN, METRIC_OTHER = 110, 400
DNS_MAIN, DNS_OTHER = 10, 100


# ------------------------------------------------------------------ radios
_modes = {}


def parse_modes(out):
    modes, in_block = set(), False
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("Supported interface modes:"):
            in_block = True
            continue
        if in_block:
            if s.startswith("* "):
                modes.add(s[2:].strip().lower())
            else:
                in_block = False
    return modes


def phy_modes(phy):
    if phy and phy not in _modes:
        _modes[phy] = parse_modes(run(["iw", "phy", phy, "info"])[1])
    return _modes.get(phy, set())


def iface_bus(name):
    dev = os.path.realpath(f"/sys/class/net/{name}/device")
    return "usb" if "/usb" in dev else ("sdio" if "mmc" in dev else "?")


def internal_iface():
    for n in wm.list_wifi_ifaces():
        if iface_bus(n) != "usb":
            return n
    return None


def default_route_dev():
    _, out, _ = run(["ip", "-4", "route", "show", "default"])
    best = None
    for line in out.splitlines():
        m = re.search(r"dev (\S+)", line)
        mm = re.search(r"metric (\d+)", line)
        if m:
            metric = int(mm.group(1)) if mm else 0
            if best is None or metric < best[1]:
                best = (m.group(1), metric)
    return best[0] if best else None


# Known USB Wi-Fi chips by USB ID (only IDs we're sure of; anything else shows the driver name)
USB_CHIPS = {"0bda:c811": "RTL8811CU", "0bda:c820": "RTL8821CU", "0bda:c821": "RTL8821CU",
             "0bda:c82b": "RTL8821CU", "0bda:b82b": "RTL8821CU", "0e8d:7961": "MT7921AU",
             "0e8d:7612": "MT7612U", "0e8d:7610": "MT7610U", "148f:7601": "MT7601U",
             "0cf3:9271": "AR9271", "148f:5370": "RT5370", "148f:3070": "RT3070"}


def chip_name(i):
    return USB_CHIPS.get(i.get("usb_id", ""), "")


def caps_key(i):
    """Survey capabilities are remembered per chip/USB ID, not just per driver
    (one driver can serve several chips, e.g. rtw88_8821cu -> RTL8811CU and RTL8821CU)."""
    return f"{i['driver']}@{i['usb_id']}" if i.get("usb_id") else i["driver"]


def collect():
    ifs = wm.collect()
    main_dev = default_route_dev()
    for i in ifs:
        i["role"] = "USB" if i["bus"] == "usb" else "INT"
        i["chip"] = chip_name(i)
        i["ap_ok"] = "ap" in phy_modes(i["phy"])
        i["is_main"] = i["name"] == main_dev
        i["hotspot"] = i["connection"] == HOTSPOT_PROFILE
    return ifs


# ------------------------------------------------------------------ scan details
def freq_to_chan(f):
    if f is None:
        return None
    if f == 2484:
        return 14
    if 2412 <= f <= 2472:
        return (f - 2407) // 5
    if 5000 <= f <= 5925:
        return (f - 5000) // 5
    if 5955 <= f <= 7115:
        return (f - 5950) // 5
    return None


def _new_bss(bssid, assoc):
    return {"bssid": bssid, "associated": assoc, "ssid": "", "freq": None, "chan": None,
            "signal": None, "last_seen_ms": None, "beacon_int": None, "capability": "",
            "privacy": False, "country": "", "rate_max": None, "stations": None,
            "utilisation": None, "ht": False, "vht": False, "he": False, "width": 20,
            "rsn": {}, "wpa": {}, "wps": {}}


def _sub(b, section, item):
    key, _, val = item.partition(":")
    key, val = key.strip(), val.strip()
    if section == "load":
        if key == "station count":
            b["stations"] = int(val) if val.isdigit() else None
        elif key == "channel utilisation":
            m = re.match(r"(\d+)/255", val)
            if m:
                b["utilisation"] = round(int(m.group(1)) * 100 / 255)
    elif section == "htop":
        if key == "secondary channel offset" and val in ("above", "below"):
            b["width"] = max(b["width"], 40)
    elif section == "vhtop":
        if key == "channel width":
            m = re.search(r"\((\d+)", val)
            if m and m.group(1) in ("80", "160"):
                b["width"] = max(b["width"], int(m.group(1)))
            elif val.startswith("1"):
                b["width"] = max(b["width"], 80)
            elif val.startswith("2") or val.startswith("3"):
                b["width"] = max(b["width"], 160)
    elif section in ("rsn", "wpa"):
        b[section][key] = val
    elif section == "wps":
        b["wps"][key] = val


def parse_iw_scan(out):
    bsses, cur, section = [], None, None
    for raw in out.splitlines():
        if raw.startswith("BSS "):
            m = re.match(r"BSS ([0-9a-fA-F:]{17})", raw)
            if not m:
                cur = None
                continue
            cur = _new_bss(m.group(1).lower(), "associated" in raw)
            bsses.append(cur)
            section = None
            continue
        if cur is None or not raw.strip():
            continue
        indent = len(raw) - len(raw.lstrip("\t"))
        line = raw.strip()
        if indent <= 1:
            section = None
            key, _, val = line.partition(":")
            k, val = key.strip(), val.strip()
            if k == "freq":
                try:
                    cur["freq"] = int(float(val.split()[0]))
                except (ValueError, IndexError):
                    pass
            elif k == "signal":
                try:
                    cur["signal"] = int(round(float(val.split()[0])))
                except (ValueError, IndexError):
                    pass
            elif k == "last seen":
                m = re.match(r"(\d+) ms ago", val)
                if m:
                    cur["last_seen_ms"] = int(m.group(1))
            elif k == "beacon interval":
                m = re.match(r"(\d+)", val)
                cur["beacon_int"] = int(m.group(1)) if m else None
            elif k == "capability":
                cur["capability"] = val
                cur["privacy"] = "Privacy" in val
            elif k == "SSID":
                cur["ssid"] = val
            elif k == "DS Parameter set":
                m = re.search(r"channel (\d+)", val)
                if m:
                    cur["chan"] = int(m.group(1))
            elif k == "Country":
                cur["country"] = val.split()[0] if val else ""
            elif k in ("Supported rates", "Extended supported rates"):
                rates = [float(x.rstrip("*")) for x in val.split() if re.match(r"^[\d.]+\*?$", x)]
                if rates:
                    cur["rate_max"] = max(rates + ([cur["rate_max"]] if cur["rate_max"] else []))
            elif k == "BSS Load":
                section = "load"
            elif k == "HT capabilities":
                cur["ht"] = True
                section = None
            elif k == "HT operation":
                cur["ht"] = True
                section = "htop"
            elif k == "VHT capabilities":
                cur["vht"] = True
                section = None
            elif k == "VHT operation":
                cur["vht"] = True
                section = "vhtop"
            elif k.startswith("HE capabilities") or k.startswith("HE operation") or k == "HE Iftypes":
                cur["he"] = True
                section = None
            elif k == "RSN":
                section = "rsn"
            elif k == "WPA":
                section = "wpa"
            elif k == "WPS":
                section = "wps"
            if section and val.startswith("*"):
                _sub(cur, section, val[1:].strip())
        elif section and line.startswith("*"):
            _sub(cur, section, line[1:].strip())
    for b in bsses:
        if b["chan"] is None:
            b["chan"] = freq_to_chan(b["freq"])
        b["band"] = band_of(b["freq"]) if b["freq"] else "?"
        b["security"] = security_summary(b)
        b["std"] = ("802.11ax" if b["he"] else "802.11ac" if b["vht"] else "802.11n" if b["ht"]
                    else "802.11a" if b["band"] == "5G" else "802.11g")
    return bsses


def security_summary(b):
    akm = " ".join([b["rsn"].get("Authentication suites", ""), b["wpa"].get("Authentication suites", "")])
    if b["rsn"] or b["wpa"]:
        parts = []
        if "PSK" in akm:
            if b["wpa"] and b["rsn"]:
                parts.append("WPA")
            parts.append("WPA2" if b["rsn"] else "WPA")
        if "SAE" in akm:
            parts.append("WPA3")
        if "802.1X" in akm:
            parts.append("802.1X")
        if "OWE" in akm:
            parts.append("OWE")
        return "/".join(parts) or ("WPA2" if b["rsn"] else "WPA")
    return "WEP" if b["privacy"] else "--"


def pmf_state(b):
    caps = b["rsn"].get("Capabilities", "")
    return "required" if "MFP-required" in caps else "capable" if "MFP-capable" in caps else "no"


def is_hidden(ssid):
    return not ssid or all(c in "\x00" for c in ssid) or ssid.replace("\\x00", "") == ""


def group_networks(bsses, saved_ssids=(), band_filter="ALL"):
    nets = {}
    for b in bsses:
        if is_hidden(b["ssid"]):
            continue
        if band_filter != "ALL" and b["band"] != band_filter:
            continue
        k = (b["ssid"], b["band"])
        n = nets.get(k)
        if n is None:
            n = nets[k] = {"ssid": b["ssid"], "band": b["band"], "bsses": [], "inuse": False}
        n["bsses"].append(b)
        n["inuse"] = n["inuse"] or b["associated"]
    out = []
    bands_by_ssid = {}
    for (ssid, band) in nets:
        bands_by_ssid.setdefault(ssid, set()).add(band)
    for n in nets.values():
        n["bsses"].sort(key=lambda b: (not b["associated"], -(b["signal"] or -200)))
        best = n["bsses"][0]
        n.update(chan=best["chan"], freq=best["freq"], signal=best["signal"], security=best["security"],
                 width=best.get("width"), std=best["std"], saved=n["ssid"] in saved_ssids,
                 dual=len(bands_by_ssid[n["ssid"]]) > 1)
        out.append(n)
    out.sort(key=lambda n: (not n["inuse"], not n["saved"], -(n["signal"] or -200)))
    return out


def linked_bssid(ifc):
    _, out, _ = run(["iw", "dev", ifc, "link"])
    m = re.search(r"Connected to ([0-9a-fA-F:]{17})", out)
    return m.group(1).lower() if m else None


NM_SCAN_FIELDS = "IN-USE,BSSID,SSID,CHAN,FREQ,SIGNAL,SECURITY,RATE"


def nm_security(sec):
    """nmcli SECURITY ('WPA1 WPA2', 'WPA2 WPA3', 'WPA2 802.1X', 'OWE', '') -> our 'WPA/WPA2' style."""
    toks = [("WPA" if t == "WPA1" else t) for t in sec.split() if t not in ("--",)]
    return "/".join(toks) if toks else "--"


def parse_nm_scan(out):
    """BSS dicts from `nmcli -t -f NM_SCAN_FIELDS dev wifi list` (fewer details than iw, but NM keeps them)."""
    res = []
    for line in out.splitlines():
        f = split_terse(line)
        if len(f) < 8 or not re.match(r"^[0-9A-Fa-f:]{17}$", f[1]):
            continue
        inuse, bssid, ssid, chan, freq, sig, sec, rate = f[:8]
        try:
            fq = int(freq.split()[0])
        except (ValueError, IndexError):
            fq = None
        try:
            pct = int(sig)
        except ValueError:
            pct = 0
        b = _new_bss(bssid.lower(), inuse.strip() == "*")
        rm = re.match(r"([\d.]+)", rate or "")
        b.update(ssid=ssid, freq=fq, chan=int(chan) if chan.isdigit() else freq_to_chan(fq),
                 signal=round(pct / 2 - 100), width=None, src="nm", nm_signal_pct=pct,
                 nm_rate=f"{float(rm.group(1)):g} Mbit/s" if rm else "")
        b["band"] = band_of(fq) if fq else "?"
        b["security"] = nm_security(sec)
        b["privacy"] = b["security"] != "--"
        b["std"] = "?"
        res.append(b)
    return res


def scan_full(ifc):
    st = nm_states().get(ifc, ("?",))[0]
    if st == "unmanaged":
        raise WifiError(f"{ifc} is unmanaged (monitor?) - switch to managed first")
    if st == "unavailable":
        run(["nmcli", "dev", "set", ifc, "managed", "yes"])
        wm.wait_nm_ready(ifc, 6)
    nm_out = ""
    for attempt in range(3):
        # `--rescan yes` blocks until the scan finishes; NM refuses it shortly after another scan
        rc, nm_out, err = run(["nmcli", "-t", "-f", NM_SCAN_FIELDS, "dev", "wifi", "list", "ifname", ifc,
                               "--rescan", "yes"], timeout=45)
        if rc == 0:
            break
        log(f"rescan {ifc} refused (attempt {attempt + 1}): {err.strip()}")
        time.sleep(2.5)
    if not nm_out.strip():  # rescan never accepted: take NM's current list (it keeps APs longer than the kernel)
        _, nm_out, _ = run(["nmcli", "-t", "-f", NM_SCAN_FIELDS, "dev", "wifi", "list", "ifname", ifc], timeout=20)
    _, iw_out, _ = run(["iw", "dev", ifc, "scan", "dump"], timeout=20)
    # rich details from the kernel cache where available, NetworkManager's list for everything else
    nm_b, dump_b = parse_nm_scan(nm_out), parse_iw_scan(iw_out)
    merged = {b["bssid"]: b for b in nm_b}
    for b in dump_b:
        merged[b["bssid"]] = b
    direct_n = 0
    if len(merged) < 2:
        # NM handed back only the current AP (it often skips scans on a connected radio):
        # run a kernel scan ourselves - works while NM manages the interface
        for attempt in range(3):
            rc, out, err = run(["iw", "dev", ifc, "scan"], timeout=30)
            if rc == 0:
                direct = parse_iw_scan(out)
                direct_n = len(direct)
                for b in direct:
                    merged[b["bssid"]] = b
                break
            log(f"direct scan {ifc} attempt {attempt + 1}: {err.strip()[:80]}")
            time.sleep(1.5)
    log(f"scan {ifc}: NM {len(nm_b)}, kernel cache {len(dump_b)}, direct {direct_n}, total {len(merged)}")
    bsses = list(merged.values())
    # "-- associated" / NM's IN-USE can be stale right after a disconnect: trust the live link only
    cur = linked_bssid(ifc)
    for b in bsses:
        b["associated"] = cur is not None and b["bssid"] == cur
    if not bsses:
        raise WifiError(f"no networks found on {ifc} - try again in a few seconds")
    return bsses


# ------------------------------------------------------------------ vendor lookup
_oui = None
OUI_FILES = ("/usr/share/ieee-data/oui.txt", "/usr/share/misc/oui.txt", "/var/lib/ieee-data/oui.txt")


def vendor(mac):
    global _oui
    if not mac or len(mac) < 8:
        return ""
    first = int(mac[:2], 16)
    if first & 0x02:
        return "randomised / locally administered"
    if _oui is None:
        _oui = {}
        for path in OUI_FILES:
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    for line in f:
                        m = re.match(r"^([0-9A-F]{2})-([0-9A-F]{2})-([0-9A-F]{2})\s+\(hex\)\s+(.+)$", line.strip())
                        if m:
                            _oui[(m.group(1) + m.group(2) + m.group(3))] = m.group(4).strip()
                break
            except OSError:
                continue
    return _oui.get(mac.replace(":", "")[:6].upper(), "unknown (install ieee-data)" if not _oui else "unknown")


def network_detail_lines(net, other_ifs=()):
    """Key/value rows for the detail screen."""
    b = net["bsses"][0]
    rows = [("SSID", net["ssid"]), ("Band", f"{net['band']}  ch{b['chan']}  {b['freq']} MHz"),
            ("Width", f"{b['width']} MHz" if b.get("width") else "? (basic scan entry)"), ("Standard", b["std"]),
            ("Signal", f"{b['signal']} dBm" if b["signal"] is not None else "?"),
            ("Security", b["security"])]
    if b["rsn"]:
        rows += [("Ciphers", b["rsn"].get("Pairwise ciphers", "?") + " / grp " + b["rsn"].get("Group cipher", "?")),
                 ("Auth (AKM)", b["rsn"].get("Authentication suites", "?")),
                 ("PMF (802.11w)", pmf_state(b))]
    elif b["wpa"]:
        rows.append(("WPA1 ciphers", b["wpa"].get("Pairwise ciphers", "?")))
    if b.get("nm_rate"):
        rows.append(("Max rate (NM)", b["nm_rate"]))
    rows.append(("Saved", "yes" if net.get("saved") else "no"))
    if b["stations"] is not None:
        rows.append(("Clients (BSS load)", str(b["stations"])))
    if b["utilisation"] is not None:
        rows.append(("Channel busy", f"{b['utilisation']}%"))
    if b["country"]:
        rows.append(("Country", b["country"]))
    if b["rate_max"]:
        rows.append(("Max legacy rate", f"{b['rate_max']:g} Mbit/s"))
    if b["beacon_int"]:
        rows.append(("Beacon interval", f"{b['beacon_int']} TU"))
    if b["last_seen_ms"] is not None:
        rows.append(("Last seen", f"{b['last_seen_ms']} ms ago"))
    w = b["wps"]
    if w:
        rows.append(("WPS", w.get("Wi-Fi Protected Setup State", "present")))
        for k in ("Manufacturer", "Model", "Model Number", "Device name"):
            if w.get(k):
                rows.append(("WPS " + k.lower(), w[k]))
    for n, x in enumerate(net["bsses"]):
        tag = " (connected)" if x["associated"] else ""
        rows.append((f"AP {n + 1} BSSID", x["bssid"] + tag))
        rows.append((f"AP {n + 1} vendor", vendor(x["bssid"])))
        rows.append((f"AP {n + 1} signal", f"{x['signal']} dBm  ch{x['chan']}"))
    warn = cochannel_warning(net, other_ifs)
    if warn:
        rows.append(("Note", warn))
    return rows


def cochannel_warning(net, other_ifs):
    if net["band"] != "2G":
        return ""
    for i in other_ifs:
        if i.get("freq") and band_of(i["freq"]) == "2G" and i.get("channel"):
            if abs(i["channel"] - net["chan"]) < 5:
                same = "same channel" if i["channel"] == net["chan"] else "overlapping channel"
                return f"{i['name']} is on ch{i['channel']} ({same}) - expect slower speeds on both"
    return ""


# ------------------------------------------------------------------ profiles
PROFILE_FIELDS = ["802-11-wireless.ssid", "802-11-wireless.band", "connection.interface-name",
                  "802-11-wireless.mode", "connection.autoconnect"]


def con_fields(ident, fields, by="uuid"):
    """Same as wifimgr.con_fields but resolves `run` here (so tests can patch netcore.run)."""
    rc, out, _ = run(["nmcli", "-t", "-f", ",".join(fields), "con", "show", by, ident])
    res = {f: "" for f in fields}
    if rc != 0:
        return res
    for line in out.splitlines():
        key, sep, rest = line.partition(":")
        if sep and key in res:
            res[key] = ":".join(split_terse(rest))
    return res


def list_profiles():
    _, out, _ = run(["nmcli", "-t", "-f", "NAME,TYPE,UUID", "con", "show"])
    profs = []
    for line in out.splitlines():
        f = split_terse(line)
        if len(f) >= 3 and f[1] == "802-11-wireless":
            g = con_fields(f[2], PROFILE_FIELDS)
            profs.append({"name": f[0], "uuid": f[2], "ssid": g["802-11-wireless.ssid"],
                          "band": g["802-11-wireless.band"], "ifname": g["connection.interface-name"],
                          "mode": g["802-11-wireless.mode"] or "infrastructure",
                          "autoconnect": g["connection.autoconnect"]})
    return profs


def saved_ssids_for(ifc, profs=None):
    profs = list_profiles() if profs is None else profs
    return {p["ssid"] for p in profs if p["mode"] == "infrastructure" and p["ifname"] in ("", ifc)}


def profiles_for(ifc, profs=None):
    profs = list_profiles() if profs is None else profs
    res = [p for p in profs if p["mode"] == "infrastructure" and p["ifname"] in ("", ifc)]
    res.sort(key=lambda p: (p["ifname"] != ifc, p["ssid"].lower()))
    return res


def saved_psk(ssid, profs=None):
    profs = list_profiles() if profs is None else profs
    for p in profs:
        if p["ssid"] == ssid and p["mode"] == "infrastructure":
            rc, o, _ = run(["nmcli", "-s", "-g", "802-11-wireless-security.psk", "con", "show", "uuid", p["uuid"]])
            if rc == 0 and o.strip():
                return o.strip()
    return None


def _up(uuid_or_name, ifc, by="uuid"):
    rc, _, err = run(["nmcli", "--wait", "40", "con", "up", by, uuid_or_name, "ifname", ifc], timeout=50)
    log(f"rc={rc} nmcli con up {by} {uuid_or_name} ifname {ifc}")
    return rc == 0, (err.strip().splitlines()[-1] if err.strip() else "")


def connect(ifc, net, ask_password):
    """Connect radio `ifc` to `net` (from group_networks). Reuses saved profiles and
    PSKs saved on the other radio; asks only when nothing saved works."""
    km = wm.key_mgmt(net["security"])
    if nodup_enabled() and iface_bus(ifc) == "usb" and net.get("band") in ("2G", "5G"):
        dup = duplicate_of(ifc, net["ssid"], net["band"])
        if dup:
            if net.get("dual"):
                log(f"nodup: {dup[0]} already on {net['ssid']} {net['band']} - using {other_band(net['band'])}")
                net = dict(net, band=other_band(net["band"]))
            else:
                raise WifiError(f"{dup[0]} is already on {net['ssid']} ({net['band']}) - "
                                "duplicate guard is on (Settings)")
    profs = list_profiles()
    want_band = {"5G": "a", "2G": "bg"}.get(net["band"]) if net.get("dual") else None
    busy = active_elsewhere(ifc)
    cands = [p for p in profiles_for(ifc, profs) if p["ssid"] == net["ssid"]
             and p["name"] not in busy
             and (p["band"] == want_band if want_band else True)]
    for p in cands:
        ok, _ = _up(p["uuid"], ifc)
        if ok:
            apply_main()
            return f"{ifc}: connected to {net['ssid']} (saved)"
    pw = saved_psk(net["ssid"], profs) if km in ("wpa-psk", "sae") else None
    reused = pw is not None
    name = f"{ifc}-{net['ssid']}"
    for attempt in range(2):
        if km in ("wpa-psk", "sae") and not pw:
            pw = ask_password()
            if not pw:
                raise WifiError("cancelled")
        run(["nmcli", "con", "delete", "id", name])
        cmd = ["nmcli", "con", "add", "type", "wifi", "ifname", ifc, "con-name", name,
               "ssid", net["ssid"], "connection.autoconnect", "yes"]
        if want_band:
            cmd += ["802-11-wireless.band", want_band]
        if net.get("hidden"):
            cmd += ["802-11-wireless.hidden", "yes"]
        if km:
            cmd += ["wifi-sec.key-mgmt", km]
        if pw:
            cmd += ["wifi-sec.psk", pw]
        sh(cmd)
        ok, msg = _up(name, ifc, by="id")
        if ok:
            apply_main()
            return f"{ifc}: connected to {net['ssid']}" + (" (password reused)" if reused else "")
        run(["nmcli", "con", "delete", "id", name])
        if reused and attempt == 0:
            pw, reused = None, False  # saved password didn't work here: ask once
            continue
        raise WifiError(f"connect failed (wrong password?): {msg}")
    raise WifiError("connect failed")


def active_elsewhere(ifc):
    """Profile names active on the OTHER radios (NM allows one activation per profile)."""
    return {con for dev, (st, con) in nm_states().items() if dev != ifc and con and st.startswith("connect")}


def connect_profile(ifc, prof):
    if prof["name"] in active_elsewhere(ifc):
        # shared profile busy on the other radio: make this radio its own copy (password reused)
        net = {"ssid": prof["ssid"], "band": {"a": "5G", "bg": "2G"}.get(prof["band"], "?"),
               "security": "WPA2", "dual": bool(prof["band"])}
        _, km_out, _ = run(["nmcli", "-g", "802-11-wireless-security.key-mgmt", "con", "show", "uuid", prof["uuid"]])
        km = km_out.strip()
        net["security"] = {"sae": "WPA3", "owe": "OWE", "": "--"}.get(km, "WPA2")
        return connect(ifc, net, lambda: None)
    ok, msg = _up(prof["uuid"], ifc)
    if not ok:
        raise WifiError(f"connect failed: {msg}")
    apply_main()
    return f"{ifc}: connected to {prof['ssid']}"


def forget_targets(ifc, ssid, profs=None):
    profs = list_profiles() if profs is None else profs
    bound = [p for p in profs if p["ssid"] == ssid and p["ifname"] == ifc and p["mode"] == "infrastructure"]
    shared = [p for p in profs if p["ssid"] == ssid and p["ifname"] == "" and p["mode"] == "infrastructure"]
    return bound, shared


def forget(profiles):
    for p in profiles:
        sh(["nmcli", "con", "delete", "uuid", p["uuid"]])
    return f"forgot {len(profiles)} saved profile(s)"


def disconnect_all():
    done = []
    for name, (state, _) in nm_states().items():
        if name in wm.list_wifi_ifaces() and state.startswith("connect"):
            run(["nmcli", "dev", "disconnect", name], timeout=20)
            done.append(name)
    return "disconnected " + (", ".join(done) if done else "nothing")


# ------------------------------------------------------------------ main network
def get_main_pref():
    try:
        with open(MAIN_FILE) as f:
            v = f.read().strip()
            return v if v in ("internal", "usb") else "internal"
    except OSError:
        return "internal"


def set_main_pref(role):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(MAIN_FILE, "w") as f:
        f.write(role + "\n")
    log(f"main network preference -> {role}")


def main_iface(pref=None):
    pref = pref or get_main_pref()
    ifs = wm.list_wifi_ifaces()
    for n in ifs:
        if (iface_bus(n) == "usb") == (pref == "usb"):
            return n
    return None


def apply_main():
    """Main-network policy stored in NetworkManager itself, so it also holds while the app is closed:
    every saved profile bound to the main radio gets the low route metric, the other radio's get the
    high one. Active connections are re-applied immediately."""
    main = main_iface()
    states = nm_states()
    ifs = set(wm.list_wifi_ifaces())
    changed = []
    active_on = {con: dev for dev, (st, con) in states.items() if st.startswith("connected") and con}
    for p in list_profiles():
        ifc = p["ifname"] or active_on.get(p["name"], "")   # unbound profile: the radio using it now
        if ifc not in ifs or p["mode"] != "infrastructure" or p["name"] == HOTSPOT_PROFILE:
            continue
        metric = METRIC_MAIN if ifc == main else METRIC_OTHER
        dns = DNS_MAIN if ifc == main else DNS_OTHER
        _, cur, _ = run(["nmcli", "-g", "ipv4.route-metric", "con", "show", "uuid", p["uuid"]])
        if cur.strip() == str(metric):
            continue
        run(["nmcli", "con", "mod", "uuid", p["uuid"], "ipv4.route-metric", str(metric),
             "ipv6.route-metric", str(metric), "ipv4.dns-priority", str(dns), "ipv6.dns-priority", str(dns)])
        st, con = states.get(ifc, ("", ""))
        if st.startswith("connected") and con == p["name"]:
            run(["nmcli", "dev", "reapply", ifc], timeout=20)
            changed.append(ifc)
    return changed


ARP_SYSCTLS = {"arp_ignore": "1", "arp_announce": "2"}


def apply_arp_fix():
    """Two radios on one subnet: each answers ARP only for its own address (else SSH can freeze).
    Runtime only (/proc); the app applies it every time it starts."""
    done = []
    for scope in ("all", "default"):
        for k, v in ARP_SYSCTLS.items():
            path = f"/proc/sys/net/ipv4/conf/{scope}/{k}"
            try:
                with open(path) as f:
                    if f.read().strip() == v:
                        continue
                with open(path, "w") as f:
                    f.write(v)
                done.append(f"{scope}.{k}={v}")
            except OSError:
                pass
    if done:
        log("arp fix: " + ", ".join(done))
    return done


class Watcher:
    """Replaces the old NetworkManager dispatcher hook while the app is open: on each radio
    connecting -> duplicate guard + main-network metrics; on the dongle dropping -> built-in fallback."""

    def __init__(self):
        self.last = None

    def poll(self):
        cur = {n: st for n, (st, _c) in nm_states().items() if n in set(wm.list_wifi_ifaces())}
        prev, self.last = self.last, cur
        if prev is None:
            apply_main()
            return []
        events = []
        for n, st in cur.items():
            was = prev.get(n, "")
            if st.startswith("connected") and not was.startswith("connected"):
                events.append((n, "up"))
            elif was.startswith("connected") and not st.startswith("connected"):
                events.append((n, "down"))
        for n in prev:
            if n not in cur and prev[n].startswith("connected"):
                events.append((n, "down"))   # unplugged
        for n, ev in events:
            log(f"watcher: {n} {ev}")
            dispatch(n, ev)
        return events


def set_main(ifc):
    set_main_pref("usb" if iface_bus(ifc) == "usb" else "internal")
    apply_main()
    return f"{ifc} is now the main network for other apps"


# ------------------------------------------------------------------ duplicate-connection guard
NODUP_FILE = os.path.join(STATE_DIR, "nodup")


def nodup_enabled():
    try:
        with open(NODUP_FILE) as f:
            return f.read().strip() != "off"
    except OSError:
        return True


def set_nodup(on):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(NODUP_FILE, "w") as f:
        f.write(("on" if on else "off") + "\n")


def link_info(ifc):
    """{'ssid','freq','band','chan'} of the live association, or None."""
    _, out, _ = run(["iw", "dev", ifc, "link"])
    if "Connected to" not in out:
        return None
    d = wm.parse_iw_link(out)
    m = re.search(r"freq:\s*([\d.]+)", out)
    f = int(float(m.group(1))) if m else None
    return {"ssid": d["ssid"], "freq": f, "band": band_of(f) if f else "?", "chan": freq_to_chan(f)}


def duplicate_of(ifc, ssid, band):
    """Other radio already on this SSID + band -> (other_ifc, its link info), else None."""
    for other in wm.list_wifi_ifaces():
        if other == ifc:
            continue
        li = link_info(other)
        if li and li["ssid"] == ssid and li["band"] == band:
            return other, li
    return None


def other_band(band):
    return {"2G": "5G", "5G": "2G"}.get(band)


def _yield_dongle(dongle, a, other):
    """Move `dongle` off the SSID+band it shares with `other`: other band if possible, else disconnect."""
    con = nm_states().get(dongle, ("", ""))[1]
    alt = {"2G": "a", "5G": "bg"}.get(a["band"])
    log(f"nodup: {dongle} and {other} both on {a['ssid']} {a['band']} - moving {dongle}")
    if con and alt:
        old = con_fields(con, ["802-11-wireless.band"], by="id")["802-11-wireless.band"]
        run(["nmcli", "con", "mod", "id", con, "802-11-wireless.band", alt])
        ok, _ = _up(con, dongle, by="id")
        if ok:
            return f"{dongle} moved to {other_band(a['band'])} (same network as {other})"
        run(["nmcli", "con", "mod", "id", con, "802-11-wireless.band", old])
    run(["nmcli", "dev", "disconnect", dongle], timeout=20)   # stays off until you reconnect it
    return f"{dongle} disconnected - {other} is already on {a['ssid']} {a['band']}"


def enforce_nodup():
    """No two radios on the same SSID + band. USB dongles always give way: to the built-in radio
    first, then to each other (the later interface name yields)."""
    if not nodup_enabled():
        return None
    ifs = wm.list_wifi_ifaces()
    internal = internal_iface()
    links = {n: link_info(n) for n in ifs}
    order = ([internal] if internal else []) + sorted(n for n in ifs if n != internal and iface_bus(n) == "usb")
    for j, dongle in enumerate(order):
        if dongle == internal or not links.get(dongle):
            continue
        a = links[dongle]
        for other in order[:j]:
            b = links.get(other)
            if b and b["ssid"] == a["ssid"] and b["band"] == a["band"]:
                return _yield_dongle(dongle, a, other)
    return None


# ------------------------------------------------------------------ failover (app watcher; CLI: dispatch)
def dispatch(iface, action):
    if action in ("up", "dhcp4-change", "connectivity-change"):
        if action == "up":
            enforce_nodup()
        apply_main()
        return
    if action == "down":
        internal = internal_iface()
        if not internal or iface == internal:
            return
        state = nm_states().get(internal, ("", ""))[0]
        if state in ("disconnected", "unavailable"):
            log(f"failover: {iface} went down, bringing {internal} up on a saved network")
            run(["nmcli", "dev", "set", internal, "autoconnect", "yes"])
            run(["nmcli", "--wait", "0", "dev", "connect", internal])


# ------------------------------------------------------------------ hotspot
def hotspot_conf():
    try:
        with open(HOTSPOT_FILE) as f:
            c = json.load(f)
            if c.get("ssid") and len(c.get("psk", "")) >= 8:
                return c
    except (OSError, ValueError):
        pass
    alphabet = string.ascii_letters + string.digits
    c = {"ssid": f"{socket.gethostname()}-AP", "psk": "".join(secrets.choice(alphabet) for _ in range(12))}
    hotspot_save(c)
    return c


def hotspot_save(c):
    if not c.get("ssid") or not (8 <= len(c.get("psk", "")) <= 63):
        raise WifiError("SSID required and password must be 8-63 characters")
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(HOTSPOT_FILE, "w") as f:
        json.dump(c, f)
    os.chmod(HOTSPOT_FILE, 0o600)


def hotspot_clients(ifc):
    _, out, _ = run(["iw", "dev", ifc, "station", "dump"])
    return len(re.findall(r"^Station ", out, re.M))


def hotspot_start(ifc):
    c = hotspot_conf()
    run(["nmcli", "con", "delete", "id", HOTSPOT_PROFILE])
    sh(["nmcli", "con", "add", "type", "wifi", "ifname", ifc, "con-name", HOTSPOT_PROFILE,
        "autoconnect", "no", "ssid", c["ssid"], "802-11-wireless.mode", "ap", "802-11-wireless.band", "bg",
        "ipv4.method", "shared", "ipv6.method", "ignore",
        "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.proto", "rsn", "wifi-sec.pairwise", "ccmp",
        "wifi-sec.group", "ccmp", "wifi-sec.psk", c["psk"]])
    ok, msg = _up(HOTSPOT_PROFILE, ifc, by="id")
    if not ok:
        raise WifiError(f"hotspot failed: {msg}")
    return f"hotspot {c['ssid']} on {ifc}"


def hotspot_stop(ifc):
    run(["nmcli", "con", "down", "id", HOTSPOT_PROFILE], timeout=20)
    run(["nmcli", "dev", "set", ifc, "autoconnect", "yes"])
    run(["nmcli", "--wait", "0", "dev", "connect", ifc])
    return f"hotspot stopped; {ifc} rejoining saved network"


# ------------------------------------------------------------------ tests
def ping_test(ifc, host="1.1.1.1", count=5):
    rc, out, err = run(["ping", "-I", ifc, "-c", str(count), "-W", "2", "-q", host], timeout=count * 3 + 5)
    loss = re.search(r"([\d.]+)% packet loss", out)
    rtt = re.search(r"= ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+) ms", out)
    if not loss:
        raise WifiError((err or out).strip().splitlines()[-1] if (err or out).strip() else "ping failed")
    res = {"host": host, "loss": float(loss.group(1))}
    if rtt:
        res.update(min=float(rtt.group(1)), avg=float(rtt.group(2)), max=float(rtt.group(3)),
                   jitter=float(rtt.group(4)))
    return res


SO_BINDTODEVICE = 25


def speed_test(ifc, nbytes=10_000_000, timeout=20, progress=None):
    host = "speed.cloudflare.com"
    addr = socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)[0][4]
    raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    raw.setsockopt(socket.SOL_SOCKET, SO_BINDTODEVICE, (ifc + "\0").encode())
    raw.settimeout(10)
    raw.connect(addr)
    s = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
    s.sendall((f"GET /__down?bytes={nbytes} HTTP/1.1\r\nHost: {host}\r\nUser-Agent: dualradio\r\n"
               "Connection: close\r\n\r\n").encode())
    head, body, t0 = b"", 0, None
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            chunk = s.recv(65536)
            if not chunk:
                break
            if t0 is None:
                head += chunk
                if b"\r\n\r\n" in head:
                    t0 = time.time()
                    body = len(head.split(b"\r\n\r\n", 1)[1])
                continue
            body += len(chunk)
            if progress:
                progress(body, time.time() - t0)
    finally:
        s.close()
    if t0 is None:
        raise WifiError("speed test: no response")
    secs = max(0.001, time.time() - t0)
    return {"bytes": body, "secs": secs, "mbps": body * 8 / secs / 1e6}


# ------------------------------------------------------------------ CLI
def _cli(argv):
    if len(argv) >= 3 and argv[0] == "dispatch":
        dispatch(argv[1], argv[2])
    elif argv[:1] == ["main"]:
        if len(argv) > 1:
            set_main_pref(argv[1])
            apply_main()
        print(get_main_pref(), "->", main_iface())
    elif argv[:1] == ["apply-main"]:
        print("changed:", apply_main())
    else:
        print(__doc__)


if __name__ == "__main__":
    _cli(sys.argv[1:])
