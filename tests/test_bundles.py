"""Bundles: many packages from the repositories in one system extension, built from real small RPMs."""

import glob
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

import support  # noqa: E402
from foundationd.backends import bundles, compat, repos  # noqa: E402
from foundationd.errors import Failed, InvalidArgument, NotFound  # noqa: E402

SPECS = {
    "one": ("1.0", "/usr/bin/one /usr/lib64/libone.so.1 /etc/one.conf", "mkdir -p %{buildroot}/usr/bin %{buildroot}/usr/lib64 %{buildroot}/etc\n"
            "printf '#!/bin/sh\\n' > %{buildroot}/usr/bin/one\nprintf lib > %{buildroot}/usr/lib64/libone.so.1\nprintf c > %{buildroot}/etc/one.conf\n"),
    "two": ("1.0", "/usr/bin/two /usr/share/two/data.txt", "mkdir -p %{buildroot}/usr/bin %{buildroot}/usr/share/two\n"
            "printf '#!/bin/sh\\n' > %{buildroot}/usr/bin/two\nprintf data > %{buildroot}/usr/share/two/data.txt\n"),
    "three": ("1.0", "/usr/bin/three", "mkdir -p %{buildroot}/usr/bin\nprintf '#!/bin/sh\\n' > %{buildroot}/usr/bin/three\n"),
}
DEPENDS = {"one": ["two"]}


def build_rpms(directory):
    top = os.path.join(directory, "top")
    for name, (version, files, install) in SPECS.items():
        spec = os.path.join(directory, f"{name}.spec")
        with open(spec, "w") as handle:
            handle.write(f"Name: {name}\nVersion: {version}\nRelease: 1\nSummary: s\nLicense: MIT\nBuildArch: noarch\n"
                         f"%description\nx\n%install\n{install}%files\n" + "\n".join(files.split()) + "\n")
        done = subprocess.run(["rpmbuild", "--define", f"_topdir {top}", "--define", f"_dbpath {directory}/db",
                               "--define", "_build_id_links none", "-bb", spec], capture_output=True)
        if done.returncode != 0:
            raise unittest.SkipTest("rpmbuild could not build the sample packages")
    return {os.path.basename(p).split("-")[0]: p for p in glob.glob(os.path.join(top, "RPMS", "*", "*.rpm"))}


class FakePool:
    """Stands in for repos.Pool: the solver is tested against real repositories, not here."""
    instances = []
    versions = {}

    def __init__(self, enabled, paths, extra=()):
        self.enabled, self.extra = enabled, list(extra)
        FakePool.instances.append(self)

    def resolve(self, wanted):
        todo, skipped, seen = [], [], set()

        def add(name, repo):
            if name in seen:
                return
            seen.add(name)
            for dependency in DEPENDS.get(name, []):
                add(dependency, repo)
            if name in self.extra_names():
                return
            todo.append(self.package(name, repo))
        for item in wanted:
            if item.get("when_installed"):
                skipped.append(item["name"])
                continue
            if item["name"] not in SPECS:
                raise NotFound(f"not found in the enabled repositories: {item['name']}")
            add(item["name"], item.get("repo") or "test")
        return todo, skipped, []

    def extra_names(self):
        return {os.path.basename(p).split("-")[0] for p in self.extra}

    def package(self, name, repo):
        rpm = RPMS[name]
        with open(rpm, "rb") as handle:
            checksum = hashlib.sha256(handle.read()).hexdigest()
        version = FakePool.versions.get(name, "1.0-1")
        return {"name": name, "evr": version, "arch": "noarch", "repo": repo, "location": "noarch/" + os.path.basename(rpm),
                "checksum": ["sha256", checksum], "size": os.path.getsize(rpm), "summary": ""}


RPMS = {}


