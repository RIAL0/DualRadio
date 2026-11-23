#!/usr/bin/env python3
"""
wifimgr - Wi-Fi radio manager for the CardputerZero (works on any Pi OS / Debian
box that uses NetworkManager).

Manages every wireless interface (onboard brcmfmac + USB dongles such as the
TP-Link Archer T2UB Nano / rtw88_8821cu):
  * live status: mode, channel, SSID, signal, bitrate, IP, driver, bus
  * monitor mode on/off (with channel picker) - only offered when the PHY supports it
  * channel change while in monitor mode
  * scan + connect (saved profiles reused, WPA2/WPA3/open), disconnect
  * guards the interface that carries your SSH session

Usage:
  wifimgr                         interactive TUI
  wifimgr status                  print status table
  wifimgr mon on  [IFACE] [CH]    monitor mode (IFACE default: first USB radio)
  wifimgr mon off [IFACE]         back to managed (NetworkManager auto-joins known nets)
  wifimgr chan IFACE CH           set channel (monitor mode)
  wifimgr scan [IFACE]            list networks
  wifimgr connect IFACE SSID      connect (asks for password if needed)
  wifimgr disconnect IFACE

Requires: python3 (stdlib only), iw, iproute2, NetworkManager (nmcli), ss.
Re-executes itself with sudo when not root.
"""
import argparse
import curses
import getpass
import locale
import os
import re
import subprocess
import sys
import time

VERSION = "1.0.0"
LOG_FILE = "/var/log/wifimgr.log"
REFRESH_S = 3
os.environ.setdefault("ESCDELAY", "25")


class WifiError(Exception):
    pass


# --------------------------------------------------------------------------- shell
def _mask(cmd):
    out, hide = [], False
    for a in cmd:
        if hide:
            out.append("********")
            hide = False
            continue
        if a in ("password", "wifi-sec.psk", "802-11-wireless-security.psk"):
            hide = True
        out.append(a)
    return " ".join(out)


def log(msg):
    try:
        with open(LOG_FILE, "a") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except OSError:
        pass


def run(cmd, timeout=30):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", f"{cmd[0]}: command not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out: {cmd[0]}"


def sh(cmd, timeout=30):
    """Run a state-changing command; log it; raise on failure."""
    rc, out, err = run(cmd, timeout)
    log(f"rc={rc} {_mask(cmd)}" + (f" :: {err.strip()}" if rc else ""))
    if rc != 0:
        raise WifiError((err or out).strip().splitlines()[-1] if (err or out).strip() else f"{cmd[0]} failed ({rc})")
    return out


def split_terse(line):
    """Split nmcli -t output on unescaped ':' and unescape '\\:' / '\\\\'."""
    fields, cur, i = [], [], 0
    while i < len(line):
        c = line[i]
        if c == "\\" and i + 1 < len(line):
            cur.append(line[i + 1])
            i += 2
            continue
        if c == ":":
            fields.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    fields.append("".join(cur))
    return fields


# --------------------------------------------------------------------------- status
_mon_cache = {}


def list_wifi_ifaces():
    _, out, _ = run(["iw", "dev"])
    return sorted(m.group(1) for m in re.finditer(r"^\s*Interface\s+(\S+)", out, re.M))


def parse_iw_info(out):
    d = {"mode": "?", "mac": "?", "phy": "", "channel": None, "freq": None, "width": None}
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("type "):
            d["mode"] = s.split(None, 1)[1]
        elif s.startswith("addr "):
            d["mac"] = s.split()[1]
        elif s.startswith("wiphy "):
            d["phy"] = "phy" + s.split()[1]
        elif s.startswith("channel "):
            m = re.match(r"channel (\d+) \((\d+)(?:\.\d+)? MHz\)(?:, width: (\d+) MHz)?", s)
            if m:
                d["channel"], d["freq"] = int(m.group(1)), int(m.group(2))
                d["width"] = int(m.group(3)) if m.group(3) else None
    return d


def parse_iw_link(out):
    d = {"ssid": "", "signal": None, "txrate": ""}
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("SSID:"):
            d["ssid"] = s[5:].strip()
        elif s.startswith("signal:"):
            m = re.search(r"(-?\d+)", s)
            d["signal"] = int(m.group(1)) if m else None
        elif s.startswith("freq:"):
            m = re.search(r"(\d+)", s)
            d["freq"] = int(m.group(1)) if m else None
        elif s.startswith("tx bitrate:"):
            m = re.search(r"([\d.]+)\s*MBit/s", s)
            d["txrate"] = f"{float(m.group(1)):.0f}M" if m else ""
    return d


