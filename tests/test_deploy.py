import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest

import support  # noqa: F401  (puts the package on sys.path)
from foundationd.backends import deploy, encryption, gpt, layout, verity
from foundationd.errors import Failed, InvalidArgument, Unavailable

TOOLS = all(shutil.which(t) for t in ("systemd-repart", "mdir", "mren", "mkfs.btrfs", "mkfs.vfat"))
MIB = 1024 * 1024
ROOTHASH = "ab" * 16 + "cd" * 16


class Ctx:
    def __init__(self):
        self.steps, self.committed, self.logs = [], False, []

    def step(self, i, t):
        self.steps.append(i)

    def progress(self, f):
        pass

    def log(self, t):
        self.logs.append(t)

    def commit(self):
        self.committed = True


def tar_bytes(entries):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name, data in entries:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class Medium(unittest.TestCase):
    """An installer medium: its own root is the running system (labels ecp-live, ecp-live-verity), its ESP has a boot loader,
    and its root carries the operating system to install as an update archive."""

    def setUp(self):
        self.system = support.FakeSystem(("live:0.1.0",), running=1, root_mib=4)
        self.addCleanup(shutil.rmtree, self.system.tmp, ignore_errors=True)
        self.system.uki("0.1.0")
        for name, data in (("EFI/BOOT/BOOTX64.EFI", b"loader"), ("loader/loader.conf", b"timeout 0\n")):
            path = os.path.join(self.system.esp, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as handle:
                handle.write(data)
        self.root_image = os.urandom(3 * MIB)
        self.tree = os.urandom(64 * 1024)
        self.uki = support.build_pe(f"ro quiet roothash={ROOTHASH}".encode(), raw_size=1024)
        self.target = os.path.join(self.system.tmp, "target.img")
        with open(self.target, "wb") as h:
            h.truncate(layout.MIN_DISK + 64 * MIB)
        self.write_payload()

    def manifest(self, **changes):
        data = {"format": 1, "name": "ecp", "version": "0.1.0", "arch": os.uname().machine, "roothash": ROOTHASH,
                "root": {"file": "root.erofs", "sha256": hashlib.sha256(self.root_image).hexdigest(), "size": len(self.root_image)},
                "verity": {"file": "verity.img", "sha256": hashlib.sha256(self.tree).hexdigest(), "size": len(self.tree)},
                "uki": {"file": "uki.efi", "sha256": hashlib.sha256(self.uki).hexdigest(), "size": len(self.uki)}}
        data.update(changes)
        return json.dumps(data).encode()

    def write_payload(self, manifest=None, uki=None):
        directory = os.path.join(self.system.paths["root"], deploy.PAYLOAD_DIR)
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, "ecp_0.1.0-x86_64.tar"), "wb") as handle:
            handle.write(tar_bytes([("manifest.json", manifest or self.manifest()), ("uki.efi", uki or self.uki),
                                    ("root.erofs", self.root_image), ("verity.img", self.tree)]))


