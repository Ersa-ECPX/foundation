"""Compat RPMs and system extensions, with a real RPM and fake base-image queries."""

import glob
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

import support  # noqa: E402
from foundationd.backends import compat, elf, extensions  # noqa: E402
from foundationd.backends.runner import run  # noqa: E402
from foundationd.errors import Failed, InvalidArgument, NotFound  # noqa: E402

RPM = next(iter(sorted(glob.glob(os.path.join(HERE, "../../mkosi.packages/foundationd-*.rpm")))), None)


def _requirements():
    """Every non-rpmlib requirement of the RPM under test, so the tests do not hard-code a list that
    changes whenever the package does."""
    if not RPM:
        return set()
    out = run(["rpm", "-qp", "--nosignature", "--nodigest", "--requires", RPM])[1]
    return {line.split()[0] for line in out.splitlines() if line.strip() and not line.startswith("rpmlib(")}


REQUIRED = _requirements()


@unittest.skipUnless(RPM and support.tool_available("rpm") and support.tool_available("rpm2cpio"),
                     "needs the built foundationd RPM and the rpm tools")
class CompatCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.paths = {"state": os.path.join(self.tmp, "state"), "root": os.path.join(self.tmp, "root"),
                      "os_release": os.path.join(self.tmp, "os-release")}
        os.makedirs(os.path.join(self.paths["root"], "var/lib/extensions"))
        with open(self.paths["os_release"], "w") as handle:
            handle.write('ID="opensuse-tumbleweed"\n')
        self.provided = set(REQUIRED)
        self.libs = {"libc.so.6"}
        self.refresh_fails = False
        self.sysext_calls = []
        self.ctx = support.Recorder()

    def runner(self, cmd, timeout=60, input_bytes=None):
        if cmd[:3] == ["rpm", "-q", "--whatprovides"]:
            return (0 if cmd[3] in self.provided else 1), "", ""
        if cmd[0] == "ldconfig":
            return 0, "".join(f"\t{lib} (libc6,x86-64) => /usr/lib64/{lib}\n" for lib in self.libs), ""
        if cmd[0] == "systemd-sysext":
            self.sysext_calls.append(cmd[1])
            if cmd[1] == "status":
                names = [n for n in os.listdir(os.path.join(self.paths["root"], "var/lib/extensions")) if not n.startswith(".")]
                return 0, json.dumps([{"hierarchy": "/usr", "extensions": names or "none", "since": None}]), ""
            return (1, "", "merge failed") if self.refresh_fails else (0, "", "")
        return run(cmd, timeout, input_bytes)

    def extension_dir(self):
        return os.path.join(self.paths["root"], "var/lib/extensions/foundationd")


class AnalyseTests(CompatCase):
    def test_a_satisfiable_package_is_compatible(self):
        report, extras = compat.analyse(RPM, self.paths, self.runner)
        self.assertEqual(report["name"], "foundationd")
        self.assertTrue(report["compatible"], report["problems"])
        self.assertGreater(report["files"], 10)
        self.assertEqual(report["outside_usr"], [])
        self.assertIn("python3-gobject", extras["requires"])

    def test_missing_requirements_are_reported_by_name(self):
        self.assertIn("python3-gobject", self.provided)
        self.provided.discard("python3-gobject")
        report, _ = compat.analyse(RPM, self.paths, self.runner)
        self.assertFalse(report["compatible"])
        self.assertEqual(report["missing_requires"], ["python3-gobject"])

    def test_rpmlib_requirements_are_ignored(self):
        report, _ = compat.analyse(RPM, self.paths, self.runner)
        self.assertFalse([r for r in report["missing_requires"] if r.startswith("rpmlib(")])

    def test_files_that_would_shadow_the_base_are_conflicts(self):
        existing = os.path.join(self.paths["root"], "usr/libexec/foundationd")
        os.makedirs(os.path.dirname(existing))
        open(existing, "w").close()
        report, _ = compat.analyse(RPM, self.paths, self.runner)
        self.assertIn("/usr/libexec/foundationd", report["conflicts"])
        self.assertFalse(report["compatible"])

    def test_not_an_rpm(self):
        bad = os.path.join(self.tmp, "x.rpm")
        with open(bad, "w") as handle:
            handle.write("not an rpm")
        with self.assertRaises(InvalidArgument):
            compat.analyse(bad, self.paths, self.runner)

    def test_a_text_file_is_never_handed_to_rpm(self):
        # `rpm -qp` treats a plain-text file as a manifest and opens every path listed in it. Running
        # as root, that would let a caller make the service open paths of the caller's choosing.
        manifest = os.path.join(self.tmp, "manifest.rpm")
        with open(manifest, "w") as handle:
            handle.write("/etc/shadow\n/root/.ssh/id_ed25519\n")
        calls = []

        def runner(cmd, timeout=60, input_bytes=None):
            calls.append(cmd)
            return 0, "", ""
        with self.assertRaisesRegex(InvalidArgument, "not an RPM"):
            compat.analyse(manifest, self.paths, runner)
        self.assertEqual(calls, [])                      # no tool saw the file

    def test_a_truncated_or_empty_file(self):
        for content in (b"", b"\xed\xab"):
            path = os.path.join(self.tmp, "t.rpm")
            with open(path, "wb") as handle:
                handle.write(content)
            with self.assertRaises(InvalidArgument):
                compat.analyse(path, self.paths, self.runner)

    def test_scratch_space_is_cleaned_up(self):
        compat.analyse(RPM, self.paths, self.runner)
        self.assertEqual(os.listdir(os.path.join(self.paths["state"], "staging")), [])


