"""Installing a system image update: the archive checks, the slot writes, the next boot.

Every slot is a dm-verity pair: the root partition and its hash partition, labelled ecp-root-a/-b and ecp-verity-a/-b (the labels
never change), named by the root hash (their GPT UUIDs). An update writes both into the standby slot, names them by the hash and
copies the signed kernel image exactly as it came."""

import contextlib
import hashlib
import io
import json
import os
import random
import shutil
import subprocess
import sys
import tarfile
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
from test_repos import GPG, _gpg_key, _sign  # noqa: E402

MIB = 1024 * 1024
ROOT_TYPE, VERITY_TYPE = str(gpt.ROOT_TYPES["x86_64"]), str(verity.ROOT_VERITY_TYPES["x86_64"])
NEW_HASH = "bb" * 16 + "cc" * 16


def tar_bytes(entries):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name, data in entries:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def cmdline(roothash):
    return f"ro quiet splash roothash={roothash} ecp.pad=----------------"


@unittest.skipUnless(support.tool_available("sfdisk"), "needs sfdisk")
class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        random.seed(5)
        cls.root = bytes(random.getrandbits(8) for _ in range(MIB))
        cls.tree = bytes(random.getrandbits(8) for _ in range(64 * 1024))
        cls.keys = tempfile.mkdtemp()
        if GPG:
            cls.good_key = _gpg_key(os.path.join(cls.keys, "good"), "good")
            cls.bad_key = _gpg_key(os.path.join(cls.keys, "bad"), "bad")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.keys, True)

    def setUp(self):
        self.system = support.FakeSystem()          # slot a runs 0.2.0, slot b is empty
        self.system.uki("0.2.0")
        self.addCleanup(self.system.cleanup)
        self.paths = self.system.paths
        self.ctx = support.Recorder()

    def uki(self, roothash=NEW_HASH):
        self.uki_bytes = support.build_pe(cmdline(roothash).encode(), raw_size=1024)
        return self.uki_bytes

    def manifest(self, **changes):
        uki = self.uki()
        data = {"format": 1, "name": "ecp", "version": "0.3.0", "arch": os.uname().machine, "roothash": NEW_HASH,
                "root": {"file": "root.erofs", "sha256": hashlib.sha256(self.root).hexdigest(), "size": len(self.root)},
                "verity": {"file": "verity.img", "sha256": hashlib.sha256(self.tree).hexdigest(), "size": len(self.tree)},
                "uki": {"file": "uki.efi", "sha256": hashlib.sha256(uki).hexdigest(), "size": len(uki)}}
        data.update(changes)
        data = {k: v for k, v in data.items() if v is not None}
        return json.dumps(data).encode()

    def archive(self, manifest=None, tree=None, with_tree=True, uki=None, root=None, extra=(), signed_by=None, order=None):
        manifest = manifest or self.manifest()
        entries = [("manifest.json", manifest)]
        if signed_by:
            path = os.path.join(self.keys, "manifest.json")
            with open(path, "wb") as handle:
                handle.write(manifest)
            _sign(os.path.join(self.keys, signed_by), signed_by, path, path + ".asc")
            with open(path + ".asc", "rb") as handle:
                entries.append(("manifest.json.sig", handle.read()))
        entries += [("root.erofs", self.root if root is None else root), ("uki.efi", uki or self.uki_bytes)]
        if with_tree:
            entries.append(("verity.img", self.tree if tree is None else tree))
        entries += list(extra)
        if order:
            entries = [entries[i] for i in order]
        path = os.path.join(self.system.tmp, "update.tar")
        with open(path, "wb") as handle:
            handle.write(tar_bytes(entries))
        return path

    def trust(self, key):
        directory = os.path.join(self.paths["root"], "usr/share/foundation/keys")
        os.makedirs(directory, exist_ok=True)
        shutil.copy(key, os.path.join(directory, "ersa-image-test.asc"))

    def install(self, path, opener=None, **kw):
        kw.setdefault("unsigned", True)
        kw.setdefault("mount", self.fake_mount)
        return updates.install(self.paths, path, self.ctx, runner=run, opener=opener or self.system.opener(), **kw)

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
        self.assertEqual([x["live"] for x in slots.slots(self.paths)], [False, False])

    def test_a_kernel_image_without_a_root_hash_matches_no_slot(self):
        path = os.path.join(self.system.esp, "EFI/Linux/ecp_0.2.0.efi")
        with open(path, "wb") as handle:
            handle.write(support.build_pe(b"quiet root=PARTUUID=aaaa ro", raw_size=1024))
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

    def test_an_update_without_its_hash_tree(self):
        self.refused(self.archive(with_tree=False), "incomplete")

    def test_a_manifest_needs_a_usable_root_hash(self):
        for bad in ("zz", "ab" * 31, None):
            self.refused(self.archive(manifest=self.manifest(roothash=bad)), "roothash")

    def test_an_update_of_another_format_is_refused(self):
        self.refused(self.archive(manifest=self.manifest(format=9)), "format")
        self.refused(self.archive(manifest=self.manifest(format=2)), "format")

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

    def test_every_step_runs_and_the_install_is_committed(self):
        result = self.install(self.archive())
        self.assertEqual((result["entry"], result["reboot_required"]), ("ecp_0.3.0+3.efi", True))
        for step in ("verify", "prepare", "write", "label", "image", "bundles", "boot"):
            self.assertIn(step, self.ctx.steps)
        self.assertTrue(self.ctx.committed)

    @unittest.skipUnless(shutil.which("zstd"), "needs zstd")
    def test_a_zstd_compressed_archive_is_read(self):
        path = self.archive()
        subprocess.run(["zstd", "-q", "-f", path, "-o", path + ".zst"], check=True)
        self.assertEqual(self.install(path + ".zst")["version"], "0.3.0")
        self.assertEqual(self.part_bytes(3, len(self.root)), self.root)

    def test_a_restart_is_recorded_for_whoever_wants_to_tell_the_user(self):
        self.install(self.archive())
        waiting = updates.pending(self.paths)
        self.assertEqual((waiting["version"], waiting["slot"]), ("0.3.0", "0.3.0"))
        self.assertEqual(oct(os.stat(updates.pending_path(self.paths)).st_mode & 0o777), "0o644")
        self.assertFalse(updates.clear_pending(self.paths), "still running the old image")
        with open(self.paths["os_release"], "w") as handle:
            handle.write("IMAGE_ID=ecp\nIMAGE_VERSION=0.3.0\n")
        self.assertTrue(updates.clear_pending(self.paths))
        self.assertEqual(updates.pending(self.paths), {})

    def test_bundles_are_prepared_against_the_new_image(self):
        mounted = []

        def mount(device):
            mounted.append(device)
            return self.fake_mount(device)
        self.install(self.archive(), mount=mount)
        self.assertEqual(mounted, [self.system.paths["dev"] + "/vda3"])

    def test_the_default_boot_entry_is_set_when_booted_through_uefi(self):
        calls = []

        def runner(cmd, timeout=60, input_bytes=None):
            calls.append(cmd)
            return run(cmd, timeout, input_bytes) if cmd[0] == "sfdisk" else (0, "", "")
        self.system.efivar("LoaderInfo", "systemd-boot")
        updates.install(self.paths, self.archive(), self.ctx, unsigned=True, runner=runner, opener=self.system.opener(),
                        mount=self.fake_mount)
        self.assertIn(["bootctl", "set-default", "ecp_0.3.0.efi"], calls)

    def test_without_uefi_the_install_still_completes_and_says_what_to_do(self):
        shutil.rmtree(self.paths["efivars"])
        result = self.install(self.archive())
        self.assertFalse(result["default_set"])
        self.assertTrue(any("boot menu" in note for note in result["notes"]))

    def test_the_version_already_running_is_refused(self):
        with self.assertRaisesRegex(InvalidArgument, "already runs"):
            self.install(self.archive(manifest=self.manifest(version="0.2.0")))
        self.untouched()

    def test_installing_the_same_version_twice_needs_force(self):
        self.install(self.archive())
        with self.assertRaisesRegex(InvalidArgument, "already installed"):
            self.install(self.archive())
        self.install(self.archive(), force=True)

    def test_the_installer_mediums_own_root_cannot_install_updates(self):
        table = self.table()
        # relabel the running root as the installer medium's
        subprocess.run(["sfdisk", "--part-label", self.system.image, "1", "ecp-live"], check=True, capture_output=True)
        subprocess.run(["sfdisk", "--part-label", self.system.image, "2", "ecp-live-verity"], check=True, capture_output=True)
        self.assertTrue(table)
        with self.assertRaisesRegex(Exception, "not an installed system"):
            self.install(self.archive())

    def refused(self, path, pattern, exception=Failed):
        with self.assertRaisesRegex(exception, pattern):
            self.install(path)
        self.untouched()


