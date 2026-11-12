"""Slots named by their root hash, and installing a dm-verity update into one: the label never changes, the partitions are named
by the hash, and the signed kernel image lands byte for byte as it came."""

import hashlib
import json
import os
import random
import shutil
import sys
import tempfile
import unittest
import uuid

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

import support  # noqa: E402
from foundationd.backends import gpt, slots, updates, verity  # noqa: E402
from foundationd.backends.runner import run  # noqa: E402
from foundationd.errors import Failed, InvalidArgument  # noqa: E402
from test_updates import RUNNING, tar_bytes  # noqa: E402

MIB = 1024 * 1024
ROOT_TYPE, VERITY_TYPE = str(gpt.ROOT_TYPES["x86_64"]), str(verity.ROOT_VERITY_TYPES["x86_64"])
RUNNING_HASH = "aa" * 32
NEW_HASH = "bb" * 16 + "cc" * 16


def cmdline(roothash):
    return f"ro quiet splash roothash={roothash} ecp.pad=----------------"


class VeritySystem(support.FakeSystem):
    """root-a (running, version 0.2.0), verity-a, root-b (empty), verity-b, then the ESP."""

    def __init__(self):
        super().__init__(slot_names=("empty",) * 4, running=1, root_mib=4)
        data_uuid, hash_uuid = verity.uuids(RUNNING_HASH)
        script = "label: gpt\nfirst-lba: 2048\n\n"
        script += f'size=4M, type={ROOT_TYPE}, name="ecp-root-a", uuid={data_uuid}\n'
        script += f'size=1M, type={VERITY_TYPE}, name="ecp-verity-a", uuid={hash_uuid}\n'
        script += f'size=4M, type={ROOT_TYPE}, name="ecp-root-b", uuid={uuid.uuid4()}\n'
        script += f'size=1M, type={VERITY_TYPE}, name="ecp-verity-b", uuid={uuid.uuid4()}\n'
        script += f'size=8M, type={support.ESP_TYPE}, name="ECP-ESP"\n'
        support.sfdisk(self.image, script)
        with open(os.path.join(self.esp, "EFI/Linux/ecp_0.2.0.efi"), "wb") as handle:
            handle.write(support.build_pe(cmdline(RUNNING_HASH).encode(), raw_size=1024))


@unittest.skipUnless(support.tool_available("sfdisk"), "needs sfdisk")
class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        random.seed(5)
        cls.root = bytes(random.getrandbits(8) for _ in range(MIB))
        cls.tree = bytes(random.getrandbits(8) for _ in range(64 * 1024))

    def setUp(self):
        self.system = VeritySystem()
        self.addCleanup(self.system.cleanup)
        self.paths = self.system.paths
        self.ctx = support.Recorder()

    def uki(self, roothash=NEW_HASH):
        self.uki_bytes = support.build_pe(cmdline(roothash).encode(), raw_size=1024)
        return self.uki_bytes

    def manifest(self, **changes):
        uki = self.uki()
        data = {"format": 2, "name": "ecp", "version": "0.3.0", "arch": os.uname().machine, "roothash": NEW_HASH,
                "root": {"file": "root.erofs", "sha256": hashlib.sha256(self.root).hexdigest(), "size": len(self.root)},
                "verity": {"file": "verity.img", "sha256": hashlib.sha256(self.tree).hexdigest(), "size": len(self.tree)},
                "uki": {"file": "uki.efi", "sha256": hashlib.sha256(uki).hexdigest(), "size": len(uki)}}
        data.update(changes)
        data = {k: v for k, v in data.items() if v is not None}
        return json.dumps(data).encode()

    def archive(self, manifest=None, tree=None, with_tree=True, uki=None):
        manifest = manifest or self.manifest()
        entries = [("manifest.json", manifest), ("root.erofs", self.root), ("uki.efi", uki or self.uki_bytes)]
        if with_tree:
            entries.append(("verity.img", self.tree if tree is None else tree))
        path = os.path.join(self.system.tmp, "update.tar")
        with open(path, "wb") as handle:
            handle.write(tar_bytes(entries))
        return path

    def install(self, path, opener=None, **kw):
        kw.setdefault("unsigned", True)
        return updates.install(self.paths, path, self.ctx, runner=run, opener=opener or self.system.opener(),
                               mount=self.fake_mount, **kw)

    def fake_mount(self, device):
        import contextlib

        @contextlib.contextmanager
        def mounted():
            root = tempfile.mkdtemp(dir=self.system.tmp)
            os.makedirs(os.path.join(root, "usr/lib"))
            with open(os.path.join(root, "usr/lib/os-release"), "w") as handle:
                handle.write('ID="opensuse-tumbleweed"\nIMAGE_ID="ecp"\nIMAGE_VERSION="0.3.0"\n')
            yield root
        return mounted()

    def table(self):
        return {p.number: p for p in gpt.read_table(self.system.image)}

    def part_bytes(self, number, length):
        part = self.table()[number]
        with open(self.system.image, "rb") as handle:
            handle.seek(part.offset)
            return handle.read(length)

    def untouched(self):
        table = self.table()
        self.assertEqual(table[3].name, "ecp-root-b")
        self.assertNotEqual(table[3].uuid, verity.uuids(NEW_HASH)[0])
        self.assertEqual(sorted(os.listdir(os.path.join(self.system.esp, "EFI/Linux"))), ["ecp_0.2.0.efi"])


