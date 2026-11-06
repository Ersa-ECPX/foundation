import os
import shutil
import subprocess
import tempfile
import unittest
import uuid

import support  # noqa: F401  (puts the package on sys.path)
from foundationd.backends import deploy, gpt, layout
from foundationd.errors import Failed, InvalidArgument

TOOLS = all(shutil.which(t) for t in ("systemd-repart", "mdir", "mren", "mkfs.btrfs", "mkfs.vfat"))
MIB = 1024 * 1024


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


class Refusals(unittest.TestCase):
    def setUp(self):
        self.system = support.FakeSystem(("0.1.0", "empty"), running=1)
        self.system.uki("0.1.0")
        self.addCleanup(shutil.rmtree, self.system.tmp, ignore_errors=True)
        self.target = os.path.join(self.system.tmp, "target.img")
        with open(self.target, "wb") as h:
            h.truncate(layout.MIN_DISK + 64 * MIB)

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

    def test_plan_lists_layout(self):
        plan = deploy.plan(self.system.paths, self.target, True)
        self.assertTrue(plan["possible"], plan["problems"])
        self.assertEqual(plan["version"], "0.1.0")
        labels = [p["label"] for p in plan["partitions"]]
        self.assertIn("ecp-root_0.1.0", labels)
        self.assertIn("_empty", labels)

    def test_list_disks_marks_boot_disk(self):
        disks = {d["name"]: d for d in deploy.list_disks(self.system.paths)}
        self.assertFalse(disks["vda"]["usable"])
        self.assertTrue(disks["vda"]["boot_medium"])


