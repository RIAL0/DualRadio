#!/usr/bin/env python3
"""
btcore - Bluetooth engine for DualRadio (BlueZ over D-Bus).

Per-adapter (built-in hci0 / USB dongle hci1) discovery of Classic + BLE
devices, rich device decoding (class, appearance, services, company,
iBeacon / Apple / Microsoft adverts, distance estimate), pair / connect /
disconnect / trust / remove with an on-screen pairing agent, and live RSSI.

Requires: bluez, python3-dbus, python3-gi (GLib main loop).
"""
import glob
import os
import queue
import subprocess
import threading
import time

try:
    import dbus
    import dbus.mainloop.glib
    import dbus.service
    from gi.repository import GLib
    AVAILABLE, IMPORT_ERROR = True, ""
except ImportError as _e:  # UI shows an install hint
    AVAILABLE, IMPORT_ERROR = False, str(_e)

BLUEZ = "org.bluez"
ADAPTER = "org.bluez.Adapter1"
DEVICE = "org.bluez.Device1"
PROPS = "org.freedesktop.DBus.Properties"
OBJMGR = "org.freedesktop.DBus.ObjectManager"
AGENT_MGR = "org.bluez.AgentManager1"
AGENT_PATH = "/com/dualradio/agent"


class BtError(Exception):
    pass


# ------------------------------------------------------------------ decoders
MAJOR_CLASS = {0: "Misc", 1: "Computer", 2: "Phone", 3: "Network AP", 4: "Audio/Video", 5: "Peripheral",
               6: "Imaging", 7: "Wearable", 8: "Toy", 9: "Health", 31: "Uncategorised"}
MINOR_CLASS = {
    1: {1: "Desktop", 2: "Server", 3: "Laptop", 4: "Handheld PC", 5: "Palm PC", 6: "Wearable PC", 7: "Tablet"},
    2: {1: "Cellular", 2: "Cordless", 3: "Smartphone", 4: "Modem", 5: "ISDN"},
    4: {1: "Headset", 2: "Hands-free", 4: "Microphone", 5: "Loudspeaker", 6: "Headphones", 7: "Portable audio",
        8: "Car audio", 9: "Set-top box", 10: "HiFi audio", 11: "VCR", 12: "Video camera", 13: "Camcorder",
        14: "Video monitor", 15: "Display + speaker", 16: "Video conferencing", 18: "Gaming/toy"},
    7: {1: "Wristwatch", 2: "Pager", 3: "Jacket", 4: "Helmet", 5: "Glasses"},
}
SERVICE_CLASS = {13: "Limited discoverable", 16: "Positioning", 17: "Networking", 18: "Rendering",
                 19: "Capturing", 20: "Object transfer", 21: "Audio", 22: "Telephony", 23: "Information"}
APPEARANCE = {1: "Phone", 2: "Computer", 3: "Watch", 4: "Clock", 5: "Display", 6: "Remote control",
              7: "Eyeglasses", 8: "Tag", 9: "Keyring", 10: "Media player", 11: "Barcode scanner",
              12: "Thermometer", 13: "Heart-rate sensor", 14: "Blood pressure", 15: "HID (keyboard/mouse)",
              16: "Glucose meter", 17: "Running sensor", 18: "Cycling sensor", 19: "Control device",
              20: "Network device", 21: "Sensor", 22: "Light fixture", 23: "Fan", 24: "HVAC", 25: "Air conditioner",
              26: "Humidifier", 27: "Heater", 28: "Access control", 29: "Motorised device", 30: "Power device",
              31: "Light source", 32: "Window covering", 33: "Audio sink", 34: "Audio source", 35: "Motorised vehicle",
              36: "Domestic appliance", 37: "Wearable audio", 38: "Aircraft", 39: "AV equipment",
              40: "Display equipment", 41: "Hearing aid", 42: "Gaming", 43: "Signage", 49: "Pulse oximeter",
              50: "Weight scale", 51: "Personal mobility", 52: "Glucose monitor", 53: "Insulin pump",
              54: "Medication delivery", 55: "Spirometer", 81: "Outdoor sports"}