@unittest.skipUnless(shutil.which("rpmbuild") and shutil.which("rpm2cpio"), "needs rpmbuild and rpm2cpio")
class BundleCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shared = tempfile.mkdtemp()
        RPMS.update(build_rpms(cls.shared))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.shared, True)

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.paths = {"state": os.path.join(self.tmp, "state"), "root": os.path.join(self.tmp, "root"),
                      "os_release": os.path.join(self.tmp, "os-release")}
        self.web = os.path.join(self.tmp, "web")
        os.makedirs(os.path.join(self.web, "noarch"))
        for rpm in RPMS.values():
            shutil.copy(rpm, os.path.join(self.web, "noarch"))
        self.extensions = os.path.join(self.paths["root"], "var/lib/extensions")
        os.makedirs(self.extensions)
        with open(self.paths["os_release"], "w") as handle:
            handle.write('ID="opensuse-tumbleweed"\n')
        vendor = os.path.join(self.paths["root"], repos.VENDOR_DIR)
        os.makedirs(vendor)
        self.write_repo("test", enabled=1)
        self.write_repo("extra", enabled=0)
        FakePool.instances, FakePool.versions = [], {}
        self.merge_fails = False
        self.sysext_calls = []
        self.ctx = support.Recorder()

    def write_repo(self, alias, enabled):
        with open(os.path.join(self.paths["root"], repos.VENDOR_DIR, f"{alias}.repo"), "w") as handle:
            handle.write(f"[{alias}]\nname={alias}\nenabled={enabled}\nbaseurl=file://{self.web}/\ngpgkey=file:///keys/{alias}.asc\n"
                         "gpgcheck=0\nrepo_gpgcheck=0\n")
        directory = repos.cache_dir(self.paths, alias)
        os.makedirs(directory, exist_ok=True)
        for name, text in (("primary.xml.gz", "x"), ("refreshed", str(int(__import__("time").time())))):
            with open(os.path.join(directory, name), "w") as handle:
                handle.write(text)

    def runner(self, cmd, timeout=60, input_bytes=None):
        if cmd[0] == "systemd-sysext":
            self.sysext_calls.append(cmd[1])
            if cmd[1] == "refresh" and self.merge_fails:
                return 1, "", "merge refused"
            return 0, json.dumps([]), ""
        if cmd[0] == "ldconfig":
            return 0, "", ""
        return 0, "", ""

    def build(self, name, specs, **kwargs):
        kwargs.setdefault("pool_factory", FakePool)
        return bundles.build(self.paths, self.ctx, name, specs, runner=self.runner, **kwargs)

    def add(self, name, packages, **kwargs):
        kwargs.setdefault("pool_factory", FakePool)
        return bundles.add_packages(self.paths, self.ctx, name, packages, runner=self.runner, **kwargs)

    def tree(self, name="media"):
        return os.path.join(self.extensions, name)

    def exists(self, relative, name="media"):
        return os.path.lexists(os.path.join(self.tree(name), relative))


