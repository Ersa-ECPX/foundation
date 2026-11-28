"""The device backend: the key (file or sealed to the TPM), the link challenge, the report, the signed check-in, and the unprivileged
sender (no real account service; a local server and fakes stand in for it)."""

import base64
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey  # noqa: E402

from foundationd.backends import devices  # noqa: E402
from foundationd.errors import Failed, InvalidArgument, Unavailable  # noqa: E402

CHALLENGE = "1900000000.AbC-123_xyz.mac-value_01"


def unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def fake_creds(calls=None, fail_encrypt=False):
    """A runner that stands in for systemd-creds: "sealing" is a reversible transform, so a test can tell sealed from plain."""
    def runner(cmd, timeout=60, input_bytes=None, env=None):
        if calls is not None:
            calls.append(cmd)
        if cmd[0] != "systemd-creds":
            return 127, "", "not found"
        if cmd[1] == "encrypt":
            if fail_encrypt:
                return 1, "", "no tpm"
            return 0, "SEALED:" + base64.b64encode(input_bytes).decode(), ""
        text = input_bytes.decode()
        if not text.startswith("SEALED:"):
            return 1, "", "bad credential"
        return 0, base64.b64decode(text[7:]).decode(), ""
    return runner


class Base(unittest.TestCase):
    tpm = False

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        release = os.path.join(self.dir.name, "os-release")
        with open(release, "w", encoding="utf-8") as handle:
            handle.write('IMAGE_VERSION="1.0.1"\n')
        tpm_dir = os.path.join(self.dir.name, "tpm")
        os.makedirs(os.path.join(tpm_dir, "tpm0"), exist_ok=True)
        rm = os.path.join(self.dir.name, "tpmrm0")
        if self.tpm:
            open(rm, "w").close()
        self.paths = {"state": self.dir.name, "os_release": release, "efivars": os.path.join(self.dir.name, "efi"),
                      "mountinfo": os.path.join(self.dir.name, "mountinfo"), "sysfs": self.dir.name, "dev": self.dir.name,
                      "tpm_class": tpm_dir if self.tpm else os.path.join(self.dir.name, "none"), "tpmrm": rm}
        open(self.paths["mountinfo"], "w").close()
        self.runner = fake_creds()


class FileKey(Base):
    def test_made_once_private_and_stable(self):
        first = devices.public_key(self.paths, self.runner)
        self.assertEqual(len(unb64(first)), 32)
        self.assertEqual(devices.public_key(self.paths, self.runner), first)
        self.assertEqual(os.stat(devices.key_path(self.paths)).st_mode & 0o777, 0o600)
        self.assertEqual(devices.key_protection(self.paths), "file")

    def test_no_key_yet(self):
        self.assertEqual(devices.key_protection(self.paths), "")

    def test_unlink_forgets_the_key(self):
        first = devices.public_key(self.paths, self.runner)
        devices.link(self.paths, "dev1", "https://id.example", self.runner)
        self.assertTrue(devices.unlink(self.paths))
        self.assertFalse(devices.unlink(self.paths))
        self.assertEqual(devices.key_protection(self.paths), "")
        self.assertNotEqual(devices.public_key(self.paths, self.runner), first)