SERVICE_UUID = {0x1101: "Serial Port", 0x1103: "Dial-up Net", 0x1105: "OBEX Push", 0x1106: "OBEX FTP",
                0x1108: "Headset", 0x110A: "Audio Source (A2DP)", 0x110B: "Audio Sink (A2DP)",
                0x110C: "AVRCP Target", 0x110E: "AVRCP Controller", 0x110F: "AVRCP", 0x1112: "Headset AG",
                0x1115: "PANU", 0x1116: "NAP", 0x1117: "GN", 0x111E: "Hands-free", 0x111F: "Hands-free AG",
                0x1124: "HID", 0x112D: "SIM Access", 0x112F: "Phonebook (PBAP)", 0x1132: "Message (MAP)",
                0x1200: "PnP Info", 0x1203: "Generic Audio", 0x1800: "Generic Access", 0x1801: "Generic Attribute",
                0x1802: "Immediate Alert", 0x1803: "Link Loss", 0x1804: "Tx Power", 0x1805: "Current Time",
                0x1809: "Health Thermometer", 0x180A: "Device Information", 0x180D: "Heart Rate",
                0x180F: "Battery", 0x1810: "Blood Pressure", 0x1812: "HID over GATT", 0x1814: "Running Speed",
                0x1816: "Cycling Speed", 0x1818: "Cycling Power", 0x1819: "Location & Navigation",
                0x181A: "Environmental Sensing", 0x181C: "User Data", 0x181D: "Weight Scale",
                0x1826: "Fitness Machine", 0x184E: "Audio Stream Control", 0x1850: "Published Audio Caps",
                0xFD6F: "Exposure Notification", 0xFE9F: "Google", 0xFEAA: "Eddystone", 0xFE2C: "Google Fast Pair",
                0xFD5A: "Samsung", 0xFEED: "Tile", 0xFE07: "Sonos", 0xFDF7: "HP"}
COMPANY = {0x0002: "Intel", 0x0003: "IBM", 0x0006: "Microsoft", 0x000A: "Qualcomm (CSR)", 0x000F: "Broadcom",
           0x001D: "Qualcomm", 0x004C: "Apple", 0x0059: "Nordic Semiconductor", 0x0075: "Samsung",
           0x0087: "Garmin", 0x009E: "Bose", 0x00E0: "Google", 0x012D: "Sony", 0x0157: "Huami",
           0x027D: "Huawei", 0x02E5: "Espressif", 0x038F: "Xiaomi", 0x0499: "Ruuvi", 0x05A7: "Sonos"}
APPLE_TYPES = {0x02: "iBeacon", 0x05: "AirDrop", 0x07: "AirPods / Beats (proximity pairing)", 0x09: "AirPlay target",
               0x0A: "AirPlay source", 0x0C: "Handoff", 0x0F: "Nearby Action", 0x10: "Nearby Info",
               0x12: "Find My / AirTag", 0x13: "Find My (separated)"}


def uuid_name(u):
    u = str(u).lower()
    if u.endswith("-0000-1000-8000-00805f9b34fb") and u.startswith("0000"):
        short = int(u[4:8], 16)
        return SERVICE_UUID.get(short, f"0x{short:04X}")
    return u


def class_info(cls):
    if cls is None:
        return None
    cls = int(cls)
    major = (cls >> 8) & 0x1F
    minor = (cls >> 2) & 0x3F
    name = MAJOR_CLASS.get(major, f"major {major}")
    if major == 5:
        kb = {1: "Keyboard", 2: "Mouse/pointer", 3: "Keyboard + mouse"}.get((minor >> 4) & 3)
        if kb:
            name += f" / {kb}"
    else:
        sub = MINOR_CLASS.get(major, {}).get(minor)
        if sub:
            name += f" / {sub}"
    services = [v for b, v in SERVICE_CLASS.items() if cls & (1 << b)]
    return name, services


def appearance_name(a):
    if a is None:
        return None
    return APPEARANCE.get(int(a) >> 6, f"category {int(a) >> 6}")


def decode_manufacturer(mfr):
    """mfr: {company_id: bytes}. Returns list of (label, text)."""
    rows = []
    for cid, data in (mfr or {}).items():
        cid = int(cid)
        data = bytes(data)
        comp = COMPANY.get(cid, f"0x{cid:04X}")
        rows.append(("Manufacturer", f"{comp} (0x{cid:04X})"))
        if cid == 0x004C and data:
            if data[0] == 0x02 and len(data) >= 23 and data[1] == 0x15:
                u = data[2:18].hex()
                uuid = f"{u[:8]}-{u[8:12]}-{u[12:16]}-{u[16:20]}-{u[20:]}"
                major = int.from_bytes(data[18:20], "big")
                minor = int.from_bytes(data[20:22], "big")
                tx = int.from_bytes(data[22:23], "big", signed=True)
                rows += [("iBeacon UUID", uuid), ("iBeacon major/minor", f"{major} / {minor}"),
                         ("iBeacon TX @1m", f"{tx} dBm")]
            else:
                rows.append(("Apple advert", APPLE_TYPES.get(data[0], f"type 0x{data[0]:02X}")))
        elif cid == 0x0006 and len(data) >= 1:
            rows.append(("Microsoft advert", {0x01: "CDP (Windows device)", 0x03: "Swift Pair"}.get(
                data[0], f"type 0x{data[0]:02X}")))
        rows.append(("Mfr data", data.hex(" ")[:47] + ("…" if len(data) > 16 else "")))
    return rows