class Discovery(Base):
    def test_slots_are_found_by_the_root_hash_not_by_a_label_with_a_version(self):
        layout = slots.discover(self.paths)
        a, b = layout.slots
        self.assertEqual((a.name, a.label, a.state, a.running), ("0.2.0", "ecp-root-a", "running", True))
        self.assertEqual((b.name, b.label, b.state), ("b", "ecp-root-b", "empty"))
        self.assertEqual((a.verity_number, b.verity_number), (2, 4))
        self.assertIs(layout.running, a)
        self.assertTrue(slots.slots(self.paths)[0]["verity"])

    def test_a_kernel_image_without_a_root_hash_matches_no_slot(self):
        path = os.path.join(self.system.esp, "EFI/Linux/ecp_0.2.0.efi")
        with open(path, "wb") as handle:
            handle.write(support.build_pe(RUNNING.encode(), raw_size=1024))
        self.assertEqual(slots.discover(self.paths).slots[0].state, "empty")


class Installing(Base):
    def test_an_update_lands_named_by_its_hash_with_the_kernel_image_untouched(self):
        result = self.install(self.archive())
        self.assertEqual((result["version"], result["label"]), ("0.3.0", "ecp-root-b"))
        table = self.table()
        self.assertEqual(table[3].name, "ecp-root-b", "the label never changes")
        self.assertEqual((table[3].uuid, table[4].uuid), verity.uuids(NEW_HASH))
        self.assertEqual(self.part_bytes(3, len(self.root)), self.root)
        self.assertEqual(self.part_bytes(4, len(self.tree)), self.tree)
        landed = os.path.join(self.system.esp, "EFI/Linux/ecp_0.3.0+3.efi")
        with open(landed, "rb") as handle:
            self.assertEqual(handle.read(), self.uki_bytes, "the signed kernel image must be copied byte for byte")
        slot = slots.discover(self.paths).slots[1]
        self.assertEqual((slot.name, slot.state), ("0.3.0", "standby"))
        self.assertEqual(slots.discover(self.paths).slots[0].state, "running", "the running slot is untouched")

    def test_the_running_slot_is_not_touched(self):
        before = self.part_bytes(1, 4096), self.table()[1].uuid, self.table()[2].uuid
        self.install(self.archive())
        self.assertEqual((self.part_bytes(1, 4096), self.table()[1].uuid, self.table()[2].uuid), before)

    def test_a_kernel_image_with_another_root_hash_is_refused(self):
        # a manifest that is self-consistent (it names this kernel image's checksum) but whose root hash is not the image's
        wrong = self.uki("dd" * 32)
        manifest = self.manifest(uki={"file": "uki.efi", "sha256": hashlib.sha256(wrong).hexdigest(), "size": len(wrong)})
        self.refused(self.archive(manifest=manifest, uki=wrong), "does not carry the root hash")

    def test_a_hash_tree_that_does_not_match_the_manifest(self):
        self.refused(self.archive(tree=self.tree[:-1] + b"X"), "checksum")

    def test_a_hash_tree_too_large_for_the_hash_partition(self):
        self.tree = b"\0" * (2 * MIB)
        self.refused(self.archive(), "hash partition", InvalidArgument)

    def test_a_format_2_update_without_its_hash_tree(self):
        self.refused(self.archive(with_tree=False), "incomplete")

    def test_a_format_2_manifest_needs_a_usable_root_hash(self):
        for bad in ("zz", "ab" * 31, None):
            self.refused(self.archive(manifest=self.manifest(roothash=bad)), "roothash")

    def test_a_format_1_update_cannot_go_into_a_verity_slot(self):
        manifest = self.manifest(format=1, roothash=None, verity=None)
        path = self.archive(manifest=manifest, with_tree=False)
        self.refused(path, "format 2", InvalidArgument)

    def test_a_failed_write_leaves_the_slot_unbootable_and_the_running_slot_alone(self):
        real = self.system.opener()
        calls = []

        def flaky(path, mode="rb"):
            calls.append(path)
            if path.endswith("vda4") and "r+b" in mode:
                raise OSError("disk went away")
            return real(path, mode)
        with self.assertRaises(OSError):
            self.install(self.archive(), opener=flaky)
        table = self.table()
        self.assertNotEqual(table[3].uuid, verity.uuids(NEW_HASH)[0], "no kernel image may match a half-written slot")
        self.assertEqual(slots.discover(self.paths).slots[0].state, "running")
        self.assertEqual(slots.discover(self.paths).slots[1].state, "empty")
        self.assertEqual(sorted(os.listdir(os.path.join(self.system.esp, "EFI/Linux"))), ["ecp_0.2.0.efi"])

    def refused(self, path, pattern, exception=Failed):
        with self.assertRaisesRegex(exception, pattern):
            self.install(path)
        self.untouched()


