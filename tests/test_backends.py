"""Backends that read or change the system, tested against fake system trees and real images."""

import os
import random
import shutil
import struct
import sys
import tempfile
import unittest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

import support  # noqa: E402
from foundationd.backends import cpio, elf, gpt, osinfo, pe, slots, verity  # noqa: E402
from foundationd.backends.runner import run  # noqa: E402
from foundationd.errors import Failed, InvalidArgument, NotFound, Unavailable  # noqa: E402

needs_sfdisk = unittest.skipUnless(support.tool_available("sfdisk"), "needs sfdisk")


class GptTests(unittest.TestCase):
    @needs_sfdisk
    def test_reads_a_real_table(self):
        system = support.FakeSystem()
        self.addCleanup(system.cleanup)
        table = gpt.read_table(system.image)
        self.assertEqual([(p.number, p.name) for p in table], [(1, "ecp-root-a"), (2, "ecp-verity-a"), (3, "ecp-root-b"),
                                                               (4, "ecp-verity-b"), (5, "ECP-ESP")])
        self.assertEqual(table[0].size, 4 * support.MIB)
        self.assertEqual((table[0].type, table[1].type), (gpt.ROOT_TYPES["x86_64"], verity.ROOT_VERITY_TYPES["x86_64"]))
        self.assertEqual((table[0].uuid, table[1].uuid), verity.uuids(support.hash_for("0.2.0")))

    @needs_sfdisk
    def test_corruption_is_detected(self):
        system = support.FakeSystem()
        self.addCleanup(system.cleanup)
        with open(system.image, "r+b") as handle:
            handle.seek(512 + 40)           # inside the primary header
            handle.write(b"\xff")
        with self.assertRaises(gpt.GptError):
            gpt.read_table(system.image)

    def test_not_a_gpt(self):
        with tempfile.NamedTemporaryFile() as handle:
            handle.write(b"\0" * 4096)
            handle.flush()
            with self.assertRaises(gpt.GptError):
                gpt.read_table(handle.name)


class PeTests(unittest.TestCase):
    def test_reads_the_command_line(self):
        self.assertEqual(pe.read_cmdline(support.build_pe(b"quiet roothash=ab rw")), "quiet roothash=ab rw")

    def test_an_image_without_a_command_line_is_refused(self):
        with self.assertRaises(Failed):
            pe.read_cmdline(support.build_pe(b"", with_cmdline=False))

    def test_not_a_pe_image(self):
        with self.assertRaises(Failed):
            pe.read_cmdline(b"not a pe image")

    def test_a_signed_image_is_read_like_any_other(self):
        self.assertEqual(pe.read_cmdline(support.build_pe(b"a b", signed=True)), "a b")


class ElfTests(unittest.TestCase):
    def test_matches_readelf(self):
        if not support.tool_available("readelf"):
            self.skipTest("needs readelf")
        import re
        import subprocess
        checked = 0
        for path in ("/usr/bin/ls", "/usr/bin/python3", "/usr/lib/libc.so.6", "/bin/sh"):
            info = elf.read_dynamic(path)
            if info is None:
                continue
            out = subprocess.run(["readelf", "-d", path], capture_output=True, text=True).stdout
            self.assertEqual(info["needed"], re.findall(r"Shared library: \[(.*?)\]", out), path)
            checked += 1
        self.assertGreater(checked, 0)

    def test_non_elf_is_none(self):
        with tempfile.NamedTemporaryFile() as handle:
            handle.write(b"just text\n")
            handle.flush()
            self.assertIsNone(elf.read_dynamic(handle.name))
        self.assertIsNone(elf.read_dynamic("/nonexistent"))


def newc(entries):
    """Build a newc cpio stream. entries: (name, mode, data, extra) with extra = dict(nlink, ino)."""
    out = b""
    for index, (name, mode, data, extra) in enumerate(entries):
        raw = name.encode() + b"\0"
        fields = [extra.get("ino", index + 1), mode, 0, 0, extra.get("nlink", 1), 0, len(data), 0, 0, 0, 0, len(raw), 0]
        header = b"070701" + b"".join(b"%08X" % value for value in fields)
        out += header + raw + b"\0" * ((4 - (110 + len(raw)) % 4) % 4)
        out += data + b"\0" * ((4 - len(data) % 4) % 4)
    return out