def measured_power(d):
    """1 m reference RSSI: iBeacon calibration > advertised TX power - 41 dB > -59 dBm default."""
    for cid, data in (d.get("manufacturer") or {}).items():
        data = bytes(data)
        if int(cid) == 0x004C and len(data) >= 23 and data[0] == 0x02 and data[1] == 0x15:
            return int.from_bytes(data[22:23], "big", signed=True), "iBeacon calibration"
    if d.get("tx_power") is not None:
        return int(d["tx_power"]) - 41, "TX power - 41 dB"
    return -59, "typical BLE @1 m"


def distance_m(rssi, ref, n=2.2):
    if rssi is None:
        return None
    return round(10 ** ((ref - rssi) / (10 * n)), 1)


def kind_of(d):
    if d.get("class") is not None:
        return "BR/EDR"
    return "LE"


def device_detail_lines(d):
    rows = [("Name", d["name"] or "(no name)"), ("Alias", d["alias"] or "-"), ("Address", d["address"]),
            ("Address type", d["address_type"] + (" (privacy)" if d["address_type"] == "random" else "")),
            ("Type", kind_of(d))]
    from netcore import vendor
    if d["address_type"] == "public":
        rows.append(("OUI vendor", vendor(d["address"])))
    ci = class_info(d.get("class"))
    if ci:
        rows.append(("Class", f"{ci[0]} (0x{int(d['class']):06X})"))
        if ci[1]:
            rows.append(("Services (class)", ", ".join(ci[1])))
    ap = appearance_name(d.get("appearance"))
    if ap:
        rows.append(("Appearance", ap))
    if d.get("icon"):
        rows.append(("Icon", d["icon"]))
    rows.append(("RSSI", f"{d['rssi']} dBm" if d["rssi"] is not None else "not seen recently"))
    if d.get("tx_power") is not None:
        rows.append(("TX power", f"{d['tx_power']} dBm"))
    ref, how = measured_power(d)
    dist = distance_m(d["rssi"], ref)
    if dist is not None:
        rows.append(("Distance (est.)", f"~{dist} m ({how})"))
    rows += [("Paired / bonded", f"{yn(d['paired'])} / {yn(d['bonded'])}"),
             ("Trusted / blocked", f"{yn(d['trusted'])} / {yn(d['blocked'])}"),
             ("Connected", yn(d["connected"])), ("Legacy pairing", yn(d["legacy_pairing"]))]
    rows += decode_manufacturer(d.get("manufacturer"))
    for k, v in (d.get("service_data") or {}).items():
        rows.append((f"Svc data {uuid_name(k)}", bytes(v).hex(" ")[:47]))
    for u in d.get("uuids") or []:
        rows.append(("Service", uuid_name(u)))
    return rows


def yn(v):
    return "yes" if v else "no"


def _py(v):
    """dbus types -> python."""
    if AVAILABLE:
        if isinstance(v, (dbus.Array,)) and v.signature == "y":
            return bytes(v)
        if isinstance(v, dbus.Dictionary):
            return {_py(k): _py(x) for k, x in v.items()}
        if isinstance(v, dbus.Array):
            return [_py(x) for x in v]
        if isinstance(v, (dbus.Boolean,)):
            return bool(v)
        if isinstance(v, (dbus.Int16, dbus.Int32, dbus.Int64, dbus.UInt16, dbus.UInt32, dbus.UInt64, dbus.Byte)):
            return int(v)
        if isinstance(v, (dbus.String, dbus.ObjectPath)):
            return str(v)
    return v