class FollowingTheImage(BundleCase):
    """A bundle belongs to the system image it was built for: slots, updates and rollbacks keep them matching."""

    def set_image(self, version, version_id="20251101"):
        with open(self.paths["os_release"], "w") as handle:
            handle.write(f'ID="opensuse-tumbleweed"\nVERSION_ID="{version_id}"\nIMAGE_ID="ecp"\nIMAGE_VERSION="{version}"\n')

    def setUp(self):
        super().setUp()
        self.set_image("0.1.0")
        real = repos.refresh
        repos.refresh = lambda repo, *a, **k: {"alias": repo.alias, "changed": False, "revision": ""}
        self.addCleanup(setattr, repos, "refresh", real)

    def state(self, name="media"):
        return {b["name"]: b["state"] for b in bundles.list_bundles(self.paths)}[name]

    def test_a_bundle_records_the_image_it_was_built_for(self):
        self.build("media", ["one"])
        (bundle,) = bundles.list_bundles(self.paths)
        self.assertEqual(bundle["built_for"], "ecp_0.1.0_20251101")
        self.assertEqual(bundle["state"], "current")

    def test_a_new_image_makes_the_bundle_stale_and_boot_switches_it_off(self):
        self.build("media", ["one"])
        self.set_image("0.2.0")
        self.assertEqual(self.state(), "stale")
        result = bundles.select(self.paths)
        self.assertEqual((result["disabled"], result["stale"]), (["media"], ["media"]))
        self.assertFalse(os.path.exists(self.tree()), "never merged against a base it was not built for")
        self.assertTrue(os.path.isfile(os.path.join(self.paths["state"], "bundles", "stale")))
        self.assertEqual(self.state(), "stale")

    def test_a_stale_bundle_is_not_re_enabled_by_revalidate(self):
        self.build("media", ["one"])
        self.set_image("0.2.0")
        bundles.select(self.paths)
        compat.revalidate(self.paths, self.ctx, self.runner)
        self.assertFalse(os.path.exists(self.tree()))

    def test_refresh_rebuilds_only_what_does_not_fit(self):
        self.build("media", ["one"])
        self.build("tools", ["three"])
        self.set_image("0.2.0")
        self.build("tools", ["three"])                    # this one was already rebuilt for the new image
        result = bundles.refresh_stale(self.paths, self.ctx, runner=self.runner, pool_factory=FakePool)
        self.assertEqual([r["name"] for r in result["bundles"]], ["media"])
        self.assertEqual((result["failed"], result["skipped"]), ([], ["tools"]))
        self.assertEqual(self.state("media"), "current")
        self.assertTrue(self.exists("usr/bin/one"))
        self.assertFalse(os.path.exists(os.path.join(self.paths["state"], "bundles", "stale")))

    def test_the_tree_for_the_old_image_is_kept_so_a_rollback_finds_it(self):
        self.build("media", ["one"])
        self.set_image("0.2.0")
        bundles.refresh_stale(self.paths, self.ctx, runner=self.runner, pool_factory=FakePool)
        self.assertTrue(os.path.isdir(os.path.join(bundles._images_dir(self.paths, "ecp_0.1.0_20251101"), "media")))
        self.set_image("0.1.0")                           # the machine falls back to the old slot
        self.assertEqual(self.state(), "stale")
        result = bundles.select(self.paths)
        self.assertEqual(result["restored"], ["media"])
        self.assertEqual(self.state(), "current")
        self.assertTrue(self.exists("usr/bin/one"))
        self.set_image("0.2.0")                           # and forward again, without any download
        self.assertEqual(bundles.select(self.paths)["restored"], ["media"])
        self.assertEqual(self.state(), "current")

    def test_a_rebuild_that_cannot_be_merged_leaves_the_old_tree_available(self):
        self.build("media", ["one"])
        self.set_image("0.2.0")
        self.merge_fails = True
        result = bundles.refresh_stale(self.paths, self.ctx, runner=self.runner, pool_factory=FakePool)
        self.assertEqual([f["name"] for f in result["failed"]], ["media"])
        self.set_image("0.1.0")
        self.merge_fails = False
        bundles.select(self.paths)
        self.assertEqual(self.state(), "current")
        self.assertTrue(self.exists("usr/bin/one"))

    def new_image(self, version, libs=()):
        root = os.path.join(self.tmp, "new-" + version)
        os.makedirs(os.path.join(root, "usr/lib"))
        os.makedirs(os.path.join(root, "usr/lib64"))
        for lib in libs:
            open(os.path.join(root, "usr/lib64", lib), "w").close()
        with open(os.path.join(root, "usr/lib/os-release"), "w") as handle:
            handle.write(f'ID="opensuse-tumbleweed"\nVERSION_ID="20251101"\nIMAGE_ID="ecp"\nIMAGE_VERSION="{version}"\n')
        return root

    def test_bundles_are_built_for_an_installed_image_before_it_boots(self):
        self.build("media", ["one"])
        result = bundles.prebuild(self.paths, self.ctx, self.new_image("0.2.0"), runner=self.runner, pool_factory=FakePool)
        self.assertEqual((result["ready"], result["failed"], result["identity"]), (["media"], [], "ecp_0.2.0_20251101"))
        self.assertEqual(self.state(), "current", "the running image is untouched")
        self.assertTrue(self.exists("usr/bin/one"))
        self.set_image("0.2.0")                           # first boot of the new image
        self.assertEqual(self.state(), "stale")
        self.assertEqual(bundles.select(self.paths)["restored"], ["media"])
        self.assertEqual(self.state(), "current")
        with open(os.path.join(self.tree(), "usr/lib/extension-release.d/extension-release.media")) as handle:
            self.assertIn("VERSION_ID=20251101", handle.read())
        self.set_image("0.1.0")                           # and the old slot still has its own
        self.assertEqual(bundles.select(self.paths)["restored"], ["media"])

    def test_a_bundle_that_does_not_fit_the_new_image_is_reported_and_left_for_later(self):
        self.build("media", ["one"])
        original = bundles.compat.scan_libraries
        bundles.compat.scan_libraries = lambda tree: ({"libgone.so.3"}, set(), set())
        self.addCleanup(setattr, bundles.compat, "scan_libraries", original)
        result = bundles.prebuild(self.paths, self.ctx, self.new_image("0.2.0", libs=["libother.so.1"]),
                                  runner=self.runner, pool_factory=FakePool)
        self.assertEqual(result["ready"], [])
        self.assertIn("libgone.so.3", result["failed"][0]["error"])
        self.assertEqual(self.state(), "current")

    def test_a_failed_refresh_of_the_package_lists_leaves_every_bundle_for_later(self):
        self.build("media", ["one"])
        def broken(repo, *a, **k):
            raise Failed("no network")
        repos.refresh = broken
        result = bundles.prebuild(self.paths, self.ctx, self.new_image("0.2.0"), runner=self.runner, pool_factory=FakePool)
        self.assertEqual([f["name"] for f in result["failed"]], ["media"])


