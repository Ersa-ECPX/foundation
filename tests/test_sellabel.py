"""SELinux labels for an extension's files, from a policy's file_contexts."""

import os
import shutil
import stat
import sys
import tempfile
import unittest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

import support  # noqa: E402,F401
from foundationd.backends import sellabel  # noqa: E402
from foundationd.errors import Failed  # noqa: E402

CONTEXTS = """# a sorted file_contexts: the last matching entry wins
/                          -d  system_u:object_r:root_t:s0
/usr(/.*)?                     system_u:object_r:usr_t:s0
/usr/lib(/.*)?                 system_u:object_r:lib_t:s0
/usr/bin(/.*)?                 system_u:object_r:bin_t:s0
/usr/lib/systemd/system(/.*)?  system_u:object_r:systemd_unit_file_t:s0
/usr/bin/special               --  system_u:object_r:special_exec_t:s0
/usr/share/none(/.*)?          <<none>>
/usr/[[:alpha:]]oops           system_u:object_r:class_t:s0
/usr/broken(                   system_u:object_r:never_t:s0
"""


class Labels(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.file = os.path.join(self.tmp, "file_contexts")
        with open(self.file, "w") as handle:
            handle.write(CONTEXTS)
        self.contexts = sellabel.Contexts([self.file])

    def test_the_most_specific_entry_wins(self):
        regular, directory = stat.S_IFREG | 0o644, stat.S_IFDIR | 0o755
        self.assertEqual(self.contexts.label("/usr/share/x", regular), "system_u:object_r:usr_t:s0")
        self.assertEqual(self.contexts.label("/usr/lib64", directory), "system_u:object_r:usr_t:s0")
        self.assertEqual(self.contexts.label("/usr/lib/libx.so", regular), "system_u:object_r:lib_t:s0")
        self.assertEqual(self.contexts.label("/usr/lib/systemd/system/a.service", regular), "system_u:object_r:systemd_unit_file_t:s0")
        self.assertEqual(self.contexts.label("/usr/bin/special", regular), "system_u:object_r:special_exec_t:s0")
        self.assertEqual(self.contexts.label("/", directory), "system_u:object_r:root_t:s0")

    def test_the_file_type_filter_is_honoured(self):
        # "/usr/bin/special" is for regular files only; a directory of that name falls back to the broader entry.
        self.assertEqual(self.contexts.label("/usr/bin/special", stat.S_IFDIR | 0o755), "system_u:object_r:bin_t:s0")

    def test_none_entries_and_unreadable_expressions_are_skipped(self):
        self.assertEqual(self.contexts.label("/usr/share/none/x", stat.S_IFREG), "system_u:object_r:usr_t:s0")
        self.assertEqual(self.contexts.skipped, 1)          # "/usr/broken(" is not a regular expression
        self.assertIsNone(self.contexts.label("/elsewhere", stat.S_IFREG))

    def test_posix_character_classes_mean_what_they_say(self):
        regular = stat.S_IFREG
        self.assertEqual(self.contexts.label("/usr/xoops", regular), "system_u:object_r:class_t:s0")
        self.assertEqual(self.contexts.label("/usr/9oops", regular), "system_u:object_r:usr_t:s0")   # a digit is not alpha
        self.assertEqual(self.contexts.label("/usr/:oops", regular), "system_u:object_r:usr_t:s0")    # nor a colon

    def test_a_tree_is_labelled_for_the_paths_it_will_have_when_merged(self):
        tree = os.path.join(self.tmp, "tree")
        os.makedirs(os.path.join(tree, "usr/lib/systemd/system"))
        os.makedirs(os.path.join(tree, "usr/bin"))
        for name in ("usr/lib/libx.so", "usr/lib/systemd/system/a.service", "usr/bin/tool"):
            open(os.path.join(tree, name), "w").close()
        os.symlink("libx.so", os.path.join(tree, "usr/lib/libx.so.1"))
        applied = {}
        count = sellabel.label_tree(tree, self.contexts, lambda path, name, value, follow_symlinks=True:
                                    applied.__setitem__(os.path.relpath(path, tree), (name, value.decode(), follow_symlinks)))
        self.assertEqual(count, len(applied))
        self.assertEqual(applied["."], ("security.selinux", "system_u:object_r:root_t:s0", False))
        self.assertEqual(applied["usr"][1], "system_u:object_r:usr_t:s0")          # not the label of /var/lib/extensions
        self.assertEqual(applied["usr/lib"][1], "system_u:object_r:lib_t:s0")
        self.assertEqual(applied["usr/lib/libx.so.1"][1], "system_u:object_r:lib_t:s0")   # the symlink itself
        self.assertEqual(applied["usr/bin/tool"][1], "system_u:object_r:bin_t:s0")
        self.assertEqual(applied["usr/lib/systemd/system/a.service"][1], "system_u:object_r:systemd_unit_file_t:s0")
        self.assertTrue(all(follow is False for _n, _v, follow in applied.values()))

    def test_a_path_the_policy_does_not_know_is_an_error(self):
        tree = os.path.join(self.tmp, "tree")
        os.makedirs(os.path.join(tree, "opt/thing"))
        with self.assertRaises(Failed) as caught:
            sellabel.label_tree(tree, self.contexts, lambda *a, **k: None)
        self.assertIn("/opt", str(caught.exception))

    def test_the_policy_directory_is_found_from_the_selinux_config(self):
        root = os.path.join(self.tmp, "root")
        self.assertIsNone(sellabel.policy_dir(root))
        os.makedirs(os.path.join(root, "etc/selinux/targeted/contexts/files"))
        with open(os.path.join(root, "etc/selinux/targeted/contexts/files/file_contexts"), "w") as handle:
            handle.write(CONTEXTS)
        self.assertTrue(sellabel.policy_dir(root).endswith("targeted/contexts/files"))
        self.assertIsNotNone(sellabel.Contexts.load(root))
        os.makedirs(os.path.join(root, "etc/selinux/mls/contexts/files"))
        with open(os.path.join(root, "etc/selinux/config"), "w") as handle:
            handle.write("SELINUX=enforcing\nSELINUXTYPE=mls\n")
        self.assertIsNone(sellabel.policy_dir(root))        # mls is named but has no file_contexts


if __name__ == "__main__":
    unittest.main()
