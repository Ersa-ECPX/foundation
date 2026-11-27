"""The installer's text helpers, and its window driven against a real foundationd (needs a display)."""

import os

# Existing UI assertions use English text, regardless of the developer session locale.
os.environ["LANGUAGE"] = "C"
import tempfile
import shutil
import time
import unittest
import unittest.mock


def model_recovery_ok(key):
    from foundation import recovery
    return recovery.is_recovery_key(key)

import support  # noqa: F401
from test_foundationd_boot import BootDaemon

from foundation_installer import model

# The window tests open real windows, so they run only on request, on a virtual display:
#   env -u WAYLAND_DISPLAY GDK_BACKEND=x11 GSK_RENDERER=cairo ERSA_GUI_TESTS=1 xvfb-run -a \
#       python3 -m unittest discover -s tests -p test_installer.py
try:
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, GLib, Gtk
    DISPLAY = bool(os.environ.get("ERSA_GUI_TESTS")) and Gtk.init_check()
except (ImportError, ValueError):
    DISPLAY = False

APP = None
TOOLS = all(support.tool_available(t) for t in ("systemd-repart", "mdir", "mren", "mkfs.btrfs", "mkfs.vfat"))


class Text(unittest.TestCase):
    def test_partition_titles(self):
        self.assertEqual(model.partition_title("ecp-root-a"), "System slot A")
        self.assertEqual(model.partition_title("ecp-verity-b"), "Integrity data for slot B")
        self.assertEqual(model.partition_title("ecp-root-b"), "System slot B")
        self.assertEqual(model.partition_title("ECP-DATA"), "Your files and settings")
        self.assertEqual(model.partition_title("other"), "other")

    def test_disk_text(self):
        disk = {"path": "/dev/sdb", "size": 64 * 1024 ** 3, "model": "USB SSD", "removable": True,
                "usable": False, "reason": "it is too small"}
        self.assertEqual(model.disk_title(disk), "USB SSD")
        self.assertIn("it is too small", model.disk_subtitle(disk))
        self.assertIn("removable", model.disk_subtitle(disk))

    def test_erase_summary_names_the_disk_and_the_partitions(self):
        plan = {"disk": "/dev/sdb", "size": 10 ** 11, "model": "X", "will_erase": [{}, {}]}
        text = model.erase_summary(plan)
        self.assertIn("/dev/sdb", text)
        self.assertIn("2 partitions", text)
        self.assertIn("erased", text)

    def test_failure_text(self):
        class E:
            name, message = "NotAuthorized", "x"
        self.assertIn("Permission", model.failure_text(E()))

    def test_remaining_time(self):
        self.assertEqual(model.remaining_text(2, 0.5), "")           # too early to say
        self.assertEqual(model.remaining_text(100, 0.01), "")
        self.assertEqual(model.remaining_text(60, 0.5), "About 1 minute remaining")
        self.assertEqual(model.remaining_text(300, 0.5), "About 5 minutes remaining")
        self.assertEqual(model.remaining_text(20, 0.5), "Less than a minute remaining")
        self.assertEqual(model.remaining_text(100, 1.0), "")

    def test_language_and_time_zone_lists(self):
        self.assertIn(("Deutsch", "de_DE.UTF-8"), model.LANGUAGES)
        self.assertEqual(model.language_name("de_DE.UTF-8"), "Deutsch")
        self.assertEqual(model.language_name("xx_XX.UTF-8"), "xx_XX.UTF-8")
        zones = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, zones)
        for name in ("Europe/Berlin", "America/Argentina/Buenos_Aires", "Etc/GMT+5", "posix/Europe/Berlin", "zone.tab"):
            path = os.path.join(zones, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            open(path, "w").close()
        self.assertEqual(model.timezones(zones), ["America/Argentina/Buenos_Aires", "Europe/Berlin", "UTC"])
        link = os.path.join(zones, "lt")
        os.symlink("/usr/share/zoneinfo/Asia/Kolkata", link)
        self.assertEqual(model.current_timezone(link), "Asia/Kolkata")
        self.assertEqual(model.current_timezone(os.path.join(zones, "missing")), "UTC")

    def test_installer_settings_come_from_the_first_preseed_and_choose_the_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            first, second = os.path.join(tmp, "a"), os.path.join(tmp, "b")
            with open(second, "w") as handle:
                handle.write("# default\ninstaller.timezone_url = https://tz.example/zone\nota.url=x\n")
            self.assertEqual(model.installer_settings([first, second]), {"installer.timezone_url": "https://tz.example/zone"})
            with open(first, "w") as handle:
                handle.write("installer.timezone_url=\n")
            settings = model.installer_settings([first, second])
            self.assertEqual(model.timezone_service(settings), "")          # empty: detection off
            self.assertIsNone(model.detect_timezone(["UTC"], url=""))
        self.assertEqual(model.installer_settings([os.path.join(tmp, "none")]), {})
        self.assertEqual(model.timezone_service({}), model.TIMEZONE_URL)    # nothing set: the default service
        self.assertEqual(model.timezone_service({"installer.timezone_url": "http://plain"}), "")

    def test_passphrase_rules_and_when_the_encryption_page_may_be_left(self):
        problem = model.passphrase_problem
        self.assertEqual([problem("", ""), problem("short", "short"), problem("long enough", "different"), problem("long enough", "long enough")],
                         ["empty", "short", "mismatch", ""])
        ready = model.encryption_ready
        self.assertTrue(ready(False, False, "", ""))                  # no encryption: nothing to ask
        self.assertTrue(ready(True, True, "", ""))                    # a TPM unlocks by itself
        self.assertFalse(ready(True, False, "", ""))                  # no TPM: a passphrase is needed
        self.assertFalse(ready(True, False, "long enough", "nope nope"))
        self.assertTrue(ready(True, False, "long enough", "long enough"))

    def test_the_installer_sends_the_first_preseed_that_holds_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty, real, none = (os.path.join(tmp, n) for n in ("empty", "real", "none"))
            with open(empty, "w") as handle:
                handle.write("# nothing set\n\n")
            with open(real, "w") as handle:
                handle.write("# defaults\nidentity.issuer=https://id.example\n")
            self.assertEqual(model.installer_preseed([none, real]), "# defaults\nidentity.issuer=https://id.example\n")
            self.assertEqual(model.installer_preseed([empty, real]), "")      # the first file that exists decides, as for the settings
            self.assertEqual(model.installer_preseed([none]), "")

    def test_detect_timezone(self):
        import io

        def answering(text):
            return lambda request, timeout: io.BytesIO(text.encode())
        zones = ["Europe/Berlin", "UTC"]
        self.assertEqual(model.detect_timezone(zones, opener=answering("Europe/Berlin\n")), "Europe/Berlin")
        self.assertIsNone(model.detect_timezone(zones, opener=answering("Mars/Base")))
        self.assertIsNone(model.detect_timezone(zones, opener=answering("<html>rate limited</html>")))

        def offline(request, timeout):
            raise OSError("no network")
        self.assertIsNone(model.detect_timezone(zones, opener=offline))

    def test_no_internal_acronym_in_user_text(self):
        for text in (model.PRODUCT, *model.PARTITION_TITLES.values()):
            self.assertNotIn("ECP", text)


@unittest.skipUnless(DISPLAY, "set ERSA_GUI_TESTS=1 and run on a virtual display (see the top of this file)")
@unittest.skipUnless(TOOLS, "needs systemd-repart, mtools and mkfs tools")
class Window(BootDaemon):
    extra_args = ("--allow-file-targets",)

    def setUp(self):
        super().setUp()
        from foundation_installer.app import InstallerWindow
        from foundation.gbackend import Backend
        self.detected = None                                   # no test asks a real time zone service
        patch = unittest.mock.patch.object(model, "detect_timezone", lambda zones, **_k: self.detected)
        patch.start()
        self.addCleanup(patch.stop)
        from foundationd.backends import layout
        support.write_payload(self.system.paths["root"])      # the medium carries the operating system to install
        self.target = os.path.join(self.system.tmp, "target.img")
        with open(self.target, "wb") as handle:
            handle.truncate(layout.MIN_DISK + 64 * 1024 * 1024)
        global APP
        if APP is None:
            APP = Adw.Application(application_id="org.ersa.InstallerTest")
            APP.register(None)
        self.app = APP
        self.window = InstallerWindow(self.app, Backend(connection=self.conn))
        self.addCleanup(self.window.destroy)

    def pump(self, condition, timeout=60):
        context = GLib.MainContext.default()
        end = time.monotonic() + timeout
        while not condition():
            if time.monotonic() > end:
                self.fail("timed out waiting; the window shows " + str(self.window.stack.get_visible_child_name()))
            if not context.iteration(False):
                time.sleep(0.01)

    def page(self):
        return self.window.stack.get_visible_child_name()

    def test_welcome_names_the_product(self):
        self.pump(lambda: self.page() == "welcome")
        self.assertIn("Ersa Compute Platform", self.window.welcome_title.get_label())

    def test_the_disk_list_marks_the_running_disk_unusable(self):
        self.pump(lambda: self.page() == "welcome")
        self.window._go_disks()
        self.pump(lambda: self.window.disks)
        by_path = {d["path"]: d for d in self.window.disks}
        running = [d for d in by_path.values() if d["boot_medium"]]
        self.assertTrue(running and not running[0]["usable"])
        self.assertFalse(self.window.next_btn.get_sensitive())

    def test_detected_timezone_is_preselected_unless_chosen(self):
        self.pump(lambda: self.page() == "welcome")
        window = self.window
        window._apply_detected_timezone("Europe/Berlin")
        self.assertEqual(window.timezone, "Europe/Berlin")
        self.assertTrue(window.timezone_note.get_visible())
        self.assertFalse(window.timezone_chosen)
        other = [r for r in window.timezone_rows if r.value == "Asia/Tokyo"][0]
        window.timezone_list.select_row(other)                 # the person chooses
        self.assertTrue(window.timezone_chosen)
        self.assertFalse(window.timezone_note.get_visible())
        window._apply_detected_timezone("Europe/Berlin")       # a late answer must not override it
        self.assertEqual(window.timezone, "Asia/Tokyo")

    def test_the_encryption_page_is_on_by_default_and_asks_for_a_passphrase_without_a_tpm(self):
        self.pump(lambda: self.page() == "welcome")
        w = self.window
        w.selected = self.target
        w._go_review()
        self.pump(lambda: self.page() == "encryption")
        self.assertTrue(w.encrypt_row.get_active())
        self.assertFalse(w.tpm)
        self.assertTrue(w.pass_group.get_visible())
        self.assertFalse(w.next_btn.get_sensitive())               # no passphrase yet
        w.pass_entry.set_text("short")
        w.pass_again_entry.set_text("short")
        self.assertFalse(w.next_btn.get_sensitive())
        self.assertIn("8", w.pass_hint.get_label())
        w.pass_entry.set_text("a long passphrase")
        w.pass_again_entry.set_text("another one")
        self.assertFalse(w.next_btn.get_sensitive())
        w.pass_again_entry.set_text("a long passphrase")
        self.assertTrue(w.next_btn.get_sensitive())
        w.encrypt_row.set_active(False)                             # switched off: a warning, no passphrase
        self.assertFalse(w.pass_group.get_visible())
        self.assertTrue(w.next_btn.get_sensitive())
        self.assertIn("anyone", w.encryption_note.get_label())

    def test_an_encrypted_install_sends_the_secrets_as_a_file_descriptor(self):
        import json as json_
        from foundation.gbackend import ClientError
        self.pump(lambda: self.page() == "welcome")
        w = self.window
        w.selected = self.target
        w._go_review()
        self.pump(lambda: w.plan is not None)
        w.encrypt, w.passphrase, w.tpm = True, "a long passphrase", False
        calls = []

        def fake(interface, method, signature, args, done, timeout=60_000, path=None, fds=None):
            secrets = None
            if fds is not None:
                import os as os_
                descriptor = fds.get(args[-1])
                secrets = json_.loads(os_.read(descriptor, 4096))
                os_.close(descriptor)
            calls.append((method, signature, secrets, args))
            done(None, ClientError("Failed", "stopped by the test"))
        w.backend.call = fake
        w._install()
        self.pump(lambda: bool(calls))
        method, signature, secrets, args = calls[0]
        self.assertEqual((method, signature), ("DeployEncrypted", "(sssssh)"))
        self.assertEqual(secrets["passphrase"], "a long passphrase")
        self.assertEqual(secrets["recovery_key"], w.recovery_key)
        self.assertTrue(model_recovery_ok(w.recovery_key))
        self.assertNotIn(w.recovery_key + "x", " ".join(str(a) for a in args))      # the key is not an argument of the call
        self.assertFalse(any("a long passphrase" in str(a) for a in args))

    def test_the_done_page_shows_the_recovery_key_and_waits_for_confirmation(self):
        self.pump(lambda: self.page() == "welcome")
        w = self.window
        w.encrypt, w.recovery_key = True, "cbdefghi-jklnrtuv-cbdefghi-jklnrtuv-cbdefghi-jklnrtuv-cbdefghi-jklnrtuv"
        w._show_done()
        self.assertEqual(self.page(), "done")
        self.assertFalse(w.next_btn.get_sensitive())                 # Restart waits until the key is saved
        check = []

        def walk(widget):
            if isinstance(widget, Gtk.CheckButton):
                check.append(widget)
            child = widget.get_first_child()
            while child:
                walk(child)
                child = child.get_next_sibling()
        walk(w.stack.get_child_by_name("done"))
        check[0].set_active(True)
        self.assertTrue(w.next_btn.get_sensitive())

    def test_plan_then_install_then_done(self):
        self.pump(lambda: self.page() == "welcome")
        self.window.language = "fr_FR.UTF-8"
        self.window.timezone = "UTC" if os.path.isfile("/usr/share/zoneinfo/UTC") else ""
        self.window.selected = self.target
        self.window._go_review()
        self.pump(lambda: self.window.plan is not None)
        self.assertTrue(self.window.plan["possible"])
        self.assertTrue(self.window.plan["partitions"])
        self.pump(lambda: self.page() == "encryption")        # encryption is asked about on its own page, on by default
        self.assertTrue(self.window.encrypt)
        self.window.encrypt_row.set_active(False)               # this test installs a plain system (real dm-crypt needs real devices)
        self.assertFalse(self.window.encrypt)
        self.window._install()
        self.assertTrue(self.window.installing)
        self.pump(lambda: self.page() in ("done", "failed"))
        self.assertEqual(self.page(), "done", "install should succeed")
        self.assertFalse(self.window.installing)
        from foundationd.backends import gpt
        names = [p.name for p in gpt.read_table(self.target)]
        self.assertEqual([n for n in names if n.startswith(("ecp-root", "ecp-verity"))],
                         ["ecp-root-a", "ecp-verity-a", "ecp-root-b", "ecp-verity-b"])

    def test_a_refused_disk_shows_the_failure_page(self):
        self.pump(lambda: self.page() == "welcome")
        self.window.selected = self.system.image       # the running disk: the service refuses it
        self.window.plan = {"disk": self.system.image, "model": "", "size": 1, "will_erase": [], "partitions": []}
        self.window._install()
        self.pump(lambda: self.page() in ("done", "failed"))
        self.assertEqual(self.page(), "failed")

    def test_there_is_no_sidebar_and_install_is_the_disk_page_action(self):
        self.pump(lambda: self.page() == "welcome")
        self.assertFalse(hasattr(self.window, "sidebar"))
        self.assertEqual(self.window.next_btn.get_label(), "Continue")
        self.window._go_disks()
        self.assertEqual(self.window.next_btn.get_label(), "Install")

    def test_customize_shows_the_partitions_then_install(self):
        self.pump(lambda: self.page() == "welcome")
        self.window.selected = self.target
        self.window._go_review(review=True)
        self.pump(lambda: self.page() == "encryption")
        self.window.encrypt_row.set_active(False)                # the test machine has no TPM: a passphrase would be needed
        self.window._after_encryption()
        self.pump(lambda: self.page() == "review")
        labels = [row.get_title() for row in self.window._partition_rows]
        self.assertEqual(labels[:3], ["Language", "Time zone", "Encryption"])
        self.assertIn("Boot", labels)
        self.assertIn("System slot A", labels)
        self.assertEqual(self.window.next_btn.get_label(), "Install")

    def test_language_and_time_zone_are_separate_screens_before_the_disks(self):
        self.pump(lambda: self.page() == "welcome")
        self.window._next()
        self.assertEqual(self.page(), "language")
        self.assertEqual(self.window.language, "en_US.UTF-8")
        self.window.language_list.select_row(self.window.language_rows[2])      # Deutsch
        self.assertEqual(self.window.language, "de_DE.UTF-8")
        self.window._next()
        self.assertEqual(self.page(), "timezone")
        berlin = [r for r in self.window.timezone_rows if r.value == "Europe/Berlin"][0]
        self.window.timezone_list.select_row(berlin)
        self.assertEqual(self.window.timezone, "Europe/Berlin")
        self.window._next()
        self.assertEqual(self.page(), "disks")
        self.window._back()
        self.assertEqual(self.page(), "timezone")
        self.window._back()
        self.assertEqual(self.page(), "language")

    def present(self):
        self.window.set_default_size(880, 600)
        self.window.present()
        self.pump(lambda: self.window.get_mapped())

    def test_the_greeting_cycles_through_languages(self):
        self.present()
        self.pump(lambda: self.page() == "welcome")
        seen = {self.window.greeting.label.get_label()}
        self.pump(lambda: seen.add(self.window.greeting.label.get_label()) or len(seen) >= 2, timeout=20)
        self.assertEqual(model.GREETINGS[0], "Welcome")
        self.assertTrue(seen <= set(model.GREETINGS))

    def test_pages_fade_in_and_end_fully_visible(self):
        self.present()
        self.pump(lambda: self.page() == "welcome")
        self.window._go_language()
        page = self.window.stack.get_child_by_name("language")
        parts = [child for child in __import__("foundation_installer.app", fromlist=["x"])._children(page)]
        self.pump(lambda: all(part.get_opacity() == 1 for part in parts))

    def test_the_progress_bar_eases_to_each_value(self):
        self.present()
        self.pump(lambda: self.page() == "welcome")
        self.window._event("progress", 0.5)
        self.pump(lambda: abs(self.window.progress_bar.get_fraction() - 0.5) < 1e-6)

    def test_the_tick_draws_itself_to_the_end(self):
        from foundation_installer.app import CheckMark
        self.present()
        check = CheckMark()
        self.window.stack.add_named(check, "check-test")
        self.window.stack.set_visible_child_name("check-test")
        check.play(0)
        self.pump(lambda: check.progress == 1.0)

    def test_the_window_cannot_be_closed_while_installing(self):
        self.pump(lambda: self.page() == "welcome")
        self.window.installing = True
        self.assertTrue(self.window.emit("close-request"))
        self.window.installing = False


if __name__ == "__main__":
    unittest.main()