class Refusing(Base):
    """Everything that is wrong with an update is found before the standby slot is touched."""

    def refuse(self, path, pattern, exception=Failed, **kw):
        with self.assertRaisesRegex(exception, pattern):
            self.install(path, **kw)
        self.untouched()

    def test_a_file_that_does_not_match_the_manifest(self):
        tampered = bytearray(self.root)
        tampered[5] ^= 1
        self.refuse(self.archive(root=bytes(tampered)), "checksum")

    def test_a_size_that_differs_from_the_manifest(self):
        self.refuse(self.archive(root=self.root[:-4096]), "manifest says")

    def test_entries_that_are_not_part_of_an_update(self):
        for name in ("../escape", "/etc/passwd", "other.bin"):
            self.refuse(self.archive(extra=[(name, b"x")]), "unexpected entry")

    def test_the_manifest_must_come_first(self):
        self.refuse(self.archive(order=[1, 0, 2, 3]), "first entry")

    def test_an_incomplete_update(self):
        path = os.path.join(self.system.tmp, "partial.tar")
        with open(path, "wb") as handle:
            handle.write(tar_bytes([("manifest.json", self.manifest())]))
        self.refuse(path, "incomplete")

    def test_another_product_or_architecture(self):
        self.refuse(self.archive(manifest=self.manifest(name="other")), "not for this system", InvalidArgument)
        self.refuse(self.archive(manifest=self.manifest(arch="riscv64")), "machines", InvalidArgument)

    def test_a_bad_version(self):
        self.refuse(self.archive(manifest=self.manifest(version="../x")), "version")

    def test_an_image_larger_than_the_slot(self):
        self.root = b"\0" * (5 * MIB)
        self.refuse(self.archive(), "does not fit", InvalidArgument)

    def test_a_signed_update_needs_a_trusted_key_unless_told_otherwise(self):
        self.refuse(self.archive(), "no image signing key", unsigned=False)

    @unittest.skipUnless(GPG, "needs gpg")
    def test_signatures(self):
        self.trust(self.good_key)
        self.refuse(self.archive(), "not signed", unsigned=False)
        self.refuse(self.archive(signed_by="bad"), "not signed by a trusted", unsigned=False)
        result = self.install(self.archive(signed_by="good"), unsigned=False)
        self.assertTrue(result["signed"])
        self.assertEqual((self.table()[3].uuid, self.table()[4].uuid), verity.uuids(NEW_HASH))