class Building(BundleCase):
    def test_many_packages_become_one_extension_with_their_dependencies(self):
        result = self.build("media", ["one"])
        self.assertEqual(sorted(result["packages"]), ["one-1.0-1", "two-1.0-1"])      # two came in as a dependency
        for path in ("usr/bin/one", "usr/bin/two", "usr/lib64/libone.so.1", "usr/share/two/data.txt"):
            self.assertTrue(self.exists(path), path)
        self.assertFalse(self.exists("etc"), "files outside /usr and /opt are dropped")
        with open(os.path.join(self.tree(), "usr/lib/extension-release.d/extension-release.media")) as handle:
            self.assertIn("ID=opensuse-tumbleweed", handle.read())
        self.assertTrue(self.ctx.committed)
        self.assertIn("refresh", self.sysext_calls)

    def test_the_manifest_records_what_was_asked_for_and_what_was_built(self):
        self.build("media", [{"name": "one", "repo": "test"}], catalog_name="codecs")
        (bundle,) = bundles.list_bundles(self.paths)
        self.assertEqual((bundle["name"], bundle["catalog"], bundle["requested"]), ("media", "codecs", ["one"]))
        self.assertEqual(sorted(bundle["packages"]), ["one-1.0-1", "two-1.0-1"])
        self.assertEqual(bundle["repos"], ["test"])
        self.assertTrue(bundle["enabled"])
        listed = {item["name"]: item for item in compat.list_installed(self.paths, self.runner)}
        self.assertIn("media", listed)                 # compat list and remove see bundles too

    def test_downloaded_rpms_are_kept_so_a_rebuild_fetches_only_what_is_new(self):
        self.build("media", ["one"])
        cache = bundles._rpm_dir(self.paths, "media")
        self.assertEqual(sorted(os.listdir(cache)), sorted(os.path.basename(RPMS[n]) for n in ("one", "two")))

    def test_nothing_to_install_is_an_error(self):
        with self.assertRaises(InvalidArgument) as caught:
            self.build("media", [{"name": "one", "when_installed": True}])
        self.assertIn("already part of the base", str(caught.exception))
        self.assertEqual(os.listdir(self.extensions), [])

    def test_unknown_packages_and_bad_names_are_refused_before_anything_is_written(self):
        with self.assertRaises(NotFound):
            self.build("media", ["nonesuch"])
        for bad in ("../x", "-x", "a b", ""):
            with self.assertRaises(InvalidArgument):
                self.build(bad, ["one"])
        with self.assertRaises(InvalidArgument):
            self.build("media", ["bad name"])
        self.assertEqual(os.listdir(self.extensions), [])

    def test_a_name_that_is_already_an_unmanaged_extension_is_refused(self):
        os.makedirs(os.path.join(self.extensions, "media"))
        with self.assertRaises(InvalidArgument):
            self.build("media", ["one"])

    def test_the_other_bundles_count_as_installed_when_solving(self):
        self.build("media", ["one"])
        self.build("tools", ["three"])
        second = FakePool.instances[-1]
        self.assertEqual(sorted(os.path.basename(p).split("-")[0] for p in second.extra), ["one", "two"])


