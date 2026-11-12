"""Remote access: SSH keys, the server settings, and turning the server on and off (with a fake systemd and firewall)."""

import base64
import hashlib
import os
import shutil
import struct
import sys
import tempfile
import unittest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

import support  # noqa: E402
from foundationd.backends import remote  # noqa: E402
from foundationd.errors import Failed, InvalidArgument, NotFound, Unavailable  # noqa: E402


def make_key(seed=b"a", comment="me@laptop", kind="ssh-ed25519"):
    blob = struct.pack(">I", len(kind)) + kind.encode() + struct.pack(">I", 32) + hashlib.sha256(seed).digest()
    text = f"{kind} {base64.b64encode(blob).decode()}" + (f" {comment}" if comment else "")
    fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    return text, fingerprint


class FakeSystem:
    def __init__(self, firewall=False, active=False, fail_start=False):
        self.state = "inactive" if not active else "active"
        self.enabled = False
        self.firewall = firewall
        self.fail_start = fail_start
        self.calls = []

    def unit_file_state(self, unit):
        return "enabled" if self.enabled else "disabled"

    def active_state(self, unit):
        return self.state

    def enable(self, unit):
        self.enabled = True
        self.calls.append("enable")

    def disable(self, unit):
        self.enabled = False
        self.calls.append("disable")

    def start(self, unit, restart=False):
        self.calls.append("restart" if restart else "start")
        self.state = "failed" if self.fail_start else "active"

    def stop(self, unit):
        self.calls.append("stop")
        self.state = "inactive"

    def firewall_allows(self, service):
        return self.firewall

    def firewall_allow(self, service):
        self.calls.append("firewall")
        self.firewall = True


