"""Package repositories: definitions, signed metadata, checksums, signatures, downloads. libsolv is not needed here."""

import gzip
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

import support  # noqa: E402,F401
from foundationd.backends import repos  # noqa: E402
from foundationd.errors import Failed, InvalidArgument, NotFound, Unavailable  # noqa: E402

GPG = shutil.which("gpg")


def _gpg_key(home, name):
    """A throwaway signing key; returns the path of its armored public key."""
    os.makedirs(home, mode=0o700, exist_ok=True)
    base = ["gpg", "--batch", "--no-tty", "--homedir", home]
    subprocess.run(base + ["--passphrase", "", "--quick-generate-key", f"{name} <{name}@example.org>", "rsa2048", "sign", "never"],
                   check=True, capture_output=True)
    out = subprocess.run(base + ["--armor", "--export", f"{name}@example.org"], check=True, capture_output=True).stdout
    path = os.path.join(home, "public.asc")
    with open(path, "wb") as handle:
        handle.write(out)
    return path


def _sign(home, key_name, data_path, signature_path):
    subprocess.run(["gpg", "--batch", "--no-tty", "--homedir", home, "--yes", "--armor", "--detach-sign", "-u",
                    f"{key_name}@example.org", "-o", signature_path, data_path], check=True, capture_output=True)


@unittest.skipUnless(GPG, "needs gpg")
class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys = tempfile.mkdtemp()
        cls.good_home, cls.other_home = os.path.join(cls.keys, "good"), os.path.join(cls.keys, "other")
        cls.good_key = _gpg_key(cls.good_home, "good")
        cls.other_key = _gpg_key(cls.other_home, "other")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.keys, True)

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.paths = {"state": os.path.join(self.tmp, "state"), "root": os.path.join(self.tmp, "root")}
        self.web = os.path.join(self.tmp, "web")
        os.makedirs(os.path.join(self.web, "repodata"))

    def publish(self, signer="good", primary=b"<metadata/>", href="repodata/primary.xml.gz", tamper=False, sign=True):
        """Write a repository into self.web; returns the Repo."""
        body = gzip.compress(primary)
        listed = hashlib.sha256(body).hexdigest()
        if tamper:
            body = gzip.compress(primary + b"<!-- changed -->")
        os.makedirs(os.path.dirname(os.path.join(self.web, href)), exist_ok=True)
        with open(os.path.join(self.web, href), "wb") as handle:
            handle.write(body)
        repomd = os.path.join(self.web, "repodata/repomd.xml")
        with open(repomd, "w") as handle:
            handle.write('<repomd xmlns="http://linux.duke.edu/metadata/repo"><revision>7</revision>'
                         f'<data type="primary"><checksum type="sha256">{listed}</checksum><location href="{href}"/></data></repomd>')
        signature = repomd + ".asc"
        if sign:
            home = self.good_home if signer == "good" else self.other_home
            _sign(home, signer, repomd, signature)
        elif os.path.exists(signature):
            os.unlink(signature)
        return repos.Repo(alias="test", name="Test", baseurl="file://" + self.web + "/", keys=[self.good_key],
                          gpgcheck=True, repo_gpgcheck=True)