class SealedKey(Base):
    tpm = True

    def test_with_a_tpm_the_key_is_sealed_and_there_is_no_plain_file(self):
        public = devices.public_key(self.paths, self.runner)
        self.assertEqual(devices.key_protection(self.paths), "tpm")
        self.assertFalse(os.path.exists(devices.key_path(self.paths)))
        self.assertEqual(os.stat(devices.sealed_path(self.paths)).st_mode & 0o777, 0o600)
        with open(devices.sealed_path(self.paths), encoding="utf-8") as handle:
            self.assertTrue(handle.read().startswith("SEALED:"))
        self.assertEqual(devices.public_key(self.paths, self.runner), public)         # unsealed again, same key

    def test_sealing_uses_the_tpm_with_no_pcr_policy(self):
        calls = []
        devices.public_key(self.paths, fake_creds(calls))
        encrypt = next(c for c in calls if c[1] == "encrypt")
        self.assertIn("--with-key=tpm2", encrypt)
        self.assertIn("--tpm2-pcrs=", encrypt)                                         # empty: no PCRs, an update keeps the key

    def test_a_failed_seal_falls_back_to_the_file_and_says_so(self):
        devices.public_key(self.paths, fake_creds(fail_encrypt=True))
        self.assertEqual(devices.key_protection(self.paths), "file")

    def test_a_file_key_is_sealed_when_a_tpm_turns_up(self):
        self.tpm = False
        os.unlink(os.path.join(self.dir.name, "tpmrm0"))
        public = devices.public_key(self.paths, self.runner)
        self.assertEqual(devices.key_protection(self.paths), "file")
        open(os.path.join(self.dir.name, "tpmrm0"), "w").close()
        self.paths["tpm_class"] = os.path.join(self.dir.name, "tpm")
        self.assertEqual(devices.public_key(self.paths, self.runner), public)
        self.assertEqual(devices.key_protection(self.paths), "tpm")
        self.assertFalse(os.path.exists(devices.key_path(self.paths)))

    def test_a_key_that_cannot_be_unsealed_is_an_error_not_a_new_key(self):
        devices.public_key(self.paths, self.runner)
        broken = fake_creds()

        def refuse(cmd, timeout=60, input_bytes=None, env=None):
            return (1, "", "tpm gone") if cmd[1] == "decrypt" else broken(cmd, timeout, input_bytes, env)
        with self.assertRaisesRegex(Failed, "cannot be unsealed"):
            devices.public_key(self.paths, refuse)
        self.assertTrue(os.path.exists(devices.sealed_path(self.paths)))               # nothing was replaced


# Talks to this computer's real TPM through systemd-creds, so it only runs when asked: ECP_TEST_REAL_TPM=1
@unittest.skipUnless(os.environ.get("ECP_TEST_REAL_TPM") == "1" and shutil.which("systemd-creds"),
                     "set ECP_TEST_REAL_TPM=1 to try the real TPM (needs systemd-creds and a TPM 2.0)")
class RealTpm(Base):
    tpm = True

    def test_the_key_round_trips_through_the_real_tpm(self):
        public = devices.public_key(self.paths)
        self.assertEqual(devices.key_protection(self.paths), "tpm")
        with open(devices.sealed_path(self.paths), encoding="utf-8") as handle:
            self.assertNotIn("BEGIN", handle.read())
        self.assertEqual(devices.public_key(self.paths), public)
        proof = devices.sign_link(self.paths, CHALLENGE)
        Ed25519PublicKey.from_public_bytes(unb64(public)).verify(
            unb64(proof["signature"]), f"prism-device-link.{CHALLENGE}.{public}".encode())


class LinkChallenge(Base):
    def test_signs_the_fixed_form_and_it_verifies(self):
        proof = devices.sign_link(self.paths, CHALLENGE, self.runner)
        self.assertEqual(proof["public_key"], devices.public_key(self.paths, self.runner))
        Ed25519PublicKey.from_public_bytes(unb64(proof["public_key"])).verify(
            unb64(proof["signature"]), f"prism-device-link.{CHALLENGE}.{proof['public_key']}".encode())

    def test_never_signs_anything_else(self):
        for bad in ("", "hello", "a.b", "1.2.3.4", "1900000000.ab.cd extra", "x" * 300, "1.a b.c", "../../etc/passwd", None, 5,
                    f"{CHALLENGE}\n", "1900000000.AbC.mac\x00"):
            with self.assertRaises(InvalidArgument, msg=repr(bad)):
                devices.sign_link(self.paths, bad, self.runner)


class Link(Base):
    def test_link_is_stored(self):
        devices.link(self.paths, "dev1", "https://id.example/", self.runner)
        self.assertEqual(devices.link_state(self.paths), {"id": "dev1", "url": "https://id.example"})

    def test_refuses_bad_id_and_plain_http(self):
        for bad_id, url in (("", "https://x"), ("a/b", "https://x"), ("d", "http://x")):
            with self.assertRaises(InvalidArgument):
                devices.link(self.paths, bad_id, url, self.runner)