def archive(entries):
    import io
    return io.BytesIO(newc(entries + [("TRAILER!!!", 0, b"", {})]))


class CpioTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def unpack(self, entries):
        return cpio.unpack(archive(entries), self.tmp)

    def test_files_directories_and_symlinks(self):
        count = self.unpack([("./usr", 0o040755, b"", {}), ("./usr/bin/tool", 0o100755, b"#!/bin/sh\n", {}),
                             ("./usr/lib/libx.so", 0o120777, b"libx.so.1", {})])
        self.assertEqual(count, 2)
        self.assertEqual(os.stat(os.path.join(self.tmp, "usr/bin/tool")).st_mode & 0o777, 0o755)
        self.assertEqual(os.readlink(os.path.join(self.tmp, "usr/lib/libx.so")), "libx.so.1")

    def test_hard_links_get_their_data_from_the_last_entry(self):
        self.unpack([("./usr/a", 0o100644, b"", {"nlink": 2, "ino": 7}),
                     ("./usr/b", 0o100644, b"payload", {"nlink": 2, "ino": 7})])
        with open(os.path.join(self.tmp, "usr/a"), "rb") as handle:
            self.assertEqual(handle.read(), b"payload")
        self.assertEqual(os.stat(os.path.join(self.tmp, "usr/a")).st_ino, os.stat(os.path.join(self.tmp, "usr/b")).st_ino)

    def test_dotdot_is_refused(self):
        with self.assertRaises(cpio.UnsafeArchive):
            self.unpack([("../escape", 0o100644, b"x", {})])
        self.assertFalse(os.path.exists(os.path.join(os.path.dirname(self.tmp), "escape")))

    def test_writing_through_a_symlink_is_refused(self):
        outside = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, outside, True)
        with self.assertRaises(cpio.UnsafeArchive):
            self.unpack([("./usr/evil", 0o120777, outside.encode(), {}),
                         ("./usr/evil/pwned", 0o100644, b"x", {})])
        self.assertEqual(os.listdir(outside), [])

    def test_absolute_names_stay_inside(self):
        self.unpack([("/usr/bin/x", 0o100755, b"x", {})])
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "usr/bin/x")))

    def test_devices_are_skipped(self):
        self.assertEqual(self.unpack([("./usr/dev", 0o060644, b"", {})]), 0)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "usr/dev")))

    def test_truncated_stream(self):
        import io
        with self.assertRaises(Failed):
            cpio.unpack(io.BytesIO(newc([("./x", 0o100644, b"abc", {})])[:60]), self.tmp)

    def test_unpacks_a_real_rpm_like_rpm_lists_it(self):
        import glob
        rpms = sorted(glob.glob(os.path.join(os.environ.get("ECP_MKOSI_DIR", "/nonexistent"), "mkosi.packages/foundationd-*.rpm")))
        if not rpms or not support.tool_available("rpm2cpio") or not support.tool_available("rpm"):
            self.skipTest("needs a built foundationd RPM and rpm tools")
        from foundationd.backends import compat
        path = rpms[0]
        compat.extract(path, self.tmp)
        listed = run(["rpm", "-qlp", "--nosignature", "--nodigest", path])[1].split()
        for filename in listed:
            target = os.path.join(self.tmp, filename.lstrip("/"))
            self.assertTrue(os.path.lexists(target), filename)


class OsInfoTests(unittest.TestCase):
    def test_mountinfo_parsing(self):
        with tempfile.NamedTemporaryFile("w", suffix="mi") as handle:
            handle.write("36 35 98:0 / / ro,relatime - erofs /dev/vda1 ro\n"
                         "40 36 0:5 / /var rw - ext4 /dev/vda4 rw\n"
                         "41 36 0:6 / /my\\040dir rw - tmpfs tmpfs rw\n")
            handle.flush()
            mounts = osinfo.mounts(handle.name)
        self.assertTrue(mounts["/"]["readonly"])
        self.assertEqual(mounts["/var"]["source"], "/dev/vda4")
        self.assertIn("/my dir", mounts)

    def test_os_release(self):
        self.assertEqual(osinfo.parse_os_release('NAME="Ersa Compute Platform"\n# c\nID=x\n')["NAME"], "Ersa Compute Platform")


