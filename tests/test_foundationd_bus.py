"""foundationd on a private session bus, with a real client connection.

These start dbus-daemon and the daemon as subprocesses, so nothing touches the machine's buses.
Polkit is replaced by the allow/deny stub authorizers; real polkit behaviour is not covered here.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

try:
    import gi

    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib
except (ImportError, ValueError):  # PyGObject missing
    gi = None

DBUS_DAEMON = shutil.which("dbus-daemon")
NAME, PATH = "org.ersa.Foundation1", "/org/ersa/Foundation1"
MANAGER, CONFIG = NAME + ".Manager", NAME + ".Config"
XML = os.path.join(ROOT, "data/dbus/org.ersa.Foundation1.xml")
SCHEMA = os.path.join(ROOT, "data/config/schema.d")


@unittest.skipUnless(gi and DBUS_DAEMON, "needs PyGObject and dbus-daemon")
class BusCase(unittest.TestCase):
    authorizer = "allow"
    idle_timeout = 0
    extra_args = ()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conf = os.path.join(self.tmp.name, "conf.d")
        self.bus = subprocess.Popen(
            [DBUS_DAEMON, "--session", "--print-address", "--nofork"],
            stdout=subprocess.PIPE, text=True)
        self.addCleanup(self._stop, self.bus)
        self.address = self.bus.stdout.readline().strip()
        self.daemon = self._start_daemon()
        self.addCleanup(self._stop, self.daemon)
        self.conn = Gio.DBusConnection.new_for_address_sync(
            self.address,
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
            None, None)
        self._wait_for_name()

    def _start_daemon(self):
        env = dict(os.environ, DBUS_SESSION_BUS_ADDRESS=self.address, PYTHONPATH=ROOT)
        return subprocess.Popen(
            [sys.executable, "-m", "foundationd", "--bus", "session",
             "--authorizer", self.authorizer, "--idle-timeout", str(self.idle_timeout),
             "--xml-path", XML, "--schema-path", SCHEMA,
             "--defaults-path", os.path.join(self.tmp.name, "defaults.d"),
             "--conf-path", self.conf, "--state-path", os.path.join(self.tmp.name, "state"),
             *self.extra_args],
            env=env, stderr=subprocess.PIPE, text=True)

    @staticmethod
    def _stop(process):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(5)
            except subprocess.TimeoutExpired:
                process.kill()
        for stream in (process.stdout, process.stderr):
            if stream:
                stream.close()

    def _wait_for_name(self):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.daemon.poll() is not None:
                self.fail(f"foundationd exited early: {self.daemon.stderr.read()}")
            try:
                self.conn.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                    "GetNameOwner", GLib.Variant("(s)", (NAME,)), None,
                                    Gio.DBusCallFlags.NONE, 1000, None)
                return
            except GLib.Error:
                time.sleep(0.05)
        self.fail("foundationd never appeared on the bus")

    def call(self, interface, method, signature=None, *args, reply="*"):
        parameters = GLib.Variant(signature, args) if signature else None
        result = self.conn.call_sync(NAME, PATH, interface, method, parameters, None,
                                     Gio.DBusCallFlags.NONE, 5000, None)
        return result.unpack()

    def error_of(self, interface, method, signature=None, *args):
        """The D-Bus error name a call fails with (the test fails if it succeeds)."""
        try:
            self.call(interface, method, signature, *args)
        except GLib.Error as error:
            return Gio.DBusError.get_remote_error(error)
        self.fail(f"{method} unexpectedly succeeded")

    def set_value(self, key, signature, value):
        return self.call(CONFIG, "Set", "(sv)", key, GLib.Variant(signature, value))


class ManagerAndReadTests(BusCase):
    def test_version_properties(self):
        version = self.conn.call_sync(
            NAME, PATH, "org.freedesktop.DBus.Properties", "Get",
            GLib.Variant("(ss)", (MANAGER, "ApiVersion")), None, Gio.DBusCallFlags.NONE, 5000, None)
        self.assertEqual(version.unpack(), ("1",))

    def test_status_reports_unprovisioned_and_capabilities(self):
        (status,) = self.call(MANAGER, "GetStatus")
        self.assertIs(status["provisioned"], False)
        self.assertIs(status["developer"], False)
        self.assertEqual(status["capabilities"], ["apps", "boot", "compat", "config", "deploy", "extensions", "jobs", "repos", "updates"])

    def test_get_default(self):
        self.assertEqual(self.call(CONFIG, "Get", "(s)", "ota.channel"), ("stable",))
        self.assertEqual(self.call(CONFIG, "Get", "(s)", "ota.auto_check"), (True,))

    def test_list_contains_every_schema_key(self):
        (values,) = self.call(CONFIG, "List")
        self.assertEqual(sorted(values), [
            "ota.auto_check", "ota.auto_download", "ota.channel", "ota.url", "system.developer_mode"])

    def test_describe(self):
        (info,) = self.call(CONFIG, "Describe", "(s)", "ota.channel")
        self.assertEqual(info["type"], "s")
        self.assertEqual(info["choices"], ["stable", "testing"])
        self.assertIs(info["writable"], True)

    def test_unknown_key_error_name(self):
        self.assertEqual(self.error_of(CONFIG, "Get", "(s)", "nope"), "org.ersa.Foundation1.Error.UnknownKey")

    def test_introspection_matches_the_contract_file(self):
        (xml,) = self.call("org.freedesktop.DBus.Introspectable", "Introspect")
        for member in ('interface name="org.ersa.Foundation1.Config"', 'method name="Set"',
                       'signal name="Changed"', 'name="Version" access="read"'):
            self.assertIn(member, xml)


class WriteWithAuthorization(BusCase):
    authorizer = "allow"

    def test_set_get_reset_and_signal(self):
        received = []
        self.conn.signal_subscribe(
            NAME, CONFIG, "Changed", PATH, None, Gio.DBusSignalFlags.NONE,
            lambda c, sender, path, iface, signal, params: received.append(params.unpack()))

        self.set_value("ota.channel", "s", "testing")
        self.assertEqual(self.call(CONFIG, "Get", "(s)", "ota.channel"), ("testing",))
        self.assertTrue(os.path.exists(os.path.join(self.conf, "90-api.conf")))

        self.call(CONFIG, "Reset", "(s)", "ota.channel")
        self.assertEqual(self.call(CONFIG, "Get", "(s)", "ota.channel"), ("stable",))
        self.assertFalse(os.path.exists(os.path.join(self.conf, "90-api.conf")))

        deadline = time.monotonic() + 3
        while len(received) < 2 and time.monotonic() < deadline:
            GLib.MainContext.default().iteration(False)
            time.sleep(0.01)
        self.assertEqual(received, [("ota.channel",), ("ota.channel",)])

    def test_setting_the_current_value_emits_no_signal(self):
        received = []
        self.conn.signal_subscribe(
            NAME, CONFIG, "Changed", PATH, None, Gio.DBusSignalFlags.NONE,
            lambda *a: received.append(a))
        self.set_value("ota.channel", "s", "stable")
        self.call(MANAGER, "GetStatus")  # round trip: any signal would have arrived before this
        for _ in range(20):
            GLib.MainContext.default().iteration(False)
            time.sleep(0.01)
        self.assertEqual(received, [])

    def test_wrong_variant_type_is_rejected(self):
        self.assertEqual(self.error_of(CONFIG, "Set", "(sv)", "ota.auto_check", GLib.Variant("s", "true")),
                         "org.ersa.Foundation1.Error.InvalidArgument")
        self.assertEqual(self.error_of(CONFIG, "Set", "(sv)", "ota.channel", GLib.Variant("b", True)),
                         "org.ersa.Foundation1.Error.InvalidArgument")

    def test_bad_choice_and_multiline_value_are_rejected(self):
        for value in ("nightly", "stable\nota.auto_download=true"):
            self.assertEqual(self.error_of(CONFIG, "Set", "(sv)", "ota.channel", GLib.Variant("s", value)),
                             "org.ersa.Foundation1.Error.InvalidArgument")
        self.assertFalse(os.path.exists(os.path.join(self.conf, "90-api.conf")))

    def test_developer_mode_shows_in_status(self):
        self.set_value("system.developer_mode", "b", True)
        (status,) = self.call(MANAGER, "GetStatus")
        self.assertIs(status["developer"], True)

    def test_unknown_key_set(self):
        self.assertEqual(self.error_of(CONFIG, "Set", "(sv)", "nope", GLib.Variant("s", "x")),
                         "org.ersa.Foundation1.Error.UnknownKey")


class WriteDenied(BusCase):
    authorizer = "deny"

    def test_set_and_reset_are_refused_and_change_nothing(self):
        self.assertEqual(self.error_of(CONFIG, "Set", "(sv)", "ota.channel", GLib.Variant("s", "testing")),
                         "org.ersa.Foundation1.Error.NotAuthorized")
        self.assertEqual(self.error_of(CONFIG, "Reset", "(s)", "ota.channel"),
                         "org.ersa.Foundation1.Error.NotAuthorized")
        self.assertFalse(os.path.exists(os.path.join(self.conf, "90-api.conf")))

    def test_reads_need_no_authorization(self):
        self.assertEqual(self.call(CONFIG, "Get", "(s)", "ota.channel"), ("stable",))
        self.call(MANAGER, "GetStatus")

    def test_invalid_requests_fail_before_authorization(self):
        # A caller must not be asked to authenticate for a request that can never succeed.
        self.assertEqual(self.error_of(CONFIG, "Set", "(sv)", "ota.channel", GLib.Variant("s", "nightly")),
                         "org.ersa.Foundation1.Error.InvalidArgument")
        self.assertEqual(self.error_of(CONFIG, "Reset", "(s)", "nope"),
                         "org.ersa.Foundation1.Error.UnknownKey")


class PolkitCase(BusCase):
    """The real PolkitAuthorizer talking to a fake polkitd on the private bus."""

    authorizer = "polkit"
    polkit_mode = "allow"

    def setUp(self):
        self.polkit_log = os.path.join(tempfile.mkdtemp(), "requests")
        self.addCleanup(shutil.rmtree, os.path.dirname(self.polkit_log), True)
        env = dict(os.environ, FAKE_POLKIT=self.polkit_mode, FAKE_POLKIT_LOG=self.polkit_log)
        # BusCase.setUp starts the bus; the fake needs it, so start the fake from the daemon hook.
        self._env = env
        super().setUp()

    def _start_daemon(self):
        env = dict(self._env, DBUS_SESSION_BUS_ADDRESS=self.address, PYTHONPATH=ROOT)
        self.fake = subprocess.Popen([sys.executable, os.path.join(ROOT, "tests/fake_polkit.py")],
                                     env=env, stdout=subprocess.PIPE, text=True)
        self.addCleanup(self._stop, self.fake)
        self.assertEqual(self.fake.stdout.readline().strip(), "ready")
        return super()._start_daemon()

    def requests(self):
        try:
            with open(self.polkit_log, encoding="utf-8") as handle:
                return handle.read().splitlines()
        except FileNotFoundError:
            return []


class PolkitAllow(PolkitCase):
    def test_set_asks_polkit_about_the_caller_by_bus_name_and_succeeds(self):
        self.set_value("ota.channel", "s", "testing")
        self.assertEqual(self.call(CONFIG, "Get", "(s)", "ota.channel"), ("testing",))
        (request,) = self.requests()
        kind, name, action, flags = request.split()
        self.assertEqual((kind, action, flags), ("system-bus-name", "org.ersa.foundation.config", "1"))
        self.assertEqual(name, self.conn.get_unique_name())

    def test_reads_do_not_ask_polkit(self):
        self.call(CONFIG, "Get", "(s)", "ota.channel")
        self.call(MANAGER, "GetStatus")
        self.assertEqual(self.requests(), [])


class PolkitDeny(PolkitCase):
    polkit_mode = "deny"

    def test_denied(self):
        self.assertEqual(self.error_of(CONFIG, "Set", "(sv)", "ota.channel", GLib.Variant("s", "testing")),
                         "org.ersa.Foundation1.Error.NotAuthorized")
        self.assertFalse(os.path.exists(os.path.join(self.conf, "90-api.conf")))
        # Without this a broken authorizer (which also denies) would pass the test.
        self.assertEqual(len(self.requests()), 1)


class PolkitFails(PolkitCase):
    polkit_mode = "error"

    def test_polkit_error_denies(self):
        self.assertEqual(self.error_of(CONFIG, "Set", "(sv)", "ota.channel", GLib.Variant("s", "testing")),
                         "org.ersa.Foundation1.Error.NotAuthorized")
        self.assertEqual(len(self.requests()), 1)


class PolkitMissing(BusCase):
    """No polkitd on the bus at all: the request must be denied, not hang or crash the daemon."""

    authorizer = "polkit"

    def test_denied_and_daemon_survives(self):
        self.assertEqual(self.error_of(CONFIG, "Set", "(sv)", "ota.channel", GLib.Variant("s", "testing")),
                         "org.ersa.Foundation1.Error.NotAuthorized")
        self.assertEqual(self.call(CONFIG, "Get", "(s)", "ota.channel"), ("stable",))
        self.assertIsNone(self.daemon.poll())


class PolkitWaitingDoesNotBlock(PolkitCase):
    polkit_mode = "hang"

    def test_other_callers_are_served_while_an_authorization_is_pending(self):
        # Start a Set that polkit never answers, then make sure reads still work.
        self.conn.call(NAME, PATH, CONFIG, "Set", GLib.Variant("(sv)", ("ota.channel", GLib.Variant("s", "testing"))),
                       None, Gio.DBusCallFlags.NONE, 60000, None, lambda *a: None)
        deadline = time.monotonic() + 5
        while not self.requests() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(len(self.requests()), 1)
        self.assertEqual(self.call(CONFIG, "Get", "(s)", "ota.channel"), ("stable",))


class AuthorizerExceptionDenies(BusCase):
    """An authorizer that raises must produce a reply and must not leak the in-flight counter."""

    def test_service_level(self):
        from foundationd.config import ConfigStore
        from foundationd.schema import Schema
        from foundationd.service import Service
        from foundationd.state import StateStore

        class Exploding:
            def authorize(self, sender, action, callback):
                raise RuntimeError("boom")

        class FakeInvocation:
            def __init__(self):
                self.errors = []

            def return_dbus_error(self, name, message):
                self.errors.append(name)

        with open(XML, encoding="utf-8") as handle:
            xml = handle.read()
        with tempfile.TemporaryDirectory() as tmp:
            service = Service(xml, ConfigStore(Schema.load(SCHEMA), tmp, tmp), StateStore(tmp), Exploding())
            invocation = FakeInvocation()
            service._on_method_call(None, ":1.1", "/org/ersa/Foundation1", CONFIG, "Reset",
                                    GLib.Variant("(s)", ("ota.channel",)), invocation)
            self.assertEqual(invocation.errors, ["org.ersa.Foundation1.Error.NotAuthorized"])
            self.assertEqual(service._pending, 0)


class IdleExit(BusCase):
    idle_timeout = 2

    def test_exits_cleanly_when_idle(self):
        self.call(MANAGER, "GetStatus")
        self.assertEqual(self.daemon.wait(timeout=10), 0)


class Startup(unittest.TestCase):
    @unittest.skipUnless(gi, "needs PyGObject")
    def test_contract_and_handlers_must_agree(self):
        from foundationd.service import Service
        from foundationd.auth import StubAuthorizer
        from foundationd.config import ConfigStore
        from foundationd.schema import Schema
        from foundationd.state import StateStore

        with open(XML, encoding="utf-8") as handle:
            xml = handle.read()
        extended = xml.replace("</node>", '<interface name="org.ersa.Foundation1.Extra">'
                                          '<method name="Nope"/></interface></node>')
        schema = Schema.load(SCHEMA)
        with tempfile.TemporaryDirectory() as tmp:
            store = ConfigStore(schema, tmp, tmp)
            Service(xml, store, StateStore(tmp), StubAuthorizer())  # must not raise
            with self.assertRaisesRegex(RuntimeError, "contract mismatch"):
                Service(extended, store, StateStore(tmp), StubAuthorizer())

    @unittest.skipUnless(gi, "needs PyGObject")
    def test_system_bus_refuses_development_options(self):
        from foundationd.__main__ import parse_args

        with self.assertRaises(SystemExit):
            parse_args(["--authorizer", "allow"])
        with self.assertRaises(SystemExit):
            parse_args(["--conf-path", "/tmp/x"])
        self.assertEqual(parse_args([]).bus, "system")
        self.assertEqual(parse_args(["--bus", "session", "--authorizer", "allow"]).authorizer, "allow")


if __name__ == "__main__":
    unittest.main()