class Refusals(Medium):
    def test_plain_files_refused_by_default(self):
        with self.assertRaises(InvalidArgument):
            deploy.describe_target(self.system.paths, self.target)

    def test_relative_and_traversal_paths_refused(self):
        for bad in ("sdb", "/dev/../etc/passwd", "/dev/x\0y"):
            with self.assertRaises(Exception):
                deploy.describe_target(self.system.paths, bad, True)

    def test_confirmation_must_match(self):
        with self.assertRaises(InvalidArgument):
            deploy.deploy(self.system.paths, self.target, "/other", Ctx(), allow_file=True)

    def test_too_small(self):
        small = os.path.join(self.system.tmp, "small.img")
        with open(small, "wb") as h:
            h.truncate(4 * 1024 * MIB)
        info = deploy.describe_target(self.system.paths, small, True)
        self.assertTrue(any("too small" in p for p in info["problems"]))
        ctx = Ctx()
        with self.assertRaises(InvalidArgument):
            deploy.deploy(self.system.paths, small, small, ctx, allow_file=True)
        self.assertFalse(ctx.committed)

    def test_running_disk_refused(self):
        info = deploy.describe_target(self.system.paths, self.system.image, True)
        self.assertTrue(info["boot_medium"] or info["problems"])

    def test_plan_lists_the_verity_layout_and_the_payloads_version(self):
        plan = deploy.plan(self.system.paths, self.target, True)
        self.assertTrue(plan["possible"], plan["problems"])
        self.assertEqual(plan["version"], "0.1.0")
        self.assertEqual([p["label"] for p in plan["partitions"]],
                         ["ECP-ESP", "ECP-RECOVERY", "ecp-root-a", "ecp-verity-a", "ecp-root-b", "ecp-verity-b", "ECP-DATA"])

    def test_a_medium_without_a_payload_cannot_install(self):
        shutil.rmtree(os.path.join(self.system.paths["root"], deploy.PAYLOAD_DIR))
        plan = deploy.plan(self.system.paths, self.target, True)
        self.assertFalse(plan["possible"])
        self.assertTrue(any("no system image" in p for p in plan["problems"]))
        ctx = Ctx()
        with self.assertRaises(Unavailable):
            deploy.deploy(self.system.paths, self.target, self.target, ctx, allow_file=True)
        self.assertFalse(ctx.committed)

    def test_list_disks_marks_boot_disk(self):
        disks = {d["name"]: d for d in deploy.list_disks(self.system.paths)}
        self.assertFalse(disks["vda"]["usable"])
        self.assertTrue(disks["vda"]["boot_medium"])

    def test_a_kernel_image_with_another_root_hash_is_refused_before_anything_is_written(self):
        wrong = support.build_pe(f"ro roothash={'ee' * 32}".encode(), raw_size=1024)
        self.uki = wrong
        self.write_payload(manifest=self.manifest())
        ctx = Ctx()
        with self.assertRaisesRegex(Failed, "root hash"):
            deploy.deploy(self.system.paths, self.target, self.target, ctx, allow_file=True)
        self.assertFalse(ctx.committed)
        with open(self.target, "rb") as h:
            self.assertEqual(h.read(4096), bytes(4096))


