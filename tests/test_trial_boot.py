"""The files that make a system on trial restart by itself (kernel panic=, the emergency target, the 15 minute timeout, the watchdog)."""

import os
import re
import unittest

import support  # noqa: F401

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
VARIABLE = "LoaderBootCountPath-4a67b082-0a4c-41cf-b6c7-440b29bb8c4f"


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as handle:
        return handle.read()


@unittest.skipUnless(os.path.isdir(os.path.join(ROOT, "mkosi.extra")), "needs the ecp-mkosi repository")
class TrialBoot(unittest.TestCase):
    def test_the_kernel_restarts_after_a_panic(self):
        for name in ("10-x86-64.conf", "10-arm64.conf"):
            self.assertRegex(read("mkosi.conf.d", name), r"KernelCommandLine=.*\bpanic=\d+\b")

    def test_the_emergency_target_restarts_a_system_on_trial_in_both_images(self):
        for tree in ("mkosi.extra", "mkosi.initrd.extra"):
            unit = read(tree, "usr/lib/systemd/system/ecp-trial-reboot.service")
            self.assertIn(VARIABLE, unit)                       # only while the entry still counts tries
            self.assertIn("systemctl --no-block --force reboot", unit)
            self.assertIn("ecp-trial-reboot.service", read(tree, "usr/lib/systemd/system/emergency.target.d/10-ecp-trial.conf"))

    def test_a_start_that_hangs_is_restarted_after_the_timeout_only_while_on_trial(self):
        unit = read("mkosi.extra", "usr/lib/systemd/system/ecp-trial-boot-timeout.service")
        self.assertIn(VARIABLE, unit)
        self.assertRegex(unit, r"ecp-trial-boot-timeout \d+")
        self.assertIn("ecp-trial-boot-timeout.service", read("mkosi.extra", "usr/lib/systemd/system/basic.target.d/10-ecp-trial.conf"))
        script = os.path.join(ROOT, "mkosi.extra", "usr", "libexec", "ecp-trial-boot-timeout")
        self.assertTrue(os.access(script, os.X_OK))
        text = read("mkosi.extra", "usr", "libexec", "ecp-trial-boot-timeout")
        self.assertIn(VARIABLE, text)
        self.assertIn('[ -e "$esp$path" ]', text)               # a blessed entry has lost its counter, so the file is gone

    def test_the_watchdog_is_fed_in_the_system_and_in_the_initrd(self):
        for tree in ("mkosi.extra", "mkosi.initrd.extra"):
            self.assertRegex(read(tree, "etc/systemd/system.conf.d/10-ecp-watchdog.conf"), r"(?m)^RuntimeWatchdogSec=\d+s$")

    def test_the_variable_is_the_one_the_boot_assessment_unit_uses(self):
        self.assertIn(re.sub(r"-.*", "", VARIABLE), read("mkosi.extra", "usr/lib/systemd/system/systemd-bless-boot.service.d/10-ecp.conf"))
        self.assertIn(VARIABLE, read("mkosi.extra", "usr/lib/systemd/system/systemd-bless-boot.service.d/10-ecp.conf"))


if __name__ == "__main__":
    unittest.main()
