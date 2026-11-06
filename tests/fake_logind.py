"""A stand-in for systemd-logind on a private bus. Appends each call to FAKE_LOGIND_LOG."""
import os

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

XML = """<node><interface name="org.freedesktop.login1.Manager">
<method name="Reboot"><arg type="b" direction="in"/></method>
<method name="SetRebootToFirmwareSetup"><arg type="b" direction="in"/></method>
</interface></node>"""


def on_call(connection, sender, path, iface, method, params, invocation):
    with open(os.environ["FAKE_LOGIND_LOG"], "a", encoding="utf-8") as handle:
        handle.write(f"{method} {params.unpack()[0]}\n")
    invocation.return_value(None)


connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
connection.register_object("/org/freedesktop/login1", Gio.DBusNodeInfo.new_for_xml(XML).interfaces[0], on_call, None, None)
Gio.bus_own_name_on_connection(connection, "org.freedesktop.login1", Gio.BusNameOwnerFlags.NONE,
                               lambda c, n: print("ready", flush=True), lambda c, n: os._exit(1))
GLib.MainLoop().run()