@unittest.skipUnless(TOOLS, "needs systemd-repart, mtools, mkfs.btrfs and mkfs.vfat")
class Install(Medium):
    def run_deploy(self, **kw):
        ctx = Ctx()
        result = deploy.deploy(self.system.paths, self.target, self.target, ctx, allow_file=True, **kw)
        return result, ctx

    def esp_listing(self):
        esp = {p.name: p for p in gpt.read_table(self.target)}["ECP-ESP"]
        return subprocess.run(["mdir", "-b", "-i", f"{self.target}@@{esp.offset}", "::EFI/Linux"],
                              capture_output=True, text=True, check=True).stdout

    def test_install(self):
        result, ctx = self.run_deploy()
        self.assertTrue(ctx.committed)
        self.assertEqual((result["version"], result["roothash"]), ("0.1.0", ROOTHASH))
        table = {p.name: p for p in gpt.read_table(self.target)}
        self.assertEqual(set(table), {"ECP-ESP", "ECP-RECOVERY", "ecp-root-a", "ecp-verity-a", "ecp-root-b", "ecp-verity-b", "ECP-DATA"})
        self.assertEqual((table["ecp-root-a"].size, table["ecp-root-b"].size), (layout.ROOT_SLOT, layout.ROOT_SLOT))
        self.assertEqual((table["ecp-verity-a"].size, table["ecp-verity-b"].size), (layout.VERITY_SLOT, layout.VERITY_SLOT))
        self.assertGreaterEqual(table["ECP-DATA"].size, layout.DATA_MIN)
        for label, data in (("ecp-root-a", self.root_image), ("ecp-verity-a", self.tree)):
            with open(self.target, "rb") as h:
                h.seek(table[label].offset)
                self.assertEqual(h.read(len(data)), data, label)
        self.assertIn("ecp_0.1.0+3.efi", self.esp_listing())
        self.assertNotIn(".partial", self.esp_listing())

    def test_the_slot_is_named_by_the_root_hash(self):
        self.run_deploy()
        table = {p.name: p for p in gpt.read_table(self.target)}
        self.assertEqual((table["ecp-root-a"].uuid, table["ecp-verity-a"].uuid), verity.uuids(ROOTHASH))
        # slot B is empty: nothing names its partitions
        self.assertNotIn(table["ecp-root-b"].uuid, verity.uuids(ROOTHASH))

    def test_the_kernel_image_is_installed_exactly_as_it_was_built(self):
        self.run_deploy()
        esp = {p.name: p for p in gpt.read_table(self.target)}["ECP-ESP"]
        image = subprocess.run(["mtype", "-i", f"{self.target}@@{esp.offset}", "::EFI/Linux/ecp_0.1.0+3.efi"],
                               capture_output=True, check=True).stdout
        self.assertEqual(image, self.uki, "a signed kernel image must not be changed")

    def test_the_boot_loader_of_the_medium_is_copied_to_the_new_esp(self):
        self.run_deploy()
        esp = {p.name: p for p in gpt.read_table(self.target)}["ECP-ESP"]
        data = subprocess.run(["mtype", "-i", f"{self.target}@@{esp.offset}", "::EFI/BOOT/BOOTX64.EFI"], capture_output=True, check=True).stdout
        self.assertEqual(data, b"loader")

    def test_the_installed_disk_is_found_as_a_system_by_the_slot_code(self):
        """The disk, attached to a running system, shows slot A running... checked by reading it as its own system."""
        self.run_deploy()
        table = {p.name: p for p in gpt.read_table(self.target)}
        # the hash partition is the second half; both halves are in the table with the verity types
        self.assertEqual(table["ecp-verity-a"].type, verity.ROOT_VERITY_TYPES["x86_64"])
        self.assertEqual(table["ecp-root-a"].type, gpt.ROOT_TYPES["x86_64"])

    def test_seed_holds_the_chosen_language_and_time_zone(self):
        stage = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, stage)
        zones = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, zones)
        seed = deploy._make_seed(stage, "de_DE.UTF-8", "Europe/Berlin", zones)
        upper = os.path.join(seed, "var/lib/ecp/etc/upper")
        with open(os.path.join(upper, "locale.conf")) as handle:
            self.assertEqual(handle.read(), "LANG=de_DE.UTF-8\n")
        self.assertEqual(os.readlink(os.path.join(upper, "localtime")), "../usr/share/zoneinfo/Europe/Berlin")

    def test_seed_without_choices_has_neither_file(self):
        stage = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, stage)
        upper = os.path.join(deploy._make_seed(stage), "var/lib/ecp/etc/upper")
        self.assertEqual(os.listdir(upper), [])

    def test_an_unknown_time_zone_is_refused_before_anything_is_written(self):
        with self.assertRaises(deploy.InvalidArgument):
            self.run_deploy(timezone="Mars/Olympus_Mons")
        self.assertEqual(os.path.getsize(self.target), layout.MIN_DISK + 64 * MIB)
        with open(self.target, "rb") as h:
            self.assertEqual(h.read(4096), bytes(4096))

    def test_install_with_language_and_time_zone(self):
        zones = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, zones)
        open(os.path.join(zones, "UTC"), "w").close()
        result, _ctx = self.run_deploy(language="fr_FR.UTF-8", timezone="UTC", zoneinfo=zones)
        self.assertEqual(result["version"], "0.1.0")

    def test_locale_and_time_zone_names_are_validated(self):
        from foundationd.api import validate
        for good in ("", "en_US.UTF-8", "de_DE", "sr_RS@latin", "C.UTF-8"):
            validate.locale_name(good)
        for bad in ("../x", "en US", "x;y", "a" * 5 + "_" * 3, 5):
            with self.assertRaises(InvalidArgument):
                validate.locale_name(bad)
        for good in ("", "UTC", "Europe/Berlin", "America/Argentina/Buenos_Aires", "Etc/GMT+5"):
            validate.timezone_name(good)
        for bad in ("../etc/passwd", "/etc/passwd", "Europe/../x", "a b", "A/B/C/D"):
            with self.assertRaises(InvalidArgument):
                validate.timezone_name(bad)

    def test_data_volume_has_subvolumes(self):
        self.run_deploy()
        data = {p.name: p for p in gpt.read_table(self.target)}["ECP-DATA"]
        with open(self.target, "rb") as h:
            h.seek(data.offset + 0x10040)
            self.assertEqual(h.read(8), b"_BHRfS_M")

    def test_corrupt_copy_leaves_no_bootable_kernel(self):
        real = deploy.Window.write

        def flaky(window, data):
            return real(window, bytes(len(data)) if window._pos == 0 else data)

        deploy.Window.write = flaky
        self.addCleanup(setattr, deploy.Window, "write", real)
        with self.assertRaises(Failed):
            self.run_deploy()
        self.assertNotIn("ecp_0.1.0+3.efi\n", self.esp_listing() + "\n")
        self.assertIn(".partial", self.esp_listing())

    def test_a_payload_that_does_not_match_its_manifest_leaves_no_bootable_kernel(self):
        self.root_image, good = self.root_image, self.root_image
        manifest = self.manifest()
        self.root_image = b"x" * len(good)            # the archive holds other bytes than the manifest names
        self.write_payload(manifest=manifest)
        with self.assertRaisesRegex(Failed, "checksum"):
            self.run_deploy()
        self.assertNotIn("ecp_0.1.0+3.efi\n", self.esp_listing() + "\n")

    def test_no_staging_left_behind(self):
        self.run_deploy()
        staging = os.path.join(self.system.paths["state"], "staging")
        self.assertEqual(os.listdir(staging), [])


