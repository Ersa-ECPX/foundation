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
        self.assertEqual(model.classify_source("https://updates.example.org/u.tar.zst"), ("url", None))
        self.assertEqual(model.classify_source("http://10.0.2.2:8000/u.tar.zst"), ("url", None))
        self.assertEqual(model.classify_source("http://updates.example.org/u.tar.zst")[0], "")
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

    def test_the_feed(self):
        text = '{"version": "0.1.1", "file": "ecp_0.1.1-x86_64.tar.zst", "size": 1610612736, "sha256": "' + "ab" * 32 + '"}'
        found = model.parse_feed(text, "http://10.0.2.2:8000/", "x86_64")
        self.assertEqual(found, {"version": "0.1.1", "url": "http://10.0.2.2:8000/ecp_0.1.1-x86_64.tar.zst",
                                 "size": 1610612736, "sha256": "ab" * 32})
        self.assertEqual(model.size_text(found["size"]), "1.5 GB")
        self.assertEqual(model.feed_url("https://u.example/ecp/", "aarch64"), "https://u.example/ecp/latest-aarch64.json")

    def test_a_bad_feed_is_refused(self):
        base = "https://u.example/ecp"
        for text, why in (("not json", "valid"), ("[]", "name a version"), ('{"version": "1"}', "name a version"),
                          ('{"version": "1", "file": "a", "arch": "aarch64"}', "aarch64"),
                          ('{"version": "1", "file": "http://evil.example/a"}', "not allowed"),
                          ('{"version": "1", "file": "a", "sha256": "xyz"}', "checksum")):
            with self.assertRaisesRegex(model.FeedError, why):
                model.parse_feed(text, base, "x86_64")

    def test_versions_compare_number_by_number(self):
        self.assertTrue(model.is_newer("0.1.10", "0.1.9"))
        self.assertTrue(model.is_newer("0.2.0", "0.1.9"))
        self.assertFalse(model.is_newer("0.1.0", "0.1.0"))
        self.assertFalse(model.is_newer("0.0.9", "0.1.0"))
        self.assertTrue(model.is_newer("0.1.0", ""))

    def test_the_placeholder_update_source_is_recognised(self):
        self.assertTrue(model.is_placeholder("https://updates.ersa.invalid/ecp"))   # an unset or fake source
        self.assertTrue(model.is_placeholder(""))
        self.assertFalse(model.is_placeholder("https://updates.example.org/ecp"))

    def test_no_internal_acronym_in_user_text(self):
        self.assertNotIn("ECP", model.PRODUCT)
        self.assertEqual(model.PRODUCT, "Ersa Compute Platform")


@unittest.skipUnless(DISPLAY, "set ERSA_GUI_TESTS=1 and run on a virtual display (see the top of this file)")
class Window(BootDaemon):
    def setUp(self):
        super().setUp()
        from foundation.gbackend import Backend
        from foundation_updater.app import UpdaterWindow
        global APP
        if APP is None:
            APP = Adw.Application(application_id="org.ersa.UpdaterTest")
            APP.register(None)
        self.window = UpdaterWindow(APP, Backend(connection=self.conn))
        self.addCleanup(self.window.destroy)

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

    def test_home_shows_what_runs_and_offers_one_button(self):
        self.pump(lambda: self.window.version_label.get_label() == "0.2.0")
        self.assertIn("slot A", self.window.slot_label.get_label())
        self.assertEqual((self.window.mode, self.window.main_btn.get_label()), ("check", "Check for Updates"))

    def test_checking_without_an_update_server_says_so_and_changes_nothing(self):
        self.pump(lambda: self.window.version_label.get_label() == "0.2.0")
        self.window.main_btn.emit("clicked")
        self.pump(lambda: self.window.mode != "checking")
        self.assertIn("No update server", self.window.check_status.get_label())
        self.assertEqual(self.window.mode, "check")

    def test_what_the_feed_says_decides_the_button(self):
        self.pump(lambda: self.window.version_label.get_label() == "0.2.0")
        self.window._checked({"version": "0.3.0", "url": "https://u.example/a", "size": 1024 ** 3, "sha256": ""}, "")
        self.assertEqual((self.window.mode, self.window.main_btn.get_label()), ("update", "Update to 0.3.0"))
        self.assertIn("1.0 GB", self.window.check_status.get_label())
        self.window._checked({"version": "0.2.0", "url": "https://u.example/a", "size": 0, "sha256": ""}, "")
        self.assertEqual((self.window.mode, self.window.main_btn.get_label()), ("check", "Check Again"))
        self.assertIn("up to date", self.window.check_status.get_label())
        self.window._checked(None, "cannot reach the server")
        self.assertEqual(self.window.main_btn.get_label(), "Try Again")

    def test_a_bad_update_fails_cleanly_through_dbus_and_changes_nothing(self):
        """The whole path: the file goes to foundationd as a descriptor, the job runs, and the window says why it failed."""
        self.pump(lambda: self.window.version_label.get_label() == "0.2.0")
        with tempfile.NamedTemporaryFile(suffix=".tar.zst", delete=False) as handle:
            handle.write(b"this is not an update")
        self.addCleanup(os.unlink, handle.name)
        before = self.uki_files()
        self.window.begin(handle.name, "", True, False)
        self.assertEqual(self.page(), "working")
        self.pump(lambda: self.page() == "done")
        self.assertEqual(self.window.done.get_title(), "The update was not installed")
        self.assertFalse(self.window.restart_btn.get_visible())
        self.assertEqual(self.uki_files(), before)
        self.assertFalse(self.window.working)


if __name__ == "__main__":
    unittest.main()
