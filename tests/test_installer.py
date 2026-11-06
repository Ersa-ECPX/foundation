"""The installer's text helpers, and its window driven against a real foundationd (needs a display)."""

import os
import time
import unittest

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
        self.assertEqual(model.partition_title("ecp-root_0.1.0"), "System slot A")
        self.assertEqual(model.partition_title("_empty"), "System slot B")
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
        from foundation_installer.backend import Backend
        from foundationd.backends import layout
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
        self.assertIn("Ersa Compute Platform", self.window.welcome_text.get_title())

    def test_the_disk_list_marks_the_running_disk_unusable(self):
        self.pump(lambda: self.page() == "welcome")
        self.window._go_disks()
        self.pump(lambda: self.window.disks)
        by_path = {d["path"]: d for d in self.window.disks}
        running = [d for d in by_path.values() if d["boot_medium"]]
        self.assertTrue(running and not running[0]["usable"])
        self.assertFalse(self.window.next_btn.get_sensitive())

    def test_plan_then_install_then_done(self):
        self.pump(lambda: self.page() == "welcome")
        self.window.selected = self.target
        self.window._go_review()
        self.pump(lambda: self.window.plan is not None)
        self.assertTrue(self.window.plan["possible"])
        self.assertTrue(self.window.plan["partitions"])
        self.window._install()
        self.assertTrue(self.window.installing)
        self.pump(lambda: self.page() in ("done", "failed"))
        self.assertEqual(self.page(), "done", "install should succeed")
        self.assertFalse(self.window.installing)
        from foundationd.backends import gpt
        self.assertIn("ecp-root_0.2.0", [p.name for p in gpt.read_table(self.target)])

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
        self.pump(lambda: self.page() == "review")
        labels = [row.get_title() for row in self.window._partition_rows]
        self.assertEqual(labels[0], "Boot")
        self.assertIn("System slot A", labels)
        self.assertEqual(self.window.next_btn.get_label(), "Install")

    def test_the_window_cannot_be_closed_while_installing(self):
        self.pump(lambda: self.page() == "welcome")
        self.window.installing = True
        self.assertTrue(self.window.emit("close-request"))
        self.window.installing = False


if __name__ == "__main__":
    unittest.main()