def parse_modes_monitor(out):
    in_block = False
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("Supported interface modes:"):
            in_block = True
            continue
        if in_block:
            if s.startswith("* "):
                if s[2:].strip() == "monitor":
                    return True
            else:
                in_block = False
    return False


def phy_supports_monitor(phy):
    if not phy:
        return False
    if phy not in _mon_cache:
        _mon_cache[phy] = parse_modes_monitor(run(["iw", "phy", phy, "info"])[1])
    return _mon_cache[phy]


def parse_channels(out):
    chans = []
    for line in out.splitlines():
        m = re.match(r"\s*\*\s+(\d+)(?:\.\d+)? MHz \[(\d+)\](.*)", line)
        if m and "disabled" not in m.group(3):
            chans.append((int(m.group(2)), int(m.group(1))))
    seen, res = set(), []
    for ch, f in chans:
        if ch not in seen:
            seen.add(ch)
            res.append((ch, f))
    return res


def phy_channels(phy):
    rc, out, _ = run(["iw", "phy", phy, "channels"])
    chans = parse_channels(out) if rc == 0 else []
    return chans or parse_channels(run(["iw", "phy", phy, "info"])[1])


def nm_states():
    _, out, _ = run(["nmcli", "-t", "-f", "DEVICE,STATE,CONNECTION", "dev"])
    res = {}
    for line in out.splitlines():
        f = split_terse(line)
        if len(f) >= 3:
            res[f[0]] = (f[1], f[2])
    return res


def ssh_ifaces():
    _, out, _ = run(["ss", "-Htn", "state", "established", "( sport = :22 )"])
    local = set()
    for line in out.splitlines():
        for tok in line.split():
            if tok.endswith(":22"):
                ip = tok.rsplit(":", 1)[0].strip("[]")
                local.add(ip[7:] if ip.startswith("::ffff:") else ip)
                break
    _, out, _ = run(["ip", "-o", "addr"])
    res = set()
    for line in out.splitlines():
        m = re.match(r"\d+:\s+(\S+)\s+inet6?\s+([^/\s]+)", line)
        if m and m.group(2) in local:
            res.add(m.group(1))
    return res


def iface_ip(name):
    _, out, _ = run(["ip", "-4", "-o", "addr", "show", "dev", name])
    m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", out)
    return m.group(1) if m else ""


def usb_id_of(devpath):
    """vendor:product of the USB device that owns this interface (sysfs parent of the interface dir)."""
    for d in (devpath, os.path.dirname(devpath)):
        try:
            with open(os.path.join(d, "idVendor")) as f:
                v = f.read().strip()
            with open(os.path.join(d, "idProduct")) as f:
                p = f.read().strip()
            return f"{v}:{p}"
        except OSError:
            continue
    return ""


SYSFS = "/sys"


def usb_device_dir(ifc):
    """sysfs dir of the USB *device* (not interface) behind a Wi-Fi netdev, or None."""
    d = os.path.realpath(f"{SYSFS}/class/net/{ifc}/device")
    for c in (d, os.path.dirname(d)):
        if os.path.exists(os.path.join(c, "authorized")) and os.path.exists(os.path.join(c, "idVendor")):
            return c
    return None


def _netdev_on(usbdev):
    for n in list_wifi_ifaces():
        dv = os.path.realpath(f"{SYSFS}/class/net/{n}/device")
        if dv.startswith(usbdev + "/") or dv == usbdev:
            return n
    return None


def usb_reset(ifc, timeout=20):
    """Software replug of a USB Wi-Fi dongle: de-authorise / re-authorise the device so the
    driver re-probes and reloads firmware. Returns the (possibly renamed) interface."""
    usbdev = usb_device_dir(ifc)
    if not usbdev:
        raise WifiError(f"{ifc} is not a USB device - can't reset it")
    auth = os.path.join(usbdev, "authorized")
    try:
        with open(auth, "w") as f:
            f.write("0")
        time.sleep(1.5)
        with open(auth, "w") as f:
            f.write("1")
        how = "authorized 0/1"
    except OSError as e:
        # fallback: unbind + bind the driver on the Wi-Fi interface
        intf = os.path.realpath(f"{SYSFS}/class/net/{ifc}/device")
        drv = os.path.realpath(os.path.join(intf, "driver"))
        name = os.path.basename(intf)
        try:
            with open(os.path.join(drv, "unbind"), "w") as f:
                f.write(name)
            time.sleep(1.5)
            with open(os.path.join(drv, "bind"), "w") as f:
                f.write(name)
            how = "driver unbind/bind"
        except OSError as e2:
            raise WifiError(f"USB reset of {ifc} failed: {e}; {e2}")
    end = time.time() + timeout
    while time.time() < end:
        n = _netdev_on(usbdev)
        if n and os.path.exists(f"{SYSFS}/class/net/{n}/phy80211"):
            log(f"usb_reset {ifc}: {how} -> {n} back after {timeout - (end - time.time()):.1f} s")
            return n
        time.sleep(0.5)
    raise WifiError(f"{ifc} did not come back after reset - replug the dongle")