@unittest.skipUnless(TOOLS, "needs systemd-repart, mtools, mkfs.btrfs and mkfs.vfat")
class Install(unittest.TestCase):
    def setUp(self):
        self.system = support.FakeSystem(("0.1.0", "empty"), running=1, root_mib=4)
        self.addCleanup(shutil.rmtree, self.system.tmp, ignore_errors=True)
        self.uki = self.system.uki("0.1.0")
        self.data = os.urandom(3 * MIB)
        part = self.system.fill_slot(1, self.data, erofs_bytes=len(self.data))
        with open(self.system.image, "rb") as h:
            h.seek(part.offset)
            self.data = h.read(len(self.data))  # as written, including the superblock
        self.system.make_device_files()
        self.target = os.path.join(self.system.tmp, "target.img")
        with open(self.target, "wb") as h:
            h.truncate(layout.MIN_DISK + 64 * MIB)

    def run_deploy(self, **kw):
        ctx = Ctx()
        result = deploy.deploy(self.system.paths, self.target, self.target, ctx, allow_file=True, **kw)
        return result, ctx

    def test_install(self):
        result, ctx = self.run_deploy()
        self.assertTrue(ctx.committed)
        self.assertEqual(result["version"], "0.1.0")
        table = {p.name: p for p in gpt.read_table(self.target)}
        self.assertEqual(set(table), {"ECP-ESP", "ECP-RECOVERY", "ecp-root_0.1.0", "_empty", "ECP-DATA"})
        self.assertEqual(table["ecp-root_0.1.0"].size, layout.ROOT_SLOT)
        self.assertEqual(table["_empty"].size, layout.ROOT_SLOT)
        self.assertGreaterEqual(table["ECP-DATA"].size, layout.DATA_MIN)
        root = table["ecp-root_0.1.0"]
        with open(self.target, "rb") as h:
            h.seek(root.offset)
            self.assertEqual(h.read(len(self.data)), self.data)
        esp = table["ECP-ESP"]
        listing = subprocess.run(["mdir", "-b", "-i", f"{self.target}@@{esp.offset}", "::EFI/Linux"],
                                 capture_output=True, text=True, check=True).stdout
        self.assertIn("ecp_0.1.0+3.efi", listing)
        self.assertNotIn(".partial", listing)

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

    def test_installed_kernel_image_names_this_installs_partuuids(self):
        self.run_deploy()
        table = {p.name: p for p in gpt.read_table(self.target)}
        esp = table["ECP-ESP"]
        image = subprocess.run(["mtype", "-i", f"{self.target}@@{esp.offset}", "::EFI/Linux/ecp_0.1.0+3.efi"],
                               capture_output=True, check=True).stdout
        from foundationd.backends import pe
        cmdline = pe.read_cmdline(image).split()
        root, data = table["ecp-root_0.1.0"].uuid, table["ECP-DATA"].uuid
        self.assertIn(f"root=PARTUUID={root}", cmdline)
        self.assertIn(f"systemd.mount-extra=PARTUUID={data}:/var:btrfs:subvol=var", cmdline)
        self.assertIn(f"systemd.mount-extra=PARTUUID={data}:/home:btrfs:subvol=home", cmdline)
        self.assertFalse([t for t in cmdline if "PARTLABEL" in t or t.startswith("ecp.pad=")], cmdline)
        self.assertIn("quiet", cmdline)       # the rest of the command line is kept

    def test_each_install_gets_its_own_uuids(self):
        self.run_deploy()
        first = {p.name: p.uuid for p in gpt.read_table(self.target)}
        with open(self.target, "wb") as h:
            h.truncate(0)
            h.truncate(layout.MIN_DISK + 64 * MIB)
        self.run_deploy()
        second = {p.name: p.uuid for p in gpt.read_table(self.target)}
        self.assertNotEqual(first["ecp-root_0.1.0"], second["ecp-root_0.1.0"])
        self.assertNotEqual(first["ECP-DATA"], second["ECP-DATA"])

    def test_installed_cmdline_drops_the_label_arguments_and_the_padding(self):
        text = ("root=PARTLABEL=ecp-root_0.1.0 ro quiet systemd.mount-extra=LABEL=ECP-DATA:/var:btrfs:subvol=var "
                "systemd.mount-extra=LABEL=ECP-DATA:/home:btrfs:subvol=home ecp.pad=-----")
        out = deploy.installed_cmdline(text, "R", "D").split()
        self.assertEqual(out[:2], ["ro", "quiet"])
        self.assertEqual(out[2:], ["root=PARTUUID=R", "systemd.mount-extra=PARTUUID=D:/var:btrfs:subvol=var",
                                   "systemd.mount-extra=PARTUUID=D:/home:btrfs:subvol=home"])

    def test_the_definitions_can_pin_the_root_and_data_uuids(self):
        plain = layout.definitions("0.1.0", "/e", "/s")
        self.assertNotIn("UUID=", "".join(plain.values()))
        pinned = layout.definitions("0.1.0", "/e", "/s", {"root": "r-uuid", "data": "d-uuid"})
        self.assertIn("UUID=r-uuid\n", pinned["10-root-a.conf"])
        self.assertIn("UUID=d-uuid\n", pinned["30-data.conf"])
        self.assertNotIn("UUID=", pinned["15-root-b.conf"])

    def test_the_rewritten_command_line_fits_in_every_image_that_can_be_deployed(self):
        """ecp.pad= in the mkosi configuration reserves the room; this fails if it ever stops being enough."""
        import re
        root = os.path.join(os.path.dirname(__file__), "..", "..")
        if not os.path.isfile(os.path.join(root, "mkosi.conf.d", "10-x86-64.conf")):
            self.skipTest("not in the source tree")        # the RPM build only has the foundation/ directory

        def cmdline(*files):
            text = ""
            for name in files:
                with open(os.path.join(root, name), encoding="utf-8") as handle:
                    body = handle.read()
                match = re.search(r"^KernelCommandLine=(.*?)(?=^\S|\Z)", body, re.S | re.M)
                if match:
                    text += " " + match.group(1)
            return " ".join(text.replace("%v", "0.1.0").split())

        images = {
            "normal": cmdline("mkosi.conf.d/10-x86-64.conf", "mkosi.conf.d/30-data-mounts.conf"),
            "installer": cmdline("mkosi.conf.d/10-x86-64.conf", "mkosi.conf.d/30-data-mounts.conf", "mkosi.profiles/installer/mkosi.conf"),
        }
        for name, text in images.items():
            self.assertIn("ecp.pad=", text, name)
            new = deploy.installed_cmdline(text, uuid.uuid4(), uuid.uuid4())
            self.assertLessEqual(len(new), len(text), f"{name}: {len(new)} > {len(text)}")
            self.assertNotIn("ecp.pad=", new)
        for name, text in images.items():             # every image, the installer medium included, mounts by label
            self.assertIn("systemd.mount-extra=LABEL=ECP-DATA:/var:btrfs:subvol=var", text.split(), name)
        self.assertIn("root=PARTLABEL=ecp-root_0.1.0", images["normal"].split())
        roots = [t for t in images["installer"].split() if t.startswith("root=")]
        self.assertEqual(roots[-1], "root=PARTLABEL=ecp-live_0.1.0", "the last root= wins")

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
        esp = {p.name: p for p in gpt.read_table(self.target)}["ECP-ESP"]
        listing = subprocess.run(["mdir", "-b", "-i", f"{self.target}@@{esp.offset}", "::EFI/Linux"],
                                 capture_output=True, text=True, check=True).stdout
        self.assertNotIn("ecp_0.1.0+3.efi\n", listing + "\n")
        self.assertIn(".partial", listing)

    def test_no_staging_left_behind(self):
        self.run_deploy()
        staging = os.path.join(self.system.paths["state"], "staging")
        self.assertEqual(os.listdir(staging), [])


class LayoutDrift(unittest.TestCase):
    def test_matches_build_layout(self):
        root = os.path.join(os.path.dirname(__file__), "..", "..", "mkosi.repart.system")
        if not os.path.isdir(root):
            self.skipTest("not in the source tree")
        built = {}
        for name in sorted(os.listdir(root)):
            with open(os.path.join(root, name)) as handle:
                text = handle.read()
            fields = dict(l.split("=", 1) for l in text.splitlines() if "=" in l)
            built[fields.get("Label", name)] = fields
        made = layout.definitions("0.1.0", "/e", "/s")
        generated = {}
        for text in made.values():
            fields = dict(l.split("=", 1) for l in text.splitlines() if "=" in l)
            generated[fields["Label"]] = fields
        for label, fields in generated.items():
            built_label = label if label in built else next((k for k in built if k.startswith("ecp-root") and label.startswith("ecp-root")), label)
            self.assertIn(built_label, built, label)
            for key in ("Type", "SizeMin", "SizeMax", "Format"):
                if key in fields and key in built[built_label]:
                    self.assertEqual(fields[key], built[built_label][key], (label, key))


if __name__ == "__main__":
    unittest.main()
