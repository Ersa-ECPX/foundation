"""Deck's text and decisions (no display needed)."""

import os
import time
import unittest

import support  # noqa: F401
from test_foundationd_boot import BootDaemon
from test_foundationd_bus import CONFIG

from foundation_deck import model

# The window test opens a real window, so it runs only on request, on a virtual display:
#   env -u WAYLAND_DISPLAY GDK_BACKEND=x11 GSK_RENDERER=cairo ERSA_GUI_TESTS=1 xvfb-run -a \
#       python3 -m unittest discover -s tests -p test_deck.py
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
    def test_identity_view_states_and_initials(self):
        from foundation import identity
        view = model.identity_view
        base = {"identity.issuer": "https://id.example/", "identity.client_id": "c"}
        self.assertEqual(view(identity.settings_from({**base, "identity.enabled": False}), None)["state"], "off")
        self.assertEqual(view(identity.settings_from({}), None)["state"], "unset")
        self.assertEqual(view(identity.settings_from(base), None)["state"], "out")
        signed = lambda **claims: view(identity.settings_from(base), {"claims": claims})
        self.assertEqual((signed(name="Ada Lovelace", email="a@x.org")["initials"], signed(name="Ada Lovelace")["state"]), ("AL", "in"))
        self.assertEqual((signed(email="grace.hopper@x.org")["name"], signed(email="grace.hopper@x.org")["initials"]), ("grace.hopper", "GH"))
        self.assertEqual((signed()["name"], signed()["initials"]), ("Signed in", ""))
        self.assertEqual(signed(name="Ada", email="a@x.org")["provider"], "id.example")


    def test_slot_actions(self):
        base = {"name": "b", "bootable": True, "running": False, "default": False}
        self.assertEqual(model.slot_actions(base), ["once", "default"])
        self.assertEqual(model.slot_actions({**base, "default": True}), ["once"])
        self.assertEqual(model.slot_actions({**base, "running": True}), [])
        self.assertEqual(model.slot_actions({**base, "bootable": False}), [])
        self.assertEqual(model.slot_actions({**base, "live": True}), [])

    def test_confirmations_say_what_happens(self):
        heading, body = model.slot_confirm({"name": "b"}, "default")
        self.assertEqual(heading, "Always start Slot B?")
        self.assertIn("not rolled back", body)
        self.assertIn("once", model.slot_confirm({"name": "b"}, "once")[0])

    def test_bundles(self):
        self.assertEqual(model.bundle_subtitle({"name": "dev", "packages": ["a", "b"], "enabled": True, "state": "ready"}), "2 packages")
        self.assertEqual(model.bundle_subtitle({"packages": ["a"], "enabled": False}), "1 package · turned off")
        self.assertIn("needs rebuild", model.bundle_subtitle({"packages": [], "state": "needs_rebuild"}))
        self.assertIn("turned off: broke", model.extension_subtitle({"version": "1", "enabled": False, "problems": ["broke"]}))

    def test_catalog_notice(self):
        self.assertEqual(model.catalog_notice({}), "")
        text = model.catalog_notice({"notice": "Not free software.", "needs_enabling": ["packman"]})
        self.assertIn("Not free software.", text)
        self.assertIn("packman", text)

    def test_update_status(self):
        self.assertEqual(model.update_status({}, {})[0], "")
        self.assertIn("1.2.0", model.update_status({"available": True, "version": "1.2.0", "size": 2 * 1024 ** 3}, {})[1])
        self.assertIn("Restart", model.update_status({}, {"version": "1.2.0"})[1])

    def test_package_lists(self):
        self.assertEqual(model.package_names(["b-1.0", "a-2"]), ["a-2", "b-1.0"])
        self.assertEqual(model.package_names([{"name": "z"}, {"name": "y"}]), ["y", "z"])
        self.assertEqual(model.package_names(None), [])
        self.assertEqual(model.shorten("Full FFmpeg libraries. More text here."), "Full FFmpeg libraries")
        self.assertTrue(model.shorten("x" * 200).endswith("\u2026"))

    def test_about_rows_skip_what_is_unknown(self):
        rows = model.about_rows({"os": {"name": "Ersa", "image_version": "1.0.0"}, "architecture": "x86_64"}, {"developer": False})
        self.assertEqual(rows, [("Name", "Ersa"), ("Version", "1.0.0"), ("Architecture", "x86_64"), ("Developer mode", "Off")])
        self.assertEqual(model.about_rows({}, {}), [])

    def test_repositories(self):
        repo = {"alias": "p", "name": "Packman", "url": "https://x", "signed": True, "shipped": False, "refreshed_seconds_ago": 7200}
        self.assertEqual(model.repo_title(repo), "Packman")
        self.assertIn("added by you", model.repo_subtitle(repo))
        self.assertIn("2 hours ago", model.repo_subtitle(repo))
        self.assertIn("never refreshed", model.repo_subtitle({**repo, "refreshed_seconds_ago": -1}))
        self.assertIn("not signed", model.repo_subtitle({**repo, "signed": False}))
        key = "-----BEGIN PGP PUBLIC KEY BLOCK-----"
        self.assertEqual(model.repo_form_problem("a", "https://x", key), "")
        self.assertIn("https", model.repo_form_problem("a", "http://x", key))
        self.assertIn("name", model.repo_form_problem("", "https://x", key))
        self.assertIn("key", model.repo_form_problem("a", "https://x", ""))
        self.assertEqual(model.alias_from("My Repo (x)"), "my-repo-x")

    def test_remote(self):
        self.assertEqual(model.remote_summary({"installed": False})[0], "Not available")
        self.assertEqual(model.remote_summary({"installed": True, "active": True, "firewall": "open"}), ("On", "the firewall allows it"))
        self.assertEqual(model.remote_summary({"installed": True, "enabled": True})[0], "On at the next start")
        self.assertEqual(model.remote_summary({"installed": True})[0], "Off")
        self.assertEqual(model.key_form_problem("me", "ssh-ed25519 AAAA me@host"), "")
        self.assertTrue(model.key_form_problem("me", "garbage"))
        self.assertTrue(model.key_form_problem("", "ssh-ed25519 AAAA"))

    def test_advanced_and_activity(self):
        self.assertEqual((model.config_group("ota.url"), model.config_title("auto_update")), ("Updates", "Auto update"))
        self.assertEqual(model.config_group("system.developer_mode"), "System")
        self.assertEqual(model.job_title("updates.install"), "Installed a system update")
        self.assertEqual(model.job_title("odd.thing_here"), "Odd thing here")
        self.assertEqual(model.job_subtitle({"State": "failed", "ErrorMessage": "no space"}), "Failed: no space")
        self.assertEqual(model.job_subtitle({"State": "done"}), "Done")

    def test_extensions_and_slots(self):
        ok, text = model.inspect_summary({"name": "htop", "version": "3", "compatible": True, "notes": ["note"], "scriptlets": True})
        self.assertTrue(ok)
        self.assertIn("note", text)
        self.assertIn("not run", text)
        ok, text = model.inspect_summary({"name": "x", "compatible": False, "missing_requires": ["libfoo"]})
        self.assertFalse(ok)
        self.assertIn("Missing: libfoo", text)
        self.assertTrue(model.classify_rpm("a.txt"))
        self.assertEqual(model.classify_rpm("a.rpm"), "")
        self.assertTrue(model.slot_can_release({"name": "b", "version": "1"}))
        self.assertFalse(model.slot_can_release({"name": "a", "version": "1", "running": True}))
        self.assertFalse(model.slot_can_release({"name": "b", "version": ""}))

    def test_sections_are_grouped_like_a_settings_app(self):
        self.assertEqual([h for h, _p in model.SECTIONS], [None, "Account", "Software", "System", "History"])
        software = dict(model.SECTIONS)["Software"]
        self.assertEqual([p[0] for p in software], ["updates", "bundles", "repositories", "appsources", "packages"])

    def test_failed_update_and_search(self):
        slots = [{"name": "a", "running": True, "version": "1.0.0", "entry": "e", "tries_left": -1},
                 {"name": "b", "running": False, "version": "1.1.0", "entry": "e", "tries_left": 0}]
        self.assertEqual(model.failed_update(slots), "1.1.0")
        slots[1]["tries_left"] = 2
        self.assertEqual(model.failed_update(slots), "")
        self.assertIn("1.1.0", model.rollback_text("1.1.0"))
        self.assertTrue(model.page_matches("remote", "Remote Access", "ssh"))
        self.assertTrue(model.page_matches("remote", "Remote Access", "ssh keys"))
        self.assertFalse(model.page_matches("slots", "Boot Slots", "ssh"))
        self.assertTrue(model.page_matches("slots", "Boot Slots", ""))
        self.assertIn("UEFI", model.splash_text({"uefi": False}))
        self.assertEqual(model.splash_text({"uefi": True}), "")
        self.assertIn("not available", model.error_text("Failed", "internal error: FileNotFoundError"))

    def test_software_rows(self):
        self.assertEqual(model.software_rows("0.1.0", ""), [("Deck", "0.1.0")])
        self.assertEqual(model.software_rows("0.1.0", "1")[1], ("foundationd", "1"))

    def test_ssh_needs_a_way_in(self):
        self.assertTrue(model.ssh_needs_a_way_in(False, False))
        self.assertFalse(model.ssh_needs_a_way_in(True, False))
        self.assertFalse(model.ssh_needs_a_way_in(False, True))

    def test_application_sources_text_and_form(self):
        remote = {"name": "acme", "title": "Acme", "url": "https://a/repo", "signed": True, "apps": 2, "default": False}
        self.assertEqual(model.remote_subtitle(remote), "https://a/repo \u00b7 signed \u00b7 2 apps installed")
        self.assertIn("comes with the system", model.remote_subtitle({**remote, "default": True, "apps": 1}))
        self.assertIn("no apps installed", model.remote_subtitle({**remote, "apps": 0}))
        key = "-----BEGIN PGP PUBLIC KEY BLOCK-----"
        self.assertEqual(model.remote_form_problem("acme", "https://a/acme.flatpakrepo", ""), "")
        self.assertEqual(model.remote_form_problem("acme", "https://a/repo", key), "")
        self.assertIn("signing key", model.remote_form_problem("acme", "https://a/repo", ""))
        self.assertIn("https", model.remote_form_problem("acme", "http://a/x.flatpakrepo", ""))
        self.assertIn("short name", model.remote_form_problem("", "https://a/x.flatpakrepo", ""))
        self.assertIn("short name", model.remote_form_problem("a b", "https://a/x.flatpakrepo", ""))
        self.assertEqual(model.suggest_remote_name("https://dl.acme.example/acme.flatpakrepo"), "acme")
        text = model.remote_trust_text({"title": "Acme", "url": "https://a/repo", "fingerprints": ["AB12"], "owners": ["Acme <a@b>"]})
        for word in ("Acme", "https://a/repo", "AB12", "Acme <a@b>", "Check the fingerprint"):
            self.assertIn(word, text)

    def test_pages(self):
        self.assertEqual(len(model.PAGES), len({p[0] for p in model.PAGES}))
        self.assertEqual(model.PAGES[0][0], "overview")
        self.assertNotIn("ECP", model.PRODUCT)


