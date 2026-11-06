"""The boot splash theme: the command line token in every UKI, and the two Plymouth scripts staying in step."""

import os
import re
import sys
import unittest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

import support  # noqa: E402
from foundationd.backends import pe, splash  # noqa: E402
from foundationd.errors import Failed, InvalidArgument  # noqa: E402

SOURCES = os.path.join(os.environ.get("ECP_PACKAGES_DIR", "/nonexistent"), "ersa-boot-splash", "SOURCES")   # a checkout of the packages repository


@unittest.skipUnless(os.path.isdir(SOURCES), "the theme sources are not part of this checkout (an RPM build tree)")
@unittest.skipUnless(os.path.isdir(SOURCES), "set ECP_PACKAGES_DIR to a packages checkout")
class Scripts(unittest.TestCase):
    def read(self, name):
        with open(os.path.join(SOURCES, name), encoding="utf-8") as handle:
            return handle.read()

    @staticmethod
    def without_theme_variables(text):
        return re.sub(r"# THEME VARIABLES.*?# END THEME VARIABLES\n", "", text, flags=re.S)

    def test_the_light_and_dark_scripts_differ_only_in_their_theme_variables(self):
        light, dark = self.read("ersa.script"), self.read("ersa-dark.script")
        self.assertIn("# THEME VARIABLES", light)
        self.assertNotEqual(light, dark)
        self.assertEqual(self.without_theme_variables(light), self.without_theme_variables(dark))

    def test_the_dark_theme_files_exist_and_are_named_for_the_theme(self):
        for name in ("ersa-dark.plymouth", "fill-dark.png", "track-dark.png"):
            self.assertTrue(os.path.exists(os.path.join(SOURCES, name)), name)
        text = self.read("ersa-dark.plymouth")
        self.assertIn("themes/ersa-dark/ersa-dark.script", text)
        self.assertIn("ersa-dark-logo.png", self.read("ersa-dark.script"))


class SyncScript(unittest.TestCase):
    def test_the_color_scheme_maps_to_a_theme(self):
        import importlib.machinery
        import importlib.util
        path = os.path.join(HERE, "..", "data", "libexec", "foundation-splash-sync")
        try:
            loader = importlib.machinery.SourceFileLoader("splash_sync", path)
            module = importlib.util.module_from_spec(importlib.util.spec_from_loader("splash_sync", loader))
            loader.exec_module(module)
        except ImportError:
            self.skipTest("needs PyGObject")
        self.assertEqual(module.theme_for("prefer-dark"), "dark")
        for value in ("default", "prefer-light", ""):
            self.assertEqual(module.theme_for(value), "light")


class Text(unittest.TestCase):
    def test_dark_adds_the_token_and_keeps_the_others(self):
        self.assertEqual(splash.apply_theme("quiet root=X rw", "dark"), "quiet root=X rw plymouth.splash=ersa-dark")

    def test_light_removes_the_token_and_keeps_the_others(self):
        self.assertEqual(splash.apply_theme("quiet plymouth.splash=ersa-dark root=X", "light"), "quiet root=X")

    def test_a_foreign_theme_is_replaced_by_dark(self):
        self.assertEqual(splash.apply_theme("a plymouth.splash=x b", "dark"), "a b plymouth.splash=ersa-dark")

    def test_an_unchanged_line_is_returned_as_it_is(self):
        self.assertEqual(splash.apply_theme("a plymouth.splash=ersa-dark b", "dark"), "a plymouth.splash=ersa-dark b")
        self.assertEqual(splash.apply_theme("a  b", "light"), "a  b")

    def test_an_unknown_theme_is_refused(self):
        with self.assertRaises(InvalidArgument):
            splash.apply_theme("a", "blue")


