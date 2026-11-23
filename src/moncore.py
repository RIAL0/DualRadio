#!/usr/bin/env python3
"""
moncore - monitor-mode AP survey + pcap capture for DualRadio (stdlib only).

Opens an AF_PACKET socket on a monitor-mode interface, parses radiotap +
802.11 management/data frames, hops channels for the selected band and keeps
a live AP table (SSID, BSSID, band, channel, RSSI, security, standard,
beacons, clients). Optionally writes every frame to a radiotap pcap
(linktype 127) for Wireshark.
"""
import errno
import json
import os
import pwd
import socket
import struct
import threading
import time

import wifimgr as wm

ETH_P_ALL = 0x0003
LINKTYPE_RADIOTAP = 127

# radiotap fields we walk: bit -> (align, size)
_RT_FIELDS = {0: (8, 8), 1: (1, 1), 2: (1, 1), 3: (2, 4), 4: (1, 2), 5: (1, 1)}

_AKM = {1: "802.1X", 2: "PSK", 3: "FT-802.1X", 4: "FT-PSK", 5: "802.1X-SHA256", 6: "PSK-SHA256",
        8: "SAE", 9: "FT-SAE", 12: "802.1X-SuiteB", 18: "OWE", 24: "SAE-EXT"}
_CIPHER = {1: "WEP40", 2: "TKIP", 4: "CCMP", 5: "WEP104", 8: "GCMP", 9: "GCMP-256", 10: "CCMP-256"}


def parse_radiotap(pkt):
    """Return (header_len, freq, signal_dbm, has_fcs) or None."""
    if len(pkt) < 8:
        return None
    _ver, _pad, rlen, present = struct.unpack_from("<BBHI", pkt, 0)
    if rlen > len(pkt) or rlen < 8:
        return None
    off, word = 8, present
    while word & 0x80000000 and off + 4 <= rlen:
        word = struct.unpack_from("<I", pkt, off)[0]
        off += 4
    freq = signal = None
    flags = 0
    for bit in range(6):
        if not present & (1 << bit):
            continue
        align, size = _RT_FIELDS[bit]
        off = (off + align - 1) & ~(align - 1)
        if off + size > rlen:
            break
        if bit == 1:
            flags = pkt[off]
        elif bit == 3:
            freq = struct.unpack_from("<H", pkt, off)[0]
        elif bit == 5:
            signal = struct.unpack_from("<b", pkt, off)[0]
        off += size
    return rlen, freq, signal, bool(flags & 0x10)


def _mac(b):
    return ":".join(f"{x:02x}" for x in b)


_WPS_ATTR = {0x1021: "manufacturer", 0x1023: "model", 0x1024: "model_number", 0x1011: "device_name"}


def parse_wps(data):
    res, i = {}, 4
    while i + 4 <= len(data):
        t, ln = struct.unpack_from(">HH", data, i)
        v = data[i + 4:i + 4 + ln]
        if t in _WPS_ATTR:
            res[_WPS_ATTR[t]] = v.decode("utf-8", "replace").strip("\x00 ")
        i += 4 + ln
    return res


