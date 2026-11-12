"""The check an update's signature has to pass: only a good signature by a currently valid trusted key counts."""

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

from foundationd.backends import repos  # noqa: E402
from test_repos import GPG, _gpg_key, _sign  # noqa: E402

MANIFEST = b'{"format": 1, "name": "ecp", "version": "0.3.0"}\n'


@unittest.skipUnless(GPG, "needs gpg")
class Signing(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.keys = {}
        for name in ("one", "two", "other"):
            home = os.path.join(cls.tmp, name)
            cls.keys[name] = (home, _gpg_key(home, name))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def sign(self, who, data=MANIFEST):
        home = self.keys[who][0]
        path = os.path.join(self.tmp, "manifest.json")
        with open(path, "wb") as handle:
            handle.write(data)
        _sign(home, who, path, path + ".sig")
        with open(path + ".sig", "rb") as handle:
            return handle.read()

    def trusted(self, *names):
        return [self.keys[n][1] for n in names]

    def test_a_good_signature_by_a_trusted_key(self):
        self.assertTrue(repos.verify_detached(MANIFEST, self.sign("one"), self.trusted("one")))

    def test_a_manifest_changed_after_signing(self):
        signature = self.sign("one")
        self.assertFalse(repos.verify_detached(MANIFEST.replace(b"0.3.0", b"9.9.9"), signature, self.trusted("one")))

    def test_a_signature_by_a_key_that_is_not_trusted(self):
        self.assertFalse(repos.verify_detached(MANIFEST, self.sign("other"), self.trusted("one", "two")))

    def test_garbage_instead_of_a_signature(self):
        self.assertFalse(repos.verify_detached(MANIFEST, b"not a signature", self.trusted("one")))
        self.assertFalse(repos.verify_detached(MANIFEST, b"", self.trusted("one")))

    def test_either_of_two_trusted_keys_is_enough_for_a_key_change(self):
        both = self.trusted("one", "two")
        self.assertTrue(repos.verify_detached(MANIFEST, self.sign("one"), both))
        self.assertTrue(repos.verify_detached(MANIFEST, self.sign("two"), both))

    def test_a_key_that_expired(self):
        home = os.path.join(self.tmp, "shortlived")
        os.makedirs(home, mode=0o700)
        base = ["gpg", "--batch", "--no-tty", "--homedir", home, "--passphrase", ""]
        subprocess.run(base + ["--quick-generate-key", "short <short@example.org>", "rsa2048", "sign", "seconds=2"],
                       check=True, capture_output=True)
        public = os.path.join(home, "public.asc")
        with open(public, "wb") as handle:
            handle.write(subprocess.run(base + ["--armor", "--export", "short@example.org"], check=True, capture_output=True).stdout)
        path = os.path.join(self.tmp, "m2")
        with open(path, "wb") as handle:
            handle.write(MANIFEST)
        _sign(home, "short", path, path + ".sig")
        with open(path + ".sig", "rb") as handle:
            signature = handle.read()
        self.assertTrue(repos.verify_detached(MANIFEST, signature, [public]))
        time.sleep(3)
        self.assertFalse(repos.verify_detached(MANIFEST, signature, [public]),
                         "a signature by a key that has since expired must not be accepted")

    def test_a_key_that_was_revoked(self):
        home = os.path.join(self.tmp, "revoked")
        public = _gpg_key(home, "revoked")
        path = os.path.join(self.tmp, "m3")
        with open(path, "wb") as handle:
            handle.write(MANIFEST)
        _sign(home, "revoked", path, path + ".sig")
        with open(path + ".sig", "rb") as handle:
            signature = handle.read()
        # gpg writes a revocation certificate next to every key it makes; its first line is commented out on purpose
        directory = os.path.join(home, "openpgp-revocs.d")
        certificate = os.path.join(directory, os.listdir(directory)[0])
        with open(certificate, encoding="ascii") as handle:
            revocation = "".join(line[1:] if line.startswith(":-----") else line for line in handle).encode()
        revoked_public = os.path.join(home, "revoked.asc")
        with open(public, "rb") as handle:
            key = handle.read()
        with open(revoked_public, "wb") as handle:
            handle.write(key + b"\n" + revocation)
        self.assertTrue(repos.verify_detached(MANIFEST, signature, [public]))
        self.assertFalse(repos.verify_detached(MANIFEST, signature, [revoked_public]))


if __name__ == "__main__":
    unittest.main()
