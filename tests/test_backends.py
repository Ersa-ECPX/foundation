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
from foundationd.backends import cpio, elf, gpt, osinfo, pe, slots  # noqa: E402
from foundationd.backends.runner import run  # noqa: E402
from foundationd.errors import Failed, InvalidArgument, NotFound, Unavailable  # noqa: E402

needs_sfdisk = unittest.skipUnless(support.tool_available("sfdisk"), "needs sfdisk")


class GptTests(unittest.TestCase):
    @needs_sfdisk
    def test_reads_a_real_table(self):
        system = support.FakeSystem()
        self.addCleanup(system.cleanup)
        table = gpt.read_table(system.image)
        self.assertEqual([(p.number, p.name) for p in table], [(1, "ecp-root_0.2.0"), (2, "_empty"), (3, "ECP-ESP")])
        self.assertEqual(table[0].size, 4 * support.MIB)
        self.assertEqual(table[0].type, gpt.ROOT_TYPES["x86_64"])

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
    def test_replaces_the_root_token_and_keeps_everything_else(self):
        image = support.build_pe(b"quiet root=PARTLABEL=ecp-root_0.2.0 rw")
        patched = pe.replace_cmdline(image, "root=PARTLABEL=ecp-root_0.2.0", "root=PARTLABEL=ecp-root_0.2.0-copy")
        self.assertEqual(pe.read_cmdline(patched), "quiet root=PARTLABEL=ecp-root_0.2.0-copy rw")
        self.assertEqual(len(patched), len(image))
        table = 0x44 + 20 + 240                                    # section table
        self.assertEqual(image[:table + 40], patched[:table + 40])  # headers and the .linux entry untouched
        self.assertEqual(image[0x400:0x600], patched[0x400:0x600])  # .linux contents untouched

    def test_other_tools_read_the_patched_section(self):
        if not (support.tool_available("objcopy")):
            self.skipTest("needs objcopy")
        import subprocess
        patched = pe.replace_cmdline(support.build_pe(b"a root=x b"), "root=x", "root=yy")
        with tempfile.TemporaryDirectory() as tmp:
            source, dump = os.path.join(tmp, "uki.efi"), os.path.join(tmp, "cmdline")
            with open(source, "wb") as handle:
                handle.write(patched)
            result = subprocess.run(["objcopy", "-O", "binary", "--only-section=.cmdline", source, dump],
                                    capture_output=True)
            if result.returncode != 0:
                self.skipTest("objcopy cannot read this synthetic PE")
            with open(dump, "rb") as handle:
                self.assertEqual(handle.read().rstrip(b"\0"), b"a root=yy b")

    def test_signed_image_is_refused(self):
        with self.assertRaisesRegex(Failed, "signed"):
            pe.replace_cmdline(support.build_pe(b"root=x", signed=True), "root=x", "root=y")

    def test_token_must_appear_exactly_once(self):
        with self.assertRaisesRegex(Failed, "exactly once"):
            pe.replace_cmdline(support.build_pe(b"a b"), "root=x", "root=y")
        with self.assertRaisesRegex(Failed, "exactly once"):
            pe.replace_cmdline(support.build_pe(b"root=x root=x"), "root=x", "root=y")

    def test_a_partial_token_does_not_match(self):
        with self.assertRaisesRegex(Failed, "exactly once"):
            pe.replace_cmdline(support.build_pe(b"root=xyz"), "root=x", "root=y")

    def test_result_that_does_not_fit_is_refused(self):
        image = support.build_pe(b"root=x " + b"a" * 400, raw_size=512)
        with self.assertRaisesRegex(Failed, "does not fit"):
            pe.replace_cmdline(image, "root=x", "root=" + "y" * 200)

    def test_no_cmdline_section(self):
        with self.assertRaisesRegex(Failed, "no .cmdline"):
            pe.replace_cmdline(support.build_pe(b"", with_cmdline=False), "a", "b")

    def test_not_a_pe(self):
        with self.assertRaises(Failed):
            pe.replace_cmdline(b"not a pe image", "a", "b")


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
        rpms = sorted(glob.glob(os.path.join(HERE, "../../mkosi.packages/foundationd-*.rpm")))
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

    def test_the_installer_mediums_root_counts_as_the_running_slot(self):
        """Its root is labelled ecp-live_<version> so an installed disk's ecp-root_<version> cannot be mistaken for it."""
        system = support.FakeSystem(("live:0.3.0",), running=1)
        self.addCleanup(shutil.rmtree, system.tmp, True)
        system.uki("0.3.0")
        layout = slots.discover(system.paths, machine="x86_64")
        self.assertEqual([(s.name, s.label, s.running) for s in layout.slots], [("0.3.0", "ecp-live_0.3.0", True)])
        self.assertIs(layout.running, layout.slots[0])
        self.assertEqual(layout.running.entry.file, "ecp_0.3.0.efi")

    def test_finds_slots_running_and_empty(self):
        self.system.uki("0.2.0", tries=None)
        layout = self.discover()
        self.assertEqual([(s.name, s.state) for s in layout.slots], [("0.2.0", "running"), ("empty2", "empty")])
        self.assertEqual(layout.running.name, "0.2.0")
        self.assertEqual(layout.running.entry.file, "ecp_0.2.0.efi")

    def test_state_without_efi_variables(self):
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

    def test_a_slot_without_a_kernel_image_is_refused(self):
        with self.assertRaisesRegex(InvalidArgument, "no kernel image"):
            slots.select(self.system.paths, "0.2.0", once=False, runner=FakeRunner())

    def test_empty_slot_is_refused(self):
        with self.assertRaisesRegex(InvalidArgument, "empty"):
            slots.select(self.system.paths, "empty2", once=False, runner=FakeRunner())

    def test_not_booted_through_uefi(self):
        shutil.rmtree(self.system.paths["efivars"])
        self.system.uki("0.2.0")
        with self.assertRaises(Unavailable):
            slots.select(self.system.paths, "0.2.0", once=False, runner=FakeRunner())