def parse_ies(body):
    ies, i = {}, 0
    out = {"ssid": None, "chan": None, "rsn": None, "wpa": False, "ht": False, "vht": False, "he": False,
           "country": "", "stations": None, "utilisation": None, "wps": {}, "width": 20, "rate_max": None}
    while i + 2 <= len(body):
        eid, ln = body[i], body[i + 1]
        data = body[i + 2:i + 2 + ln]
        if len(data) < ln:
            break
        if eid == 0 and out["ssid"] is None:
            out["ssid"] = data.decode("utf-8", "replace")
        elif eid == 3 and ln >= 1:
            out["chan"] = data[0]
        elif eid == 61 and ln >= 1 and out["chan"] is None:
            out["chan"] = data[0]
            out["ht"] = True
            if ln >= 2 and data[1] & 0x03 in (1, 3):
                out["width"] = max(out["width"], 40)
        elif eid == 48:
            out["rsn"] = parse_rsn(data)
        elif eid == 221 and ln >= 4 and data[:4] == b"\x00\x50\xf2\x01":
            out["wpa"] = True
        elif eid == 221 and ln >= 4 and data[:4] == b"\x00\x50\xf2\x04":
            out["wps"] = parse_wps(data)
        elif eid == 7 and ln >= 2:
            out["country"] = data[:2].decode("ascii", "replace")
        elif eid == 11 and ln >= 3:
            out["stations"] = struct.unpack_from("<H", data, 0)[0]
            out["utilisation"] = round(data[2] * 100 / 255)
        elif eid in (1, 50):
            rates = [(r & 0x7F) / 2 for r in data if r & 0x7F not in (0x7F, 0x7E)]
            if rates:
                out["rate_max"] = max(rates + ([out["rate_max"]] if out["rate_max"] else []))
        elif eid == 61 and ln >= 2:
            out["ht"] = True
            if data[1] & 0x03 in (1, 3):
                out["width"] = max(out["width"], 40)
        elif eid == 192 and ln >= 1:
            out["vht"] = True
            if data[0] in (1, 3):
                out["width"] = max(out["width"], 80)
            elif data[0] == 2:
                out["width"] = max(out["width"], 160)
        elif eid == 45:
            out["ht"] = True
        elif eid == 191:
            out["vht"] = True
        elif eid == 255 and ln >= 1 and data[0] == 35:
            out["he"] = True
        ies[eid] = True
        i += 2 + ln
    return out


def parse_rsn(d):
    res = {"akm": [], "pairwise": [], "group": None, "pmf": "no"}
    try:
        off = 2
        if len(d) >= off + 4:
            res["group"] = _CIPHER.get(d[off + 3], f"#{d[off + 3]}")
            off += 4
        if len(d) >= off + 2:
            n = struct.unpack_from("<H", d, off)[0]
            off += 2
            for _ in range(n):
                res["pairwise"].append(_CIPHER.get(d[off + 3], f"#{d[off + 3]}"))
                off += 4
        if len(d) >= off + 2:
            n = struct.unpack_from("<H", d, off)[0]
            off += 2
            for _ in range(n):
                res["akm"].append(_AKM.get(d[off + 3], f"#{d[off + 3]}"))
                off += 4
        if len(d) >= off + 2:
            caps = struct.unpack_from("<H", d, off)[0]
            res["pmf"] = "required" if caps & 0x40 else "capable" if caps & 0x80 else "no"
    except (IndexError, struct.error):
        pass
    return res


def security_of(ies, cap):
    rsn = ies["rsn"]
    if rsn:
        akm = rsn["akm"]
        parts = []
        if any("PSK" in a for a in akm):
            if ies["wpa"]:
                parts.append("WPA")
            parts.append("WPA2")
        if any("SAE" in a for a in akm):
            parts.append("WPA3")
        if any("802.1X" in a for a in akm):
            parts.append("802.1X")
        if "OWE" in akm:
            parts.append("OWE")
        return "/".join(parts) or "WPA2"
    if ies["wpa"]:
        return "WPA"
    return "WEP" if cap & 0x0010 else "OPEN"


def _home_for_captures():
    try:
        pw = pwd.getpwuid(1000)
        return pw.pw_dir, pw.pw_uid, pw.pw_gid
    except KeyError:
        return "/root", 0, 0


def _freq_to_chan(f):
    if not f:
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


def vif_name(phy):
    """Safe, short, unique listen-only interface name (never derived from the real interface name,
    which could already be 15 chars, e.g. wlx00c0ca123456)."""
    return "drmon" + "".join(ch for ch in str(phy) if ch.isdigit())[:4]