class Definitions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write(self, text, name="a.repo"):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as handle:
            handle.write(text)
        return path

    GOOD = """[alpha]
name=Alpha
enabled=1
baseurl=https://example.org/repo/
gpgkey=file:///usr/share/foundation/keys/alpha.asc
priority=90
"""

    def test_a_repository_file_is_read(self):
        (repo,) = repos.parse_repo_file(self.write(self.GOOD))
        self.assertEqual((repo.alias, repo.name, repo.priority, repo.enabled), ("alpha", "Alpha", 90, True))
        self.assertEqual(repo.baseurl, "https://example.org/repo/")
        self.assertEqual(repo.keys, ["/usr/share/foundation/keys/alpha.asc"])
        self.assertTrue(repo.gpgcheck and repo.repo_gpgcheck)

    def test_keys_are_found_under_a_different_root(self):
        (repo,) = repos.parse_repo_file(self.write(self.GOOD), root="/tmp/rootfs")
        self.assertEqual(repo.keys, ["/tmp/rootfs/usr/share/foundation/keys/alpha.asc"])

    def test_unsafe_definitions_are_refused(self):
        for text, why in ((self.GOOD.replace("https://", "http://"), "only https"),
                          (self.GOOD + "gpgcheck=0\n", "cannot be switched off"),
                          (self.GOOD + "repo_gpgcheck=0\n", "cannot be switched off"),
                          (self.GOOD.replace("gpgkey=file://", "gpgkey=https://example.org"), "must be a file"),
                          (self.GOOD.replace("gpgkey=file:///usr/share/foundation/keys/alpha.asc\n", ""), "no gpgkey"),
                          (self.GOOD.replace("[alpha]", "[-bad]"), "alias"),
                          (self.GOOD.replace("priority=90", "priority=high"), "priority"),
                          (self.GOOD.replace("baseurl=https://example.org/repo/\n", ""), "no baseurl")):
            with self.subTest(why):
                with self.assertRaises(InvalidArgument) as caught:
                    repos.parse_repo_file(self.write(text))
                self.assertIn(why, str(caught.exception))

    def test_a_local_file_repository_may_skip_signatures(self):
        text = "[dev]\nbaseurl=file:///srv/repo/\ngpgcheck=0\nrepo_gpgcheck=0\n"
        (repo,) = repos.parse_repo_file(self.write(text))
        self.assertFalse(repo.gpgcheck)

    def test_a_local_file_replaces_a_shipped_one_and_can_enable_it(self):
        root = os.path.join(self.tmp, "root")
        vendor = os.path.join(root, repos.VENDOR_DIR)
        os.makedirs(vendor)
        os.makedirs(os.path.join(root, "usr/share/foundation/keys"))
        with open(os.path.join(vendor, "a.repo"), "w") as handle:
            handle.write(self.GOOD.replace("enabled=1", "enabled=0"))
        paths = {"root": root, "state": os.path.join(self.tmp, "state")}
        self.assertFalse(repos.load_repos(paths)["alpha"].enabled)
        self.assertTrue(repos.load_repos(paths)["alpha"].vendor)
        repos.set_enabled(paths, "alpha", True)
        again = repos.load_repos(paths)["alpha"]
        self.assertTrue(again.enabled)
        self.assertFalse(again.vendor)
        self.assertEqual(again.keys, [os.path.join(root, "usr/share/foundation/keys/alpha.asc")])
        repos.set_enabled(paths, "alpha", False)
        self.assertFalse(repos.load_repos(paths)["alpha"].enabled)
        with self.assertRaises(NotFound):
            repos.set_enabled(paths, "nope", True)


