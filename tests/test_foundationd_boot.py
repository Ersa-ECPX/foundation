"""The real boot workers through D-Bus: a real GPT image, plain files as partition devices, a fake bootctl."""

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

import support  # noqa: E402
from test_foundationd_bus import BusCase, NAME, gi  # noqa: E402
from test_foundationd_jobs import BOOT, ERR, JOB, JobCase  # noqa: E402

if gi:
    from gi.repository import Gio, GLib


@unittest.skipUnless(support.tool_available("sfdisk"), "needs sfdisk")
class BootDaemon(JobCase):
    """A daemon with the real workers, running against a fake system (no tests of its own)."""

    extra_args = ()   # the real worker; paths and PATH are set in _start_daemon

    def setUp(self):
        self.system = support.FakeSystem()
        self.addCleanup(self.system.cleanup)
        self.system.fill_slot(1, os.urandom(1024 * 1024 + 4096), erofs_bytes=1024 * 1024)
        self.system.uki("0.2.0", tries=2)
        self.system.efivar("LoaderEntrySelected", "ecp_0.2.0+2.efi")
        self.system.make_device_files()
        self.bin = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.bin, True)
        os.symlink(os.path.join(HERE, "fake_bootctl.py"), os.path.join(self.bin, "bootctl"))
        self.bootctl_log = os.path.join(self.bin, "log")
        super().setUp()

    def _start_daemon(self):
        s = self.system
        env = dict(os.environ, DBUS_SESSION_BUS_ADDRESS=self.address, PYTHONPATH=os.path.join(HERE, ".."),
                   PATH=self.bin + os.pathsep + os.environ["PATH"], FAKE_BOOTCTL_LOG=self.bootctl_log,
                   FAKE_EFIVARS=s.efivars)
        return subprocess.Popen(
            [sys.executable, "-m", "foundationd", "--bus", "session", "--authorizer", self.authorizer,
             "--idle-timeout", "0", "--worker-command", f"{sys.executable} -m foundationd.worker",
             "--xml-path", os.path.join(HERE, "../data/dbus/org.ersa.Foundation1.xml"),
             "--schema-path", os.path.join(HERE, "../data/config/schema.d"),
             "--defaults-path", os.path.join(self.tmp.name, "defaults.d"), "--conf-path", self.conf,
             "--state-path", os.path.join(self.tmp.name, "state"),
             "--efivars-path", s.efivars, "--mountinfo-path", s.mountinfo, "--sysfs-path", s.paths["sysfs"],
             "--dev-path", s.paths["dev"], "--esp-path", s.esp, "--os-release-path", s.paths["os_release"],
             "--root-path", s.paths["root"], *self.extra_args],
            env=env, stderr=subprocess.PIPE, text=True)

    def slots(self):
        return {slot["name"]: slot for slot in self.call(BOOT, "GetSlots")[0]}

    def labels(self):
        from foundationd.backends import gpt
        return {p.number: p.name for p in gpt.read_table(self.system.image)}

    def uki_files(self):
        return sorted(os.listdir(os.path.join(self.system.esp, "EFI/Linux")))

    def run_job(self, method, signature, *args):
        path = self.start(BOOT, method, signature, *args)
        return path, self.finish(path)


@unittest.skipUnless(support.tool_available("sfdisk"), "needs sfdisk")
class BootEndToEnd(BootDaemon):
    def test_boot_state(self):
        (state,) = self.call(BOOT, "GetBootState")
        self.assertEqual((state["efi"], state["running_slot"], state["assessment"], state["tries_left"]),
                         (True, "a", "pending", 2))
        self.assertEqual(state["running_version"], "0.2.0")

    def test_slots(self):
        slots = self.slots()
        self.assertEqual(set(slots), {"a", "b"})
        self.assertEqual((slots["a"]["version"], slots["b"]["version"]), ("0.2.0", ""))
        self.assertEqual((slots["a"]["state"], slots["a"]["running"], slots["a"]["bootable"],
                          slots["a"]["tries_left"], slots["a"]["entry"]),
                         ("running", True, True, 2, "ecp_0.2.0+2.efi"))
        self.assertEqual((slots["b"]["state"], slots["b"]["bootable"]), ("empty", False))

    def test_set_default_stores_the_counter_free_id(self):
        _path, props = self.run_job("SetDefaultSlot", "(s)", "a")
        self.assertEqual(props["State"], "done", props["ErrorMessage"])
        self.assertEqual(props["Result"]["stored"], "ecp_0.2.0.efi")
        self.assertEqual(self.slots()["a"]["default"], True)
        with open(self.bootctl_log) as handle:
            self.assertEqual(handle.read().split(), ["set-default", "ecp_0.2.0.efi"])

    def test_set_next_boot(self):
        _path, props = self.run_job("SetNextBoot", "(s)", "a")
        self.assertEqual((props["State"], props["Result"]["mode"]), ("done", "next"))

    def test_set_splash_sets_the_uefi_variable_and_reports_it(self):
        _path, props = self.run_job("SetSplash", "(s)", "dark")
        self.assertEqual(props["State"], "done", props["ErrorMessage"])
        self.assertEqual(props["Result"]["theme"], "dark")
        self.assertTrue(any(c.startswith("UEFI variable ErsaSplashDark-") for c in props["Result"]["changed"]))
        _path, props = self.run_job("SetSplash", "(s)", "dark")
        self.assertEqual(list(props["Result"]["changed"]), [])
        _path, props = self.run_job("SetSplash", "(s)", "light")
        self.assertEqual(len(list(props["Result"]["changed"])), 2)

    def test_selecting_an_empty_slot_fails_with_a_clear_error(self):
        _path, props = self.run_job("SetDefaultSlot", "(s)", "b")
        self.assertEqual((props["State"], props["ErrorName"]), ("failed", ERR + "InvalidArgument"))
        self.assertIn("empty", props["ErrorMessage"])

    def test_release(self):
        self.system.hold(1, "0.1.0")
        self.system.uki("0.1.0")
        _path, props = self.run_job("ReleaseSlot", "(s)", "b")
        self.assertEqual(props["State"], "done", props["ErrorMessage"])
        self.assertEqual(self.labels()[3], "ecp-root-b", "labels never change")
        self.assertEqual(self.uki_files(), ["ecp_0.2.0+2.efi"])
        slots = self.slots()
        self.assertEqual(slots["b"]["state"], "empty")

    def test_releasing_the_running_slot_is_refused_and_changes_nothing(self):
        self.system.hold(1, "0.1.0")
        self.system.uki("0.1.0")
        before = (self.labels(), self.uki_files())
        _path, props = self.run_job("ReleaseSlot", "(s)", "a")
        self.assertEqual(props["State"], "failed")
        self.assertEqual((self.labels(), self.uki_files()), before)