class Changing(BundleCase):
    def test_adding_a_package_rebuilds_the_bundle_with_all_of_them(self):
        self.add("media", ["one"])
        result = self.add("media", ["three"])
        self.assertEqual(sorted(result["packages"]), ["one-1.0-1", "three-1.0-1", "two-1.0-1"])
        for path in ("usr/bin/one", "usr/bin/two", "usr/bin/three"):
            self.assertTrue(self.exists(path), path)
        self.assertEqual([b["requested"] for b in bundles.list_bundles(self.paths)], [["one", "three"]])
        self.assertEqual(os.listdir(self.extensions), ["media"], "no leftover .previous directory for sysext to pick up")

    def test_adding_a_package_that_is_already_there_changes_nothing_in_the_request(self):
        self.add("media", ["one"])
        self.add("media", ["one", "three"])
        self.assertEqual([b["requested"] for b in bundles.list_bundles(self.paths)], [["one", "three"]])

    def test_removing_a_package_takes_its_exclusive_dependencies_with_it(self):
        self.add("media", ["one", "three"])
        self.assertTrue(self.exists("usr/bin/two"))
        bundles.remove_packages(self.paths, self.ctx, "media", ["one"], runner=self.runner, pool_factory=FakePool)
        self.assertFalse(self.exists("usr/bin/one"))
        self.assertFalse(self.exists("usr/bin/two"))
        self.assertTrue(self.exists("usr/bin/three"))
        self.assertEqual(os.listdir(bundles._rpm_dir(self.paths, "media")), [os.path.basename(RPMS["three"])])

    def test_removing_the_last_package_removes_the_bundle(self):
        self.add("media", ["one"])
        bundles.remove_packages(self.paths, self.ctx, "media", ["one"], runner=self.runner, pool_factory=FakePool)
        self.assertEqual(os.listdir(self.extensions), [])
        self.assertEqual(bundles.list_bundles(self.paths), [])
        self.assertFalse(os.path.exists(bundles._rpm_dir(self.paths, "media")))

    def test_only_packages_that_were_asked_for_can_be_removed(self):
        self.add("media", ["one"])
        with self.assertRaises(NotFound) as caught:
            bundles.remove_packages(self.paths, self.ctx, "media", ["two"], runner=self.runner, pool_factory=FakePool)
        self.assertIn("asked for", str(caught.exception))
        with self.assertRaises(NotFound):
            bundles.remove_packages(self.paths, self.ctx, "nope", ["one"], runner=self.runner, pool_factory=FakePool)

    def test_a_failed_merge_brings_the_previous_bundle_back(self):
        self.add("media", ["one"])
        before = bundles.list_bundles(self.paths)
        self.merge_fails = True
        with self.assertRaises(Failed) as caught:
            self.add("media", ["three"])
        self.assertIn("previous state was restored", str(caught.exception))
        self.assertTrue(self.exists("usr/bin/one"))
        self.assertFalse(self.exists("usr/bin/three"))
        self.assertEqual(bundles.list_bundles(self.paths), before)
        self.assertEqual(os.listdir(self.extensions), ["media"])

    def test_a_failed_first_merge_leaves_nothing_behind(self):
        self.merge_fails = True
        with self.assertRaises(Failed):
            self.add("media", ["one"])
        self.assertEqual(os.listdir(self.extensions), [])
        self.assertEqual(bundles.list_bundles(self.paths), [])

    def test_update_rebuilds_only_when_packages_changed(self):
        self.add("media", ["one"])
        FakePool.versions = {"two": "1.1-1"}
        refreshed = []
        real = repos.refresh
        repos.refresh = lambda repo, *a, **k: refreshed.append((repo.alias, k.get("force")))
        self.addCleanup(setattr, repos, "refresh", real)
        result = bundles.update(self.paths, self.ctx, "media", runner=self.runner, pool_factory=FakePool)
        (outcome,) = result["bundles"]
        self.assertEqual(outcome["changed"], ["two-1.1-1"])
        self.assertIn(("test", True), refreshed, "an update refreshes the repository data first")
        with self.assertRaises(NotFound):
            bundles.update(self.paths, self.ctx, "nope", runner=self.runner, pool_factory=FakePool)

    def test_a_bundle_that_other_bundles_use_cannot_be_removed(self):
        self.add("media", ["one"])
        self.add("tools", ["three"])
        for name, provides, needs in (("media", ["libone.so.1"], []), ("tools", [], ["libone.so.1"])):
            meta = bundles._meta(self.paths, name)
            meta["provides_libs"], meta["needed_libs"] = provides, needs
            compat._write_json(compat._meta_path(self.paths, name), meta)
        self.assertEqual(bundles.dependents(self.paths, "media", self.runner), ["tools"])
        with self.assertRaises(InvalidArgument) as caught:
            bundles.remove_bundle(self.paths, self.ctx, "media", self.runner)
        self.assertIn("tools", str(caught.exception))
        bundles.remove_bundle(self.paths, self.ctx, "tools", self.runner)
        bundles.remove_bundle(self.paths, self.ctx, "media", self.runner)

    def test_a_package_from_an_rpm_file_is_not_a_bundle(self):
        compat._write_json(compat._meta_path(self.paths, "legacy"), {"name": "legacy", "version": "1", "release": "1"})
        with self.assertRaises(InvalidArgument):
            self.add("legacy", ["one"])
        with self.assertRaises(NotFound):
            bundles.remove_packages(self.paths, self.ctx, "legacy", ["one"], runner=self.runner)