@unittest.skipUnless(DISPLAY, "set ERSA_GUI_TESTS=1 and run on a virtual display (see the top of this file)")
class Window(BootDaemon):
    def setUp(self):
        super().setUp()
        from foundation.gbackend import Backend
        from foundation_deck.app import DeckWindow
        with open(self.system.paths["os_release"], "w") as handle:
            handle.write("ID=opensuse-tumbleweed\nIMAGE_ID=ecp\nIMAGE_VERSION=0.2.0\n")
        global APP
        if APP is None:
            APP = Adw.Application(application_id="org.ersa.DeckTest")
            APP.register(None)
        self.window = DeckWindow(APP, Backend(connection=self.conn))
        self.addCleanup(self.window.destroy)

    def pump(self, condition, timeout=30):
        context = GLib.MainContext.default()
        end = time.monotonic() + timeout
        while not condition():
            if time.monotonic() > end:
                self.fail("timed out waiting for the window")
            if not context.iteration(False):
                time.sleep(0.01)

    @staticmethod
    def rows(group):
        return getattr(group, "_rows", [])

    def show(self, name):
        self.window.select(name)
        return self.window.pages[name]

    def test_the_ersa_id_page_shows_the_signed_in_person_and_no_devices(self):
        import tempfile
        from foundation import identity
        home = tempfile.mkdtemp()
        os.environ["XDG_STATE_HOME"] = home
        self.addCleanup(os.environ.pop, "XDG_STATE_HOME", None)
        page = self.show("ersaid")
        values = {"identity.enabled": True, "identity.issuer": "https://id.example", "identity.client_id": "c"}
        page.got_settings(values)
        self.assertEqual((page.name.get_label(), page.sign_in_button.get_visible(), page.sign_out_button.get_visible()),
                         ("Ersa ID", True, False))
        identity.save_session({"issuer": "https://id.example", "client_id": "c", "token_endpoint": "https://id.example/t",
                               "access_token": "a", "refresh_token": None, "expires_at": 9e9,
                               "claims": {"sub": "u", "name": "Ada Lovelace", "email": "ada@example.org"}})
        page.got_settings(values)
        self.assertEqual((page.name.get_label(), page.email.get_label(), page.avatar.get_text()), ("Ada Lovelace", "ada@example.org", "AL"))
        self.assertEqual((page.sign_in_button.get_visible(), page.sign_out_button.get_visible()), (False, True))
        page.got_settings({**values, "identity.enabled": False})
        self.assertFalse(page.sign_out_button.get_visible())
        self.assertIn("turned off", page.email.get_label())

    def test_the_one_time_offer_window_has_sign_in_and_not_now(self):
        from foundation_deck.app import OfferWindow
        calls = []

        class FakeApp:
            def accept_offer(self):
                calls.append("accept")

            def decline_offer(self):
                calls.append("decline")
        app = self.window.get_application()
        app.accept_offer, app.decline_offer = lambda: calls.append("accept"), lambda: calls.append("decline")
        offer = OfferWindow(app)
        labels = []

        def walk(widget):
            if isinstance(widget, Gtk.Button):
                labels.append(widget)
            child = widget.get_first_child()
            while child:
                walk(child)
                child = child.get_next_sibling()
        walk(offer)
        by_label = {b.get_label(): b for b in labels if b.get_label()}
        self.assertEqual(sorted(by_label), ["Not Now", "Sign In"])
        by_label["Not Now"].emit("clicked")
        by_label["Sign In"].emit("clicked")
        self.assertEqual(calls, ["decline", "accept"])

    def test_every_page_in_the_sidebar_exists_and_the_sidebar_is_grouped(self):
        for name, _title, _icon in model.PAGES:
            self.assertIn(name, self.window.pages)
            self.assertIn(name, self.window.rows)
        self.assertEqual([h for h, _p in model.SECTIONS], [None, "Account", "Software", "System", "History"])

    def test_overview_shows_the_running_version(self):
        page = self.window.pages["overview"]
        self.pump(lambda: page.version_label.get_label() == "0.2.0")
        self.assertIn("slot A", page.slot_label.get_label())

    def test_slots_page_lists_both_slots(self):
        page = self.show("slots")
        self.pump(lambda: self.rows(page.slots_group))
        self.assertEqual([r.get_title() for r in self.rows(page.slots_group)], ["Slot A", "Slot B"])

    def test_updates_page_says_to_check(self):
        page = self.show("updates")
        self.pump(lambda: page.status.get_label())
        self.assertEqual(page.status.get_label(), "Check to see whether an update is available.")

    def test_overview_also_shows_the_system_and_the_software(self):
        page = self.window.pages["overview"]
        self.pump(lambda: self.rows(page.software) and "Architecture" in [r.get_title() for r in self.rows(page.system)])
        self.assertEqual([r.get_title() for r in self.rows(page.software)][0], "Deck")
        self.assertIn("Architecture", [r.get_title() for r in self.rows(page.system)])
        self.assertNotIn("about", self.window.pages)

    def test_application_sources_page_builds_and_lists_nothing_without_flatpak(self):
        page = self.show("appsources")
        self.pump(lambda: page.error.get_revealed() or self.rows(page.group_), 15)

    def test_turning_ssh_on_without_a_key_asks_for_one_instead_of_failing(self):
        page = self.show("remote")
        self.assertFalse(hasattr(page, "password_row"))              # sign-in is by key only in Deck
        page.status, page.keys = {"installed": True}, []
        asked, jobs = [], []
        page.win.confirm_choice = lambda heading, body, responses, action: asked.append([r[0] for r in responses])
        page.win.run_job = lambda call, on_ok=None, label="": jobs.append(label)
        page.request_ssh(True)
        self.assertEqual((jobs, asked), ([], [["key"]]))             # nothing was sent: it would have been refused
        self.assertFalse(page.ssh_row.get_active())                  # the switch went back
        page.keys = ["ssh-ed25519 AAAA key"]
        page.request_ssh(True)
        self.assertEqual(len(jobs), 1)

    def test_the_task_panel_follows_a_job_to_its_end(self):
        panel = self.window.tasks
        self.assertFalse(panel.get_visible())          # nothing is shown while there are no tasks
        self.assertEqual(panel.sub.get_label(), "Nothing running")
        task = panel.add("Installing the update")
        self.assertTrue(panel.get_visible())
        self.assertEqual(panel.title.get_label(), "Installing the update")
        self.assertTrue(panel.revealer.get_reveal_child())
        task.set_fraction(0.5)
        task.set_step("Writing the root image")
        self.assertEqual(panel.sub.get_label(), "Writing the root image")
        task.finish(True, "Done")
        self.assertEqual(panel.sub.get_label(), "Done: Installing the update")
        self.assertTrue(panel.clear.get_visible())
        panel.clear_finished()
        self.assertEqual(panel.sub.get_label(), "Nothing running")
        self.assertFalse(panel.get_visible())

    def test_a_real_job_shows_up_as_a_task(self):
        self.pump(lambda: self.window.pages["overview"].version_label.get_label() == "0.2.0")
        self.window.run_job(self.window.job_call("Boot", "SetSplash", "(s)", ("dark",), timeout=30_000), None, "Changing the boot splash")
        self.pump(lambda: not self.window.tasks.running() and self.window.tasks.tasks)
        self.assertIn("Changing the boot splash", self.window.tasks.tasks[0].title)

    def test_the_sidebar_search_hides_pages_and_headings(self):
        self.window.search.set_text("ssh")
        self.pump(lambda: not self.window.rows["slots"].get_child_visible() or True, 1)
        self.assertTrue(self.window._filter(self.window.rows["remote"]))
        self.assertFalse(self.window._filter(self.window.rows["slots"]))
        self.window.search.set_text("")

    def test_the_boot_splash_choice_is_read_back(self):
        page = self.show("appearance")
        self.pump(lambda: page.light.get_active() or page.dark.get_active())
        self.assertTrue(page.light.get_active())

    def test_the_unsigned_install_switch_shows_only_in_developer_mode(self):
        page = self.show("updates")
        self.pump(lambda: page.status.get_label())
        self.assertFalse(page.developer_group.get_visible())
        self.call(CONFIG, "Set", "(sv)", "system.developer_mode", GLib.Variant("b", True))
        page.reload()
        self.pump(lambda: page.developer_group.get_visible())
        self.call(CONFIG, "Set", "(sv)", "system.developer_mode", GLib.Variant("b", False))
        page.reload()
        self.pump(lambda: not page.developer_group.get_visible())

    def test_bundles_page_builds(self):
        page = self.show("bundles")
        self.pump(lambda: self.rows(page.bundles_group) and self.rows(page.extensions_group))

    def test_advanced_lists_the_settings_by_group(self):
        page = self.show("advanced")
        self.pump(lambda: len(getattr(page, "_rows", {})) >= 6)
        self.assertEqual(sorted(page.groups), ["Applications", "Identity", "System", "Updates"])

    def test_remote_and_repositories_and_activity_build(self):
        for name in ("remote", "repositories", "activity", "appearance", "packages"):
            self.show(name)
        self.pump(lambda: self.window.pages["remote"].ssh_row.get_subtitle() is not None, 10)


if __name__ == "__main__":
    unittest.main()
