"""The boot splash theme: the command line token in every UKI, and the two Plymouth scripts staying in step."""

import os
import re
import shutil
import struct
import sys
import tempfile
import unittest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

import support  # noqa: E402
from foundationd.backends import splash  # noqa: E402
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
    def test_dark_sets_the_theme_line_and_keeps_the_others(self):
        self.assertEqual(splash.apply_theme("[Daemon]\nTheme=ersa\nShowDelay=0\n", "dark"), "[Daemon]\nTheme=ersa-dark\nShowDelay=0\n")

    def test_light_sets_the_default_theme_again(self):
        self.assertEqual(splash.apply_theme("[Daemon]\nTheme=ersa-dark\n", "light"), "[Daemon]\nTheme=ersa\n")

    def test_a_missing_line_is_added_under_daemon_and_a_missing_section_is_made(self):
        self.assertEqual(splash.apply_theme("[Daemon]\nShowDelay=0\n", "dark"), "[Daemon]\nTheme=ersa-dark\nShowDelay=0\n")
        self.assertEqual(splash.apply_theme("", "dark"), "[Daemon]\nTheme=ersa-dark\n")

    def test_an_unchanged_file_is_returned_as_it_is(self):
        self.assertEqual(splash.apply_theme("[Daemon]\nTheme=ersa-dark\n", "dark"), "[Daemon]\nTheme=ersa-dark\n")

    def test_the_theme_of_a_file(self):
        self.assertEqual(splash.theme_of("[Daemon]\nTheme=ersa-dark\n"), "dark")
        self.assertEqual(splash.theme_of("[Daemon]\nTheme=ersa\n"), "light")
        self.assertEqual(splash.theme_of(""), "light")

    def test_an_unknown_theme_is_refused(self):
        with self.assertRaises(InvalidArgument):
            splash.apply_theme("a", "blue")


class Backend(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.efivars = os.path.join(self.root, "efivars")
        os.makedirs(self.efivars)
        os.makedirs(os.path.join(self.root, "root/etc/plymouth"))
        self.paths = {"efivars": self.efivars, "root": os.path.join(self.root, "root")}
        self.config = os.path.join(self.root, "root/etc/plymouth/plymouthd.conf")
        with open(self.config, "w") as handle:
            handle.write("[Daemon]\nTheme=ersa\n")
        self.ctx = support.Recorder()

    def variable(self):
        return os.path.join(self.efivars, splash.VARIABLE)

    def theme_in_config(self):
        with open(self.config) as handle:
            return splash.theme_of(handle.read())

    def test_dark_creates_the_variable_with_its_attributes_and_edits_the_configuration(self):
        result = splash.set_theme(self.paths, "dark", self.ctx)
        self.assertEqual(result, {"theme": "dark", "changed": [f"UEFI variable {splash.VARIABLE}", "/etc/plymouth/plymouthd.conf"],
                                  "skipped": []})
        with open(self.variable(), "rb") as handle:
            data = handle.read()
        self.assertEqual(struct.unpack("<I", data[:4])[0], 0x7)          # non-volatile, boot services, runtime
        self.assertEqual(data[4:], b"\x01")
        self.assertEqual(self.theme_in_config(), "dark")

    def test_it_is_idempotent_and_changes_nothing_the_second_time(self):
        splash.set_theme(self.paths, "dark", self.ctx)
        self.assertEqual(splash.set_theme(self.paths, "dark", self.ctx)["changed"], [])
        splash.set_theme(self.paths, "light", self.ctx)
        self.assertEqual(splash.set_theme(self.paths, "light", self.ctx)["changed"], [])

    def test_light_removes_the_variable_and_restores_the_default_theme(self):
        splash.set_theme(self.paths, "dark", self.ctx)
        result = splash.set_theme(self.paths, "light", self.ctx)
        self.assertEqual(len(result["changed"]), 2)
        self.assertFalse(os.path.exists(self.variable()))
        self.assertEqual(self.theme_in_config(), "light")

    def test_the_other_lines_of_the_configuration_are_kept(self):
        with open(self.config, "w") as handle:
            handle.write("[Daemon]\nTheme=ersa\nShowDelay=3\n")
        splash.set_theme(self.paths, "dark", self.ctx)
        with open(self.config) as handle:
            self.assertEqual(handle.read(), "[Daemon]\nTheme=ersa-dark\nShowDelay=3\n")

    def test_a_missing_configuration_file_is_created(self):
        os.unlink(self.config)
        splash.set_theme(self.paths, "dark", self.ctx)
        self.assertEqual(self.theme_in_config(), "dark")

    def test_a_computer_without_uefi_variables_still_gets_the_file_and_is_told(self):
        shutil.rmtree(self.efivars)
        result = splash.set_theme(self.paths, "dark", self.ctx)
        self.assertEqual(result["changed"], ["/etc/plymouth/plymouthd.conf"])
        self.assertEqual(len(result["skipped"]), 1)
        self.assertIn("did not start with UEFI", result["skipped"][0]["reason"])
        self.assertEqual(self.theme_in_config(), "dark")

    def test_current_reads_the_variable(self):
        self.assertEqual(splash.current(self.paths), {"theme": "light", "uefi": True})
        splash.set_theme(self.paths, "dark", self.ctx)
        self.assertEqual(splash.current(self.paths), {"theme": "dark", "uefi": True})

    def test_a_bad_theme_is_refused_before_anything_is_written(self):
        with self.assertRaises(InvalidArgument):
            splash.set_theme(self.paths, "blue", self.ctx)
        self.assertFalse(os.path.exists(self.variable()))

    def test_a_variable_that_cannot_be_written_is_a_clear_failure(self):
        os.chmod(self.efivars, 0o500)
        self.addCleanup(os.chmod, self.efivars, 0o700)
        if os.access(self.efivars, os.W_OK):
            self.skipTest("running as a user that can write anywhere")
        with self.assertRaisesRegex(Failed, "cannot write the UEFI variable"):
            splash.set_theme(self.paths, "dark", self.ctx)


class InitrdFiles(unittest.TestCase):
    """The files that make the initrd follow the variable (mkosi.initrd.extra) agree with the backend."""

    MKOSI = os.path.join(os.environ.get("ECP_MKOSI_DIR", os.path.join(HERE, "..", "..")))

    def read(self, relative):
        path = os.path.join(self.MKOSI, relative)
        if not os.path.exists(path):
            self.skipTest("the image configuration is not part of this checkout")
        with open(path, encoding="utf-8") as handle:
            return handle.read()

    def test_the_unit_runs_only_when_the_variable_exists_and_before_plymouth(self):
        unit = self.read("mkosi.initrd.extra/usr/lib/systemd/system/ersa-splash-dark.service")
        self.assertIn(f"ConditionPathExists=/sys/firmware/efi/efivars/{splash.VARIABLE}", unit)
        self.assertIn("Before=plymouth-start.service", unit)

    def test_plymouth_waits_for_it(self):
        dropin = self.read("mkosi.initrd.extra/usr/lib/systemd/system/plymouth-start.service.d/50-ersa-splash.conf")
        self.assertIn("After=ersa-splash-dark.service", dropin)
        self.assertIn("Wants=ersa-splash-dark.service", dropin)

    def test_the_dark_configuration_selects_the_dark_theme_and_the_default_the_light_one(self):
        self.assertEqual(splash.theme_of(self.read("mkosi.extra/etc/plymouth/plymouthd-dark.conf")), "dark")
        self.assertEqual(splash.theme_of(self.read("mkosi.extra/etc/plymouth/plymouthd.conf")), "light")


if __name__ == "__main__":
    unittest.main()