class Repositories(BundleCase):
    def test_a_disabled_repository_is_only_enabled_with_the_callers_agreement(self):
        with self.assertRaises(InvalidArgument) as caught:
            self.build("media", [{"name": "one", "repo": "extra"}])
        self.assertIn("not enabled", str(caught.exception))
        self.assertFalse(repos.load_repos(self.paths)["extra"].enabled)
        self.build("media", [{"name": "one", "repo": "extra"}], allow_repos=("extra",))
        self.assertTrue(repos.load_repos(self.paths)["extra"].enabled)

    def test_an_unknown_repository_is_refused(self):
        with self.assertRaises(NotFound):
            self.build("media", [{"name": "one", "repo": "elsewhere"}])


class Catalog(BundleCase):
    def write(self, name, body):
        directory = os.path.join(self.paths["root"], bundles.CATALOG_DIR)
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, f"{name}.json"), "w") as handle:
            json.dump(body, handle)

    def test_catalog_entries_are_listed_and_installed_as_bundles(self):
        self.write("codecs", {"name": "codecs", "title": "Codecs", "description": "d", "notice": "n", "repos": ["extra"],
                              "packages": [{"name": "one", "repo": "extra"}, "three", {"name": "gone", "when_installed": True}]})
        (entry,) = bundles.catalog(self.paths)
        self.assertEqual((entry["name"], entry["repos"], [p["name"] for p in entry["packages"]]), ("codecs", ["extra"], ["one", "three", "gone"]))
        result = bundles.install_catalog(self.paths, self.ctx, "codecs", runner=self.runner, pool_factory=FakePool)
        self.assertIn("one-1.0-1", result["packages"])
        self.assertEqual(result["skipped"], ["gone"])
        self.assertTrue(repos.load_repos(self.paths)["extra"].enabled, "the entry's repository was enabled")
        (bundle,) = bundles.list_bundles(self.paths)
        self.assertEqual((bundle["name"], bundle["catalog"]), ("codecs", "codecs"))

    def test_a_catalog_bundle_can_be_given_another_name(self):
        self.write("codecs", {"name": "codecs", "repos": [], "packages": ["three"]})
        bundles.install_catalog(self.paths, self.ctx, "codecs", name="mine", runner=self.runner, pool_factory=FakePool)
        self.assertTrue(self.exists("usr/bin/three", "mine"))

    def test_unknown_and_malformed_entries(self):
        with self.assertRaises(NotFound):
            bundles.catalog_entry(self.paths, "nope")
        self.write("bad", {"name": "../bad", "packages": []})
        with self.assertRaises(InvalidArgument):
            bundles.catalog(self.paths)

    def test_the_shipped_codecs_entry_is_well_formed(self):
        shipped = os.path.join(HERE, "..", "data", "catalog.d")
        if not os.path.isdir(shipped):
            self.skipTest("no shipped catalog yet")
        directory = os.path.join(self.paths["root"], bundles.CATALOG_DIR)
        os.makedirs(os.path.dirname(directory), exist_ok=True)
        shutil.copytree(shipped, directory)
        entries = {e["name"]: e for e in bundles.catalog(self.paths)}
        self.assertIn("codecs", entries)
        self.assertEqual(entries["codecs"]["repos"], ["packman-essentials"])
        self.assertTrue(entries["codecs"]["notice"])
        self.assertTrue(all(p.get("repo") == "packman-essentials" for p in entries["codecs"]["packages"]))


class Labelling(BundleCase):
    def test_every_file_is_labelled_when_the_system_has_a_policy(self):
        calls = []
        real = bundles.sellabel
        policy = os.path.join(self.paths["root"], "etc/selinux/targeted/contexts/files")
        os.makedirs(policy)
        with open(os.path.join(policy, "file_contexts"), "w") as handle:
            handle.write("/.*  system_u:object_r:usr_t:s0\n")
        bundles.sellabel = type("Fake", (), {
            "Contexts": real.Contexts,
            "label_tree": staticmethod(lambda tree, contexts: calls.append(sorted(os.listdir(tree))) or 1)})
        self.addCleanup(setattr, bundles, "sellabel", real)
        self.build("media", ["one"])
        self.assertEqual(calls, [["usr"]])

    def test_without_a_policy_nothing_is_labelled(self):
        called = []
        real = bundles.sellabel.label_tree
        bundles.sellabel.label_tree = lambda *a, **k: called.append(1)
        self.addCleanup(setattr, bundles.sellabel, "label_tree", real)
        self.build("media", ["one"])
        self.assertEqual(called, [])


if __name__ == "__main__":
    unittest.main()