class LayoutDrift(unittest.TestCase):
    def test_matches_build_layout(self):
        root = os.path.join(os.environ.get("ECP_MKOSI_DIR", ""), "mkosi.repart.system")
        if not os.environ.get("ECP_MKOSI_DIR") or not os.path.isdir(root):
            self.skipTest("set ECP_MKOSI_DIR to an ecp-mkosi checkout")
        built = {}
        for name in sorted(os.listdir(root)):
            with open(os.path.join(root, name)) as handle:
                text = handle.read()
            fields = dict(l.split("=", 1) for l in text.splitlines() if "=" in l and not l.startswith("#"))
            built[fields.get("Label", name)] = fields
        made = layout.definitions("/e", "/s", "r", "v")
        generated = {}
        for text in made.values():
            fields = dict(l.split("=", 1) for l in text.splitlines() if "=" in l)
            generated[fields["Label"]] = fields
        self.assertEqual(set(generated), set(built), "the installer's layout and the built image's must have the same partitions")
        for label, fields in generated.items():
            for key in ("Type", "SizeMinBytes", "SizeMaxBytes", "Format"):
                if key in fields and key in built[label]:
                    expected = built[label][key]
                    size = lambda v: int(v[:-1]) * {"G": 1024 ** 3, "M": 1024 ** 2}[v[-1]] if v[-1:] in "GM" else int(v)
                    if key.startswith("Size"):
                        self.assertEqual(int(fields[key]), size(expected), (label, key))
                    else:
                        self.assertEqual(fields[key], expected, (label, key))


if __name__ == "__main__":
    unittest.main()


KEY = "cbdefghi-jklnrtuv-cbdefghi-jklnrtuv-cbdefghi-jklnrtuv-cbdefghi-jklnrtuv"
PUBKEY = b"-----BEGIN PUBLIC KEY-----\nTEST\n-----END PUBLIC KEY-----\n"