def iface_info(name, nm, ssh):
    i = {"name": name}
    i.update(parse_iw_info(run(["iw", "dev", name, "info"])[1]))
    try:
        i["driver"] = os.path.basename(os.readlink(f"/sys/class/net/{name}/device/driver"))
    except OSError:
        i["driver"] = "?"
    dev = os.path.realpath(f"/sys/class/net/{name}/device")
    i["bus"] = "usb" if "/usb" in dev else ("sdio" if "mmc" in dev else "?")
    i["usb_id"] = usb_id_of(dev) if i["bus"] == "usb" else ""
    try:
        with open(f"/sys/class/net/{name}/flags") as f:
            i["up"] = bool(int(f.read().strip(), 16) & 1)
    except (OSError, ValueError):
        i["up"] = False
    i["mon_ok"] = phy_supports_monitor(i["phy"])
    i["nm_state"], i["connection"] = nm.get(name, ("?", ""))
    i.update({"ssid": "", "signal": None, "txrate": ""})
    if i["mode"] == "managed":
        i.update(parse_iw_link(run(["iw", "dev", name, "link"])[1]))
    i["ip"] = iface_ip(name)
    i["ssh"] = name in ssh
    return i


def collect():
    nm, ssh = nm_states(), ssh_ifaces()
    return [iface_info(n, nm, ssh) for n in list_wifi_ifaces()]


def default_iface(ifs):
    for i in ifs:
        if i["bus"] == "usb" and i["mon_ok"]:
            return i["name"]
    for i in ifs:
        if i["mon_ok"]:
            return i["name"]
    return ifs[0]["name"] if ifs else None


# --------------------------------------------------------------------------- actions
def wait_nm_ready(ifc, secs=8):
    end = time.time() + secs
    while time.time() < end:
        st = nm_states().get(ifc, ("?", ""))[0]
        if st not in ("unmanaged", "unavailable", "?"):
            return True
        time.sleep(0.5)
    return False


def set_channel(ifc, ch):
    rc, _, err = run(["iw", "dev", ifc, "set", "channel", str(ch), "HT20"])
    if rc != 0:
        rc, _, err = run(["iw", "dev", ifc, "set", "channel", str(ch)])
    log(f"rc={rc} iw dev {ifc} set channel {ch}")
    if rc != 0:
        raise WifiError(f"channel {ch}: {err.strip() or 'failed'}")


def mon_on(ifc, channel=None):
    run(["nmcli", "dev", "disconnect", ifc], timeout=15)
    sh(["nmcli", "dev", "set", ifc, "managed", "no"])
    sh(["ip", "link", "set", ifc, "down"])
    rc, _, err = run(["iw", "dev", ifc, "set", "type", "monitor"])
    log(f"rc={rc} iw dev {ifc} set type monitor")
    if rc != 0:
        run(["ip", "link", "set", ifc, "up"])
        run(["nmcli", "dev", "set", ifc, "managed", "yes"])
        raise WifiError(f"{ifc}: monitor not supported ({err.strip() or rc}) - rolled back")
    sh(["ip", "link", "set", ifc, "up"])
    if channel:
        set_channel(ifc, channel)
    return f"{ifc}: MONITOR" + (f" ch{channel}" if channel else "")


def mon_off(ifc, autoconnect=True):
    sh(["ip", "link", "set", ifc, "down"])
    sh(["iw", "dev", ifc, "set", "type", "managed"])
    sh(["ip", "link", "set", ifc, "up"])
    sh(["nmcli", "dev", "set", ifc, "managed", "yes"])
    if autoconnect:
        run(["nmcli", "dev", "set", ifc, "autoconnect", "yes"])
    return f"{ifc}: MANAGED" + (" (auto-joins known networks)" if autoconnect else "")


