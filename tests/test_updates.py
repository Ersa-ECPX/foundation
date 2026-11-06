"""Installing a system image update: the archive checks, the slot writes, the next boot."""

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

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

import support  # noqa: E402
from foundationd.backends import gpt, pe, slots, updates  # noqa: E402
from foundationd.backends.runner import run  # noqa: E402
from foundationd.errors import Failed, InvalidArgument  # noqa: E402
from test_repos import GPG, _gpg_key, _sign  # noqa: E402

ROOT_BYTES = 1024 * 1024
CMDLINE = "quiet splash root=PARTLABEL=ecp-root_0.3.0 systemd.mount-extra=LABEL=ECP-DATA:/var:btrfs:subvol=var ecp.pad=----------"
RUNNING = ("quiet root=PARTUUID=aaaa systemd.mount-extra=PARTUUID=bbbb:/var:btrfs:subvol=var,x-initrd.mount "
           "systemd.mount-extra=PARTUUID=bbbb:/home:btrfs:subvol=home")


def tar_bytes(entries):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name, data in entries:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class Fixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        random.seed(3)
        cls.root = bytes(random.getrandbits(8) for _ in range(ROOT_BYTES))
        cls.keys = tempfile.mkdtemp()
        if GPG:
            cls.good_home, cls.bad_home = os.path.join(cls.keys, "good"), os.path.join(cls.keys, "bad")
            cls.good_key = _gpg_key(cls.good_home, "good")
            cls.bad_key = _gpg_key(cls.bad_home, "bad")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.keys, True)

    def setUp(self):
        self.system = support.FakeSystem()
        self.addCleanup(self.system.cleanup)
        with open(os.path.join(self.system.esp, "EFI/Linux/ecp_0.2.0.efi"), "wb") as handle:
            handle.write(support.build_pe(RUNNING.encode(), raw_size=1024))
        self.paths = self.system.paths
        self.ctx = support.Recorder()
        self.mounted = []

    def manifest(self, **changes):
        uki = support.build_pe(CMDLINE.encode(), raw_size=1024)
        self.uki = uki
        data = {"format": 1, "name": "ecp", "version": "0.3.0", "arch": os.uname().machine,
                "root": {"file": "root.erofs", "sha256": hashlib.sha256(self.root).hexdigest(), "size": len(self.root)},
                "uki": {"file": "uki.efi", "sha256": hashlib.sha256(uki).hexdigest(), "size": len(uki)}}
        data.update(changes)
        return json.dumps(data).encode()

    def archive(self, manifest=None, extra=(), signed_by=None, root=None, order=None):
        manifest = manifest or self.manifest()
        entries = [("manifest.json", manifest)]
        if signed_by:
            path = os.path.join(self.keys, "manifest.json")
            with open(path, "wb") as handle:
                handle.write(manifest)
            _sign(os.path.join(self.keys, signed_by), signed_by, path, path + ".asc")
            with open(path + ".asc", "rb") as handle:
                entries.append(("manifest.json.sig", handle.read()))
        entries += [("root.erofs", self.root if root is None else root), ("uki.efi", self.uki)] + list(extra)
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

    @contextlib.contextmanager
    def fake_mount(self, device):
        self.mounted.append(device)
        root = tempfile.mkdtemp(dir=self.system.tmp)
        os.makedirs(os.path.join(root, "usr/lib"))
        with open(os.path.join(root, "usr/lib/os-release"), "w") as handle:
            handle.write('ID="opensuse-tumbleweed"\nIMAGE_ID="ecp"\nIMAGE_VERSION="0.3.0"\nVERSION_ID="20251101"\n')
        yield root

    def install(self, path, **kw):
        kw.setdefault("unsigned", True)
        kw.setdefault("mount", self.fake_mount)
        return updates.install(self.paths, path, self.ctx, runner=run, opener=self.system.opener(), **kw)

    def labels(self):
        return {p.number: p.name for p in gpt.read_table(self.system.image)}

    def partition_bytes(self, number, length):
        part = [p for p in gpt.read_table(self.system.image) if p.number == number][0]
        with open(self.system.image, "rb") as handle:
            handle.seek(part.offset)
            return handle.read(length)

    def untouched(self):
        self.assertEqual(self.labels()[2], "_empty")
        self.assertEqual(sorted(os.listdir(os.path.join(self.system.esp, "EFI/Linux"))), ["ecp_0.2.0.efi"])
        self.assertEqual(os.listdir(os.path.join(self.paths["state"], "staging")), [])


