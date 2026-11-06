"""A stand-in for polkitd on a private bus, for tests of foundationd's PolkitAuthorizer.

Behaviour comes from the FAKE_POLKIT environment variable: allow, deny, hang (never answers) or
error (replies with a D-Bus error). Every request is appended to the file named by FAKE_POLKIT_LOG
as one line: "<subject kind> <subject name> <action> <flags>".
"""
import os

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

XML = """<node><interface name="org.freedesktop.PolicyKit1.Authority">
<method name="CheckAuthorization">
  <arg type="(sa{sv})" name="subject" direction="in"/><arg type="s" name="action_id" direction="in"/>
  <arg type="a{ss}" name="details" direction="in"/><arg type="u" name="flags" direction="in"/>
  <arg type="s" name="cancellation_id" direction="in"/>
  <arg type="(bba{ss})" name="result" direction="out"/></method></interface></node>"""

mode = os.environ.get("FAKE_POLKIT", "allow")
log_path = os.environ["FAKE_POLKIT_LOG"]


def on_call(connection, sender, path, iface, method, params, invocation):
    (kind, details), action, _d, flags, _c = params.unpack()
    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write(f"{kind} {details.get('name')} {action} {flags}\n")
    if mode == "hang":
        return
    if mode == "error":
        invocation.return_dbus_error("org.freedesktop.PolicyKit1.Error.Failed", "boom")
        return
    allowed = mode == "allow"
    invocation.return_value(GLib.Variant("((bba{ss}))", ((allowed, False, {}),)))


connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
connection.register_object("/org/freedesktop/PolicyKit1/Authority",
                           Gio.DBusNodeInfo.new_for_xml(XML).interfaces[0], on_call, None, None)
Gio.bus_own_name_on_connection(connection, "org.freedesktop.PolicyKit1", Gio.BusNameOwnerFlags.NONE,
                               lambda c, n: print("ready", flush=True), lambda c, n: os._exit(1))
GLib.MainLoop().run()