def add_mon_vif(phy, name):
    """Listen-only monitor interface on the same radio (used while the main interface stays managed)."""
    rc, out, _ = wm.run(["iw", "dev", name, "info"], timeout=5)
    if rc == 0 and "type monitor" not in out:
        raise wm.WifiError(f"refusing to replace {name}: it is not a monitor interface")
    if rc == 0:
        wm.run(["iw", "dev", name, "del"], timeout=5)
    rc, _, err = wm.run(["iw", "phy", phy, "interface", "add", name, "type", "monitor"], timeout=5)
    if rc != 0:
        raise wm.WifiError(f"this driver can't add a listen interface ({err.strip() or rc})")
    wm.run(["nmcli", "dev", "set", name, "managed", "no"], timeout=5)
    rc, _, err = wm.run(["ip", "link", "set", name, "up"], timeout=5)
    if rc != 0:
        wm.run(["iw", "dev", name, "del"], timeout=5)
        raise wm.WifiError(f"can't bring {name} up: {err.strip() or rc}")
    wm.log(f"monitor vif {name} added on {phy}")


def del_mon_vif(name):
    rc, out, _ = wm.run(["iw", "dev", name, "info"], timeout=5)
    if rc == 0 and "type monitor" not in out:
        wm.log(f"not deleting {name}: not a monitor interface")
        return
    wm.run(["ip", "link", "set", name, "down"], timeout=5)
    wm.run(["iw", "dev", name, "del"], timeout=5)


# ------------------------------------------------------------------ per-driver capabilities
CAPS_FILE = "/etc/dualradio/caps.json"


def load_caps():
    try:
        with open(CAPS_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def caps_for(driver):
    return load_caps().get(driver)


def save_caps(driver, caps):
    c = load_caps()
    c[driver] = dict(caps, probed=time.strftime("%Y-%m-%d %H:%M"))
    try:
        os.makedirs(os.path.dirname(CAPS_FILE), exist_ok=True)
        with open(CAPS_FILE, "w") as f:
            json.dump(c, f, indent=1)
    except OSError:
        pass


def clear_caps():
    try:
        os.remove(CAPS_FILE)
    except OSError:
        pass


def count_frames(ifname, seconds, band=None, action=None):
    """Frames heard on `ifname` in `seconds` (optionally only one band), while `action` runs."""
    try:
        sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL))
        sock.bind((ifname, 0))
    except OSError:
        return 0
    sock.settimeout(0.3)
    if action:
        threading.Thread(target=action, daemon=True).start()
    n, end = 0, time.time() + seconds
    while time.time() < end:
        try:
            pkt = sock.recv(4096)
        except socket.timeout:
            continue
        except OSError:
            break
        rt = parse_radiotap(pkt)
        if rt and (band is None or (rt[1] and wm.band_of(rt[1]) == band)):
            n += 1
    sock.close()
    return n


def probe(ifc, phy, channels):
    """One-time test of what this dongle's driver can really do on 2.4 GHz:
    mon24 = true monitor mode hears 2.4 GHz;  sweep = listen-interface + scans works."""
    res = {"mon24": False, "sweep": False}
    ch24 = [(c, f) for c, f in channels if wm.band_of(f) == "2G"]
    if not ch24:
        return res
    test = [x for x in ch24 if x[0] in (1, 6, 11)] or ch24[:3]
    wm.run(["iw", "dev", ifc, "scan", "abort"], timeout=5)
    try:
        wm.mon_on(ifc)
        heard = 0
        for c, _ in test:
            wm.run(["iw", "dev", ifc, "set", "channel", str(c)], timeout=3)
            heard += count_frames(ifc, 1.5, band="2G")
        res["mon24"] = heard >= 3
    except wm.WifiError as e:
        wm.log(f"probe {ifc}: monitor mode failed: {e}")
    finally:
        try:
            wm.mon_off(ifc)
            wm.wait_nm_ready(ifc, 8)
        except wm.WifiError:
            pass
    if not res["mon24"]:
        name = vif_name(phy)
        try:
            add_mon_vif(phy, name)
            freqs = [str(f) for _, f in test]

            def sweep():
                rc, _, _ = wm.run(["iw", "dev", ifc, "scan", "freq"] + freqs + ["passive"], timeout=20)
                if rc != 0:
                    wm.run(["iw", "dev", ifc, "scan", "freq"] + freqs, timeout=20)
            res["sweep"] = count_frames(name, 6, band="2G", action=sweep) >= 3
        except wm.WifiError as e:
            wm.log(f"probe {ifc}: sweep not possible: {e}")
        finally:
            del_mon_vif(name)
    wm.log(f"probe {ifc}: {res}")
    return res


