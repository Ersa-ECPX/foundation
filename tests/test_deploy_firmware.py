"""Registering the installed disk with the firmware, so a plugged-in installer medium is not started again."""

import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))

from foundationd.backends import deploy  # noqa: E402

LISTING = """BootCurrent: 0001
BootOrder: 0001,0000
Boot0000* Ersa Compute Platform\tHD(1,GPT,1111,0x800,0x200000)/File(\\EFI\\BOOT\\BOOTX64.EFI)
Boot0001* UEFI QEMU DVD-ROM
Boot0002  Ersa Compute Platform Rescue
"""


class Runner:
    def __init__(self, listing=LISTING, create_code=0, list_code=0):
        self.calls, self.listing, self.create_code, self.list_code = [], listing, create_code, list_code

    def __call__(self, cmd, timeout=60, input_bytes=None):
        self.calls.append(cmd)
        if cmd == ["efibootmgr"]:
            return self.list_code, self.listing, "" if not self.list_code else "EFI variables are not supported"
        if "--create" in cmd:
            return self.create_code, "", "" if not self.create_code else "cannot write"
        return 0, "", ""


class Firmware(unittest.TestCase):
    def setUp(self):
        self.efivars = tempfile.mkdtemp()
        open(os.path.join(self.efivars, "LoaderInfo-4a67b082-0a4c-41cf-b6c7-440b29bb8c4f"), "w").close()
        self.paths = {"efivars": self.efivars}

    def register(self, runner, **kw):
        return deploy.register_firmware_entry(self.paths, "/dev/sdb", 1, runner, machine="x86_64", **kw)

    def test_a_boot_entry_for_the_new_disk_is_created_with_the_removable_path(self):
        runner = Runner()
        registered, note = self.register(runner)
        self.assertTrue(registered)
        self.assertIn("first boot choice", note)
        create = runner.calls[-1]
        self.assertEqual(create[:2], ["efibootmgr", "--create"])
        self.assertEqual(create[create.index("--disk") + 1], "/dev/sdb")
        self.assertEqual(create[create.index("--part") + 1], "1")
        self.assertEqual(create[create.index("--loader") + 1], "\\EFI\\BOOT\\BOOTX64.EFI")
        self.assertEqual(create[create.index("--label") + 1], "Ersa Compute Platform")

    def test_entries_of_earlier_installs_are_removed_but_others_are_not(self):
        runner = Runner()
        self.register(runner)
        deleted = [c[c.index("--bootnum") + 1] for c in runner.calls if "--delete-bootnum" in c]
        self.assertEqual(deleted, ["0000"])                      # not the DVD entry, not "... Rescue"

    def test_other_architectures_use_their_own_loader(self):
        runner = Runner()
        deploy.register_firmware_entry(self.paths, "/dev/sdb", 1, runner, machine="aarch64")
        self.assertIn("\\EFI\\BOOT\\BOOTAA64.EFI", runner.calls[-1])
        registered, note = deploy.register_firmware_entry(self.paths, "/dev/sdb", 1, runner, machine="riscv64")
        self.assertFalse(registered)
        self.assertIn("unknown architecture", note)

    def test_a_file_target_or_a_computer_without_uefi_is_skipped_quietly(self):
        runner = Runner()
        self.assertEqual(self.register(runner, allow_file=True)[0], False)
        os.unlink(os.path.join(self.efivars, "LoaderInfo-4a67b082-0a4c-41cf-b6c7-440b29bb8c4f"))
        registered, note = deploy.register_firmware_entry({"efivars": self.efivars + "-missing"}, "/dev/sdb", 1, runner, machine="x86_64")
        self.assertFalse(registered)
        self.assertIn("not started through UEFI", note)
        self.assertEqual(runner.calls, [])

    def test_a_failure_is_a_note_not_an_error(self):
        registered, note = self.register(Runner(create_code=1))
        self.assertFalse(registered)
        self.assertIn("boot menu", note)
        registered, note = self.register(Runner(list_code=1))
        self.assertFalse(registered)
        self.assertIn("efibootmgr", note)


if __name__ == "__main__":
    unittest.main()