def band_of(freq):
    try:
        f = int(str(freq).split()[0].split(".")[0])
    except ValueError:
        return "?"
    return "6G" if f >= 5925 else ("5G" if f >= 4900 else "2G")


def parse_scan(out):
    best = {}
    for line in out.splitlines():
        f = split_terse(line)
        if len(f) < 6 or not f[1]:
            continue
        inuse, ssid, chan, freq, sig, sec = f[:6]
        try:
            sig = int(sig)
        except ValueError:
            sig = 0
        band = band_of(freq)
        e = {"inuse": inuse.strip() == "*", "ssid": ssid, "chan": chan, "band": band,
             "signal": sig, "security": sec.strip() or "--"}
        k = (ssid, band)
        if k not in best or e["inuse"] or (sig > best[k]["signal"] and not best[k]["inuse"]):
            best[k] = e
    nets = list(best.values())
    bands = {}
    for n in nets:
        bands.setdefault(n["ssid"], set()).add(n["band"])
    for n in nets:
        n["dual"] = len(bands[n["ssid"]]) > 1
    nets.sort(key=lambda n: (not n["inuse"], -n["signal"]))
    return nets


def scan(ifc):
    if nm_states().get(ifc, ("?",))[0] == "unmanaged":
        raise WifiError(f"{ifc} is unmanaged (monitor?) - switch to managed first")
    rc, out, err = run(["nmcli", "-t", "-f", "IN-USE,SSID,CHAN,FREQ,SIGNAL,SECURITY",
                        "dev", "wifi", "list", "ifname", ifc, "--rescan", "yes"], timeout=45)
    if rc != 0:
        raise WifiError(err.strip() or "scan failed")
    return parse_scan(out)


def key_mgmt(sec):
    s = sec.upper()
    if s in ("", "--"):
        return None
    if "802.1X" in s:
        raise WifiError("WPA-Enterprise network - use 'sudo nmtui' for this one")
    if "WEP" in s:
        raise WifiError("WEP network - use 'sudo nmtui' for this one")
    if s == "OWE":
        return "owe"
    if "WPA3" in s and "WPA2" not in s and "WPA1" not in s:
        return "sae"
    return "wpa-psk"


def con_fields(ident, fields, by="uuid"):
    """Read profile properties as a dict. Uses terse multiline 'key:value' output
    (nmcli -g with several fields prints one value per line, so it can't be split on ':')."""
    rc, out, _ = run(["nmcli", "-t", "-f", ",".join(fields), "con", "show", by, ident])
    res = {f: "" for f in fields}
    if rc != 0:
        return res
    for line in out.splitlines():
        key, sep, rest = line.partition(":")
        if sep and key in res:
            res[key] = ":".join(split_terse(rest))
    return res


def wifi_profiles():
    _, out, _ = run(["nmcli", "-t", "-f", "NAME,TYPE,UUID", "con", "show"])
    profs = []
    for line in out.splitlines():
        f = split_terse(line)
        if len(f) >= 3 and f[1] == "802-11-wireless":
            g = con_fields(f[2], ["802-11-wireless.ssid", "802-11-wireless.band", "connection.interface-name"])
            profs.append({"name": f[0], "ssid": g["802-11-wireless.ssid"], "band": g["802-11-wireless.band"],
                          "ifname": g["connection.interface-name"]})
    return profs