class Backend(unittest.TestCase):
    def setUp(self):
        self.system = support.FakeSystem(slot_names=("0.2.0", "0.1.0"), running=1)
        self.addCleanup(self.system.cleanup)
        self.system.uki("0.2.0")
        self.system.uki("0.1.0", tries=2)
        self.paths = self.system.paths
        self.ctx = support.Recorder()

    def cmdline(self, name):
        with open(os.path.join(self.system.esp, "EFI/Linux", name), "rb") as handle:
            return pe.read_cmdline(handle.read())

    def files(self):
        return sorted(os.listdir(os.path.join(self.system.esp, "EFI/Linux")))

    def test_dark_changes_every_image_and_keeps_names_and_other_tokens(self):
        result = splash.set_theme(self.paths, "dark", self.ctx)
        self.assertEqual(result, {"theme": "dark", "changed": ["ecp_0.1.0+2.efi", "ecp_0.2.0.efi"], "skipped": []})
        self.assertEqual(self.files(), ["ecp_0.1.0+2.efi", "ecp_0.2.0.efi"])
        self.assertEqual(self.cmdline("ecp_0.2.0.efi"), "quiet root=PARTLABEL=ecp-root_0.2.0 rw plymouth.splash=ersa-dark")
        self.assertEqual(self.cmdline("ecp_0.1.0+2.efi"), "quiet root=PARTLABEL=ecp-root_0.1.0 rw plymouth.splash=ersa-dark")

    def test_it_is_idempotent_and_writes_nothing_the_second_time(self):
        splash.set_theme(self.paths, "dark", self.ctx)
        before = {n: os.stat(os.path.join(self.system.esp, "EFI/Linux", n)).st_mtime_ns for n in self.files()}
        result = splash.set_theme(self.paths, "dark", self.ctx)
        self.assertEqual(result["changed"], [])
        after = {n: os.stat(os.path.join(self.system.esp, "EFI/Linux", n)).st_mtime_ns for n in self.files()}
        self.assertEqual(before, after)

    def test_light_removes_the_token_again(self):
        splash.set_theme(self.paths, "dark", self.ctx)
        result = splash.set_theme(self.paths, "light", self.ctx)
        self.assertEqual(len(result["changed"]), 2)
        self.assertEqual(self.cmdline("ecp_0.2.0.efi"), "quiet root=PARTLABEL=ecp-root_0.2.0 rw")

    def test_light_without_a_token_changes_nothing(self):
        self.assertEqual(splash.set_theme(self.paths, "light", self.ctx)["changed"], [])

    def test_a_signed_image_is_skipped_with_a_reason_and_the_others_are_changed(self):
        with open(os.path.join(self.system.esp, "EFI/Linux/ecp_0.1.0+2.efi"), "wb") as handle:
            handle.write(support.build_pe(b"quiet root=X", raw_size=512, signed=True))
        result = splash.set_theme(self.paths, "dark", self.ctx)
        self.assertEqual(result["changed"], ["ecp_0.2.0.efi"])
        self.assertEqual([s["file"] for s in result["skipped"]], ["ecp_0.1.0+2.efi"])
        self.assertIn("signed", result["skipped"][0]["reason"])
        self.assertEqual(self.cmdline("ecp_0.1.0+2.efi"), "quiet root=X")

    def test_no_room_in_the_command_line_section_is_a_clear_failure(self):
        full = b"quiet root=PARTLABEL=ecp-root_0.2.0 rw"
        with open(os.path.join(self.system.esp, "EFI/Linux/ecp_0.2.0.efi"), "wb") as handle:
            handle.write(support.build_pe(full, raw_size=len(full) + 1))
        with self.assertRaisesRegex(Failed, r"ecp_0\.2\.0\.efi.*does not fit"):
            splash.set_theme(self.paths, "dark", self.ctx)
        self.assertEqual(self.cmdline("ecp_0.2.0.efi"), full.decode())
        self.assertFalse([n for n in os.listdir(os.path.join(self.system.esp, "EFI/Linux")) if n.startswith(".")])

    def test_a_bad_theme_is_refused_before_anything_is_written(self):
        with self.assertRaises(InvalidArgument):
            splash.set_theme(self.paths, "blue", self.ctx)

    def test_current_reads_the_running_slots_image(self):
        self.assertEqual(splash.current(self.paths), {"theme": "light", "file": "ecp_0.2.0.efi"})
        splash.set_theme(self.paths, "dark", self.ctx)
        self.assertEqual(splash.current(self.paths)["theme"], "dark")


if __name__ == "__main__":
    unittest.main()