class InstallTests(CompatCase):
    def install(self, force=False):
        return compat.install(RPM, force, self.paths, self.ctx, self.runner)

    def test_install_unpacks_labels_and_merges(self):
        result = self.install()
        self.assertEqual((result["name"], result["merged"]), ("foundationd", True))
        self.assertTrue(os.path.isfile(os.path.join(self.extension_dir(), "usr/libexec/foundationd")))
        with open(os.path.join(self.extension_dir(), "usr/lib/extension-release.d/extension-release.foundationd")) as handle:
            self.assertEqual(handle.read(), "ID=opensuse-tumbleweed\n")
        with open(os.path.join(self.paths["state"], "compat/foundationd.json")) as handle:
            meta = json.load(handle)
        self.assertEqual(meta["version"], run(["rpm", "-qp", "--nosignature", "--nodigest", "--qf", "%{VERSION}", RPM])[1])
        self.assertIn("refresh", self.sysext_calls)
        self.assertEqual(self.ctx.steps, ["inspect", "unpack", "install", "merge"])
        self.assertTrue(self.ctx.committed)

    def test_extension_release_follows_the_host_version_fields(self):
        with open(self.paths["os_release"], "w") as handle:
            handle.write('ID=x\nVERSION_ID=7\nSYSEXT_LEVEL=3\n')
        self.assertEqual(compat.release_file("n", "noarch", self.paths), "ID=x\nSYSEXT_LEVEL=3\n")
        with open(self.paths["os_release"], "w") as handle:
            handle.write('ID=x\nVERSION_ID=7\n')
        self.assertEqual(compat.release_file("n", "x86_64", self.paths), "ID=x\nVERSION_ID=7\nARCHITECTURE=x86-64\n")

    def test_incompatible_package_is_refused_unless_forced(self):
        self.provided.discard("polkit")
        with self.assertRaisesRegex(InvalidArgument, "not compatible"):
            self.install()
        self.assertFalse(os.path.exists(self.extension_dir()))
        self.assertTrue(self.install(force=True)["merged"])

    def test_installing_twice_is_refused(self):
        self.install()
        with self.assertRaisesRegex(InvalidArgument, "already installed"):
            self.install()

    def test_a_failed_merge_removes_the_extension_again(self):
        self.refresh_fails = True
        with self.assertRaisesRegex(Failed, "removed again"):
            self.install()
        self.assertFalse(os.path.exists(self.extension_dir()))
        self.assertFalse(os.path.exists(os.path.join(self.paths["state"], "compat/foundationd.json")))

    def test_nothing_is_left_in_the_scratch_directory(self):
        self.install()
        self.assertEqual(os.listdir(os.path.join(self.paths["state"], "staging")), [])

    def test_list_and_remove(self):
        self.install()
        (item,) = compat.list_installed(self.paths, self.runner)
        self.assertEqual((item["name"], item["enabled"], item["merged"]), ("foundationd", True, True))
        compat.remove("foundationd", self.paths, support.Recorder(), self.runner)
        self.assertFalse(os.path.exists(self.extension_dir()))
        self.assertEqual(compat.list_installed(self.paths, self.runner), [])

    def test_remove_unknown(self):
        with self.assertRaises(NotFound):
            compat.remove("nope", self.paths, support.Recorder(), self.runner)