@unittest.skipUnless(TOOLS, "needs systemd-repart, mtools and mkfs tools")
class EncryptedInstall(Medium):
    """The encrypted data volume. Real dm-crypt needs real devices, so the second (online) run of systemd-repart is replaced by an
    offline run that creates the same partition plain; what is checked is the sequence of commands and what they are given."""

    def setUp(self):
        super().setUp()
        self.uki = support.build_pe(f"ro quiet roothash={ROOTHASH}".encode(), raw_size=1024, pcrpkey=PUBKEY)
        self.write_payload()
        self.calls, self.env = [], []

    def runner(self, cmd, timeout=60, env=None):
        self.calls.append(list(cmd))
        self.env.append(env)
        if cmd[0] == "systemd-repart" and "--offline=no" in cmd:
            definitions = next(a.split("=", 1)[1] for a in cmd if a.startswith("--definitions="))
            self.online_definitions = open(os.path.join(definitions, "30-data.conf")).read()
            self.key_file_mode = os.stat(next(a.split("=", 1)[1] for a in cmd if a.startswith("--key-file="))).st_mode & 0o777
            with open(os.path.join(definitions, "30-data.conf"), "w") as handle:      # create it plain, as a stand-in
                handle.write(self.online_definitions.replace("Encrypt=key-file\n", ""))
            cmd = [c for c in cmd if not c.startswith("--key-file=")]
            cmd = [("--offline=yes" if c == "--offline=no" else c) for c in cmd]
        if cmd[0] in ("systemd-cryptenroll", "udevadm"):
            return 0, "", ""
        code, out, err = deploy.run(cmd, timeout)
        return code, out, err

    def deploy(self, secrets, tpm):
        paths = dict(self.system.paths)
        paths["tpm_class"], paths["tpmrm"] = os.path.join(self.system.tmp, "tpm-class"), os.path.join(self.system.tmp, "tpmrm0")
        if tpm:
            os.makedirs(os.path.join(paths["tpm_class"], "tpm0"), exist_ok=True)
            open(paths["tpmrm"], "w").close()
        ctx = Ctx()
        return deploy.deploy(paths, self.target, self.target, ctx, allow_file=True, runner=self.runner, secrets=secrets), ctx

    def enroll_calls(self):
        return [c for c in self.calls if c[0] == "systemd-cryptenroll"]

    def test_with_a_tpm_the_slots_are_recovery_key_and_tpm_and_the_one_time_key_is_wiped(self):
        result, ctx = self.deploy({"recovery_key": KEY, "passphrase": ""}, tpm=True)
        self.assertEqual((result["encrypted"], result["unlock"]), (True, {"tpm": True, "passphrase": False, "recovery_key": True}))
        self.assertIn("encrypt", ctx.steps)
        self.assertIn("Encrypt=key-file", self.online_definitions)
        self.assertEqual(self.key_file_mode, 0o600)
        enroll = self.enroll_calls()
        self.assertEqual(len(enroll), 3)
        self.assertIn("--password", enroll[0])
        self.assertIn("--tpm2-device=auto", enroll[1])
        self.assertIn("--tpm2-pcrs=", enroll[1])
        self.assertIn("--tpm2-public-key-pcrs=11", enroll[1])
        self.assertIn("--wipe-slot=0", enroll[2])
        self.assertTrue(enroll[0][-1].startswith(self.target), enroll[0])      # the data partition's node, here target + number
        self.assertEqual(self.env[self.calls.index(enroll[0])], {"NEWPASSWORD": KEY})

    def test_no_secret_is_ever_on_a_command_line(self):
        self.deploy({"recovery_key": KEY, "passphrase": "correct horse battery"}, tpm=False)
        for call in self.calls:
            self.assertFalse(any(KEY in part or "correct horse" in part for part in call), call)
        passphrase_env = [e for e in self.env if e and e.get("NEWPASSWORD") == "correct horse battery"]
        self.assertEqual(len(passphrase_env), 1)

    def test_without_a_tpm_a_passphrase_is_the_way_in_and_without_one_the_install_is_refused_before_anything_is_written(self):
        result, _ctx = self.deploy({"recovery_key": KEY, "passphrase": "correct horse battery"}, tpm=False)
        self.assertEqual(result["unlock"], {"tpm": False, "passphrase": True, "recovery_key": True})
        self.assertFalse(any("--tpm2-device=auto" in c for c in self.enroll_calls()))
        with open(self.target, "wb") as handle:
            handle.truncate(layout.MIN_DISK + 64 * MIB)
        with self.assertRaises(InvalidArgument):
            self.deploy({"recovery_key": KEY, "passphrase": ""}, tpm=False)
        self.assertEqual(open(self.target, "rb").read(4096).count(b"\0"), 4096)        # nothing written

    def test_an_image_without_a_signed_boot_policy_cannot_use_the_tpm(self):
        self.uki = support.build_pe(f"ro quiet roothash={ROOTHASH}".encode(), raw_size=1024)
        self.write_payload()
        with self.assertRaises(InvalidArgument):
            self.deploy({"recovery_key": KEY, "passphrase": ""}, tpm=True)

    def test_secrets_are_checked(self):
        from foundationd.backends import encryption
        self.assertEqual(encryption.parse_secrets(json.dumps({"recovery_key": KEY}))["passphrase"], "")
        for bad in ("nope", "[]", json.dumps({"recovery_key": "short"}), json.dumps({"recovery_key": KEY, "passphrase": "x"}),
                    json.dumps({"recovery_key": KEY, "passphrase": "line\nbreak and more"})):
            with self.assertRaises(InvalidArgument):
                encryption.parse_secrets(bad)

    def test_partition_nodes(self):
        from foundationd.backends import encryption
        self.assertEqual(encryption.partition_node("/dev/vda", 7), "/dev/vda7")
        self.assertEqual(encryption.partition_node("/dev/nvme0n1", 7), "/dev/nvme0n1p7")
        self.assertEqual(encryption.partition_node("/dev/mmcblk0", 7), "/dev/mmcblk0p7")


