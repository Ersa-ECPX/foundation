"""The updater's text and decisions (always), and its window driven against a real foundationd (needs a display)."""

import os
import tempfile
import time
import unittest

import support  # noqa: F401
from test_foundationd_boot import BootDaemon

from foundation_updater import model

# The window tests open real windows, so they run only on request, on a virtual display:
#   env -u WAYLAND_DISPLAY GDK_BACKEND=x11 GSK_RENDERER=cairo ERSA_GUI_TESTS=1 xvfb-run -a \
#       python3 -m unittest discover -s tests -p test_updater.py
try:
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, GLib, Gtk
    DISPLAY = bool(os.environ.get("ERSA_GUI_TESTS")) and Gtk.init_check()
except (ImportError, ValueError):
    DISPLAY = False

APP = None


class Text(unittest.TestCase):
    def test_slot_text(self):
        slot = {"name": "a", "version": "0.2.0", "running": True, "default": True, "bootable": True, "tries_left": -1}
        self.assertEqual(model.slot_title(slot), "Slot A")
        self.assertEqual(model.slot_subtitle(slot), "Version 0.2.0 · running now · starts by default")
        spare = {"name": "b", "version": "", "running": False, "default": False, "bootable": False, "tries_left": -1}
        self.assertEqual(model.slot_subtitle(spare), "Empty")
        trying = {"name": "b", "version": "0.3.0", "running": False, "default": True, "bootable": True, "tries_left": 2}
        self.assertIn("2 start attempts left", model.slot_subtitle(trying))
        self.assertEqual(model.slot_subtitle({"name": "live", "live": True, "version": "0.1.0"}), "The installer medium's own system")

    def test_running_text(self):
        state = {"running_version": "0.2.0", "running_slot": "b", "assessment": "good"}
        self.assertEqual((model.running_title(state), model.running_subtitle(state)), ("Version 0.2.0", "Running from slot B"))
        self.assertEqual(model.running_title({}), "Version unknown")
        self.assertEqual(model.assessment(state)[0], "good")
        self.assertEqual(model.assessment({"assessment": "pending", "tries_left": 2}), ("pending", "Waiting to be confirmed (2 attempts left)"))
        self.assertEqual(model.assessment({})[0], "unknown")

    def test_pending_banner(self):
        self.assertEqual(model.pending_text({}), "")
        self.assertIn("Version 0.3.0", model.pending_text({"version": "0.3.0"}))

    def test_what_may_be_installed(self):
        with tempfile.TemporaryDirectory() as tmp:
            good, odd = os.path.join(tmp, "u.tar.zst"), os.path.join(tmp, "u.txt")
            for path in (good, odd):
                open(path, "w").close()
            self.assertEqual(model.classify_source(good), ("file", None))
            self.assertEqual(model.classify_source(odd)[0], "")
            self.assertEqual(model.classify_source(os.path.join(tmp, "missing.tar.zst"))[0], "")
        self.assertEqual(model.classify_source("")[0], "")

    def test_result_and_errors(self):
        lines = model.result_lines({"version": "0.3.0", "slot": "b", "signed": True,
                                    "bundles": {"ready": ["codecs"], "failed": []}, "notes": ["a note"]})
        self.assertEqual(lines[0], "Version 0.3.0 was written to slot B.")
        self.assertIn("The signature was checked.", lines)
        self.assertTrue(any("codecs" in line for line in lines))
        self.assertEqual(lines[-1], "a note")
        self.assertIn("not checked", model.result_lines({"version": "1", "slot": "a", "signed": False})[1])
        self.assertIn("password", model.error_text("NotAuthorized", "x"))
        self.assertEqual(model.error_text("InvalidArgument", "no standby slot"), "no standby slot")

    def test_no_internal_acronym_in_user_text(self):
        self.assertNotIn("ECP", model.PRODUCT)
        self.assertEqual(model.PRODUCT, "Ersa Compute Platform")