class RevalidateTests(CompatCase):
    def test_extension_that_no_longer_fits_is_disabled_not_deleted_and_comes_back(self):
        compat.install(RPM, False, self.paths, self.ctx, self.runner)
        self.provided.discard("polkit")                      # the new base dropped polkit
        result = compat.revalidate(self.paths, support.Recorder(), self.runner)
        self.assertEqual((result["checked"], result["disabled"], result["enabled"]), (1, ["foundationd"], []))
        self.assertFalse(os.path.exists(self.extension_dir()))
        self.assertTrue(os.path.isdir(os.path.join(self.paths["state"], "compat/disabled/foundationd")))
        (item,) = compat.list_installed(self.paths, self.runner)
        self.assertFalse(item["enabled"])
        self.assertIn("polkit", item["problems"][0])

        self.provided.add("polkit")                          # a later base provides it again
        result = compat.revalidate(self.paths, support.Recorder(), self.runner)
        self.assertEqual((result["disabled"], result["enabled"]), ([], ["foundationd"]))
        self.assertTrue(os.path.isfile(os.path.join(self.extension_dir(), "usr/libexec/foundationd")))

    def test_nothing_changes_when_everything_still_fits(self):
        compat.install(RPM, False, self.paths, self.ctx, self.runner)
        before = list(self.sysext_calls)
        result = compat.revalidate(self.paths, support.Recorder(), self.runner)
        self.assertEqual((result["disabled"], result["enabled"]), ([], []))
        self.assertEqual([c for c in self.sysext_calls[len(before):] if c == "refresh"], [])   # no needless refresh


class RichRequirements(unittest.TestCase):
    """check_requirements with the rich dependencies from Google Chrome's real RPM header."""

    CURL = ("(libcurl.so()(64bit) or libcurl-gnutls.so.4()(64bit) or libcurl-nss.so.4()(64bit) "
            "or libcurl.so.4()(64bit))")
    GTK = "(libgtk-3.so.0()(64bit) or libgtk-4.so.1()(64bit))"

    def runner(self, have):
        def run_it(cmd, timeout=60, input_bytes=None):
            return (0 if cmd[3] in have else 1), "", ""
        return run_it

    def test_alternatives_are_satisfied_by_any_one(self):
        have = {"libcurl.so.4()(64bit)", "libgtk-3.so.0()(64bit)", "bash"}
        self.assertEqual(compat.check_requirements([self.CURL, self.GTK, "bash"], set(), self.runner(have)), [])

    def test_an_unsatisfied_alternative_is_reported_as_the_whole_expression(self):
        missing = compat.check_requirements([self.CURL, "bash"], set(), self.runner({"bash"}))
        self.assertEqual(missing, [self.CURL])

    def test_a_rich_requirement_the_package_provides_itself_is_satisfied(self):
        self.assertEqual(compat.check_requirements(["(a or b)"], {"b"}, self.runner(set())), [])

    def test_an_unparseable_one_is_reported_rather_than_assumed_fine(self):
        self.assertEqual(compat.check_requirements(["(a and"], set(), self.runner({"a"})), ["(a and"])


def _system_binary(name):
    import shutil
    return shutil.which(name)


def _system_library(name):
    import glob
    for pattern in (f"/usr/lib*/{name}", f"/usr/lib/*-linux-gnu/{name}", f"/lib*/{name}"):
        for path in sorted(glob.glob(pattern)):
            if elf.read_dynamic(path) is not None:
                return path
    return None


class LibraryClassification(unittest.TestCase):
    """Programs versus plugins, with real binaries from this system."""

    def setUp(self):
        self.tree = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tree, True)
        self.program = _system_binary("ls")
        self.plugin = _system_library("libglib-2.0.so.0")
        if not (self.program and self.plugin):
            self.skipTest("needs /usr/bin/ls and libglib on this system")
        self.program_needs = set(elf.read_dynamic(self.program)["needed"])
        self.plugin_needs = set(elf.read_dynamic(self.plugin)["needed"])
        if not (self.plugin_needs - self.program_needs):
            self.skipTest("the sample plugin needs nothing the sample program does not")

    def put(self, source, relative):
        target = os.path.join(self.tree, relative)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copy(source, target)

    def test_a_library_no_program_links_is_optional(self):
        self.put(self.program, "usr/bin/prog")
        self.put(self.plugin, "usr/lib/plugin.so")             # nothing links against it: a plugin
        required, optional, provided = compat.scan_libraries(self.tree)
        self.assertEqual(required, self.program_needs)
        self.assertEqual(optional, self.plugin_needs - self.program_needs)
        self.assertIn("plugin.so", provided)

    def test_a_library_a_program_links_is_followed(self):
        self.put(self.program, "usr/bin/prog")
        linked = next((n for n in sorted(self.program_needs) if _system_library(n)), None)
        if linked is None:
            self.skipTest("no linkable library of the sample program found")
        self.put(_system_library(linked), f"usr/lib/{linked}")  # the package ships a library prog needs
        required, _optional, provided = compat.scan_libraries(self.tree)
        self.assertIn(linked, provided)
        self.assertTrue(set(elf.read_dynamic(_system_library(linked))["needed"]) <= required)

    def test_a_package_with_no_program_requires_all_its_libraries(self):
        self.put(self.plugin, "usr/lib/libthing.so.1")
        required, optional, _provided = compat.scan_libraries(self.tree)
        self.assertEqual(required, self.plugin_needs)
        self.assertEqual(optional, set())