@unittest.skipUnless(os.uname().machine == "x86_64" and shutil.which("zstd"), "needs x86_64 and zstd")
class ArchiveScript(Base):
    """scripts/make-update-archive makes an archive from the verity build outputs, and the installer takes it."""

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

    def test_the_hash_tree_and_root_hash_are_required(self):
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
        done = subprocess.run([sys.executable, script, "0.3.0", "--source", os.path.join(work, "src"), "--output", os.path.join(work, "out")],
                              capture_output=True, text=True)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("dm-verity", done.stderr)

    def make(self, *extra, env=None):
        work = os.path.join(self.system.tmp, "mkarchive")
        os.makedirs(os.path.join(work, "src"), exist_ok=True)
        for name, data in (("root-x86-64", self.root), ("root-verity-x86-64", self.tree)):
            raw = os.path.join(work, name)
            with open(raw, "wb") as handle:
                handle.write(data)
            subprocess.run(["zstd", "-q", "-f", raw, "-o", os.path.join(work, f"src/ecp_0.3.0.{name}.raw.zst")], check=True)
        with open(os.path.join(work, "src/ecp_0.3.0.roothash"), "w") as handle:
            handle.write(NEW_HASH + "\n")
        with open(os.path.join(work, "src/ecp_0.3.0.efi"), "wb") as handle:
            handle.write(self.uki())
        script = os.path.join(HERE, "..", "..", "scripts", "make-update-archive")
        if not os.path.exists(script):
            self.skipTest("scripts/ is not part of this checkout")
        subprocess.run([sys.executable, script, "0.3.0", "--source", os.path.join(work, "src"), "--output", os.path.join(work, "out"), *extra],
                       check=True, capture_output=True, env={**os.environ, **(env or {})})
        return os.path.join(work, "out", "ecp_0.3.0-x86_64.tar.zst")

    @unittest.skipUnless(GPG, "needs gpg")
    def test_a_signed_archive_verifies_against_the_installed_key(self):
        self.trust(self.good_key)
        archive = self.make("--sign", env={"ECP_SIGN_KEY": "good@example.org", "GNUPGHOME": os.path.join(self.keys, "good")})
        self.assertTrue(self.install(archive, unsigned=False)["signed"])

    @unittest.skipUnless(GPG, "needs gpg")
    def test_the_key_script_and_the_archive_script_work_together(self):
        """scripts/make-image-signing-key makes a key, its public half is what the system trusts, and an update signed
        with it (passphrase from a file, as in CI) installs."""
        script = os.path.join(HERE, "..", "..", "scripts", "make-image-signing-key")
        if not os.path.exists(script):
            self.skipTest("scripts/ is not part of this checkout")
        made = os.path.join(self.system.tmp, "imagekey")
        subprocess.run(["bash", script, made], check=True, capture_output=True)
        public = [n for n in os.listdir(made) if n.startswith("ersa-image-") and n.endswith(".asc")]
        self.assertEqual(len(public), 1)
        self.trust(os.path.join(made, public[0]))
        fingerprint = subprocess.run(["gpg", "--homedir", os.path.join(made, "gnupg"), "--list-keys", "--with-colons"], check=True,
                                     capture_output=True, text=True).stdout.split("fpr:::::::::")[1].split(":")[0]
        env = {"GNUPGHOME": os.path.join(made, "gnupg"), "ECP_SIGN_KEY": fingerprint,
               "ECP_SIGN_PASSPHRASE_FILE": os.path.join(made, "passphrase.txt")}
        self.assertTrue(self.install(self.make("--sign", env=env), unsigned=False)["signed"])

    @unittest.skipUnless(GPG, "needs gpg")
    def test_a_signed_archive_whose_manifest_was_changed_is_refused(self):
        self.trust(self.good_key)
        archive = self.make("--sign", env={"ECP_SIGN_KEY": "good@example.org", "GNUPGHOME": os.path.join(self.keys, "good")})
        data = subprocess.run(["zstd", "-dc", archive], check=True, capture_output=True).stdout
        changed = data.replace(b'"version": "0.3.0"', b'"version": "0.3.1"', 1)
        self.assertNotEqual(changed, data)
        path = os.path.join(self.system.tmp, "changed.tar")
        with open(path, "wb") as handle:
            handle.write(changed)
        with self.assertRaisesRegex(Failed, "not signed by a trusted"):
            self.install(path, unsigned=False)
        self.untouched()


if __name__ == "__main__":
    unittest.main()