@unittest.skipUnless(DISPLAY, "set ERSA_GUI_TESTS=1 and run on a virtual display (see the top of this file)")
class Window(BootDaemon):
    """The window against a real foundationd (fake system) and a local web server standing in for the update source."""

    def setUp(self):
        super().setUp()
        import functools
        import http.server
        import json
        import shutil
        import threading
        from gi.repository import GLib as _GLib
        from foundation.gbackend import Backend
        from foundation_updater.app import UpdaterWindow
        self.json, self.glib = json, _GLib
        with open(self.system.paths["os_release"], "w") as handle:           # the running system is 0.2.0
            handle.write("ID=opensuse-tumbleweed\nIMAGE_ID=ecp\nIMAGE_VERSION=0.2.0\n")
        self.www = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.www, True)
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=self.www)
        handler.log_message = lambda *a, **k: None
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.set_config("ota.url", f"http://127.0.0.1:{self.server.server_address[1]}")
        global APP
        if APP is None:
            APP = Adw.Application(application_id="org.ersa.UpdaterTest")
            APP.register(None)
        self.Backend, self.UpdaterWindow = Backend, UpdaterWindow
        self.window = self.new_window()

    def new_window(self):
        window = self.UpdaterWindow(APP, self.Backend(connection=self.conn))
        self.addCleanup(window.destroy)
        return window

    def set_config(self, key, value):
        from test_foundationd_bus import CONFIG
        self.call(CONFIG, "Set", "(sv)", key, self.glib.Variant("b" if isinstance(value, bool) else "s", value))

    def get_config(self, key):
        from test_foundationd_bus import CONFIG
        return self.call(CONFIG, "Get", "(s)", key)[0]

    def feed(self, version, size=1024 ** 3):
        with open(os.path.join(self.www, f"latest-{os.uname().machine}.json"), "w") as handle:
            self.json.dump({"version": version, "file": f"ecp_{version}.tar.zst", "size": size}, handle)

    def pump(self, condition, timeout=30):
        context = GLib.MainContext.default()
        end = time.monotonic() + timeout
        while not condition():
            if time.monotonic() > end:
                self.fail("timed out waiting; the window shows " + str(self.window.stack.get_visible_child_name()))
            if not context.iteration(False):
                time.sleep(0.01)

    def page(self):
        return self.window.stack.get_visible_child_name()

    def check(self):
        self.pump(lambda: self.window.version_label.get_label() == "0.2.0")
        self.window.main_btn.emit("clicked")
        self.pump(lambda: self.window.mode != "checking")

    def test_home_shows_what_runs_and_offers_one_button(self):
        self.pump(lambda: self.window.version_label.get_label() == "0.2.0")
        self.assertIn("slot A", self.window.slot_label.get_label())
        self.assertEqual((self.window.mode, self.window.main_btn.get_label()), ("check", "Check for Updates"))

    def test_checking_without_an_update_source_says_so_and_changes_nothing(self):
        self.set_config("ota.url", "https://updates.ersa.invalid/ecp")
        self.check()
        self.assertIn("No update source", self.window.check_status.get_label())
        self.assertEqual(self.window.mode, "check")

    def test_a_newer_update_on_the_source_is_offered(self):
        self.feed("0.3.0")
        self.check()
        self.assertEqual((self.window.mode, self.window.main_btn.get_label()), ("update", "Update to 0.3.0"))
        self.assertIn("1.0 GB", self.window.check_status.get_label())

    def test_an_update_that_is_not_newer_says_up_to_date(self):
        self.feed("0.2.0")
        self.check()
        self.assertEqual((self.window.mode, self.window.main_btn.get_label()), ("check", "Check Again"))
        self.assertIn("up to date", self.window.check_status.get_label())

    def test_an_unreachable_source_is_reported_and_can_be_retried(self):
        self.set_config("ota.url", "http://127.0.0.1:1")
        self.check()
        self.assertIn("cannot reach", self.window.check_status.get_label())
        self.assertEqual((self.window.mode, self.window.main_btn.get_label()), ("check", "Try Again"))

    def test_what_a_background_check_found_is_shown_when_the_window_opens(self):
        directory = os.path.join(self.tmp.name, "state")
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, "update-check.json"), "w") as handle:
            self.json.dump({"version": "0.3.0", "size": 1024 ** 3, "checked": 1, "source": "x"}, handle)
        self.window = self.new_window()
        self.pump(lambda: self.window.mode == "update")
        self.assertEqual(self.window.main_btn.get_label(), "Update to 0.3.0")

    def test_the_two_switches_show_and_change_the_settings(self):
        self.pump(lambda: self.window.version_label.get_label() == "0.2.0")
        auto = self.window.lookup_action("auto-update")
        self.assertFalse(auto.get_state().get_boolean())
        auto.change_state(GLib.Variant.new_boolean(True))
        self.pump(lambda: auto.get_state().get_boolean())
        self.assertIs(self.get_config("ota.auto_update"), True)
        self.set_config("ota.auto_check", False)
        self.window.refresh()
        check = self.window.lookup_action("auto-check")
        self.pump(lambda: not check.get_state().get_boolean())

    def test_the_boot_slots_dialog_opens_with_the_apps_own_mark_for_the_running_slot(self):
        self.pump(lambda: self.window.version_label.get_label() == "0.2.0")
        self.window.show_slots()                    # builds the rows from Boot.GetSlots; must not need any icon the theme may lack
        self.pump(lambda: self.window.get_visible_dialog() is not None)
        self.assertIsNotNone(self.window.get_visible_dialog())

    def test_a_bad_local_update_fails_cleanly_through_dbus_and_changes_nothing(self):
        """The whole path: the file goes to foundationd as a descriptor, the job runs, and the window says why it failed."""
        self.pump(lambda: self.window.version_label.get_label() == "0.2.0")
        with tempfile.NamedTemporaryFile(suffix=".tar.zst", delete=False) as handle:
            handle.write(b"this is not an update")
        self.addCleanup(os.unlink, handle.name)
        before = self.uki_files()
        self.window.install(handle.name, True, False)
        self.assertEqual(self.page(), "working")
        self.pump(lambda: self.page() == "done")
        self.assertEqual(self.window.done.get_title(), "The update was not installed")
        self.assertFalse(self.window.restart_btn.get_visible())
        self.assertEqual(self.uki_files(), before)
        self.assertFalse(self.window.working)

    def test_installing_from_the_source_downloads_in_the_daemon_and_an_unsigned_update_is_refused(self):
        """Update to X: Updates.InstallLatest downloads and verifies; with no trusted signing key installed the signature check fails."""
        support.write_payload(self.www, "0.3.0")
        with open(os.path.join(self.www, f"latest-{os.uname().machine}.json"), "w") as handle:
            self.json.dump({"version": "0.3.0", "file": f"usr/share/ersa/payload/ecp_0.3.0-{os.uname().machine}.tar"}, handle)
        self.check()
        self.assertEqual(self.window.mode, "update")
        before = self.uki_files()
        self.window.install_latest()
        self.assertEqual(self.page(), "working")
        self.pump(lambda: self.page() == "done")
        self.assertEqual(self.window.done.get_title(), "The update was not installed")
        self.assertIn("signing key", self.window.done.get_description())
        self.assertEqual(self.uki_files(), before)
        self.assertEqual(os.listdir(os.path.join(self.tmp.name, "state", "staging")), [], "the download is removed")


if __name__ == "__main__":
    unittest.main()