def device_from_props(path, p):
    return {"path": path, "adapter": p.get("Adapter", ""), "address": p.get("Address", ""),
            "address_type": p.get("AddressType", "public"), "name": p.get("Name", ""),
            "alias": p.get("Alias", ""), "class": p.get("Class"), "appearance": p.get("Appearance"),
            "icon": p.get("Icon", ""), "paired": p.get("Paired", False), "bonded": p.get("Bonded", False),
            "trusted": p.get("Trusted", False), "blocked": p.get("Blocked", False),
            "connected": p.get("Connected", False), "legacy_pairing": p.get("LegacyPairing", False),
            "rssi": p.get("RSSI"), "tx_power": p.get("TxPower"), "uuids": p.get("UUIDs", []),
            "manufacturer": p.get("ManufacturerData", {}), "service_data": p.get("ServiceData", {})}


def _log(msg):
    try:
        import wifimgr
        wifimgr.log("bt: " + msg)
    except Exception:
        pass


def rfkill_state(hci):
    """(soft_blocked, hard_blocked, sysfs_dir) for this adapter's own rfkill switch, or None."""
    for d in sorted(glob.glob(f"/sys/class/bluetooth/{hci}/rfkill*")):
        try:
            with open(os.path.join(d, "soft")) as f:
                soft = f.read().strip() == "1"
            with open(os.path.join(d, "hard")) as f:
                hard = f.read().strip() == "1"
            return soft, hard, d
        except OSError:
            continue
    return None


def rfkill_unblock(hci):
    st = rfkill_state(hci)
    if st and st[0]:
        try:
            with open(os.path.join(st[2], "soft"), "w") as f:
                f.write("0")
            _log(f"{hci}: rfkill soft block cleared")
            time.sleep(0.4)
        except OSError as e:
            _log(f"{hci}: rfkill unblock failed: {e}")
    return rfkill_state(hci)


def adapter_bus(hci):
    dev = os.path.realpath(f"/sys/class/bluetooth/{hci}/device")
    return "usb" if "/usb" in dev else ("uart" if "serial" in dev or "tty" in dev else "?")


# ------------------------------------------------------------------ agent
if AVAILABLE:
    class Rejected(dbus.DBusException):
        _dbus_error_name = "org.bluez.Error.Rejected"

    class Canceled(dbus.DBusException):
        _dbus_error_name = "org.bluez.Error.Canceled"

    class Agent(dbus.service.Object):
        """Forwards pairing requests to the UI via bt.prompts (a Queue)."""

        def __init__(self, bt):
            self.bt = bt
            super().__init__(bt.bus, AGENT_PATH)

        def _devname(self, device):
            try:
                p = self.bt._props(device, DEVICE)
                return p.get("Alias") or p.get("Address") or str(device)
            except Exception:
                return str(device)

        def _ask(self, kind, device, value, ok, err):
            self.bt.prompts.put({"kind": kind, "device": self._devname(device), "value": value,
                                 "ok": ok, "err": err})

        @dbus.service.method("org.bluez.Agent1", in_signature="", out_signature="")
        def Release(self):
            pass

        @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="s",
                             async_callbacks=("ok", "err"))
        def RequestPinCode(self, device, ok, err):
            self._ask("pin", device, None, ok, err)

        @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="u",
                             async_callbacks=("ok", "err"))
        def RequestPasskey(self, device, ok, err):
            self._ask("passkey", device, None, ok, err)

        @dbus.service.method("org.bluez.Agent1", in_signature="os", out_signature="")
        def DisplayPinCode(self, device, pincode):
            self._ask("show", device, str(pincode), None, None)

        @dbus.service.method("org.bluez.Agent1", in_signature="ouq", out_signature="")
        def DisplayPasskey(self, device, passkey, entered):
            self._ask("show", device, f"{int(passkey):06d}", None, None)

        @dbus.service.method("org.bluez.Agent1", in_signature="ou", out_signature="",
                             async_callbacks=("ok", "err"))
        def RequestConfirmation(self, device, passkey, ok, err):
            self._ask("confirm", device, f"{int(passkey):06d}", ok, err)

        @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="",
                             async_callbacks=("ok", "err"))
        def RequestAuthorization(self, device, ok, err):
            self._ask("authorize", device, None, ok, err)

        @dbus.service.method("org.bluez.Agent1", in_signature="os", out_signature="",
                             async_callbacks=("ok", "err"))
        def AuthorizeService(self, device, uuid, ok, err):
            try:
                p = self.bt._props(device, DEVICE)
                if p.get("Paired") or p.get("Trusted"):
                    ok()
                    return
            except Exception:
                pass
            self._ask("service", device, uuid_name(uuid), ok, err)

        @dbus.service.method("org.bluez.Agent1", in_signature="", out_signature="")
        def Cancel(self):
            self.bt.prompts.put({"kind": "cancel", "device": "", "value": None, "ok": None, "err": None})