def connect(ifc, net, ask_password):
    """net: dict from parse_scan. ask_password: callable -> str|None."""
    km = key_mgmt(net["security"])
    want = {"5G": "a", "2G": "bg"}.get(net["band"]) if net.get("dual") else None
    cands = [p for p in wifi_profiles()
             if p["ssid"] == net["ssid"] and p["ifname"] in ("", ifc)
             and (not p["band"] or not want or p["band"] == want)]
    for p in cands:
        rc, _, err = run(["nmcli", "--wait", "40", "con", "up", "id", p["name"], "ifname", ifc], timeout=50)
        log(f"rc={rc} nmcli con up id {p['name']} ifname {ifc}")
        if rc == 0:
            return f"{ifc}: connected to {net['ssid']} (profile {p['name']})"
    pw = None
    if km in ("wpa-psk", "sae"):
        pw = ask_password()
        if not pw:
            raise WifiError("cancelled")
    if cands:
        name = cands[0]["name"]
        if pw:
            sh(["nmcli", "con", "mod", "id", name, "wifi-sec.key-mgmt", km, "wifi-sec.psk", pw])
        rc, _, err = run(["nmcli", "--wait", "40", "con", "up", "id", name, "ifname", ifc], timeout=50)
        if rc != 0:
            raise WifiError(f"connect failed: {err.strip().splitlines()[-1] if err.strip() else rc}")
        return f"{ifc}: connected to {net['ssid']} (profile {name}, password updated)"
    existing = {p["name"] for p in wifi_profiles()}
    base = net["ssid"] + ({"a": "-5g", "bg": "-2g"}[want] if want else "")
    name, n = base, 2
    while name in existing:
        name, n = f"{base}-{n}", n + 1
    cmd = ["nmcli", "con", "add", "type", "wifi", "con-name", name, "ssid", net["ssid"]]
    if want:
        cmd += ["802-11-wireless.band", want]
    if km:
        cmd += ["wifi-sec.key-mgmt", km]
    if pw:
        cmd += ["wifi-sec.psk", pw]
    sh(cmd)
    rc, _, err = run(["nmcli", "--wait", "40", "con", "up", "id", name, "ifname", ifc], timeout=50)
    log(f"rc={rc} nmcli con up id {name} ifname {ifc}")
    if rc != 0:
        run(["nmcli", "con", "delete", "id", name])
        msg = err.strip().splitlines()[-1] if err.strip() else str(rc)
        raise WifiError(f"connect failed (wrong password?): {msg}")
    return f"{ifc}: connected to {net['ssid']} (new profile {name})"


def disconnect(ifc):
    sh(["nmcli", "dev", "disconnect", ifc])
    return f"{ifc}: disconnected"


# --------------------------------------------------------------------------- TUI
C_OK, C_MON, C_BAD, C_DIM, C_SEL = 1, 2, 3, 4, 5


def put(scr, y, x, s, attr=0):
    h, w = scr.getmaxyx()
    if y < 0 or y >= h or x >= w:
        return
    s = s[: max(0, w - x - (1 if y == h - 1 else 0))]
    try:
        scr.addstr(y, x, s, attr)
    except curses.error:
        pass


def status_line(scr, msg, attr=0):
    h, _ = scr.getmaxyx()
    scr.move(h - 1, 0)
    scr.clrtoeol()
    put(scr, h - 1, 0, msg, attr)
    scr.refresh()


def confirm(scr, msg):
    scr.timeout(-1)
    status_line(scr, msg + " [y/N] ", curses.A_BOLD)
    return scr.getch() in (ord("y"), ord("Y"))


def prompt(scr, label, secret=False):
    scr.timeout(-1)
    buf, show = "", False
    try:
        curses.curs_set(1)
    except curses.error:
        pass
    h, _ = scr.getmaxyx()
    scr.move(h - 2, 0)
    scr.clrtoeol()
    put(scr, h - 2, 0, ("Tab show/hide  " if secret else "") + "Enter ok  Esc cancel", curses.A_DIM)
    try:
        while True:
            disp = buf if (show or not secret) else "*" * len(buf)
            status_line(scr, label + disp)
            k = scr.get_wch()
            if k in ("\n", "\r", curses.KEY_ENTER):
                return buf
            if k == "\x1b":
                return None
            if k in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                buf = buf[:-1]
            elif k == "\t" and secret:
                show = not show
            elif k == "\x15":
                buf = ""
            elif isinstance(k, str) and k.isprintable():
                buf += k
    finally:
        try:
            curses.curs_set(0)
        except curses.error:
            pass