@unittest.skipUnless(support.tool_available("rpmbuild") and support.tool_available("rpm") and support.tool_available("rpm2cpio"),
                     "needs rpmbuild and the rpm tools")
class VerdictRules(unittest.TestCase):
    """Problems stop an install; notes do not. Built from a small real RPM."""

    SPEC = """Name: probe
Version: 1.0
Release: 1
Summary: test package
License: MIT
BuildArch: noarch
%description
x
%install
mkdir -p %{buildroot}/usr/bin %{buildroot}/etc/cron.daily %{buildroot}/bin
printf '#!/bin/sh\n' > %{buildroot}/usr/bin/probe
chmod 755 %{buildroot}/usr/bin/probe
printf 'job\n' > %{buildroot}/etc/cron.daily/probe
%post
echo installed
%files
/usr/bin/probe
/etc/cron.daily/probe
"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        spec = os.path.join(self.tmp, "probe.spec")
        with open(spec, "w") as handle:
            handle.write(self.SPEC)
        top = os.path.join(self.tmp, "top")
        done = run(["rpmbuild", "--define", f"_topdir {top}", "--define", f"_dbpath {self.tmp}/db",
                    "--define", "_build_id_links none", "-bb", spec], timeout=120)
        if done[0] != 0:
            self.skipTest("rpmbuild could not build the sample package: " + done[2][-200:])
        self.rpm = next(iter(glob.glob(os.path.join(top, "RPMS", "*", "probe-*.rpm"))))
        self.paths = {"state": os.path.join(self.tmp, "state"), "root": os.path.join(self.tmp, "root"),
                      "os_release": os.path.join(self.tmp, "os-release")}
        os.makedirs(self.paths["root"])
        with open(self.paths["os_release"], "w") as handle:
            handle.write("ID=x\n")

    def runner(self, cmd, timeout=60, input_bytes=None):
        if cmd[:3] == ["rpm", "-q", "--whatprovides"]:
            return 0, "", ""                       # the base provides whatever is asked for
        if cmd[0] == "ldconfig":
            return 0, "", ""
        return run(cmd, timeout, input_bytes)

    def test_files_outside_usr_and_scriptlets_are_notes_not_problems(self):
        report, _extras = compat.analyse(self.rpm, self.paths, self.runner)
        self.assertTrue(report["compatible"], report["problems"])
        self.assertEqual(report["problems"], [])
        self.assertEqual(report["outside_usr"], ["/etc/cron.daily/probe"])
        self.assertTrue(any("outside /usr and /opt" in note for note in report["notes"]), report["notes"])
        self.assertTrue(any("scriptlets" in note for note in report["notes"]), report["notes"])
        self.assertEqual(report["files"], 1)       # only /usr/bin/probe would be installed

    def test_an_installed_package_is_labelled_for_the_paths_it_will_have_when_merged(self):
        policy = os.path.join(self.paths["root"], "etc/selinux/targeted/contexts/files")
        os.makedirs(policy)
        with open(os.path.join(policy, "file_contexts"), "w") as handle:
            handle.write("/.*   system_u:object_r:usr_t:s0\n")
        seen = []
        from foundationd.backends import sellabel
        real = sellabel.label_tree
        sellabel.label_tree = lambda tree, contexts, setter=None: seen.append(sorted(os.listdir(tree)))
        self.addCleanup(setattr, sellabel, "label_tree", real)

        def runner(cmd, timeout=60, input_bytes=None):
            if cmd[0] == "systemd-sysext":
                return 0, json.dumps([{"hierarchy": "/usr", "extensions": ["probe"]}]), ""
            return self.runner(cmd, timeout, input_bytes)
        compat.install(self.rpm, False, self.paths, support.Recorder(), runner)
        self.assertEqual(seen, [["usr"]])

    def test_installing_such_a_package_succeeds_and_reports_what_was_dropped(self):
        ctx = support.Recorder()

        def runner(cmd, timeout=60, input_bytes=None):
            if cmd[0] == "systemd-sysext":
                return 0, json.dumps([{"hierarchy": "/usr", "extensions": ["probe"]}]), ""
            return self.runner(cmd, timeout, input_bytes)
        result = compat.install(self.rpm, False, self.paths, ctx, runner)
        self.assertEqual(result["name"], "probe")
        self.assertTrue(os.path.isfile(os.path.join(self.paths["root"], "var/lib/extensions/probe/usr/bin/probe")))
        self.assertFalse(os.path.exists(os.path.join(self.paths["root"], "var/lib/extensions/probe/etc")))
        self.assertEqual(result["dropped"], ["/etc"])


class RemapTests(unittest.TestCase):
    def test_usrmerge_mapping(self):
        self.assertEqual(compat.remap("/bin/foo"), "usr/bin/foo")
        self.assertEqual(compat.remap("/lib64/libx.so"), "usr/lib64/libx.so")
        self.assertEqual(compat.remap("/usr/bin/foo"), "usr/bin/foo")
        self.assertEqual(compat.remap("/opt/app/x"), "opt/app/x")
        self.assertIsNone(compat.remap("/etc/foo.conf"))
        self.assertIsNone(compat.remap("/var/lib/x"))

    def test_apply_usrmerge_moves_and_drops(self):
        with tempfile.TemporaryDirectory() as tree:
            for path in ("bin/a", "usr/bin/b", "etc/c.conf", "opt/d", "lib64/e"):
                os.makedirs(os.path.dirname(os.path.join(tree, path)), exist_ok=True)
                open(os.path.join(tree, path), "w").close()
            dropped = compat.apply_usrmerge(tree)
            self.assertEqual(dropped, ["/etc"])
            self.assertTrue(all(os.path.exists(os.path.join(tree, p)) for p in ("usr/bin/a", "usr/bin/b", "opt/d", "usr/lib64/e")))
            self.assertFalse(os.path.exists(os.path.join(tree, "bin")))


class ExtensionTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_discover_finds_directories_images_and_links_and_skips_hidden(self):
        base = os.path.join(self.root, "var/lib/extensions")
        os.makedirs(os.path.join(base, "tree"))
        os.makedirs(os.path.join(base, ".hidden"))
        open(os.path.join(base, "img.raw"), "w").close()
        open(os.path.join(base, "notes.txt"), "w").close()
        os.symlink("tree", os.path.join(base, "alias"))
        found = {e["name"]: e["type"] for e in extensions.discover("sysext", self.root)}
        self.assertEqual(found, {"alias": "link", "img": "image", "tree": "directory"})

    def test_first_directory_wins_like_systemd(self):
        for relative in ("etc/extensions", "var/lib/extensions"):
            os.makedirs(os.path.join(self.root, relative, "dup"))
        (item,) = extensions.discover("sysext", self.root)
        self.assertIn("etc/extensions", item["path"])

    def test_confext_uses_its_own_directories(self):
        os.makedirs(os.path.join(self.root, "var/lib/confexts/cfg"))
        os.makedirs(os.path.join(self.root, "var/lib/extensions/sys"))
        self.assertEqual([e["name"] for e in extensions.discover("confext", self.root)], ["cfg"])

    def test_merged_names_from_status_json(self):
        def runner(cmd, timeout=60, input_bytes=None):
            return 0, json.dumps([{"hierarchy": "/usr", "extensions": ["a", "b"]}, {"hierarchy": "/opt", "extensions": "none"}]), ""
        self.assertEqual(extensions.merged_names("sysext", runner), {"a", "b"})

    def test_a_missing_tool_is_reported(self):
        from foundationd.errors import Unavailable
        with self.assertRaises(Unavailable):
            extensions.merged_names("sysext", lambda cmd, timeout=60, input_bytes=None: (127, "", "not found"))

    def test_operate_returns_what_is_merged(self):
        calls = []

        def runner(cmd, timeout=60, input_bytes=None):
            calls.append(cmd[:2])
            return 0, json.dumps([{"hierarchy": "/usr", "extensions": ["x"]}]), ""
        self.assertEqual(extensions.operate("confext", "refresh", runner), {"kind": "confext", "merged": ["x"]})
        self.assertEqual(calls[0], ["systemd-confext", "refresh"])


if __name__ == "__main__":
    unittest.main()