@needs_sfdisk
class SlotDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.system = support.FakeSystem()
        self.addCleanup(self.system.cleanup)

    def discover(self):
        return slots.discover(self.system.paths, machine="x86_64")

    def test_the_installer_mediums_root_counts_as_the_running_slot_but_is_marked_live(self):
        """Its root is labelled ecp-live so an installed disk's ecp-root-a cannot be mistaken for it."""
        system = support.FakeSystem(("live:0.3.0",), running=1)
        self.addCleanup(shutil.rmtree, system.tmp, True)
        system.uki("0.3.0")
        layout = slots.discover(system.paths, machine="x86_64")
        self.assertEqual([(s.name, s.label, s.running, s.live) for s in layout.slots], [("0.3.0", "ecp-live", True, True)])
        self.assertIs(layout.running, layout.slots[0])
        self.assertEqual(layout.running.entry.file, "ecp_0.3.0.efi")

    def test_finds_slots_running_and_empty(self):
        self.system.uki("0.2.0", tries=None)
        layout = self.discover()
        self.assertEqual([(s.name, s.state) for s in layout.slots], [("0.2.0", "running"), ("b", "empty")])
        self.assertEqual(layout.running.name, "0.2.0")
        self.assertEqual(layout.running.entry.file, "ecp_0.2.0.efi")

    def test_state_without_efi_variables(self):
        self.system.uki("0.2.0")
        shutil.rmtree(self.system.paths["efivars"])
        state = slots.boot_state(self.system.paths)
        self.assertEqual(state["running_slot"], "0.2.0")
        self.assertEqual(state["assessment"], "unknown")

    def test_assessment_pending_and_good(self):
        self.system.uki("0.2.0", tries=2)
        self.system.efivar("LoaderEntrySelected", "ecp_0.2.0+2.efi")
        state = slots.boot_state(self.system.paths)
        self.assertEqual((state["assessment"], state["tries_left"]), ("pending", 2))
        os.rename(os.path.join(self.system.esp, "EFI/Linux/ecp_0.2.0+2.efi"),
                  os.path.join(self.system.esp, "EFI/Linux/ecp_0.2.0.efi"))
        self.assertEqual(slots.boot_state(self.system.paths)["assessment"], "good")

    def test_default_flag_ignores_boot_counters(self):
        self.system.uki("0.2.0")
        self.system.efivar("LoaderEntryDefault", "ecp_0.2.0+3.efi")
        self.assertTrue(self.discover().running.default)

    def test_root_not_on_a_partition(self):
        self.system.write_mountinfo([])
        with open(self.system.mountinfo, "w") as handle:
            handle.write("36 35 0:1 / / ro - erofs overlay ro\n")
        with self.assertRaises(Unavailable):
            self.discover()

    def test_unknown_slot(self):
        with self.assertRaises(NotFound):
            self.discover().find("nope")


class FakeRunner:
    def __init__(self, fail=None):
        self.calls, self.fail = [], fail

    def __call__(self, cmd, timeout=60, input_bytes=None):
        self.calls.append(cmd)
        if self.fail and cmd[0] == self.fail:
            return 1, "", "boom"
        return 0, "", ""


@needs_sfdisk
class SlotSelectTests(unittest.TestCase):
    def setUp(self):
        self.system = support.FakeSystem()
        self.addCleanup(self.system.cleanup)
        os.makedirs(self.system.paths["efivars"], exist_ok=True)

    def test_select_passes_the_counter_free_id_to_bootctl(self):
        self.system.uki("0.2.0", tries=3)
        runner = FakeRunner()
        result = slots.select(self.system.paths, "0.2.0", once=False, runner=runner)
        self.assertEqual(runner.calls, [["bootctl", "set-default", "ecp_0.2.0.efi"]])
        self.assertEqual(result["mode"], "default")

    def test_oneshot(self):
        self.system.uki("0.2.0")
        runner = FakeRunner()
        slots.select(self.system.paths, "0.2.0", once=True, runner=runner)
        self.assertEqual(runner.calls[0][1], "set-oneshot")

    def test_a_slot_without_a_kernel_image_is_empty_and_refused(self):
        # nothing names its partitions, so there is nothing to boot
        with self.assertRaisesRegex(InvalidArgument, "empty"):
            slots.select(self.system.paths, "a", once=False, runner=FakeRunner())

    def test_empty_slot_is_refused(self):
        with self.assertRaisesRegex(InvalidArgument, "empty"):
            slots.select(self.system.paths, "ecp-root-b", once=False, runner=FakeRunner())

    def test_not_booted_through_uefi(self):
        shutil.rmtree(self.system.paths["efivars"])
        self.system.uki("0.2.0")
        with self.assertRaises(Unavailable):
            slots.select(self.system.paths, "0.2.0", once=False, runner=FakeRunner())