class Metadata(Base):
    def test_signed_metadata_is_fetched_verified_and_cached(self):
        repo = self.publish()
        result = repos.refresh(repo, self.paths)
        self.assertEqual((result["changed"], result["revision"]), (True, "7"))
        self.assertTrue(repos.cached_primary(self.paths, "test").endswith("primary.xml.gz"))
        self.assertIsNotNone(repos.metadata_age(self.paths, "test"))
        self.assertFalse(repos.refresh(repo, self.paths)["changed"])
        self.assertTrue(repos.refresh(repo, self.paths, force=True)["changed"])

    def test_metadata_signed_by_another_key_is_refused(self):
        repo = self.publish(signer="other")
        with self.assertRaises(Failed) as caught:
            repos.refresh(repo, self.paths)
        self.assertIn("not signed", str(caught.exception))
        self.assertIsNone(repos.cached_primary(self.paths, "test"))

    def test_unsigned_metadata_is_refused(self):
        repo = self.publish(sign=False)
        with self.assertRaises(Failed):
            repos.refresh(repo, self.paths)

    def test_a_package_list_that_does_not_match_the_signed_checksum_is_refused(self):
        repo = self.publish(tamper=True)
        with self.assertRaises(Failed) as caught:
            repos.refresh(repo, self.paths)
        self.assertIn("checksum", str(caught.exception))
        self.assertIsNone(repos.cached_primary(self.paths, "test"))

    def test_an_unsafe_location_in_repomd_is_refused(self):
        repo = self.publish()
        path = os.path.join(self.web, "repodata/repomd.xml")
        with open(path) as handle:
            text = handle.read().replace("repodata/primary.xml.gz", "../outside.xml.gz")
        with open(path, "w") as handle:
            handle.write(text)
        _sign(self.good_home, "good", path, path + ".asc")
        with self.assertRaises(Failed) as caught:
            repos.refresh(repo, self.paths)
        self.assertIn("unsafe", str(caught.exception))

    def test_only_https_and_file_urls_are_fetched(self):
        with self.assertRaises(Failed):
            repos.fetch("http://example.org/x", 10)
        with self.assertRaises(Failed):
            repos.fetch("ftp://example.org/x", 10)

    def test_a_download_larger_than_the_limit_is_refused(self):
        path = os.path.join(self.tmp, "big")
        with open(path, "wb") as handle:
            handle.write(b"x" * 100)
        with self.assertRaises(Failed):
            repos.fetch("file://" + path, 10)
        with self.assertRaises(Failed):
            repos.fetch_file("file://" + path, os.path.join(self.tmp, "out"), 10)

    def test_a_changed_package_list_replaces_the_cached_one(self):
        repo = self.publish(primary=b"<metadata a='1'/>")
        repos.refresh(repo, self.paths)
        repo = self.publish(primary=b"<metadata a='2'/>")
        self.assertTrue(repos.refresh(repo, self.paths)["changed"])
        with gzip.open(repos.cached_primary(self.paths, "test")) as handle:
            self.assertIn(b"a='2'", handle.read())