class Survey:
    """mode="monitor": the interface itself is in monitor mode and hops channels (preferred;
    probe() confirms it hears 2.4 GHz on each chip). mode="sweep": fallback for drivers that
    hear nothing on 2.4 GHz in plain monitor mode - the interface stays managed and runs
    passive scans while a listen-only drmon<N> interface captures."""

    def __init__(self, ifc, channels, band="ALL", dwell=0.3, mode="monitor", phy=None):
        self.ifc = ifc
        self.mode = mode
        self.phy = phy
        self.cap_if = ifc if mode == "monitor" else vif_name(phy)
        self.sweeps = 0
        self.note = ""
        self.all_channels = channels          # [(ch, freq), ...]
        self.band = band
        self.dwell = dwell
        self.locked = None
        self.cur_chan = None
        self.aps = {}
        self.frames = 0
        self.lock = threading.Lock()
        self.stop_ev = threading.Event()
        self.sock = None
        self.pcap = None
        self.pcap_path = None
        self.pcap_pkts = 0
        self.pcap_bytes = 0
        self.threads = []
        self.error = None
        self.band_frames = {"2G": 0, "5G": 0, "6G": 0}
        self.kicking = False
        self.started = time.time()
        self.kick_on_start = False

    # -------- lifecycle
    def start(self):
        if self.mode == "sweep":
            add_mon_vif(self.phy, self.cap_if)
        self.sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL))
        try:
            self.sock.bind((self.cap_if, 0))
        except OSError:
            if self.mode == "sweep":
                del_mon_vif(self.cap_if)
            raise
        self.sock.settimeout(0.5)
        for fn in (self._reader, self._hopper if self.mode == "monitor" else self._sweeper):
            t = threading.Thread(target=fn, daemon=True)
            t.start()
            self.threads.append(t)

    def stop(self):
        self.stop_ev.set()
        if self.mode == "sweep":
            wm.run(["iw", "dev", self.ifc, "scan", "abort"], timeout=5)  # unblock an in-flight sweep scan
        for t in self.threads:
            t.join(timeout=2)
        self.stop_capture()
        if self.sock:
            self.sock.close()
            self.sock = None
        if self.mode == "sweep":
            del_mon_vif(self.cap_if)

    # -------- controls
    def channels(self):
        if self.band == "ALL":
            return [c for c, _ in self.all_channels]
        return [c for c, f in self.all_channels if wm.band_of(f) == self.band]

    def set_band(self, band):
        self.band = band
        with self.lock:
            self.aps = {k: v for k, v in self.aps.items() if band == "ALL" or v["band"] == band}

    def lock_channel(self, ch):
        self.locked = ch
        if ch and self.mode == "monitor":
            try:
                wm.set_channel(self.ifc, ch)
                self.cur_chan = ch
            except wm.WifiError as e:
                self.error = str(e)

    def start_capture(self):
        home, uid, gid = _home_for_captures()
        d = os.path.join(home, "captures")
        os.makedirs(d, exist_ok=True)
        try:
            os.chown(d, uid, gid)
        except OSError:
            pass
        self.pcap_path = os.path.join(d, time.strftime(f"dualradio-{self.ifc}-{self.band}-%Y%m%d-%H%M%S.pcap"))
        f = open(self.pcap_path, "wb")
        f.write(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, LINKTYPE_RADIOTAP))
        self.pcap_pkts = self.pcap_bytes = 0
        self._owner = (uid, gid)
        self.pcap = f

    def stop_capture(self):
        f, self.pcap = self.pcap, None
        if f:
            f.close()
            try:
                os.chown(self.pcap_path, *self._owner)
            except OSError:
                pass
            return self.pcap_path, self.pcap_pkts, self.pcap_bytes
        return None

    def snapshot(self):
        with self.lock:
            aps = [dict(a, clients=len(a["clients"]), client_list=dict(a["clients"])) for a in self.aps.values()]
        named = [a for a in aps if a["ssid"]]
        for a in aps:
            if not a["ssid"]:
                sib = bss_sibling(a, named)
                a["sibling"] = sib["ssid"] if sib else ""
                a["hint"] = a["sibling"] or _vendor_hint(a["bssid"])
        aps.sort(key=lambda a: -(a["rssi"] if a["rssi"] is not None else -200))
        return aps

    # -------- workers
    def kick_24(self):
        """rtw88/8821cu workaround: managed -> legacy 1 Mbit/s on 2.4 GHz -> monitor restores 2.4 GHz RX."""
        self.kicking = True
        ifc = self.ifc
        steps = [["ip", "link", "set", ifc, "down"], ["iw", "dev", ifc, "set", "type", "managed"],
                 ["ip", "link", "set", ifc, "up"], ["iw", "dev", ifc, "set", "bitrates", "legacy-2.4", "1"],
                 ["ip", "link", "set", ifc, "down"], ["iw", "dev", ifc, "set", "type", "monitor"],
                 ["ip", "link", "set", ifc, "up"]]
        try:
            for c in steps:
                rc, _, err = wm.run(c, timeout=5)
                if rc != 0:
                    wm.log(f"kick_24 step failed: {' '.join(c)}: {err.strip()}")
            if self.locked:
                wm.run(["iw", "dev", ifc, "set", "channel", str(self.locked)], timeout=3)
        finally:
            self.kicking = False

    def _reinit_monitor(self):
        ifc = self.ifc
        for c in (["ip", "link", "set", ifc, "down"], ["iw", "dev", ifc, "set", "type", "monitor"],
                  ["ip", "link", "set", ifc, "up"]):
            wm.run(c, timeout=5)
        wm.log(f"survey watchdog: {ifc} re-initialised (no frames / channel changes refused)")

    def _hopper(self):
        idx = 0
        if self.kick_on_start:
            self.kick_24()
        resets, t0, last_frames = 0, time.time(), 0
        while not self.stop_ev.is_set():
            # watchdog: radio not hearing anything (e.g. switched while a scan was still running) -> re-init
            if time.time() - t0 > 5:
                if self.frames == last_frames and resets < 2:
                    resets += 1
                    self.note = f"no frames - resetting {self.ifc} ({resets}/2)"
                    self._reinit_monitor()
                    if self.locked:
                        wm.run(["iw", "dev", self.ifc, "set", "channel", str(self.locked)], timeout=3)
                elif self.frames == last_frames:
                    self.note = "no frames - press B to restart, or replug the dongle"
                else:
                    self.note = ""
                t0, last_frames = time.time(), self.frames
            if self.kicking:
                self.stop_ev.wait(0.3)
                continue
            if self.locked:
                self.stop_ev.wait(0.5)
                continue
            chans = self.channels()
            if not chans:
                self.stop_ev.wait(0.5)
                continue
            ch = chans[idx % len(chans)]
            idx += 1
            rc, _, _ = wm.run(["iw", "dev", self.ifc, "set", "channel", str(ch)], timeout=3)
            if rc == 0:
                self.cur_chan = ch
            self.stop_ev.wait(self.dwell)

    def _sweeper(self):
        """Passive scans on the managed interface sweep the radio; <ifc>mon hears everything on the way."""
        use_passive = True
        while not self.stop_ev.is_set():
            if self.locked:
                freqs = [f for c, f in self.all_channels if c == self.locked]
            else:
                freqs = [f for c, f in self.all_channels if self.band == "ALL" or wm.band_of(f) == self.band]
            if not freqs:
                self.stop_ev.wait(0.5)
                continue
            cmd = ["iw", "dev", self.ifc, "scan", "freq"] + [str(f) for f in freqs] + (["passive"] if use_passive else [])
            rc, _, err = wm.run(cmd, timeout=40)
            if rc == 0:
                self.sweeps += 1
                self.stop_ev.wait(0.05)
            else:
                busy = "busy" in err.lower() or rc == 240
                if use_passive and not busy:
                    use_passive = False          # older iw / driver without passive scan: plain scan instead
                    wm.log(f"sweep: passive scan refused ({err.strip()[:80]}), using active scans")
                    continue
                if not busy:
                    self.error = err.strip()
                self.stop_ev.wait(0.6)  # NetworkManager is scanning, or the radio is busy: try again shortly

    def _reader(self):
        while not self.stop_ev.is_set():
            try:
                pkt = self.sock.recv(4096)
            except socket.timeout:
                continue
            except OSError as e:
                if e.errno in (errno.ENETDOWN, errno.EAGAIN, errno.EINTR) or self.kicking:
                    self.stop_ev.wait(0.3)  # interface bouncing (kick / channel change) - keep listening
                    continue
                self.error = str(e)
                return
            self.frames += 1
            if self.pcap:
                ts = time.time()
                self.pcap.write(struct.pack("<IIII", int(ts), int((ts % 1) * 1e6), len(pkt), len(pkt)) + pkt)
                self.pcap_pkts += 1
                self.pcap_bytes += len(pkt) + 16
            try:
                self.handle(pkt)
            except (IndexError, struct.error):
                pass

    def handle(self, pkt):
        rt = parse_radiotap(pkt)
        if not rt:
            return
        rlen, freq, sig, fcs = rt
        if freq and self.mode == "sweep":
            self.cur_chan = _freq_to_chan(freq)
        if freq:
            b_ = wm.band_of(freq)
            if b_ in self.band_frames:
                self.band_frames[b_] += 1
        frame = pkt[rlen:len(pkt) - 4] if fcs else pkt[rlen:]
        if len(frame) < 24:
            return
        fc = frame[0] | (frame[1] << 8)
        ftype, sub = (fc >> 2) & 3, (fc >> 4) & 0xF
        tods, fromds = (fc >> 8) & 1, (fc >> 9) & 1
        now = time.time()
        if ftype == 0 and sub in (8, 5) and len(frame) >= 36:
            bssid = _mac(frame[16:22])
            cap = struct.unpack_from("<H", frame, 34)[0]
            ies = parse_ies(frame[36:])
            ch = ies["chan"]
            if ch is None and freq:
                from netcore import freq_to_chan
                ch = freq_to_chan(freq)
            f = freq or 0
            band = wm.band_of(f) if f else ("5G" if (ch or 0) > 14 else "2G")
            if self.band != "ALL" and band != self.band:
                return
            with self.lock:
                a = self.aps.get(bssid)
                if a is None:
                    a = self.aps[bssid] = {"bssid": bssid, "ssid": "", "hidden": False, "chan": ch,
                                           "freq": freq, "band": band, "rssi": sig, "rssi_max": sig,
                                           "rssi_min": sig, "beacons": 0, "security": "?", "std": "",
                                           "clients": {}, "first_seen": now, "last_seen": now, "rsn": None,
                                           "data_frames": 0, "data_bytes": 0, "country": "", "stations": None,
                                           "utilisation": None, "wps": {}, "width": 20, "rate_max": None,
                                           "beacon_int": None}
                ssid = ies["ssid"] or ""
                if ssid.strip("\x00"):
                    a["ssid"] = ssid          # a probe response names a hidden AP too
                    if sub == 8:
                        a["hidden"] = False
                elif sub == 8:
                    a["hidden"] = True        # cloaked beacon; keep any name already revealed
                a["chan"], a["band"] = ch or a["chan"], band
                if freq:
                    a["freq"] = freq
                if sig is not None:
                    a["rssi"] = sig
                    a["rssi_max"] = sig if a["rssi_max"] is None else max(a["rssi_max"], sig)
                    a["rssi_min"] = sig if a.get("rssi_min") is None else min(a["rssi_min"], sig)
                a["beacons"] += 1
                a["last_seen"] = now
                a["beacon_int"] = struct.unpack_from("<H", frame, 32)[0]
                for k in ("country", "stations", "utilisation", "rate_max"):
                    if ies[k] not in (None, ""):
                        a[k] = ies[k]
                if ies["wps"]:
                    a["wps"] = ies["wps"]
                a["width"] = max(a.get("width", 20), ies["width"])
                a["security"] = security_of(ies, cap)
                a["rsn"] = ies["rsn"]
                for k in ("he", "vht", "ht"):
                    a[k] = a.get(k, False) or ies[k]
                a["std"] = ("ax" if a["he"] else "ac" if a["vht"] else "n" if a["ht"]
                            else "a" if band == "5G" else "g")
        elif ftype == 0 and sub in (0, 2, 4):
            # Uncloak hidden networks: (re)association requests and directed probe requests
            # carry the real SSID and the AP's BSSID in addr3.
            off = {0: 28, 2: 34, 4: 24}[sub]
            if len(frame) <= off:
                return
            bssid = _mac(frame[16:22])
            if frame[16] & 1:                 # broadcast probe - no BSSID to attach it to
                return
            ssid = (parse_ies(frame[off:])["ssid"] or "").strip("\x00")
            if not ssid:
                return
            with self.lock:
                a = self.aps.get(bssid)
                if a is not None and not a["ssid"]:
                    a["ssid"] = ssid
                    wm.log(f"survey: hidden {bssid} revealed as {ssid!r}")
        elif ftype == 2:
            a1, a2, a3 = frame[4:10], frame[10:16], frame[16:22]
            if tods and not fromds:
                bssid, sta = a1, a2
            elif fromds and not tods:
                bssid, sta = a2, a1
            elif not tods and not fromds:
                bssid, sta = a3, a2
            else:
                return
            if sta[0] & 1:
                return
            b, s = _mac(bssid), _mac(sta)
            with self.lock:
                a = self.aps.get(b)
                if a is None:
                    return
                a["data_frames"] += 1
                a["data_bytes"] += len(frame)
                if s != b and (s in a["clients"] or len(a["clients"]) < 512):
                    c = a["clients"].setdefault(s, {"rssi": None, "frames": 0, "last": now})
                    c["frames"] += 1
                    c["last"] = now
                    if tods and sig is not None:  # signal is the client's own transmission
                        c["rssi"] = sig