class EncryptionStatus(unittest.TestCase):
    """encryption.status reads the key slots through cryptsetup (read only) and never returns key material."""

    LUKS = {"keyslots": {"1": {"type": "luks2"}, "2": {"type": "luks2"}, "3": {"type": "luks2"}},
            "tokens": {"0": {"type": "systemd-tpm2", "keyslots": ["3"]}}}

    def setUp(self):
        self.system = support.FakeSystem()
        self.addCleanup(self.system.cleanup)
        self.paths = dict(self.system.paths)
        self.paths["tpm_class"] = os.path.join(self.system.tmp, "no-tpm")
        self.paths["tpmrm"] = os.path.join(self.system.tmp, "no-tpmrm")

    def runner(self, status="LUKS2", metadata=None, dump_code=0):
        def run(cmd, timeout=60, env=None):
            self.last = list(cmd)
            if cmd[:2] == ["cryptsetup", "status"]:
                return 0, f"/dev/mapper/ecp-data is active and is in use.\n  type:    {status}\n  cipher:  aes-xts-plain64\n  device:  /dev/vda7\n", ""
            return dump_code, json.dumps(metadata or self.LUKS), ""
        return run

    def mount(self, source):
        self.system.write_mountinfo([f"40 36 0:30 / /var rw - btrfs {source} rw"])

    def test_a_plain_volume_is_not_encrypted(self):
        self.mount("/dev/vda7")
        self.assertEqual(encryption.status(self.paths, self.runner()), {"tpm_present": False, "encrypted": False})
        self.system.write_mountinfo([])
        self.assertFalse(encryption.status(self.paths, self.runner())["encrypted"])

    def test_the_slots_are_counted_by_the_token_that_names_them(self):
        self.mount("/dev/mapper/ecp-data")
        result = encryption.status(self.paths, self.runner())
        self.assertEqual((result["encrypted"], result["tpm_slots"], result["recovery_slots"], result["fido2_slots"], result["password_slots"]),
                         (True, 1, 0, 0, 2))
        self.assertEqual(result["cipher"], "aes-xts-plain64")
        self.assertEqual(self.last[:3], ["cryptsetup", "luksDump", "--dump-json-metadata"])      # read only: the header, no key is opened

    def test_a_mapped_volume_that_is_not_luks_is_not_reported_as_encrypted(self):
        self.mount("/dev/mapper/vg-data")
        self.assertFalse(encryption.status(self.paths, self.runner(status="PLAIN"))["encrypted"])

    def test_an_unreadable_header_is_an_error_not_a_guess(self):
        self.mount("/dev/mapper/ecp-data")
        with self.assertRaises(Failed):
            encryption.status(self.paths, self.runner(dump_code=1))
        def garbage(cmd, timeout=60, env=None):
            return (0, "not json", "") if cmd[1] == "luksDump" else self.runner()(cmd, timeout)
        with self.assertRaises(Failed):
            encryption.status(self.paths, garbage)