class Case(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        uid, gid = os.getuid(), os.getgid()
        os.makedirs(os.path.join(self.root, "etc"))
        os.makedirs(os.path.join(self.root, "home/anna"))
        os.makedirs(os.path.join(self.root, "home/ben"))
        with open(os.path.join(self.root, "etc/passwd"), "w") as handle:
            handle.write("root:x:0:0::/root:/bin/bash\n"
                         f"anna:x:1000:{gid}::/home/anna:/bin/bash\n"
                         f"ben:x:1001:{gid}::/home/ben:/bin/bash\n"
                         "svc:x:1002:1002::/var/svc:/usr/sbin/nologin\n")
        self.paths = {"root": self.root, "passwd": os.path.join(self.root, "etc/passwd"), "bus": "session"}
        self.ctx = support.Recorder()

    def install_server(self):
        path = os.path.join(self.root, remote.UNIT_FILE)
        os.makedirs(os.path.dirname(path))
        open(path, "w").close()

    def dropin(self):
        with open(os.path.join(self.root, remote.DROPIN)) as handle:
            return handle.read()

    def keyfile(self, user):
        return os.path.join(self.root, "home", user, ".ssh/authorized_keys")


class Keys(Case):
    def test_only_real_login_accounts_are_users(self):
        self.assertEqual(sorted(remote.login_users(self.paths)), ["anna", "ben"])

    def test_a_key_is_added_listed_and_removed_by_fingerprint(self):
        text, fingerprint = make_key()
        self.assertEqual(remote.add_key(self.paths, "anna", text), {"user": "anna", "fingerprint": fingerprint, "added": True})
        self.assertEqual(oct(os.stat(self.keyfile("anna")).st_mode & 0o777), "0o600")
        self.assertEqual(oct(os.stat(os.path.dirname(self.keyfile("anna"))).st_mode & 0o777), "0o700")
        listed = remote.list_keys(self.paths)
        self.assertEqual([(k["user"], k["type"], k["fingerprint"], k["comment"]) for k in listed],
                         [("anna", "ssh-ed25519", fingerprint, "me@laptop")])
        self.assertEqual(remote.remove_key(self.paths, "anna", fingerprint)["removed"], 1)
        self.assertEqual(remote.list_keys(self.paths), [])

    def test_the_same_key_twice_is_added_once(self):
        text, _ = make_key()
        remote.add_key(self.paths, "anna", text)
        self.assertFalse(remote.add_key(self.paths, "anna", text.replace("me@laptop", "other"))["added"])
        with open(self.keyfile("anna")) as handle:
            self.assertEqual(len(handle.read().splitlines()), 1)

    def test_other_keys_and_lines_are_kept_when_one_is_removed(self):
        one, _ = make_key(b"1", "one")
        two, finger_two = make_key(b"2", "two")
        os.makedirs(os.path.dirname(self.keyfile("anna")))
        with open(self.keyfile("anna"), "w") as handle:
            handle.write("# my notes\n" + one + "\n")
        remote.add_key(self.paths, "anna", two)
        remote.remove_key(self.paths, "anna", finger_two)
        with open(self.keyfile("anna")) as handle:
            self.assertEqual(handle.read(), "# my notes\n" + one + "\n")

    def test_keys_with_options_are_refused_but_listed_when_already_there(self):
        text, _ = make_key()
        with self.assertRaises(InvalidArgument):
            remote.add_key(self.paths, "anna", 'command="rm -rf /" ' + text)
        os.makedirs(os.path.dirname(self.keyfile("anna")))
        with open(self.keyfile("anna"), "w") as handle:
            handle.write('from="10.0.0.1" ' + text + "\n")
        (listed,) = remote.list_keys(self.paths, "anna")
        self.assertEqual(listed["options"], 'from="10.0.0.1"')

    def test_garbage_and_several_lines_are_refused(self):
        for bad in ("hello", "ssh-ed25519 not-base64!", make_key()[0] + "\n" + make_key(b"z")[0], "", "ssh-ed25519"):
            with self.assertRaises(InvalidArgument, msg=bad):
                remote.check_new_key(bad)

    def test_an_unknown_user_or_fingerprint_is_not_found(self):
        text, fingerprint = make_key()
        with self.assertRaises(NotFound):
            remote.add_key(self.paths, "svc", text)
        remote.add_key(self.paths, "anna", text)
        with self.assertRaises(NotFound):
            remote.remove_key(self.paths, "anna", make_key(b"q")[1])
        with self.assertRaises(InvalidArgument):
            remote.remove_key(self.paths, "anna", "SHA256:short")

    def test_a_linked_ssh_directory_is_not_followed(self):
        outside = os.path.join(self.root, "elsewhere")
        os.makedirs(outside)
        os.symlink(outside, os.path.join(self.root, "home/anna/.ssh"))
        with self.assertRaises(Failed):
            remote.add_key(self.paths, "anna", make_key()[0])
        self.assertEqual(os.listdir(outside), [])

    def test_a_linked_authorized_keys_file_is_not_followed(self):
        os.makedirs(os.path.dirname(self.keyfile("anna")))
        target = os.path.join(self.root, "target")
        open(target, "w").close()
        os.symlink(target, self.keyfile("anna"))
        with self.assertRaises(Failed):
            remote.list_keys(self.paths, "anna")


class Server(Case):
    def setUp(self):
        super().setUp()
        self.install_server()
        self.system = FakeSystem()

    def ok(self, cmd, timeout=60, input_bytes=None):
        self.checked = getattr(self, "checked", []) + [cmd]
        return 0, "", ""

    def turn_on(self, allow_password=False, runner=None, system=None):
        return remote.set_ssh(self.paths, self.ctx, True, allow_password, system or self.system, runner or self.ok)

    def test_it_refuses_to_start_when_nobody_could_log_in(self):
        with self.assertRaises(InvalidArgument) as caught:
            self.turn_on()
        self.assertIn("nobody could log in", str(caught.exception))
        self.assertEqual(self.system.calls, [])
        self.assertFalse(os.path.exists(os.path.join(self.root, remote.DROPIN)))

    def test_with_a_key_it_writes_key_only_settings_starts_and_opens_the_firewall(self):
        remote.add_key(self.paths, "anna", make_key()[0])
        result = self.turn_on()
        self.assertEqual(result, {"enabled": True, "active": True, "password_auth": False, "firewall": "opened"})
        self.assertEqual(self.system.calls, ["enable", "start", "firewall"])
        with open(os.path.join(self.root, remote.DROPIN)) as handle:
            text = handle.read()
        self.assertIn("PasswordAuthentication no", text)
        self.assertIn("PermitRootLogin no", text)
        keygen, check = self.checked
        self.assertEqual(keygen[:2], ["ssh-keygen", "-q"])
        self.assertEqual(check[:2], ["sshd", "-t"])
        self.assertEqual(check[2], "-h")          # the check uses a throwaway key, so it works before any host key exists

    def test_passwords_can_be_allowed_without_keys_and_an_open_firewall_is_left_alone(self):
        result = self.turn_on(allow_password=True, system=FakeSystem(firewall=True))
        self.assertEqual(result["firewall"], "open")
        self.assertTrue(remote.read_password_auth(self.paths))

    def test_a_running_server_is_restarted_to_pick_up_new_settings(self):
        self.turn_on(allow_password=True, system=(system := FakeSystem(firewall=True, active=True)))
        self.assertIn("restart", system.calls)

    def test_settings_the_server_rejects_are_rolled_back_and_nothing_is_started(self):
        remote.add_key(self.paths, "anna", make_key()[0])
        self.turn_on()
        before = self.dropin()
        system = FakeSystem()
        with self.assertRaises(Failed) as caught:
            self.turn_on(allow_password=True, system=system,
                         runner=lambda cmd, timeout=60, input_bytes=None: (255, "", "bad option on line 3"))
        self.assertIn("bad option", str(caught.exception))
        self.assertEqual(self.dropin(), before)
        self.assertEqual(system.calls, [])

    def test_a_computer_without_host_keys_is_not_a_settings_error(self):
        remote.add_key(self.paths, "anna", make_key()[0])

        def runner(cmd, timeout=60, input_bytes=None):
            if cmd[0] == "ssh-keygen":
                return 127, "", "ssh-keygen: not found"        # not even a throwaway key can be made
            return 255, "", "sshd: no hostkeys available -- exiting."
        self.assertEqual(self.turn_on(runner=runner)["active"], True)

    def test_a_real_settings_error_is_still_reported_with_a_throwaway_key(self):
        remote.add_key(self.paths, "anna", make_key()[0])
        seen = []

        def runner(cmd, timeout=60, input_bytes=None):
            seen.append(cmd)
            return (0, "", "") if cmd[0] == "ssh-keygen" else (255, "", "line 3: Bad configuration option")
        with self.assertRaises(Failed) as caught:
            self.turn_on(runner=runner)
        self.assertIn("Bad configuration option", str(caught.exception))
        self.assertIn("-h", seen[1])

    def test_a_server_that_does_not_start_is_reported(self):
        remote.add_key(self.paths, "anna", make_key()[0])
        with self.assertRaises(Failed) as caught:
            self.turn_on(system=FakeSystem(fail_start=True))
        self.assertIn("did not start", str(caught.exception))

    def test_a_firewall_that_cannot_be_changed_is_reported_not_fatal(self):
        remote.add_key(self.paths, "anna", make_key()[0])

        class Stubborn(FakeSystem):
            def firewall_allow(self, service):
                raise Failed("denied")
        self.assertEqual(self.turn_on(system=Stubborn())["firewall"], "closed")

    def test_without_the_server_installed_it_says_so(self):
        os.unlink(os.path.join(self.root, remote.UNIT_FILE))
        with self.assertRaises(Unavailable):
            self.turn_on(allow_password=True)
        self.assertFalse(remote.status(self.paths, self.system)["installed"])

    def test_turning_it_off_stops_and_disables_it(self):
        system = FakeSystem(active=True)
        system.enabled = True
        self.assertEqual(remote.set_ssh(self.paths, self.ctx, False, False, system, self.ok), {"enabled": False, "active": False})
        self.assertEqual(system.calls, ["stop", "disable"])

    def test_status_reports_what_is_true(self):
        remote.add_key(self.paths, "anna", make_key()[0])
        self.turn_on()
        status = remote.status(self.paths, self.system)
        self.assertEqual(status, {"installed": True, "enabled": True, "active": True, "keys": 1, "users_with_keys": ["anna"],
                                  "password_auth": False, "firewall": "open"})


if __name__ == "__main__":
    unittest.main()