def _fmt_bytes(n):
    return f"{n / 1e6:.1f} MB" if n >= 1e6 else f"{n / 1e3:.0f} kB" if n >= 1e3 else f"{n} B"


def _octets(mac):
    try:
        return [int(x, 16) for x in mac.split(":")]
    except ValueError:
        return None


def _vendor_hint(bssid):
    """Short maker name for a hidden network with no named sibling (e.g. an IoT device's own AP)."""
    from netcore import vendor
    v = vendor(bssid) or ""
    if not v or v == "?" or any(w in v.lower() for w in ("random", "locally", "unknown")):
        return ""
    for suf in (" LLC", " Ltd", " Inc", " Co.", " Corp", " Limited", " GmbH", ",", "."):
        v = v.split(suf)[0]
    return v.strip()[:18]


def bss_sibling(a, named):
    """A hidden BSS broadcast by the same physical radio as a named one (extra BSSIDs for mesh
    backhaul / IoT / guest networks): same channel, BSSID differing only in the locally-administered
    bits of the first octet and/or the low bits of the last octet."""
    o = _octets(a["bssid"])
    if not o:
        return None
    best = None
    for b in named:
        if b["chan"] != a["chan"]:
            continue
        p = _octets(b["bssid"])
        if not p or o[1:5] != p[1:5]:
            continue
        # first octet: same, or a locally-administered variant (vendors flip index bits anywhere in
        # this octet, e.g. be:12:34:56:78:96 -> ae:12:34:56:78:97); never the multicast bit
        first_ok = o[0] == p[0] or ((o[0] | p[0]) & 0x02 and not (o[0] ^ p[0]) & 0x01)
        last_diff = abs(o[5] - p[5])
        if (o[0] == p[0] and 0 < last_diff <= 15) or (first_ok and last_diff <= 15):
            score = last_diff + (0 if o[0] == p[0] else 1)
            if best is None or score < best[0]:
                best = (score, b)
    return best[1] if best else None