@needs_sfdisk
class SlotCopyTests(unittest.TestCase):
    FS_BYTES = 1 * support.MIB

    def setUp(self):
        self.system = support.FakeSystem()
        self.addCleanup(self.system.cleanup)
        random.seed(7)
        self.data = bytes(random.getrandbits(8) for _ in range(self.FS_BYTES + 4096))
        self.system.fill_slot(1, self.data, erofs_bytes=self.FS_BYTES)
        self.data = self.partition_bytes(1, self.FS_BYTES + 4096)   # what is really in the source
        self.system.uki("0.2.0", tries=None)

    def copy(self, ctx=None, **kw):
        ctx = ctx or support.Recorder()
        args = dict(source_name="", target_name="empty2", force=False)
        args.update(kw)
        return slots.copy(self.system.paths, args["source_name"], args["target_name"], args["force"], ctx,
                          runner=run, opener=self.system.opener()), ctx

    def labels(self):
        return {p.number: p.name for p in gpt.read_table(self.system.image)}

    def partition_bytes(self, number, length):
        part = [p for p in gpt.read_table(self.system.image) if p.number == number][0]
        with open(self.system.image, "rb") as handle:
            handle.seek(part.offset)
            return handle.read(length)

    def test_copy_into_an_empty_slot(self):
        result, ctx = self.copy()
        self.assertEqual(result["slot"], "0.2.0-copy")
        self.assertEqual(result["bytes"], self.FS_BYTES)
        self.assertEqual(self.labels()[2], "ecp-root_0.2.0-copy")
        self.assertEqual(self.partition_bytes(2, self.FS_BYTES), self.data[:self.FS_BYTES])
        self.assertEqual(ctx.steps, ["prepare", "invalidate", "copy", "verify", "label", "image"])
        self.assertTrue(ctx.committed)
        self.assertEqual(max(ctx.progress_values), 1.0)

    def test_the_copy_gets_its_own_kernel_image_naming_its_own_root(self):
        self.copy()
        uki = os.path.join(self.system.esp, "EFI/Linux/ecp_0.2.0-copy+3.efi")
        with open(uki, "rb") as handle:
            self.assertEqual(pe.read_cmdline(handle.read()), "quiet root=PARTLABEL=ecp-root_0.2.0-copy rw")
        with open(os.path.join(self.system.esp, "EFI/Linux/ecp_0.2.0.efi"), "rb") as handle:
            self.assertIn("root=PARTLABEL=ecp-root_0.2.0 ", pe.read_cmdline(handle.read()) + " ")  # source untouched

    def test_the_source_slot_is_never_modified(self):
        before = self.partition_bytes(1, self.FS_BYTES + 4096)
        self.copy()
        self.assertEqual(self.partition_bytes(1, self.FS_BYTES + 4096), before)
        self.assertEqual(self.labels()[1], "ecp-root_0.2.0")

    def test_the_running_slot_cannot_be_a_target(self):
        self.copy()                                        # slot 2 now holds a populated copy
        with self.assertRaisesRegex(InvalidArgument, "running slot"):
            self.copy(source_name="0.2.0-copy", target_name="0.2.0", force=True)

    def test_same_source_and_target(self):
        with self.assertRaises(InvalidArgument):
            self.copy(source_name="empty2", target_name="empty2")

    def test_a_slot_in_use_needs_force_and_nothing_changes_without_it(self):
        self.copy()                       # now slot 2 holds 0.2.0-copy
        before = (self.labels(), sorted(os.listdir(os.path.join(self.system.esp, "EFI/Linux"))))
        with self.assertRaisesRegex(InvalidArgument, "force"):
            self.copy(target_name="0.2.0-copy")
        self.assertEqual((self.labels(), sorted(os.listdir(os.path.join(self.system.esp, "EFI/Linux")))), before)

    def test_force_overwrites_a_slot_in_use(self):
        self.copy()
        self.system.fill_slot(2, b"\xaa" * 8192)          # damage the copy
        result, _ctx = self.copy(target_name="0.2.0-copy", force=True)
        self.assertEqual(self.partition_bytes(2, self.FS_BYTES), self.data[:self.FS_BYTES])
        self.assertEqual(self.labels()[2], "ecp-root_0.2.0-copy")
        self.assertEqual(result["slot"], "0.2.0-copy")

    def test_a_signed_kernel_image_fails_before_anything_is_destroyed(self):
        os.unlink(os.path.join(self.system.esp, "EFI/Linux/ecp_0.2.0.efi"))
        self.system.uki("0.2.0", signed=True)
        ctx = support.Recorder()
        with self.assertRaisesRegex(Failed, "signed"):
            self.copy(ctx)
        self.assertFalse(ctx.committed)
        self.assertEqual(self.labels()[2], "_empty")

    def test_source_without_a_kernel_image(self):
        os.unlink(os.path.join(self.system.esp, "EFI/Linux/ecp_0.2.0.efi"))
        with self.assertRaisesRegex(InvalidArgument, "no kernel image"):
            self.copy()

    def test_a_target_that_is_too_small(self):
        system = support.FakeSystem(slot_names=("0.2.0", "empty"), root_mib=4)
        self.addCleanup(system.cleanup)
        system.fill_slot(1, b"x" * 4096, erofs_bytes=3 * support.MIB)
        system.uki("0.2.0")
        # Pretend the filesystem is bigger than the partition by shrinking the target: use a 1 MiB table.
        support.sfdisk(system.image, "label: gpt\nfirst-lba: 2048\n\n"
                       f'size=4M, type={support.ROOT_TYPE}, name="ecp-root_0.2.0"\n'
                       f'size=1M, type={support.ROOT_TYPE}, name="_empty"\n'
                       f'size=8M, type={support.ESP_TYPE}, name="ECP-ESP"\n')
        system.fill_slot(1, b"x" * 4096, erofs_bytes=3 * support.MIB)
        with self.assertRaisesRegex(InvalidArgument, "smaller"):
            slots.copy(system.paths, "", "empty2", False, support.Recorder(), opener=system.opener())

    def test_a_failed_verification_leaves_the_target_empty_and_unbootable(self):
        real_opener = self.system.opener()

        class Corrupting(support.SliceFile):
            def write(self, data):
                return super().write(bytes([data[0] ^ 0xFF]) + data[1:])

        def opener(path, mode="rb"):
            if "+" in mode:
                from foundationd.backends import gpt as g
                part = [p for p in g.read_table(self.system.image) if p.number == 2][0]
                return Corrupting(self.system.image, part.offset, part.size, mode)
            return real_opener(path, mode)

        with self.assertRaisesRegex(Failed, "does not match"):
            slots.copy(self.system.paths, "", "empty2", False, support.Recorder(), opener=opener)
        self.assertEqual(self.labels()[2], "_empty")
        self.assertNotIn("ecp_0.2.0-copy+3.efi", os.listdir(os.path.join(self.system.esp, "EFI/Linux")))

    def test_old_kernel_image_of_the_overwritten_slot_is_removed(self):
        self.system.fill_slot(2, b"old" * 100)
        support.sfdisk_label = None
        import subprocess
        subprocess.run(["sfdisk", "--part-label", self.system.image, "2", "ecp-root_0.1.0"], check=True, capture_output=True)
        self.system.uki("0.1.0")
        self.copy(target_name="0.1.0", force=True)
        files = os.listdir(os.path.join(self.system.esp, "EFI/Linux"))
        self.assertNotIn("ecp_0.1.0.efi", files)
        self.assertIn("ecp_0.2.0-copy+3.efi", files)

    def test_copying_a_copy_does_not_stack_suffixes(self):
        self.assertEqual(slots.new_slot_name("0.2.0"), "0.2.0-copy")
        self.assertEqual(slots.new_slot_name("0.2.0-copy"), "0.2.0-copy")

    def test_the_target_must_not_be_mounted(self):
        self.system.write_mountinfo([f"40 36 0:5 / /mnt rw - ext4 {self.system.paths['dev']}/vda2 rw"])
        with self.assertRaisesRegex(InvalidArgument, "mounted"):
            self.copy()

    def test_relabel_failure_is_detected_even_if_the_tool_lies(self):
        layout = slots.discover(self.system.paths, machine="x86_64")
        with self.assertRaisesRegex(Failed, "did not change"):
            slots._relabel(layout, layout.slots[1], "ecp-root_zzz", FakeRunner())   # reports success, changes nothing


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
        self.assertEqual({p.number: p.name for p in gpt.read_table(self.system.image)}[2], "_empty")
        self.assertEqual(os.listdir(os.path.join(self.system.esp, "EFI/Linux")), ["ecp_0.2.0.efi"])

    def test_running_slot_cannot_be_released(self):
        with self.assertRaisesRegex(InvalidArgument, "running"):
            slots.release(self.system.paths, "0.2.0", support.Recorder())

    def test_never_release_the_only_bootable_slot(self):
        os.unlink(os.path.join(self.system.esp, "EFI/Linux/ecp_0.2.0.efi"))
        with self.assertRaisesRegex(InvalidArgument, "only slot"):
            slots.release(self.system.paths, "0.1.0", support.Recorder())

    def test_releasing_an_empty_slot_is_a_no_op(self):
        slots.release(self.system.paths, "0.1.0", support.Recorder())
        self.assertFalse(slots.release(self.system.paths, "empty2", support.Recorder())["released"])


if __name__ == "__main__":
    unittest.main()