class Report(Base):
    def test_only_known_fields_and_the_version(self):
        out = devices.report(self.paths)
        self.assertEqual(out["schema"], 1)
        self.assertEqual(out["version"], "1.0.1")
        self.assertEqual(out["update_state"], "idle")
        self.assertTrue(set(out) <= set(devices.REPORT_FIELDS))

    def test_a_waiting_update_is_reported(self):
        with open(os.path.join(self.dir.name, "update-pending.json"), "w", encoding="utf-8") as handle:
            json.dump({"version": "1.0.2"}, handle)
        self.assertEqual(devices.report(self.paths)["update_state"], "pending-reboot")


class Checkin(Base):
    def setUp(self):
        super().setUp()
        self.pub = devices.public_key(self.paths, self.runner)
        devices.link(self.paths, "dev1", "https://id.example", self.runner)
        self.sent = []
        self.answer = (200, "")

    def sender(self, url, headers, data):
        self.sent.append((url, headers, data))
        return self.answer

    def test_signature_verifies_over_id_time_and_body(self):
        body = devices.checkin(self.paths, self.sender, now=lambda: 1000, runner=self.runner)
        url, headers, data = self.sent[0]
        self.assertEqual(url, "https://id.example/api/oauth/me/devices/dev1/checkin")
        self.assertEqual(json.loads(data), body)
        Ed25519PublicKey.from_public_bytes(unb64(self.pub)).verify(unb64(headers["X-Device-Signature"]), f"dev1.1000.{data.decode()}".encode())

    def test_the_request_names_itself(self):
        """A CDN in front of the account service refuses the default Python user agent with a 403: the check-in must send its own."""
        devices.checkin(self.paths, self.sender, now=lambda: 1000, runner=self.runner)
        headers = self.sent[0][1]
        self.assertEqual(headers["User-Agent"], "foundation-device-checkin")
        self.assertNotIn("Python", headers["User-Agent"])

    def test_a_changed_body_would_not_verify(self):
        devices.checkin(self.paths, self.sender, now=lambda: 1000, runner=self.runner)
        _url, headers, data = self.sent[0]
        with self.assertRaises(Exception):
            Ed25519PublicKey.from_public_bytes(unb64(self.pub)).verify(unb64(headers["X-Device-Signature"]), b"dev1.1000." + data + b" ")

    def test_not_linked(self):
        devices.unlink(self.paths)
        with self.assertRaises(Unavailable):
            devices.checkin(self.paths, self.sender, runner=self.runner)

    def refused(self, status, error, message):
        self.answer = (status, error)
        with self.assertRaisesRegex(Failed, message):
            devices.checkin(self.paths, self.sender, runner=self.runner)
        self.assertTrue(devices.link_state(self.paths))                                 # a refusal never drops the link

    def test_refusals_are_named_and_the_link_stays(self):
        self.refused(401, "device_clock_skew", "clock")
        self.refused(401, "device_signature_invalid", "unlinked on the website")
        self.refused(409, "device_replayed", "not newer")
        self.refused(429, "rate_limited", "limiting")
        self.refused(500, "", "HTTP 500")

    def test_unreachable_service(self):
        self.answer = (0, "")
        with self.assertRaises(Unavailable):
            devices.checkin(self.paths, self.sender, runner=self.runner)


