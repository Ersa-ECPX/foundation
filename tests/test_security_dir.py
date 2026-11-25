"""scripts/security-dir: test keys by default, your own when named or present."""

import os
import subprocess
import tempfile
import unittest

import support  # noqa: F401

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SCRIPT = os.path.join(ROOT, "scripts", "security-dir")


@unittest.skipUnless(os.path.isfile(SCRIPT), "needs the ecp-mkosi repository")
class SecurityDir(unittest.TestCase):
    def run_script(self, *args, **env):
        base = {k: v for k, v in os.environ.items() if k != "ECP_SECURITY_DIR"}
        return subprocess.run([SCRIPT, *args], capture_output=True, text=True, env=dict(base, **env))

    def test_the_default_is_the_public_test_keys(self):
        if os.path.isdir(os.path.join(ROOT, "build", "security", "release")):
            self.skipTest("a release directory exists on this machine")
        self.assertEqual(self.run_script().stdout.strip(), os.path.join(ROOT, "build", "security", "testkeys"))
        self.assertEqual(self.run_script("--kind").stdout.strip(), "test")

    def test_a_named_directory_is_a_release_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_script(ECP_SECURITY_DIR=tmp)
            self.assertEqual(result.stdout.strip(), os.path.realpath(tmp))
            self.assertEqual(self.run_script("--kind", ECP_SECURITY_DIR=tmp).stdout.strip(), "release")

    def test_a_named_directory_that_does_not_exist_is_refused(self):
        self.assertNotEqual(self.run_script(ECP_SECURITY_DIR="/nonexistent-keys").returncode, 0)

    def test_the_test_keys_are_marked_and_complete(self):
        keys = os.path.join(ROOT, "build", "security", "testkeys")
        for name in ("TEST-KEYS", "secure-boot/ersa-secure-boot.key", "secure-boot/ersa-secure-boot.crt", "image-update/secret-key.asc",
                     "image-update/passphrase.txt"):
            self.assertTrue(os.path.isfile(os.path.join(keys, name)), name)
        self.assertTrue([n for n in os.listdir(os.path.join(keys, "image-update")) if n.startswith("ersa-image-")])


if __name__ == "__main__":
    unittest.main()