@needs_sfdisk
class FindEspTests(unittest.TestCase):
    def test_a_lazily_mounted_esp_is_found_after_looking_at_it(self):
        system = support.FakeSystem()
        self.addCleanup(system.cleanup)
        paths = {k: v for k, v in system.paths.items() if k != "esp"}
        system.write_mountinfo(["40 36 0:30 / /efi rw - autofs systemd-1 rw"])
        looked = []

        def trigger(target):
            looked.append(target)           # systemd mounts the partition when something looks at the directory
            system.write_mountinfo(["40 36 0:30 / /efi rw - autofs systemd-1 rw", "41 40 259:3 / /efi rw - vfat /dev/vda3 rw"])
        mounts = osinfo.mounts(paths["mountinfo"])
        self.assertEqual(slots.find_esp(paths, mounts, trigger), "/efi")
        self.assertEqual(looked, ["/efi"])

    def test_an_autofs_that_never_mounts_is_no_esp(self):
        system = support.FakeSystem()
        self.addCleanup(system.cleanup)
        paths = {k: v for k, v in system.paths.items() if k != "esp"}
        system.write_mountinfo(["40 36 0:30 / /efi rw - autofs systemd-1 rw"])
        self.assertEqual(slots.find_esp(paths, osinfo.mounts(paths["mountinfo"]), lambda target: None), "")
        self.assertEqual(slots.find_esp(paths, osinfo.mounts(paths["mountinfo"]), lambda target: (_ for _ in ()).throw(OSError())), "")


@needs_sfdisk
class SlotReleaseTests(unittest.TestCase):
    def setUp(self):
        self.system = support.FakeSystem(slot_names=("0.2.0", "0.1.0"))
        self.addCleanup(self.system.cleanup)
        self.system.uki("0.2.0")
        self.system.uki("0.1.0", tries=1)

    def test_release_a_standby_slot(self):
        result = slots.release(self.system.paths, "0.1.0", support.Recorder())
        self.assertTrue(result["released"])
        table = {p.number: p for p in gpt.read_table(self.system.image)}
        self.assertEqual((table[3].name, table[4].name), ("ecp-root-b", "ecp-verity-b"), "labels never change")
        self.assertNotEqual(table[3].uuid, verity.uuids(support.hash_for("0.1.0"))[0], "nothing names the slot any more")
        self.assertEqual(os.listdir(os.path.join(self.system.esp, "EFI/Linux")), ["ecp_0.2.0.efi"])
        self.assertEqual(slots.discover(self.system.paths).slots[1].state, "empty")

    def test_the_installer_mediums_root_cannot_be_released(self):
        system = support.FakeSystem(("live:0.3.0", "0.2.0"), running=2)
        self.addCleanup(system.cleanup)
        system.uki("0.3.0")
        system.uki("0.2.0")
        with self.assertRaisesRegex(InvalidArgument, "installer medium"):
            slots.release(system.paths, "0.3.0", support.Recorder())

    def test_running_slot_cannot_be_released(self):
        with self.assertRaisesRegex(InvalidArgument, "running"):
            slots.release(self.system.paths, "0.2.0", support.Recorder())

    def test_never_release_the_only_bootable_slot(self):
        os.unlink(os.path.join(self.system.esp, "EFI/Linux/ecp_0.2.0.efi"))
        with self.assertRaisesRegex(InvalidArgument, "only slot"):
            slots.release(self.system.paths, "0.1.0", support.Recorder())

    def test_releasing_an_empty_slot_is_a_no_op(self):
        slots.release(self.system.paths, "0.1.0", support.Recorder())
        self.assertFalse(slots.release(self.system.paths, "ecp-root-b", support.Recorder())["released"])


if __name__ == "__main__":
    unittest.main()
