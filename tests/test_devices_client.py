"""The person's side of linking a computer: the sign-in, the challenge, the registration (no network; the account service is faked),
and the commands against a fake foundationd client."""

import contextlib
import io
import json
import os
import sys
import unittest
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from foundation import cli, devices  # noqa: E402

PROVIDER = {"enabled": True, "configured": True, "issuer": "https://id.example", "client_id": "ecp", "scopes": "openid profile"}


class Reply(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeClient:
    def __init__(self, status=None):
        self.calls = []
        self.status = status or {"linked": False, "id": "", "url": "https://id.example", "enabled": True, "key_protection": "tpm",
                                 "last_attempt": 0, "last_error": ""}

    def one(self, interface, method, signature=None, *args, **kw):
        self.calls.append((method, args))
        if method == "SignLink":
            return {"public_key": "PUB", "signature": "SIG"}
        if method == "Status":
            return self.status
        if method == "Report":
            return {"schema": 1, "version": "1.0.2", "tpm": True}
        if method == "Unlink":
            return {"was_linked": True}
        if method == "CheckinNow":
            return {"result": "sent"}
        raise AssertionError(method)

    def call(self, interface, method, signature=None, *args, **kw):
        self.calls.append((method, args))
        return ({},)


class FakeService:
    """The account service: the device flow, the challenge and the registration."""

    def __init__(self, register=None):
        self.requests = []
        self.register = register

    def __call__(self, request, timeout=0):
        url = request.full_url
        data = request.data.decode() if request.data else ""
        self.requests.append((url, data, dict(request.header_items())))
        if url.endswith("/.well-known/openid-configuration"):
            return Reply(json.dumps({"issuer": "https://id.example", "device_authorization_endpoint": "https://id.example/device",
                                     "token_endpoint": "https://id.example/token", "jwks_uri": "https://id.example/jwks"}).encode())
        if url.endswith("/device"):
            return Reply(json.dumps({"device_code": "dc", "user_code": "ABCD", "verification_uri": "https://id.example/d",
                                     "interval": 1, "expires_in": 60}).encode())
        if url.endswith("/token"):
            return Reply(json.dumps({"access_token": "TOKEN", "token_type": "Bearer", "expires_in": 300}).encode())
        if url.endswith("/challenge"):
            return Reply(json.dumps({"challenge": "1900000000.nonce.mac"}).encode())
        if url.endswith("/api/oauth/me/devices"):
            if self.register:
                raise urllib.error.HTTPError(url, self.register[0], "x", {}, io.BytesIO(json.dumps({"error": self.register[1]}).encode()))
            return Reply(json.dumps({"id": "dev-1", "name": "Laptop"}).encode())
        raise AssertionError(url)


def link(client, service, **kw):
    return devices.link(client, PROVIDER, "https://id.example", "Laptop", opener=service, announce=lambda *_: None,
                        sleep=lambda _s: None, **kw)


class Link(unittest.TestCase):
    def test_links_and_records_the_id(self):
        client, service = FakeClient(), FakeService()
        self.assertEqual(link(client, service), "dev-1")
        self.assertEqual([m for m, _a in client.calls], ["SignLink", "CompleteLink"])
        self.assertEqual(client.calls[0][1], ("1900000000.nonce.mac",))
        self.assertEqual(client.calls[1][1], ("dev-1",))

    def test_sends_the_key_the_challenge_and_the_proof_to_the_account(self):
        service = FakeService()
        link(FakeClient(), service)
        url, body, headers = next(r for r in service.requests if r[0].endswith("/api/oauth/me/devices"))
        self.assertEqual(json.loads(body), {"name": "Laptop", "public_key": "PUB", "challenge": "1900000000.nonce.mac", "signature": "SIG"})
        self.assertEqual(headers["Authorization"], "Bearer TOKEN")

    def test_the_sign_in_asks_for_nothing_but_devices_write(self):
        service = FakeService()
        link(FakeClient(), service)
        asked = next(body for url, body, _h in service.requests if url.endswith("/device"))
        self.assertIn("scope=devices%3Awrite", asked)
        self.assertNotIn("profile", asked)

    def test_the_token_is_not_kept(self):
        client = FakeClient()
        link(client, FakeService())
        self.assertNotIn("TOKEN", json.dumps(client.calls))                           # never handed to foundationd

    def test_the_account_services_refusals_become_plain_messages(self):
        for status, code, words in ((403, "insufficient_scope", "devices:write"), (400, "device_proof_invalid", "proof"),
                                    (409, "device_already_linked", "already linked"), (409, "devices_limit", "most computers"),
                                    (429, "rate_limited", "limiting"), (500, "weird", "HTTP 500")):
            with self.assertRaisesRegex(devices.DevicesError, words):
                link(FakeClient(), FakeService(register=(status, code)))

    def test_a_failure_to_record_says_how_to_clean_up(self):
        class Broken(FakeClient):
            def call(self, *a, **k):
                raise RuntimeError("boom")
        with self.assertRaisesRegex(devices.DevicesError, "dev-1.*Remove it on the website"):
            link(Broken(), FakeService())

    def test_only_https(self):
        with self.assertRaisesRegex(devices.DevicesError, "https"):
            devices.link(FakeClient(), PROVIDER, "http://id.example", "x", opener=FakeService(), announce=lambda *_: None, sleep=lambda _s: None)


class Commands(unittest.TestCase):
    def run_cmd(self, func, client, **args):
        namespace = type("Args", (), dict({"json": False, "yes": True, "name": None}, **args))()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = func(client, namespace)
        return code, out.getvalue()

    def test_status_when_not_linked_says_how(self):
        code, out = self.run_cmd(cli.cmd_devices_status, FakeClient())
        self.assertEqual(code, 0)
        self.assertIn("foundation devices link", out)
        self.assertIn("sealed to this computer's TPM", out)

    def test_status_json(self):
        _code, out = self.run_cmd(cli.cmd_devices_status, FakeClient(), json=True)
        self.assertEqual(json.loads(out)["key_protection"], "tpm")

    def test_show_lists_the_report_and_says_what_is_not_in_it(self):
        _code, out = self.run_cmd(cli.cmd_devices_show, FakeClient())
        self.assertIn("1.0.2", out)
        self.assertIn("no file names", out)

    def test_link_refuses_when_already_linked(self):
        linked = FakeClient({"linked": True, "id": "x", "url": "u", "enabled": True, "key_protection": "file", "last_attempt": 0,
                             "last_error": ""})
        with self.assertRaisesRegex(cli.UserError, "already linked"):
            self.run_cmd(cli.cmd_devices_link, linked)

    def test_unlink_and_checkin(self):
        self.assertIn("Unlinked", self.run_cmd(cli.cmd_devices_unlink, FakeClient())[1])
        code, out = self.run_cmd(cli.cmd_devices_checkin, FakeClient())
        self.assertEqual((code, out.strip()), (0, "Reported."))


if __name__ == "__main__":
    unittest.main()