@unittest.skipUnless(os.uname().machine == "x86_64" and shutil.which("zstd"), "needs x86_64 and zstd")
class ArchiveScript(Base):
    """scripts/make-update-archive makes a format 2 archive from the verity outputs, and the installer takes it."""

    def test_the_archive_script_output_installs_into_a_verity_slot(self):
        import subprocess
        script = os.path.join(HERE, "..", "..", "scripts", "make-update-archive")
        if not os.path.exists(script):
            self.skipTest("scripts/ is not part of this checkout")
        work = os.path.join(self.system.tmp, "mk")
        os.makedirs(os.path.join(work, "src"))
        for name, data in (("root-x86-64", self.root), ("root-verity-x86-64", self.tree)):
            raw = os.path.join(work, name)
            with open(raw, "wb") as handle:
                handle.write(data)
            subprocess.run(["zstd", "-q", raw, "-o", os.path.join(work, f"src/ecp_0.3.0.{name}.raw.zst")], check=True)
        with open(os.path.join(work, "src/ecp_0.3.0.roothash"), "w") as handle:
            handle.write(NEW_HASH + "\n")
        with open(os.path.join(work, "src/ecp_0.3.0.efi"), "wb") as handle:
            handle.write(self.uki())
        subprocess.run([sys.executable, script, "0.3.0", "--source", os.path.join(work, "src"), "--output", os.path.join(work, "out")],
                       check=True, capture_output=True)
        archive = os.path.join(work, "out", "ecp_0.3.0-x86_64.tar.zst")
        result = self.install(archive)
        self.assertEqual((result["version"], result["label"]), ("0.3.0", "ecp-root-b"))
        self.assertEqual(self.part_bytes(4, len(self.tree)), self.tree)

    def test_a_roothash_without_its_hash_tree_is_not_made_into_an_archive(self):
        import subprocess
        script = os.path.join(HERE, "..", "..", "scripts", "make-update-archive")
        if not os.path.exists(script):
            self.skipTest("scripts/ is not part of this checkout")
        work = os.path.join(self.system.tmp, "mk2")
        os.makedirs(os.path.join(work, "src"))
        raw = os.path.join(work, "r")
        with open(raw, "wb") as handle:
            handle.write(self.root)
        subprocess.run(["zstd", "-q", raw, "-o", os.path.join(work, "src/ecp_0.3.0.root-x86-64.raw.zst")], check=True)
        with open(os.path.join(work, "src/ecp_0.3.0.efi"), "wb") as handle:
            handle.write(self.uki())
        with open(os.path.join(work, "src/ecp_0.3.0.roothash"), "w") as handle:
            handle.write(NEW_HASH)
        done = subprocess.run([sys.executable, script, "0.3.0", "--source", os.path.join(work, "src"), "--output", os.path.join(work, "out")],
                              capture_output=True, text=True)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("needs both", done.stderr)


if __name__ == "__main__":
    unittest.main()