# ------------------------------------------------------------------ engine
class Bt:
    def __init__(self):
        if not AVAILABLE:
            raise BtError(f"Bluetooth needs python3-dbus and python3-gi ({IMPORT_ERROR})")
        dbus.mainloop.glib.threads_init()
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        self.bus = dbus.SystemBus()
        self.prompts = queue.Queue()
        self.loop = GLib.MainLoop()
        threading.Thread(target=self.loop.run, daemon=True).start()
        self.agent = Agent(self)
        try:
            mgr = dbus.Interface(self.bus.get_object(BLUEZ, "/org/bluez"), AGENT_MGR)
            mgr.RegisterAgent(AGENT_PATH, "KeyboardDisplay")
            mgr.RequestDefaultAgent(AGENT_PATH)
            self.agent_ok = True
        except dbus.DBusException as e:
            self.agent_ok = False
            self.agent_error = e.get_dbus_message()
        self.scanning = set()

    def close(self):
        for path in list(self.scanning):
            self.stop_scan(path)
        try:
            mgr = dbus.Interface(self.bus.get_object(BLUEZ, "/org/bluez"), AGENT_MGR)
            mgr.UnregisterAgent(AGENT_PATH)
        except Exception:
            pass
        self.loop.quit()

    # -------- helpers
    def _obj(self, path):
        return self.bus.get_object(BLUEZ, path)

    def _props(self, path, iface):
        return _py(dbus.Interface(self._obj(path), PROPS).GetAll(iface))

    def _set(self, path, iface, name, value):
        dbus.Interface(self._obj(path), PROPS).Set(iface, name, value)

    def _objects(self):
        try:
            om = dbus.Interface(self._obj("/"), OBJMGR)
            return _py(om.GetManagedObjects())
        except dbus.DBusException as e:
            raise BtError(f"bluetoothd not reachable: {e.get_dbus_message()}")

    @staticmethod
    def _err(e):
        return BtError(e.get_dbus_message() or e.get_dbus_name())

    # -------- adapters
    def adapters(self):
        res = []
        objs = self._objects()
        for path, ifaces in objs.items():
            a = ifaces.get(ADAPTER)
            if a is None:
                continue
            hci = path.rsplit("/", 1)[-1]
            bus = adapter_bus(hci)
            rk = rfkill_state(hci)
            res.append({"path": path, "hci": hci, "address": a.get("Address", ""), "name": a.get("Name", ""),
                        "blocked": ("hard" if rk[1] else "soft" if rk[0] else "") if rk else "",
                        "alias": a.get("Alias", ""), "powered": a.get("Powered", False),
                        "discovering": a.get("Discovering", False), "discoverable": a.get("Discoverable", False),
                        "bus": bus, "role": "USB" if bus == "usb" else "INT",
                        "devices": sum(1 for p, i in objs.items()
                                       if DEVICE in i and i[DEVICE].get("Adapter") == path and i[DEVICE].get("Paired"))})
        res.sort(key=lambda a: a["hci"])
        return res

    def _powered(self, path):
        try:
            return bool(self._props(path, ADAPTER).get("Powered"))
        except dbus.DBusException:
            return None

    def power(self, path, on):
        hci = path.rsplit("/", 1)[-1]
        if on:
            st = rfkill_unblock(hci)
            if st and st[1]:
                raise BtError(f"{hci} is hard-blocked (driver/hardware rfkill) - replug the dongle")
        errors = []
        for attempt in range(2):
            try:
                self._set(path, ADAPTER, "Powered", dbus.Boolean(on))
            except dbus.DBusException as e:
                errors.append(f"{e.get_dbus_name().split('.')[-1]}: {e.get_dbus_message()}")
                _log(f"{hci} Powered={on} failed: {errors[-1]}")
                if on:
                    rfkill_unblock(hci)
                time.sleep(0.8)
                continue
            time.sleep(0.3)
            if self._powered(path) == on:
                return f"{hci} {'on' if on else 'off'}"
        # last resort: kernel mgmt API directly (bypasses bluetoothd's cached state)
        try:
            r = subprocess.run(["btmgmt", "--index", hci[3:], "power", "on" if on else "off"],
                               capture_output=True, text=True, timeout=10)
            errors.append("btmgmt: " + (r.stdout + r.stderr).strip().splitlines()[-1] if (r.stdout + r.stderr).strip()
                          else f"btmgmt rc={r.returncode}")
        except (OSError, subprocess.TimeoutExpired) as e:
            errors.append(f"btmgmt: {e}")
        time.sleep(0.5)
        if self._powered(path) == on:
            return f"{hci} {'on' if on else 'off'} (via btmgmt)"
        _log(f"{hci} power {'on' if on else 'off'} gave up: {' | '.join(errors)}")
        main_err = next((e for e in errors if not e.startswith("btmgmt")), errors[-1] if errors else "unknown")
        raise BtError(f"{hci} won't power {'on' if on else 'off'}: {main_err}")

    def start_scan(self, path):
        ad = dbus.Interface(self._obj(path), ADAPTER)
        try:
            ad.SetDiscoveryFilter({"Transport": dbus.String("auto"), "DuplicateData": dbus.Boolean(True)})
        except dbus.DBusException:
            pass
        try:
            ad.StartDiscovery()
        except dbus.DBusException as e:
            if "InProgress" not in e.get_dbus_name():
                raise self._err(e)
        self.scanning.add(path)

    def stop_scan(self, path):
        self.scanning.discard(path)
        try:
            dbus.Interface(self._obj(path), ADAPTER).StopDiscovery()
        except dbus.DBusException:
            pass

    # -------- devices
    def devices(self, adapter_path, paired_only=False):
        res = []
        for path, ifaces in self._objects().items():
            p = ifaces.get(DEVICE)
            if p is None or p.get("Adapter") != adapter_path:
                continue
            if paired_only and not p.get("Paired"):
                continue
            res.append(device_from_props(path, p))
        res.sort(key=lambda d: (not d["connected"], not d["paired"],
                                -(d["rssi"] if d["rssi"] is not None else -200)))
        return res

    def device(self, path):
        return device_from_props(path, self._props(path, DEVICE))

    def rssi(self, path):
        try:
            return self._props(path, DEVICE).get("RSSI")
        except dbus.DBusException:
            return None

    def pair(self, path, timeout=60):
        done, result = threading.Event(), {}

        def ok():
            result["ok"] = True
            done.set()

        def err(e):
            result["err"] = e
            done.set()

        dbus.Interface(self._obj(path), DEVICE).Pair(reply_handler=ok, error_handler=err, timeout=timeout)
        if not done.wait(timeout + 5):
            raise BtError("pairing timed out")
        if "err" in result:
            e = result["err"]
            msg = e.get_dbus_message() if hasattr(e, "get_dbus_message") else str(e)
            if "AlreadyExists" in getattr(e, "get_dbus_name", lambda: "")():
                return "already paired"
            raise BtError(f"pairing failed: {msg}")
        try:
            self._set(path, DEVICE, "Trusted", dbus.Boolean(True))
        except dbus.DBusException:
            pass
        return "paired + trusted"

    def connect(self, path):
        try:
            dbus.Interface(self._obj(path), DEVICE).Connect(timeout=30)
        except dbus.DBusException as e:
            raise self._err(e)
        return "connected"

    def disconnect(self, path):
        try:
            dbus.Interface(self._obj(path), DEVICE).Disconnect(timeout=15)
        except dbus.DBusException as e:
            raise self._err(e)
        return "disconnected"

    def trust(self, path, on):
        self._set(path, DEVICE, "Trusted", dbus.Boolean(on))
        return "trusted" if on else "untrusted"

    def remove(self, adapter_path, path):
        try:
            dbus.Interface(self._obj(adapter_path), ADAPTER).RemoveDevice(dbus.ObjectPath(path))
        except dbus.DBusException as e:
            raise self._err(e)
        return "removed"

    @staticmethod
    def reply(prompt, accept, value=None):
        """Answer an agent prompt from the UI thread (marshalled onto the GLib loop)."""
        ok, err = prompt.get("ok"), prompt.get("err")
        if ok is None:
            return

        def _do():
            try:
                if accept:
                    if prompt["kind"] == "pin":
                        ok(dbus.String(value or ""))
                    elif prompt["kind"] == "passkey":
                        ok(dbus.UInt32(int(value or 0)))
                    else:
                        ok()
                else:
                    err(Rejected("Rejected by user"))
            except Exception:
                pass
            return False

        GLib.idle_add(_do)


def rssi_bar_pct(rssi):
    if rssi is None:
        return 0
    return max(0, min(100, int((rssi + 100) * 100 / 60)))


def trend(history):
    if len(history) < 4:
        return 0
    a = sum(history[-4:-2]) / 2
    b = sum(history[-2:]) / 2
    return 1 if b - a > 2 else -1 if a - b > 2 else 0