@unittest.skipUnless(support.tool_available("sfdisk"), "needs sfdisk")
class Installing(Fixture):
    def test_the_image_is_written_to_the_standby_slot_and_made_bootable(self):
        result = self.install(self.archive())
        self.assertEqual((result["version"], result["entry"], result["reboot_required"]), ("0.3.0", "ecp_0.3.0+3.efi", True))
        self.assertEqual(self.labels()[2], "ecp-root_0.3.0")
        self.assertEqual(self.partition_bytes(2, ROOT_BYTES), self.root)
        self.assertEqual(self.labels()[1], "ecp-root_0.2.0", "the running slot is never touched")
        for step in ("verify", "prepare", "write", "label", "image", "bundles", "boot"):
            self.assertIn(step, self.ctx.steps)
        self.assertTrue(self.ctx.committed)

    @unittest.skipUnless(shutil.which("zstd"), "needs zstd")
    def test_a_zstd_compressed_archive_is_read(self):
        path = self.archive()
        subprocess.run(["zstd", "-q", "-f", path, "-o", path + ".zst"], check=True)
        result = self.install(path + ".zst")
        self.assertEqual(result["version"], "0.3.0")
        self.assertEqual(self.partition_bytes(2, ROOT_BYTES), self.root)

    def test_the_new_kernel_image_boots_the_new_slot_and_keeps_this_installs_data_volume(self):
        self.install(self.archive())
        with open(os.path.join(self.system.esp, "EFI/Linux/ecp_0.3.0+3.efi"), "rb") as handle:
            text = pe.read_cmdline(handle.read())
        tokens = text.split()
        self.assertIn("root=PARTLABEL=ecp-root_0.3.0", tokens)
        self.assertEqual(sum(t.startswith("root=") for t in tokens), 1)
        self.assertIn("systemd.mount-extra=PARTUUID=bbbb:/var:btrfs:subvol=var,x-initrd.mount", tokens)
        self.assertIn("systemd.mount-extra=PARTUUID=bbbb:/home:btrfs:subvol=home", tokens)
        self.assertFalse(any("LABEL=ECP-DATA" in t or t.startswith("ecp.pad") for t in tokens))
        self.assertIn("splash", tokens)                       # the new image's own options stay

    def test_a_restart_is_recorded_for_whoever_wants_to_tell_the_user(self):
        self.install(self.archive())
        waiting = updates.pending(self.paths)
        self.assertEqual((waiting["version"], waiting["slot"]), ("0.3.0", "0.3.0"))
        self.assertEqual(oct(os.stat(updates.pending_path(self.paths)).st_mode & 0o777), "0o644")
        self.assertFalse(updates.clear_pending(self.paths), "still running the old image")
        with open(self.paths["os_release"], "w") as handle:
            handle.write('IMAGE_ID=ecp\nIMAGE_VERSION=0.3.0\n')
        self.assertTrue(updates.clear_pending(self.paths))
        self.assertEqual(updates.pending(self.paths), {})

    def test_bundles_are_prepared_against_the_new_image(self):
        self.install(self.archive())
        self.assertEqual(self.mounted, [self.system.paths["dev"] + "/vda2"])

    def test_the_default_boot_entry_is_set_when_booted_through_uefi(self):
        calls = []

        def runner(cmd, timeout=60, input_bytes=None):
            calls.append(cmd)
            return run(cmd, timeout, input_bytes) if cmd[0] == "sfdisk" else (0, "", "")
        self.system.efivar("LoaderInfo", "systemd-boot")
        updates.install(self.paths, self.archive(), self.ctx, unsigned=True, runner=runner, opener=self.system.opener(), mount=self.fake_mount)
        self.assertIn(["bootctl", "set-default", "ecp_0.3.0.efi"], calls)

    def test_without_uefi_the_install_still_completes_and_says_what_to_do(self):
        shutil.rmtree(self.paths["efivars"])
        result = self.install(self.archive())
        self.assertFalse(result["default_set"])
        self.assertTrue(any("boot menu" in note for note in result["notes"]))

    def test_the_version_already_running_is_refused(self):
        with open(self.paths["os_release"], "w") as handle:
            handle.write("IMAGE_ID=ecp\n")
        with self.assertRaisesRegex(InvalidArgument, "already runs"):
            self.install(self.archive(manifest=self.manifest(version="0.2.0")))
        self.untouched()

    def test_installing_the_same_version_twice_needs_force(self):
        self.install(self.archive())
        with self.assertRaisesRegex(InvalidArgument, "already installed"):
            self.install(self.archive())
        self.install(self.archive(), force=True)


