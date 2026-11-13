"""dm-verity naming: UUIDs from a root hash, the root hash on a command line, reading a kernel image's command line."""

import os
import sys
import tempfile
import unittest
import uuid

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

import support  # noqa: E402
from foundationd.backends import verity  # noqa: E402
from foundationd.errors import Failed  # noqa: E402

HASH = "00112233445566778899aabbccddeeff" + "0123456789abcdef0123456789abcdef"


class Naming(unittest.TestCase):
    def test_the_two_halves_of_the_hash_name_the_two_partitions(self):
        data, hashes = verity.uuids(HASH)
        self.assertEqual(data, uuid.UUID("00112233-4455-6677-8899-aabbccddeeff"))
        self.assertEqual(hashes, uuid.UUID("01234567-89ab-cdef-0123-456789abcdef"))

    def test_uppercase_hex_gives_the_same_names(self):
        self.assertEqual(verity.uuids(HASH.upper()), verity.uuids(HASH))

    def test_a_malformed_hash_is_refused(self):
        for bad in ("", "xyz", HASH[:-1], HASH + "0", "g" * 64):
            with self.assertRaises(Failed):
                verity.uuids(bad)

    def test_the_root_hash_on_a_command_line(self):
        self.assertEqual(verity.roothash_of(f"ro quiet roothash={HASH} splash"), HASH)
        self.assertEqual(verity.roothash_of(f"roothash={HASH.upper()}"), HASH)
        self.assertEqual(verity.roothash_of("ro quiet root=PARTLABEL=x"), "")
        self.assertEqual(verity.roothash_of(f"ro myroothash={HASH}"), "", "only the roothash= token counts")
        self.assertEqual(verity.roothash_of(f"ro roothash={HASH[:-2]}"), "", "a short hash is not one")


class Reading(unittest.TestCase):
    def test_a_kernel_images_command_line_is_read_from_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "k.efi")
            text = f"ro roothash={HASH}"
            with open(path, "wb") as handle:
                handle.write(support.build_pe(text.encode(), raw_size=4096))
            self.assertEqual(verity.cmdline_of_file(path), text)

    def test_a_file_that_is_not_a_kernel_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "x")
            with open(path, "wb") as handle:
                handle.write(b"not a pe" * 100)
            with self.assertRaises(Failed):
                verity.cmdline_of_file(path)
            path2 = os.path.join(tmp, "y.efi")
            with open(path2, "wb") as handle:
                handle.write(support.build_pe(b"", with_cmdline=False))
            with self.assertRaises(Failed):
                verity.cmdline_of_file(path2)


if __name__ == "__main__":
    unittest.main()