class Downloads(Base):
    def package(self, repo_dir, name="pkg-1-1.x86_64.rpm", body=b"payload", **extra):
        os.makedirs(os.path.join(repo_dir, "x86_64"), exist_ok=True)
        with open(os.path.join(repo_dir, "x86_64", name), "wb") as handle:
            handle.write(body)
        return {"name": "pkg", "evr": "1-1", "arch": "x86_64", "repo": "test", "location": f"x86_64/{name}",
                "checksum": ["sha256", hashlib.sha256(body).hexdigest()], "size": len(body), **extra}

    def repo(self):
        return repos.Repo(alias="test", name="Test", baseurl="file://" + self.web + "/", keys=[self.good_key])

    def runner(self, signed=True):
        def run(cmd, timeout=60, input_bytes=None):
            self.calls.append(list(cmd))
            if "--checksig" in cmd:
                return (0, f"{cmd[-1]}: digests signatures OK\n", "") if signed else (1, f"{cmd[-1]}: digests SIGNATURES NOT OK\n", "")
            return 0, "", ""
        return run

    def setUp(self):
        super().setUp()
        self.calls = []
        self.dest = os.path.join(self.tmp, "rpms")

    def test_a_package_is_downloaded_checked_and_its_signature_verified(self):
        got = repos.download([self.package(self.web)], [self.repo()], self.dest, self.runner())
        with open(got[0]["file"], "rb") as handle:
            self.assertEqual(handle.read(), b"payload")
        self.assertTrue(any("--import" in c for c in self.calls) and any("--checksig" in c for c in self.calls))

    def test_a_checksum_mismatch_is_refused_and_leaves_nothing_behind(self):
        package = self.package(self.web)
        package["checksum"] = ["sha256", "0" * 64]
        with self.assertRaises(Failed):
            repos.download([package], [self.repo()], self.dest, self.runner())
        self.assertEqual(os.listdir(self.dest), [])

    def test_a_bad_signature_is_refused(self):
        with self.assertRaises(Failed) as caught:
            repos.download([self.package(self.web)], [self.repo()], self.dest, self.runner(signed=False))
        self.assertIn("signature", str(caught.exception))

    def test_a_missing_key_counts_as_a_bad_signature(self):
        def run(cmd, timeout=60, input_bytes=None):
            return (0, f"{cmd[-1]}: RSA sha256 (MISSING KEYS) OK\n", "") if "--checksig" in cmd else (0, "", "")
        with self.assertRaises(Failed):
            repos.download([self.package(self.web)], [self.repo()], self.dest, run)

    def test_a_file_that_is_already_there_is_reused(self):
        package = self.package(self.web)
        repos.download([package], [self.repo()], self.dest, self.runner())
        fetched = []
        real = repos.fetch_file
        repos.fetch_file = lambda *a, **k: fetched.append(a) or real(*a, **k)
        self.addCleanup(setattr, repos, "fetch_file", real)
        repos.download([package], [self.repo()], self.dest, self.runner(), file_fetcher=repos.fetch_file)
        self.assertEqual(fetched, [])

    def test_unsafe_file_names_and_unknown_repositories_are_refused(self):
        package = self.package(self.web)
        with self.assertRaises(Failed):
            repos.download([{**package, "location": "x86_64/../../evil.rpm"}], [self.repo()], self.dest, self.runner())
        with self.assertRaises(Failed):
            repos.download([{**package, "location": "x86_64/not-an-rpm.txt"}], [self.repo()], self.dest, self.runner())
        with self.assertRaises(Failed):
            repos.download([{**package, "repo": "elsewhere"}], [self.repo()], self.dest, self.runner())

    def test_forget_removes_only_files_that_are_no_longer_wanted(self):
        os.makedirs(self.dest)
        for name in ("keep.rpm", "drop.rpm"):
            open(os.path.join(self.dest, name), "w").close()
        repos.forget(self.dest, [os.path.join(self.dest, "keep.rpm")])
        self.assertEqual(os.listdir(self.dest), ["keep.rpm"])


@unittest.skipUnless(GPG and shutil.which("rpmkeys"), "needs gpg and rpmkeys")
class RealSignatures(Base):
    def test_verify_rpms_rejects_an_unsigned_file(self):
        """rpmkeys itself, not a stub: an RPM with no signature must not pass."""
        top = os.path.join(self.tmp, "top")
        spec = os.path.join(self.tmp, "p.spec")
        with open(spec, "w") as handle:
            handle.write("Name: p\nVersion: 1\nRelease: 1\nSummary: s\nLicense: MIT\nBuildArch: noarch\n%description\nx\n%files\n")
        done = subprocess.run(["rpmbuild", "--define", f"_topdir {top}", "--define", f"_dbpath {self.tmp}/db", "-bb", spec],
                              capture_output=True)
        if done.returncode != 0 or not shutil.which("rpmbuild"):
            self.skipTest("cannot build a sample RPM")
        rpm = [os.path.join(d, f) for d, _s, fs in os.walk(top) for f in fs if f.endswith(".rpm")][0]
        with self.assertRaises(Failed):
            repos.verify_rpms([rpm], [self.good_key])


class NoSolver(unittest.TestCase):
    def test_a_missing_solver_is_reported_as_unavailable(self):
        saved = sys.modules.get("solv", "absent")
        sys.modules["solv"] = None           # makes "import solv" raise ImportError
        try:
            with self.assertRaises(Unavailable):
                repos.Pool([], {"root": "/", "state": "/nonexistent"})
        finally:
            if saved == "absent":
                del sys.modules["solv"]
            else:
                sys.modules["solv"] = saved


if __name__ == "__main__":
    unittest.main()
