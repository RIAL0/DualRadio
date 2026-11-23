#!/usr/bin/env python3
"""Minimal mock of bluetoothd's D-Bus API for testing btcore (not shipped)."""
import dbus
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib

dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
bus = dbus.SystemBus()
name = dbus.service.BusName("org.bluez", bus)

ADAPTERS = {
    "/org/bluez/hci0": {"Address": "B8:27:EB:00:00:01", "Name": "CardputerZero", "Alias": "CardputerZero",
                        "Powered": True, "Discovering": False, "Discoverable": False},
    "/org/bluez/hci1": {"Address": "98:BA:5F:D2:0F:C3", "Name": "CardputerZero #2", "Alias": "CardputerZero #2",
                        "Powered": False, "Discovering": False, "Discoverable": False, "_refuse_power": True},
}
DEVICES = {
    "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_01": {
        "Adapter": dbus.ObjectPath("/org/bluez/hci0"), "Address": "AA:BB:CC:DD:EE:01", "AddressType": "public",
        "Name": "WH-1000XM4", "Alias": "WH-1000XM4", "Class": dbus.UInt32(0x240404), "Icon": "audio-headset",
        "Paired": False, "Bonded": False, "Trusted": False, "Blocked": False, "Connected": False,
        "LegacyPairing": False, "RSSI": dbus.Int16(-58),
        "UUIDs": dbus.Array(["0000110b-0000-1000-8000-00805f9b34fb", "0000111e-0000-1000-8000-00805f9b34fb"], signature="s")},
    "/org/bluez/hci1/dev_5A_11_22_33_44_55": {
        "Adapter": dbus.ObjectPath("/org/bluez/hci1"), "Address": "5A:11:22:33:44:55", "AddressType": "random",
        "Name": "", "Alias": "5A-11-22-33-44-55", "Appearance": dbus.UInt16(0x0040),
        "Paired": False, "Bonded": False, "Trusted": False, "Blocked": False, "Connected": False,
        "LegacyPairing": False, "RSSI": dbus.Int16(-71), "TxPower": dbus.Int16(-12),
        "ManufacturerData": dbus.Dictionary({dbus.UInt16(0x004C): dbus.Array(
            bytes.fromhex("0215f7826da64fa24e988024bc5b71e0893e00010002c5"), signature="y")}, signature="qv"),
        "UUIDs": dbus.Array([], signature="s")},
}
agent = {}


class Props(dbus.service.Object):
    def __init__(self, path, iface, store):
        super().__init__(bus, path)
        self.iface, self.store = iface, store

    @dbus.service.method("org.freedesktop.DBus.Properties", in_signature="s", out_signature="a{sv}")
    def GetAll(self, iface):
        return self.store

    @dbus.service.method("org.freedesktop.DBus.Properties", in_signature="ss", out_signature="v")
    def Get(self, iface, prop):
        return self.store[prop]

    @dbus.service.method("org.freedesktop.DBus.Properties", in_signature="ssv", out_signature="")
    def Set(self, iface, prop, value):
        if prop == "Powered" and self.store.get("_refuse_power"):
            raise dbus.DBusException("Blocked through rfkill", name="org.bluez.Error.Failed")
        self.store[prop] = value


class Adapter(Props):
    @dbus.service.method("org.bluez.Adapter1", in_signature="", out_signature="")
    def StartDiscovery(self):
        if self.store["Discovering"]:
            raise dbus.DBusException("busy", name="org.bluez.Error.InProgress")
        self.store["Discovering"] = True

    @dbus.service.method("org.bluez.Adapter1", in_signature="", out_signature="")
    def StopDiscovery(self):
        self.store["Discovering"] = False

    @dbus.service.method("org.bluez.Adapter1", in_signature="a{sv}", out_signature="")
    def SetDiscoveryFilter(self, f):
        self.store["_filter"] = f

    @dbus.service.method("org.bluez.Adapter1", in_signature="o", out_signature="")
    def RemoveDevice(self, path):
        DEVICES.pop(str(path), None)


class Device(Props):
    @dbus.service.method("org.bluez.Device1", in_signature="", out_signature="",
                         async_callbacks=("ok", "err"))
    def Pair(self, ok, err):
        a = bus.get_object(agent["sender"], agent["path"])

        def accepted():
            self.store["Paired"] = self.store["Bonded"] = True
            ok()

        def rejected(e):
            print('agent error:', e, flush=True)
            err(dbus.DBusException("auth failed", name="org.bluez.Error.AuthenticationRejected"))

        a.RequestConfirmation(dbus.ObjectPath(self._object_path), dbus.UInt32(123456),
                              dbus_interface="org.bluez.Agent1", reply_handler=accepted,
                              error_handler=rejected, timeout=60)

    @dbus.service.method("org.bluez.Device1", in_signature="", out_signature="")
    def Connect(self):
        self.store["Connected"] = True

    @dbus.service.method("org.bluez.Device1", in_signature="", out_signature="")
    def Disconnect(self):
        self.store["Connected"] = False


class Root(dbus.service.Object):
    @dbus.service.method("org.freedesktop.DBus.ObjectManager", in_signature="", out_signature="a{oa{sa{sv}}}")
    def GetManagedObjects(self):
        objs = {}
        for p, s in ADAPTERS.items():
            objs[p] = {"org.bluez.Adapter1": {k: v for k, v in s.items() if not k.startswith("_")}}
        for p, s in DEVICES.items():
            objs[p] = {"org.bluez.Device1": s}
        return objs


class AgentMgr(dbus.service.Object):
    @dbus.service.method("org.bluez.AgentManager1", in_signature="os", out_signature="", sender_keyword="sender")
    def RegisterAgent(self, path, cap, sender=None):
        agent.update(path=str(path), cap=str(cap), sender=sender)

    @dbus.service.method("org.bluez.AgentManager1", in_signature="o", out_signature="")
    def RequestDefaultAgent(self, path):
        agent["default"] = True

    @dbus.service.method("org.bluez.AgentManager1", in_signature="o", out_signature="")
    def UnregisterAgent(self, path):
        agent.clear()


Root(bus, "/")
AgentMgr(bus, "/org/bluez")
for p, s in ADAPTERS.items():
    Adapter(p, "org.bluez.Adapter1", s)
for p, s in DEVICES.items():
    Device(p, "org.bluez.Device1", s)
print("mock bluez ready", flush=True)
GLib.MainLoop().run()