class Daily(Base):
    """Check in at most once a day, only when linked and switched on; a failure is not retried before the day is over."""

    def setUp(self):
        super().setUp()
        devices.link(self.paths, "dev1", "https://id.example", self.runner)
        self.sent = []
        self.answer = (200, "")

    def sender(self, url, headers, data):
        self.sent.append(url)
        return self.answer

    def go(self, at, **kwargs):
        return devices.checkin_if_due(self.paths, kwargs.pop("enabled", True), now=lambda: at, sender=self.sender, runner=self.runner, **kwargs)

    def test_first_then_not_due_then_due_again(self):
        self.assertEqual(self.go(1000), "sent")
        self.assertEqual(self.go(1000 + 3600), "not due")
        self.assertEqual(self.go(1000 + devices.DAY), "sent")
        self.assertEqual(len(self.sent), 2)

    def test_switched_off_and_not_linked_send_nothing(self):
        self.assertEqual(self.go(1000, enabled=False), "off")
        devices.unlink(self.paths)
        self.assertEqual(self.go(1000), "not linked")
        self.assertEqual(self.sent, [])

    def test_a_failure_is_recorded_and_not_retried_the_same_day(self):
        self.answer = (0, "")
        with self.assertRaises(Unavailable):
            self.go(1000)
        self.assertIn("cannot be reached", devices.last(self.paths)["error"])
        self.assertEqual(self.go(1000 + 600), "not due")
        self.assertEqual(len(self.sent), 1)

    def test_now_ignores_the_day(self):
        self.go(1000)
        self.assertEqual(self.go(1001, due=False), "sent")

    def test_status_shows_the_link_the_key_and_the_last_attempt(self):
        self.go(1000)
        shown = devices.status(self.paths, True, "https://id.example")
        self.assertEqual((shown["linked"], shown["id"], shown["last_attempt"], shown["last_error"], shown["key_protection"]),
                         (True, "dev1", 1000, "", "file"))