def picker(scr, title, labels, idx=0):
    if not labels:
        return None
    scr.timeout(-1)
    buf, last = "", 0.0
    idx = max(0, min(idx, len(labels) - 1))
    while True:
        h, w = scr.getmaxyx()
        scr.erase()
        put(scr, 0, 0, title, curses.A_BOLD)
        rows = max(1, h - 2)
        top = max(0, min(idx - rows // 2, len(labels) - rows))
        for r in range(rows):
            j = top + r
            if j >= len(labels):
                break
            put(scr, 1 + r, 0, labels[j].ljust(w), curses.color_pair(C_SEL) | curses.A_BOLD if j == idx else 0)
        put(scr, h - 1, 0, "j/k move  Enter ok  Esc back" + (f"  [{buf}]" if buf else ""), curses.A_DIM)
        scr.refresh()
        k = scr.getch()
        if k in (curses.KEY_UP, ord("k")):
            idx = max(0, idx - 1)
        elif k in (curses.KEY_DOWN, ord("j")):
            idx = min(len(labels) - 1, idx + 1)
        elif k == curses.KEY_PPAGE:
            idx = max(0, idx - rows)
        elif k == curses.KEY_NPAGE:
            idx = min(len(labels) - 1, idx + rows)
        elif k in (10, 13, curses.KEY_ENTER):
            return idx
        elif k in (27, ord("q")):
            return None
        elif ord("0") <= k <= ord("9"):
            now = time.time()
            buf = (buf if now - last < 1.5 else "") + chr(k)
            last = now
            for j, lab in enumerate(labels):
                if lab.strip().startswith(buf):
                    idx = j
                    break


HELP = [
    "wifimgr " + VERSION,
    "",
    "j/k, arrows, Tab, 1-9  select radio",
    "m   monitor on/off",
    "c   channel (monitor mode)",
    "s   scan + connect (managed)",
    "d   disconnect",
    "r   refresh now",
    "q   quit",
    "",
    "[SSH] = carries your SSH session.",
    "Changing it may drop you - you'll",
    "be asked to confirm.",
    "mon:n = driver can't do monitor.",
    "Log: " + LOG_FILE,
    "",
    "Any key to return",
]


def draw(scr, ifs, sel, msg, msg_attr):
    scr.erase()
    h, w = scr.getmaxyx()
    put(scr, 0, 0, f"wifimgr  {os.uname().nodename}".ljust(w - 7) + "?=help", curses.A_BOLD)
    y = 1
    if not ifs:
        put(scr, 2, 0, "No wireless interfaces found.", curses.color_pair(C_BAD))
    for n, i in enumerate(ifs):
        is_sel = n == sel
        mode = i["mode"]
        mcol = C_MON if mode == "monitor" else (C_OK if i["nm_state"] == "connected" else C_DIM)
        head = f"{'>' if is_sel else ' '}{n + 1} {i['name']} {i['driver']} {i['bus'].upper()}"
        head += f" mon:{'y' if i['mon_ok'] else 'n'}"
        if i["ssh"]:
            head += " [SSH]"
        if not i["up"]:
            head += " DOWN"
        put(scr, y, 0, head.ljust(w), (curses.color_pair(C_SEL) | curses.A_BOLD) if is_sel else curses.A_BOLD)
        ch = f" ch{i['channel']} {i['freq']}MHz" + (f" {i['width']}MHz" if i["width"] else "") if i["channel"] else ""
        put(scr, y + 1, 2, f"{mode.upper()}{ch}", curses.color_pair(mcol) | curses.A_BOLD)
        if mode == "monitor":
            l3 = "capturing - NM unmanaged"
        elif i["ssid"]:
            sig = f" {i['signal']}dBm" if i["signal"] is not None else ""
            l3 = f"{i['ssid']}{sig} {i['txrate']} {i['ip']}".rstrip()
        else:
            l3 = f"NM: {i['nm_state']}" + (f"  {i['ip']}" if i["ip"] else "")
        put(scr, y + 2, 2, l3)
        y += 3
    put(scr, h - 3, 0, "-" * (w - 1), curses.A_DIM)
    put(scr, h - 2, 0, "m mon  c chan  s scan  d disc  q quit", curses.A_DIM)
    put(scr, h - 1, 0, msg, msg_attr)
    scr.refresh()


def tui(scr):
    curses.curs_set(0)
    if curses.has_colors():
        curses.start_color()
        try:
            curses.use_default_colors()
            bg = -1
        except curses.error:
            bg = curses.COLOR_BLACK
        curses.init_pair(C_OK, curses.COLOR_GREEN, bg)
        curses.init_pair(C_MON, curses.COLOR_YELLOW, bg)
        curses.init_pair(C_BAD, curses.COLOR_RED, bg)
        curses.init_pair(C_DIM, curses.COLOR_CYAN, bg)
        curses.init_pair(C_SEL, curses.COLOR_BLACK, curses.COLOR_CYAN)
    sel, msg, mattr = 0, "Ready.", 0
    ifs, last = [], 0.0

    def busy(text):
        status_line(scr, "... " + text, curses.color_pair(C_MON) | curses.A_BOLD)

    while True:
        if time.time() - last >= REFRESH_S or not ifs:
            ifs, last = collect(), time.time()
            sel = min(sel, max(0, len(ifs) - 1))
        draw(scr, ifs, sel, msg, mattr)
        scr.timeout(REFRESH_S * 1000)
        k = scr.getch()
        if k == -1:
            continue
        if k in (ord("q"), ord("Q")):
            return
        if k in (curses.KEY_UP, ord("k")):
            sel = max(0, sel - 1)
            continue
        if k in (curses.KEY_DOWN, ord("j"), ord("\t")):
            sel = (sel + 1) % len(ifs) if (k == ord("\t") and ifs) else min(len(ifs) - 1, sel + 1)
            continue
        if ord("1") <= k <= ord("9") and k - ord("1") < len(ifs):
            sel = k - ord("1")
            continue
        if k == ord("r"):
            last = 0
            msg, mattr = "Refreshed.", 0
            continue
        if k == ord("?"):
            scr.erase()
            for n, line in enumerate(HELP):
                put(scr, n, 0, line)
            scr.refresh()
            scr.timeout(-1)
            scr.getch()
            continue
        if not ifs:
            continue
        i = ifs[sel]
        ifc = i["name"]
        try:
            if k == ord("m"):
                if i["mode"] == "monitor":
                    busy(f"{ifc} -> managed")
                    msg = mon_off(ifc)
                else:
                    if not i["mon_ok"]:
                        raise WifiError(f"{ifc} ({i['driver']}) doesn't support monitor mode")
                    if i["ssh"] and not confirm(scr, f"{ifc} carries your SSH session - it WILL drop. Continue?"):
                        raise WifiError("cancelled")
                    chans = phy_channels(i["phy"])
                    if not chans:
                        raise WifiError("no usable channels (check 'iw reg get')")
                    labels = [f"{c:>3}  {f} MHz  {band_of(f)}" for c, f in chans]
                    start = next((n for n, (c, _) in enumerate(chans) if c == (i["channel"] or 36)), 0)
                    p = picker(scr, f"{ifc}: monitor channel", labels, start)
                    if p is None:
                        raise WifiError("cancelled")
                    busy(f"{ifc} -> monitor ch{chans[p][0]}")
                    msg = mon_on(ifc, chans[p][0])
                mattr = curses.color_pair(C_OK)
            elif k == ord("c"):
                if i["mode"] != "monitor":
                    raise WifiError("channel change is for monitor mode (managed follows the AP)")
                chans = phy_channels(i["phy"])
                labels = [f"{c:>3}  {f} MHz  {band_of(f)}" for c, f in chans]
                start = next((n for n, (c, _) in enumerate(chans) if c == i["channel"]), 0)
                p = picker(scr, f"{ifc}: channel", labels, start)
                if p is None:
                    raise WifiError("cancelled")
                set_channel(ifc, chans[p][0])
                msg, mattr = f"{ifc}: ch{chans[p][0]}", curses.color_pair(C_OK)
            elif k == ord("s"):
                if i["mode"] == "monitor":
                    if not confirm(scr, f"{ifc} is in monitor mode. Switch to managed?"):
                        raise WifiError("cancelled")
                    busy(f"{ifc} -> managed")
                    mon_off(ifc)
                    wait_nm_ready(ifc)
                busy(f"scanning on {ifc} (5-10 s)")
                nets = scan(ifc)
                if not nets:
                    raise WifiError("no networks found")
                labels = [f"{'*' if n['inuse'] else ' '}{n['signal']:>3}% {n['band']} ch{n['chan']:<3} "
                          f"{n['ssid'][:22]:<22} {n['security']}" for n in nets]
                p = picker(scr, f"{ifc}: networks (* = connected)", labels)
                if p is None:
                    raise WifiError("cancelled")
                net = nets[p]
                if i["ssh"] and not confirm(scr, f"{ifc} carries your SSH session - may drop. Continue?"):
                    raise WifiError("cancelled")
                draw(scr, ifs, sel, "", 0)
                busy(f"connecting {ifc} to {net['ssid']}")
                msg = connect(ifc, net, lambda: prompt(scr, f"Password for {net['ssid']}: ", secret=True))
                mattr = curses.color_pair(C_OK)
            elif k == ord("d"):
                if i["mode"] == "monitor":
                    raise WifiError("in monitor mode - nothing to disconnect")
                if i["ssh"] and not confirm(scr, f"{ifc} carries your SSH session - it WILL drop. Continue?"):
                    raise WifiError("cancelled")
                busy(f"disconnecting {ifc}")
                msg, mattr = disconnect(ifc), curses.color_pair(C_OK)
            else:
                continue
        except WifiError as e:
            msg, mattr = f"! {e}", curses.color_pair(C_BAD) | curses.A_BOLD
        last = 0


# --------------------------------------------------------------------------- CLI
def print_status(ifs):
    if not ifs:
        print("No wireless interfaces found.")
        return
    fmt = "{:<7} {:<14} {:<4} {:<4} {:<8} {:<16} {:<16} {:<6} {:<15} {}"
    print(fmt.format("IFACE", "DRIVER", "BUS", "MON", "MODE", "CHANNEL", "SSID/STATE", "SIG", "IP", "FLAGS"))
    for i in ifs:
        ch = f"{i['channel']} ({i['freq']}MHz)" if i["channel"] else "-"
        st = i["ssid"] or ("-" if i["mode"] == "monitor" else i["nm_state"])
        sig = f"{i['signal']}dBm" if i["signal"] is not None else "-"
        flags = " ".join(x for x in ("SSH" if i["ssh"] else "", "" if i["up"] else "DOWN") if x)
        print(fmt.format(i["name"], i["driver"][:14], i["bus"], "y" if i["mon_ok"] else "n",
                         i["mode"], ch, st[:16], sig, i["ip"] or "-", flags))


def pick_iface(ifs, name):
    if name:
        if name not in [i["name"] for i in ifs]:
            sys.exit(f"wifimgr: no wireless interface '{name}'")
        return next(i for i in ifs if i["name"] == name)
    d = default_iface(ifs)
    if not d:
        sys.exit("wifimgr: no wireless interfaces found")
    return next(i for i in ifs if i["name"] == d)


def cli(argv):
    ap = argparse.ArgumentParser(prog="wifimgr", description="Wi-Fi radio manager (TUI + CLI)")
    ap.add_argument("--version", action="version", version=VERSION)
    sp = ap.add_subparsers(dest="cmd")
    sp.add_parser("status")
    m = sp.add_parser("mon")
    m.add_argument("state", choices=["on", "off"])
    m.add_argument("iface", nargs="?")
    m.add_argument("channel", nargs="?", type=int)
    m.add_argument("--no-autoconnect", action="store_true", help="(off) don't auto-join known networks")
    c = sp.add_parser("chan")
    c.add_argument("iface")
    c.add_argument("channel", type=int)
    s = sp.add_parser("scan")
    s.add_argument("iface", nargs="?")
    cn = sp.add_parser("connect")
    cn.add_argument("iface")
    cn.add_argument("ssid")
    d = sp.add_parser("disconnect")
    d.add_argument("iface")
    a = ap.parse_args(argv)

    if a.cmd is None:
        locale.setlocale(locale.LC_ALL, "")
        try:
            curses.wrapper(tui)
        except KeyboardInterrupt:
            pass
        return 0
    ifs = collect()
    try:
        if a.cmd == "status":
            print_status(ifs)
        elif a.cmd == "mon":
            i = pick_iface(ifs, a.iface)
            if a.state == "on":
                if not i["mon_ok"]:
                    sys.exit(f"wifimgr: {i['name']} ({i['driver']}) doesn't support monitor mode")
                print(mon_on(i["name"], a.channel))
            else:
                print(mon_off(i["name"], not a.no_autoconnect))
        elif a.cmd == "chan":
            set_channel(pick_iface(ifs, a.iface)["name"], a.channel)
            print(f"{a.iface}: ch{a.channel}")
        elif a.cmd == "scan":
            for n in scan(pick_iface(ifs, a.iface)["name"]):
                print(f"{'*' if n['inuse'] else ' '} {n['signal']:>3}% {n['band']} ch{n['chan']:<3} "
                      f"{n['ssid']:<32} {n['security']}")
        elif a.cmd == "connect":
            i = pick_iface(ifs, a.iface)
            nets = [n for n in scan(i["name"]) if n["ssid"] == a.ssid]
            if not nets:
                sys.exit(f"wifimgr: '{a.ssid}' not found on {i['name']}")
            print(connect(i["name"], nets[0], lambda: getpass.getpass(f"Password for {a.ssid}: ")))
        elif a.cmd == "disconnect":
            print(disconnect(pick_iface(ifs, a.iface)["name"]))
    except WifiError as e:
        sys.exit(f"wifimgr: {e}")
    return 0


def main():
    if os.geteuid() != 0:
        os.execvp("sudo", ["sudo", sys.executable, os.path.abspath(sys.argv[0])] + sys.argv[1:])
    sys.exit(cli(sys.argv[1:]))


if __name__ == "__main__":
    main()