def ap_detail_lines(a):
    """What was actually observed over the air (vs. the scan detail, which is the driver's scan cache)."""
    from netcore import vendor
    age = max(1.0, time.time() - a["first_seen"])
    ssid = a["ssid"] or "(hidden - not yet revealed)"
    if a["ssid"] and a.get("hidden"):
        ssid += "  (hidden, revealed)"
    rows = [("SSID", ssid)]
    if not a["ssid"]:
        if a.get("sibling"):
            rows.append(("Same router as", f"{a['sibling']} (extra hidden network: mesh backhaul, IoT or guest)"))
        elif a.get("hint"):
            rows.append(("Likely", f"a {a['hint']} device's own network (no named network on this radio)"))
        rows.append(("Name", "not broadcast - shown once a device joins it (lock this channel with L and wait)"))
    rows += [("BSSID", a["bssid"]),
            ("Vendor", vendor(a["bssid"])),
            ("Band", f"{a['band']}  ch{a['chan']}" + (f"  {a['freq']} MHz" if a.get("freq") else "")),
            ("Width (beacon)", f"{a.get('width', 20)} MHz"),
            ("Standard", "802.11" + a["std"] if a["std"] else "?"),
            ("RSSI now", f"{a['rssi']} dBm"),
            ("RSSI best / worst", f"{a['rssi_max']} / {a.get('rssi_min')} dBm"),
            ("Security", a["security"])]
    r = a.get("rsn")
    if r:
        rows += [("AKM", " ".join(r["akm"]) or "?"),
                 ("Ciphers", (" ".join(r["pairwise"]) or "?") + f" / grp {r['group'] or '?'}"),
                 ("PMF", r["pmf"])]
    if a.get("country"):
        rows.append(("Country", a["country"]))
    if a.get("stations") is not None:
        rows.append(("Clients (AP says)", str(a["stations"])))
    if a.get("utilisation") is not None:
        rows.append(("Channel busy", f"{a['utilisation']}%"))
    if a.get("rate_max"):
        rows.append(("Max legacy rate", f"{a['rate_max']:g} Mbit/s"))
    if a.get("beacon_int"):
        rows.append(("Beacon interval", f"{a['beacon_int']} TU"))
    w = a.get("wps") or {}
    for k, lab in (("manufacturer", "WPS maker"), ("model", "WPS model"), ("model_number", "WPS model no."),
                   ("device_name", "WPS device")):
        if w.get(k):
            rows.append((lab, w[k]))
    rows += [("Beacons seen", f"{a['beacons']}  ({a['beacons'] / age:.1f}/s)"),
             ("Data frames", f"{a.get('data_frames', 0)}  ({_fmt_bytes(a.get('data_bytes', 0))})"),
             ("First seen", time.strftime("%H:%M:%S", time.localtime(a["first_seen"]))),
             ("Last seen", f"{time.time() - a['last_seen']:.1f} s ago"),
             ("Clients seen", str(a["clients"]))]
    cl = sorted((a.get("client_list") or {}).items(), key=lambda kv: -(kv[1]["frames"]))
    for n, (mac, c) in enumerate(cl[:40]):
        rs = f"{c['rssi']} dBm" if c["rssi"] is not None else "-"
        rows.append((f"Client {n + 1}", f"{mac}  {rs}  {c['frames']} fr"))
        rows.append(("", vendor(mac)))
    return rows