class HostName(Base):
    def setUp(self):
        super().setUp()
        devices.link(self.paths, "dev1", "https://id.example", self.runner)
        self.set = []
        self.setter = lambda paths, name: self.set.append(name)

    def test_the_host_name_is_made_from_the_name(self):
        for name, expected in (("Ersa Workstation", "Ersa-Workstation"), ("  Ersa   Workstation  ", "Ersa-Workstation"), ("-x-", "x"),
                               ("Caf\u00e9 d\u00e9j\u00e0", "Cafe-deja"), ("a_b.c", "abc"), ("A--B", "A-B"), ("   ", None), ("", None),
                               ("x" * 80, "x" * 63), (None, None), (5, None)):
            self.assertEqual(devices.hostname_from_name(name), expected, repr(name))

    def test_a_hostile_name_stays_a_plain_host_name(self):
        wanted = devices.hostname_from_name("a b\n; rm -rf / `x` $(y) ../../etc")
        self.assertRegex(wanted, r"^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?$")
        self.assertLessEqual(len(wanted), 63)

    def test_it_follows_a_changed_name_once(self):
        self.assertEqual(devices.sync_hostname(self.paths, "Ersa Workstation", True, self.setter), "Ersa-Workstation")
        self.assertIsNone(devices.sync_hostname(self.paths, "Ersa Workstation", True, self.setter))         # same name: leave it
        self.assertEqual(devices.sync_hostname(self.paths, "Studio PC", True, self.setter), "Studio-PC")
        self.assertEqual(self.set, ["Ersa-Workstation", "Studio-PC"])

    def test_a_name_that_is_already_the_host_name_changes_nothing(self):
        """The default name at linking is the host name: it must not be rewritten (dots stripped) into a different host name."""
        import socket
        current = socket.gethostname()
        self.assertIsNone(devices.sync_hostname(self.paths, current, True, self.setter))
        self.assertEqual(self.set, [])
        self.assertEqual(devices.link_state(self.paths)["name"], current)                       # but it is remembered

    def test_a_name_with_a_dot_is_not_applied_when_it_is_the_host_name(self):
        import socket
        original = socket.gethostname
        socket.gethostname = lambda: "localhost.localdomain"
        try:
            self.assertIsNone(devices.sync_hostname(self.paths, "localhost.localdomain", True, self.setter))
            self.assertEqual(self.set, [])
            self.assertEqual(devices.sync_hostname(self.paths, "Ersa Workstation", True, self.setter), "Ersa-Workstation")   # a real rename still applies
        finally:
            socket.gethostname = original

    def test_a_host_name_set_by_hand_stays_until_the_name_changes(self):
        devices.sync_hostname(self.paths, "Ersa Workstation", True, self.setter)
        self.set.clear()
        devices.sync_hostname(self.paths, "Ersa Workstation", True, self.setter)                              # a later check-in
        self.assertEqual(self.set, [])

    def test_off_and_unusable_names_change_nothing(self):
        self.assertIsNone(devices.sync_hostname(self.paths, "Ersa Workstation", False, self.setter))
        self.assertIsNone(devices.sync_hostname(self.paths, "!!!", True, self.setter))
        self.assertEqual(self.set, [])

    def test_a_checkin_brings_the_name_and_sets_the_host_name(self):
        sender = lambda url, headers, data: (200, "", "Ersa Workstation")
        devices.checkin_if_due(self.paths, True, now=lambda: 1000, sender=sender, runner=self.runner, setter=self.setter)
        self.assertEqual(self.set, ["Ersa-Workstation"])

    def test_the_setting_can_turn_it_off(self):
        sender = lambda url, headers, data: (200, "", "Ersa Workstation")
        devices.checkin_if_due(self.paths, True, now=lambda: 1000, sender=sender, runner=self.runner, follow_name=False, setter=self.setter)
        self.assertEqual(self.set, [])

    def test_a_host_name_that_cannot_be_set_does_not_lose_the_report(self):
        def broken(paths, name):
            raise Failed("no hostnamed")
        sender = lambda url, headers, data: (200, "", "Ersa Workstation")
        self.assertEqual(devices.checkin_if_due(self.paths, True, now=lambda: 1000, sender=sender, runner=self.runner, setter=broken), "sent")
        self.assertIn("no hostnamed", devices.last(self.paths)["error"])


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        route = self.path
        if route == "/ok":
            self.send_response(200)
            self.end_headers()
        elif route == "/name":
            body = json.dumps({"ok": True, "name": "Ersa Workstation"}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif route == "/refuse":
            body = json.dumps({"error": "device_clock_skew"}).encode()
            self.send_response(401)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif route == "/redirect":
            self.send_response(307)
            self.send_header("Location", "http://127.0.0.1:1/elsewhere")
            self.end_headers()
        elif route == "/flood":
            self.send_response(401)
            self.end_headers()
            try:
                for _ in range(2000):
                    self.wfile.write(b"x" * 8192)
            except OSError:
                pass


class SendTests(unittest.TestCase):
    """The real sender, in a real child process, against a local server."""

    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def post(self, route):
        return devices.send(self.base + route, {"Content-Type": "application/json"}, b"{}")

    def test_success(self):
        self.assertEqual(self.post("/ok"), (200, "", ""))

    def test_the_computers_name_comes_back_from_a_successful_answer(self):
        self.assertEqual(self.post("/name"), (200, "", "Ersa Workstation"))

    def test_the_error_code_is_read_from_the_answer(self):
        self.assertEqual(self.post("/refuse"), (401, "device_clock_skew", ""))

    def test_a_redirect_is_an_error_and_is_not_followed(self):
        self.assertEqual(self.post("/redirect")[0], 307)

    def test_an_endless_answer_is_read_only_a_little(self):
        status, error, _name = self.post("/flood")
        self.assertEqual(status, 401)
        self.assertEqual(error, "")                                                    # not json, and not read to the end

    def test_nothing_listening(self):
        self.assertEqual(devices.send("http://127.0.0.1:1/x", {}, b"{}"), (0, "", ""))


class DropPrivileges(unittest.TestCase):
    def drop(self, uid, regain_fails=True):
        calls = []

        def setuid(value):
            calls.append(("setuid", value))
            if value == 0 and not regain_fails:
                return
            if value == 0:
                raise PermissionError

        class Entry:
            pw_uid, pw_gid = 65534, 65533
        result = None
        try:
            result = devices.drop_privileges(lambda groups: calls.append(("setgroups", groups)), lambda gid: calls.append(("setgid", gid)),
                                             setuid, lambda: uid, lambda name: Entry)
        except Failed:
            result = "failed"
        return result, calls

    def test_not_root_does_nothing(self):
        self.assertEqual(self.drop(1000), (False, []))

    def test_root_drops_groups_then_gid_then_uid_and_cannot_come_back(self):
        result, calls = self.drop(0)
        self.assertTrue(result)
        self.assertEqual(calls, [("setgroups", []), ("setgid", 65533), ("setuid", 65534), ("setuid", 0)])

    def test_refuses_to_go_on_if_root_could_be_regained(self):
        self.assertEqual(self.drop(0, regain_fails=False)[0], "failed")


if __name__ == "__main__":
    unittest.main()
