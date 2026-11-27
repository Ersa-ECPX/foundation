"""/usr/libexec/ecp/data-link: the data volume is the one on the disk the system started from, whatever else carries the same label."""

import os
import re
import subprocess
import tempfile
import unittest

import support  # noqa: F401

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SCRIPT = os.path.join(ROOT, "mkosi.initrd.extra", "usr", "libexec", "ecp", "data-link")
UNITS = os.path.join(ROOT, "mkosi.initrd.extra", "usr", "lib", "systemd", "system")
VARIABLE = "LoaderDevicePartUUID-4a67b082-0a4c-41cf-b6c7-440b29bb8c4f"


class Machine:
    """A fake /sys, /dev and EFI variable directory: disks with partitions that have names, and what the boot loader recorded."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        for sub in ("sys/devices/pci", "sys/class/block", "dev/disk/by-partuuid", "efivars", "run"):
            os.makedirs(os.path.join(self.root, sub))

    def path(self, *parts):
        return os.path.join(self.root, *parts)

    def disk(self, name, partitions):
        """partitions: [(device name, GPT name, PARTUUID)]."""
        directory = self.path("sys/devices/pci", name)
        os.makedirs(directory)
        os.symlink(os.path.join("../../devices/pci", name), self.path("sys/class/block", name))
        for part, partname, uuid in partitions:
            os.makedirs(os.path.join(directory, part))
            with open(os.path.join(directory, part, "uevent"), "w") as handle:
                handle.write(f"MAJOR=8\nMINOR=1\nDEVNAME={part}\nDEVTYPE=partition\nPARTN=1\nPARTNAME={partname}\n")
            os.symlink(os.path.join("../../devices/pci", name, part), self.path("sys/class/block", part))
            open(self.path("dev", part), "w").close()
            os.symlink(os.path.join("../..", part), self.path("dev/disk/by-partuuid", uuid))

    def started_from(self, esp_uuid):
        with open(self.path("efivars", VARIABLE), "wb") as handle:
            handle.write(b"\x06\x00\x00\x00" + esp_uuid.upper().encode("utf-16-le") + b"\0\0")

    def encrypt(self, device, unlock_works=True):
        """Make a partition a LUKS container (its first bytes) and give the helper a fake unlock tool that records how it was called and,
        when `unlock_works`, creates the unlocked device."""
        with open(self.path("dev", device), "wb") as handle:
            handle.write(b"LUKS\xba\xbe" + b"\0" * 100)
        tool = self.path("fake-cryptsetup")
        with open(tool, "w") as handle:
            handle.write("#!/bin/sh\necho \"$@\" > " + self.path("cryptsetup.args") + "\n"
                         + (f"mkdir -p {self.path('dev/mapper')} && : > {self.path('dev/mapper/ecp-data')}\n" if unlock_works else "exit 1\n"))
        os.chmod(tool, 0o755)
        self.cryptsetup = tool

    def unlock_arguments(self):
        try:
            with open(self.path("cryptsetup.args")) as handle:
                return handle.read().strip()
        except OSError:
            return None

    cryptsetup = None
    extra = None

    def run(self, wait=1):
        env = dict(os.environ, ECP_EFIVARS=self.path("efivars"), ECP_SYSFS=self.path("sys"), ECP_DEV=self.path("dev"),
                   ECP_RUN=self.path("run/ecp"), ECP_WAIT=str(wait), ECP_EXTRA=self.extra or self.path("no-extra"))
        if self.cryptsetup:
            env["ECP_CRYPTSETUP"] = self.cryptsetup
        return subprocess.run(["/bin/sh", SCRIPT], capture_output=True, text=True, env=env, timeout=60)

    def linked(self):
        link = self.path("run/ecp/data")
        return os.path.basename(os.readlink(link)) if os.path.islink(link) else None


def install(esp, data="ECP-DATA"):
    """The partitions of an installed disk (device names are per disk)."""
    return [("1", "ECP-ESP", esp), ("3", "ecp-root-a", "r-" + esp), ("7", data, "d-" + esp)]


@unittest.skipUnless(os.path.isfile(SCRIPT), "needs the ecp-mkosi repository (the RPM build has only foundation/)")
class DataLink(unittest.TestCase):
    def setUp(self):
        self.m = Machine()
        self.addCleanup(self.m.tmp.cleanup)

    def disk(self, name, esp, data="ECP-DATA"):
        self.m.disk(name, [(name + p, n, u) for p, n, u in install(esp, data)])

    def test_the_data_volume_of_the_disk_it_started_from_is_found(self):
        self.disk("vda", "aaaaaaaa-0000-0000-0000-000000000001")
        self.m.started_from("AAAAAAAA-0000-0000-0000-000000000001")
        result = self.m.run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.m.linked(), "vda7")

    def test_a_second_disk_with_the_same_label_is_not_taken(self):
        self.disk("vda", "aaaaaaaa-0000-0000-0000-000000000001")
        self.disk("vdb", "bbbbbbbb-0000-0000-0000-000000000002")
        self.m.started_from("bbbbbbbb-0000-0000-0000-000000000002")
        self.assertEqual(self.m.run().returncode, 0)
        self.assertEqual(self.m.linked(), "vdb7")                     # the one it started from, not the first with the label
        self.m.started_from("aaaaaaaa-0000-0000-0000-000000000001")
        self.assertEqual(self.m.run().returncode, 0)
        self.assertEqual(self.m.linked(), "vda7")

    def test_the_installer_medium_uses_its_own_volume_even_with_an_installed_disk_attached(self):
        self.disk("vda", "aaaaaaaa-0000-0000-0000-000000000001")                     # an installed disk
        self.m.disk("vdb", [("vdb1", "ECP-ESP", "bbbbbbbb-0000-0000-0000-000000000002"), ("vdb2", "ecp-live", "r-b"),
                            ("vdb4", "ECP-LIVE", "d-b")])
        self.m.started_from("bbbbbbbb-0000-0000-0000-000000000002")
        self.assertEqual(self.m.run().returncode, 0)
        self.assertEqual(self.m.linked(), "vdb4")

    def test_nvme_style_partition_names_work(self):
        self.m.disk("nvme0n1", [("nvme0n1p1", "ECP-ESP", "cccccccc-0000-0000-0000-000000000003"), ("nvme0n1p7", "ECP-DATA", "d-c")])
        self.m.started_from("cccccccc-0000-0000-0000-000000000003")
        self.assertEqual(self.m.run().returncode, 0)
        self.assertEqual(self.m.linked(), "nvme0n1p7")

    def test_an_encrypted_volume_is_unlocked_and_the_unlocked_device_is_linked(self):
        self.disk("vda", "aaaaaaaa-0000-0000-0000-000000000001")
        self.m.started_from("aaaaaaaa-0000-0000-0000-000000000001")
        self.m.encrypt("vda7")
        extra = self.m.path("extra")
        os.makedirs(extra)
        open(os.path.join(extra, "tpm2-pcr-signature.json"), "w").close()
        self.m.extra = extra
        result = self.m.run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.m.linked(), "ecp-data")                   # the mapper device, not the partition
        arguments = self.m.unlock_arguments()
        self.assertTrue(arguments.startswith("attach ecp-data " + self.m.path("dev", "vda7") + " - tpm2-device=auto"), arguments)
        self.assertIn("tpm2-signature=" + os.path.join(extra, "tpm2-pcr-signature.json"), arguments)   # the kernel image's signed policy

    def test_an_encrypted_volume_without_a_signature_file_still_asks_the_tool_to_unlock(self):
        self.disk("vda", "aaaaaaaa-0000-0000-0000-000000000001")
        self.m.started_from("aaaaaaaa-0000-0000-0000-000000000001")
        self.m.encrypt("vda7")
        self.assertEqual(self.m.run().returncode, 0)
        self.assertNotIn("tpm2-signature", self.m.unlock_arguments())

    def test_a_volume_that_cannot_be_unlocked_fails_with_a_reason_and_is_not_linked(self):
        self.disk("vda", "aaaaaaaa-0000-0000-0000-000000000001")
        self.m.started_from("aaaaaaaa-0000-0000-0000-000000000001")
        self.m.encrypt("vda7", unlock_works=False)
        result = self.m.run()
        self.assertEqual(result.returncode, 1)
        self.assertIn("could not be unlocked", result.stderr)
        self.assertIsNone(self.m.linked())

    def test_a_plain_volume_never_calls_the_unlock_tool(self):
        self.disk("vda", "aaaaaaaa-0000-0000-0000-000000000001")
        self.m.started_from("aaaaaaaa-0000-0000-0000-000000000001")
        self.m.encrypt("vda6")                       # some other partition is encrypted; the data volume is vda7
        self.assertEqual(self.m.run().returncode, 0)
        self.assertEqual(self.m.linked(), "vda7")
        self.assertIsNone(self.m.unlock_arguments())

    def test_a_disk_without_a_data_volume_fails_with_a_reason(self):
        self.m.disk("vda", [("vda1", "ECP-ESP", "aaaaaaaa-0000-0000-0000-000000000001"), ("vda3", "ecp-root-a", "r-a")])
        self.m.started_from("aaaaaaaa-0000-0000-0000-000000000001")
        result = self.m.run()
        self.assertEqual(result.returncode, 1)
        self.assertIn("no data volume", result.stderr)
        self.assertIsNone(self.m.linked())

    def test_no_record_of_the_boot_partition_fails_instead_of_guessing(self):
        self.disk("vda", "aaaaaaaa-0000-0000-0000-000000000001")
        self.disk("vdb", "bbbbbbbb-0000-0000-0000-000000000002")
        result = self.m.run()                                          # nothing says which disk it started from
        self.assertEqual(result.returncode, 1)
        self.assertIn("LoaderDevicePartUUID", result.stderr)
        self.assertIsNone(self.m.linked())

    def test_a_boot_partition_that_never_appears_fails(self):
        self.disk("vda", "aaaaaaaa-0000-0000-0000-000000000001")
        self.m.started_from("dddddddd-0000-0000-0000-000000000004")
        self.assertEqual(self.m.run().returncode, 1)


@unittest.skipUnless(os.path.isfile(SCRIPT), "needs the ecp-mkosi repository (the RPM build has only foundation/)")
class Units(unittest.TestCase):
    def read(self, name):
        with open(os.path.join(UNITS, name), encoding="utf-8") as handle:
            return handle.read()

    def test_var_and_home_are_mounted_before_switch_root_from_the_linked_volume(self):
        for where, subvolume in (("var", "var"), ("home", "home")):
            unit = self.read(f"sysroot-{where}.mount")
            self.assertIn("What=/run/ecp/data", unit)
            self.assertIn(f"Where=/sysroot/{where}", unit)
            self.assertIn(f"Options=subvol={subvolume}", unit)
            self.assertIn("Requires=ecp-data-device.service", unit)
            self.assertTrue(os.path.islink(os.path.join(UNITS, "initrd-fs.target.requires", f"sysroot-{where}.mount")))

    def test_the_device_service_waits_for_the_tpm_and_the_measurement_barrier(self):
        unit = self.read("ecp-data-device.service")
        self.assertIn("After=tpm2.target cryptsetup-pre.target", unit)

    def test_the_device_service_runs_the_helper_before_the_mounts(self):
        unit = self.read("ecp-data-device.service")
        self.assertIn("ExecStart=/usr/libexec/ecp/data-link", unit)
        self.assertIn("Before=sysroot-var.mount sysroot-home.mount", unit)

    def test_no_label_is_used_on_the_kernel_command_line(self):
        for path in ("mkosi.conf", "mkosi.conf.d/10-x86-64.conf", "mkosi.conf.d/10-arm64.conf", "mkosi.profiles/installer/mkosi.conf"):
            with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
                text = "\n".join(line for line in handle.read().splitlines() if not line.lstrip().startswith("#"))
            self.assertNotRegex(text, r"mount-extra=LABEL=", path)
        self.assertFalse(os.path.exists(os.path.join(ROOT, "mkosi.conf.d", "30-data-mounts.conf")))

    def test_the_variable_is_the_one_systemd_boot_sets(self):
        self.assertIn(VARIABLE, open(SCRIPT, encoding="utf-8").read())
        self.assertTrue(re.fullmatch(r"LoaderDevicePartUUID-[0-9a-f-]{36}", VARIABLE))


if __name__ == "__main__":
    unittest.main()