@unittest.skipUnless(support.tool_available("sfdisk"), "needs sfdisk")
class Refusing(Fixture):
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
        self.refuse(self.archive(order=[1, 0]), "first entry")

    def test_an_incomplete_update(self):
        path = os.path.join(self.system.tmp, "partial.tar")
        with open(path, "wb") as handle:
            handle.write(tar_bytes([("manifest.json", self.manifest())]))
        self.refuse(path, "incomplete")

    def test_another_product_or_architecture(self):
        self.refuse(self.archive(manifest=self.manifest(name="other")), "not for this system", InvalidArgument)
        self.refuse(self.archive(manifest=self.manifest(arch="riscv64")), "machines", InvalidArgument)

    def test_a_bad_version_or_format(self):
        self.refuse(self.archive(manifest=self.manifest(version="../x")), "version")
        self.refuse(self.archive(manifest=self.manifest(format=9)), "format")

    def test_an_image_larger_than_the_slot(self):
        self.root = b"\0" * (5 * 1024 * 1024)
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
        self.assertEqual(self.labels()[2], "ecp-root_0.3.0")


@unittest.skipUnless(os.uname().machine == "x86_64" and shutil.which("zstd"), "needs x86_64 and zstd")
class ArchiveScript(Fixture):
    """The script that makes update archives produces what the installer reads."""

    def make(self, *extra, env=None):
        work = os.path.join(self.system.tmp, "mkarchive")
        os.makedirs(os.path.join(work, "src"))
        raw = os.path.join(work, "r.erofs")
        with open(raw, "wb") as handle:
            handle.write(self.root)
        subprocess.run(["zstd", "-q", raw, "-o", os.path.join(work, "src/ecp_0.3.0.root-x86-64.raw.zst")], check=True)
        with open(os.path.join(work, "src/ecp_0.3.0.efi"), "wb") as handle:
            handle.write(support.build_pe(CMDLINE.encode(), raw_size=1024))
        script = os.path.join(HERE, "..", "..", "scripts", "make-update-archive")
        if not os.path.exists(script):
            self.skipTest("scripts/ is not part of this checkout")
        subprocess.run([sys.executable, script, "0.3.0", "--source", os.path.join(work, "src"), "--output", os.path.join(work, "out"), *extra],
                       check=True, capture_output=True, env={**os.environ, **(env or {})})
        return os.path.join(work, "out", "ecp_0.3.0-x86_64.tar.zst")

    def test_an_unsigned_archive_installs_with_the_unsigned_flag(self):
        result = self.install(self.make())
        self.assertEqual((result["version"], result["signed"]), ("0.3.0", False))
        self.assertEqual(self.partition_bytes(2, ROOT_BYTES), self.root)

    @unittest.skipUnless(GPG, "needs gpg")
    def test_a_signed_archive_verifies_against_the_installed_key(self):
        self.trust(self.good_key)
        archive = self.make("--sign", env={"ECP_SIGN_KEY": "good@example.org", "GNUPGHOME": os.path.join(self.keys, "good")})
        result = self.install(archive, unsigned=False)
        self.assertTrue(result["signed"])


class Cmdline(unittest.TestCase):
    def test_the_new_slot_is_named_and_the_data_volume_of_this_install_is_kept(self):
        text = updates.update_cmdline("a b root=X systemd.mount-extra=LABEL=D:/var ecp.pad=--", "r root=Y systemd.mount-extra=PARTUUID=P:/var",
                                      "ecp-root_9")
        self.assertEqual(text, "a b root=PARTLABEL=ecp-root_9 systemd.mount-extra=PARTUUID=P:/var")

    def test_a_running_system_without_pinned_mounts_keeps_the_image_own(self):
        text = updates.update_cmdline("a root=X systemd.mount-extra=LABEL=D:/var", "r root=Y", "ecp-root_9")
        self.assertEqual(sorted(text.split()), sorted("a root=PARTLABEL=ecp-root_9 systemd.mount-extra=LABEL=D:/var".split()))


if __name__ == "__main__":
    unittest.main()
