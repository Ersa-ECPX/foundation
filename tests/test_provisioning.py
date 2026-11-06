"""First-boot provisioning: the evidence rules, the one-way latch, and the D-Bus methods."""

import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

from foundationd.backends import provisioning  # noqa: E402
from foundationd.state import StateStore  # noqa: E402
from test_foundationd_bus import CONFIG, MANAGER, NAME, gi  # noqa: E402
from test_foundationd_jobs import JobCase  # noqa: E402

if gi:
    from gi.repository import GLib

ERR = "org.ersa.Foundation1.Error."


class Tree:
    """A fake /etc/passwd, /etc/group and home directories."""

    def __init__(self):
        self.tmp = tempfile.mkdtemp()
        # The account must have a UID the rules accept (1000 or above). A normal user keeps its own;
        # root (as in a package build) uses 1000 and chowns the files it creates.
        self.uid = os.getuid() if os.getuid() >= 1000 else 1000
        self.passwd = os.path.join(self.tmp, "passwd")
        self.group = os.path.join(self.tmp, "group")
        self.home = os.path.join(self.tmp, "home", "ersa")
        os.makedirs(os.path.join(self.home, ".config"))
        self.write(users=[("ersa", self.uid, 1000, self.home)], wheel_members="ersa")

    def write(self, users, wheel_members="", wheel_gid=10):
        with open(self.passwd, "w") as handle:
            handle.write("root:x:0:0:root:/root:/bin/bash\n")
            for name, uid, gid, home in users:
                handle.write(f"{name}:x:{uid}:{gid}:{name}:{home}:/bin/bash\n")
        with open(self.group, "w") as handle:
            handle.write(f"wheel:x:{wheel_gid}:{wheel_members}\n")

    def done(self):
        path = os.path.join(self.home, provisioning.SETUP_DONE)
        open(path, "w").close()
        if os.getuid() != self.uid:
            try:
                os.chown(path, self.uid, -1)
            except OSError:
                raise unittest.SkipTest("cannot create a file owned by an unprivileged uid here") from None
        return path

    def evidence(self):
        return provisioning.evidence(self.passwd, self.group)

    def cleanup(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class Evidence(unittest.TestCase):
    def setUp(self):
        self.tree = Tree()
        self.addCleanup(self.tree.cleanup)

    def test_no_file_no_evidence(self):
        self.assertIsNone(self.tree.evidence())

    def test_an_administrators_done_file_counts(self):
        self.tree.done()
        self.assertEqual(self.tree.evidence(), "ersa")

    def test_a_non_administrator_does_not_count(self):
        self.tree.write(users=[("ersa", self.tree.uid, 1000, self.tree.home)], wheel_members="")
        self.tree.done()
        self.assertIsNone(self.tree.evidence())

    def test_primary_group_membership_counts_as_administrator(self):
        self.tree.write(users=[("ersa", self.tree.uid, 10, self.tree.home)], wheel_members="", wheel_gid=10)
        self.tree.done()
        self.assertEqual(self.tree.evidence(), "ersa")

    def test_system_accounts_do_not_count(self):
        self.tree.write(users=[("daemon", self.tree.uid, 1000, self.tree.home)], wheel_members="daemon")
        # a uid below 1000 is never considered, whatever file exists
        with open(self.tree.passwd, "w") as handle:
            handle.write(f"daemon:x:500:500::{self.tree.home}:/bin/false\n")
        self.tree.done()
        self.assertIsNone(self.tree.evidence())

    def test_root_never_counts_even_with_a_done_file(self):
        with open(self.tree.passwd, "w") as handle:
            handle.write(f"root:x:0:0::{self.tree.home}:/bin/bash\n")
        with open(self.tree.group, "w") as handle:
            handle.write("wheel:x:10:root\n")
        open(os.path.join(self.tree.home, provisioning.SETUP_DONE), "w").close()
        self.assertIsNone(self.tree.evidence())

    def test_a_symlink_is_not_evidence(self):
        target = os.path.join(self.tree.tmp, "elsewhere")
        open(target, "w").close()
        os.symlink(target, os.path.join(self.tree.home, provisioning.SETUP_DONE))
        self.assertIsNone(self.tree.evidence())

    def test_a_file_owned_by_someone_else_is_not_evidence(self):
        self.tree.done()
        # the account in passwd has a different uid from the file's owner
        self.tree.write(users=[("ersa", self.tree.uid + 1, 1000, self.tree.home)], wheel_members="ersa")
        self.assertIsNone(self.tree.evidence())

    def test_a_directory_with_that_name_is_not_evidence(self):
        os.makedirs(os.path.join(self.tree.home, provisioning.SETUP_DONE))
        self.assertIsNone(self.tree.evidence())

    def test_missing_account_files_are_no_evidence_not_a_crash(self):
        self.assertIsNone(provisioning.evidence("/nonexistent/passwd", "/nonexistent/group"))

    def test_malformed_passwd_lines_are_skipped(self):
        self.assertEqual(provisioning.parse_passwd("junk\nx:y:z\na:x:1000:1000:n:/h:/bin/sh\n"),
                         [("a", 1000, 1000, "/h")])

    def test_relative_home_is_ignored(self):
        self.tree.write(users=[("ersa", self.tree.uid, 1000, "relative/home")], wheel_members="ersa")
        self.assertIsNone(self.tree.evidence())


class Latch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = StateStore(os.path.join(self.tmp, "state"))

    def test_starts_unprovisioned_without_a_marker(self):
        self.assertFalse(self.state.provisioned)
        self.assertFalse(os.path.exists(self.state.marker))

    def test_latching_records_time_and_creates_the_marker(self):
        self.assertTrue(self.state.latch_provisioned("2026-11-05T10:00:00Z"))
        self.assertTrue(self.state.provisioned)
        self.assertEqual(self.state.provisioned_at, "2026-11-05T10:00:00Z")
        self.assertTrue(os.path.isfile(self.state.marker))
        with open(self.state.path) as handle:
            self.assertEqual(json.load(handle)["system.provisioned"], True)

    def test_it_is_a_one_way_latch_and_keeps_the_first_time(self):
        self.state.latch_provisioned("2026-11-05T10:00:00Z")
        self.assertFalse(self.state.latch_provisioned("2030-01-01T00:00:00Z"))
        self.assertEqual(self.state.provisioned_at, "2026-11-05T10:00:00Z")

    def test_no_temporary_files_are_left(self):
        self.state.latch_provisioned()
        self.assertEqual(sorted(os.listdir(os.path.join(self.tmp, "state"))), ["provisioned", "state.json"])

    def test_a_corrupt_state_file_is_not_provisioned_and_can_be_latched(self):
        os.makedirs(os.path.dirname(self.state.path))
        with open(self.state.path, "w") as handle:
            handle.write("{broken")
        self.assertFalse(self.state.provisioned)
        self.assertTrue(self.state.latch_provisioned())


@unittest.skipUnless(gi, "needs PyGObject")
class OverDbus(JobCase):
    def setUp(self):
        self.tree = Tree()
        self.addCleanup(self.tree.cleanup)
        self.extra_args = (*JobCase.extra_args, "--passwd-path", self.tree.passwd, "--group-path", self.tree.group)
        super().setUp()

    def status(self):
        return self.call(MANAGER, "GetStatus")[0]

    def marker(self):
        return os.path.join(self.tmp.name, "state", "provisioned")

    def test_a_new_machine_is_not_provisioned_and_has_no_timestamp(self):
        status = self.status()
        self.assertIs(status["provisioned"], False)
        self.assertNotIn("provisioned_at", status)

    def test_check_without_evidence_changes_nothing(self):
        self.assertEqual(self.call(MANAGER, "CheckProvisioning"), (False,))
        self.assertFalse(os.path.exists(self.marker()))

    def test_check_with_evidence_latches_and_survives_a_restart(self):
        self.tree.done()
        self.assertEqual(self.call(MANAGER, "CheckProvisioning"), (True,))
        status = self.status()
        self.assertIs(status["provisioned"], True)
        self.assertRegex(status["provisioned_at"], r"^\d{4}-\d\d-\d\dT")
        self.assertTrue(os.path.isfile(self.marker()))
        self.restart()
        self.assertIs(self.status()["provisioned"], True)

    def test_the_latch_stays_even_if_the_evidence_disappears(self):
        path = self.tree.done()
        self.call(MANAGER, "CheckProvisioning")
        os.unlink(path)
        self.assertEqual(self.call(MANAGER, "CheckProvisioning"), (True,))

    def test_complete_records_it_and_a_second_call_is_refused(self):
        self.call(MANAGER, "CompleteProvisioning")
        self.assertIs(self.status()["provisioned"], True)
        self.assertEqual(self.error_of(MANAGER, "CompleteProvisioning"), ERR + "AlreadyProvisioned")


@unittest.skipUnless(gi, "needs PyGObject")
class WithoutAuthorization(JobCase):
    authorizer = "deny"

    def setUp(self):
        self.tree = Tree()
        self.addCleanup(self.tree.cleanup)
        self.extra_args = (*JobCase.extra_args, "--passwd-path", self.tree.passwd, "--group-path", self.tree.group)
        super().setUp()

    def test_complete_needs_authorization_and_changes_nothing(self):
        self.assertEqual(self.error_of(MANAGER, "CompleteProvisioning"), ERR + "NotAuthorized")
        self.assertIs(self.call(MANAGER, "GetStatus")[0]["provisioned"], False)

    def test_check_needs_none_because_the_caller_supplies_no_evidence(self):
        self.assertEqual(self.call(MANAGER, "CheckProvisioning"), (False,))
        self.tree.done()
        self.assertEqual(self.call(MANAGER, "CheckProvisioning"), (True,))


if __name__ == "__main__":
    unittest.main()
