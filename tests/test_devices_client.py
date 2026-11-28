"""The person's side of linking a computer: the sign-in, the challenge, the registration (no network; the account service is faked),
and the commands against a fake foundationd client."""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from foundation import cli, devices, identity  # noqa: E402

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
        if method == "List":
            return {"identity.issuer": PROVIDER["issuer"], "identity.client_id": PROVIDER["client_id"]}
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


def stored_session(home, scope="openid profile devices:write"):
    identity.save_session({"issuer": PROVIDER["issuer"], "client_id": PROVIDER["client_id"], "token_endpoint": "https://id.example/token",
                           "access_token": "STORED", "refresh_token": None, "scope": scope, "expires_at": 9e12,
                           "claims": {"sub": "u-1"}}, home)


def link(client, service, scope="openid profile devices:write", url="https://id.example", home=None):
    home = home or tempfile.mkdtemp()                        # never the real session of whoever runs the tests
    if scope is not None:
        stored_session(home, scope)
    return devices.link(client, PROVIDER, url, "Laptop", opener=service, state_home=home)


class Link(unittest.TestCase):
    def test_links_with_the_stored_sign_in_and_records_the_id(self):
        client, service = FakeClient(), FakeService()
        self.assertEqual(link(client, service), "dev-1")
        self.assertEqual([m for m, _a in client.calls], ["SignLink", "CompleteLink", "CheckinNow"])      # then it reports at once
        self.assertEqual(client.calls[0][1], ("1900000000.nonce.mac",))
        self.assertEqual(client.calls[1][1], ("dev-1",))

    def test_there_is_one_sign_in_and_no_second_one_is_started(self):
        service = FakeService()
        link(FakeClient(), service)
        self.assertFalse(any(url.endswith("/device") or url.endswith("/token") for url, _b, _h in service.requests))
        registered = next(r for r in service.requests if r[0].endswith("/api/oauth/me/devices"))
        self.assertEqual(registered[2]["Authorization"], "Bearer STORED")

    def test_sends_the_key_the_challenge_and_the_proof_to_the_account(self):
        service = FakeService()
        link(FakeClient(), service)
        _url, body, _headers = next(r for r in service.requests if r[0].endswith("/api/oauth/me/devices"))
        self.assertEqual(json.loads(body), {"name": "Laptop", "public_key": "PUB", "challenge": "1900000000.nonce.mac", "signature": "SIG"})

    def test_the_token_never_reaches_foundationd(self):
        client = FakeClient()
        link(client, FakeService())
        self.assertNotIn("STORED", json.dumps(client.calls))

    def test_nobody_signed_in_says_to_sign_in(self):
        with self.assertRaisesRegex(devices.DevicesError, "Sign in to Ersa ID first"):
            link(FakeClient(), FakeService(), scope=None)

    def test_a_sign_in_without_the_permission_says_what_to_do(self):
        with self.assertRaisesRegex(devices.DevicesError, "Sign in again"):
            link(FakeClient(), FakeService(), scope="openid profile")

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
            link(FakeClient(), FakeService(), url="http://id.example")


LINKED = {"linked": True, "id": "dev-1", "url": "https://id.example", "enabled": True, "key_protection": "tpm", "last_attempt": 0, "last_error": ""}


class Unlink(unittest.TestCase):
    def service(self, status=200, code=""):
        class Deleting(FakeService):
            def __call__(self, request, timeout=0):
                if request.get_method() == "DELETE":
                    self.requests.append((request.full_url, "", dict(request.header_items())))
                    if status != 200:
                        raise urllib.error.HTTPError(request.full_url, status, "x", {}, io.BytesIO(json.dumps({"error": code}).encode()))
                    return Reply(b"{}")
                return super().__call__(request, timeout)
        return Deleting()

    def home(self, scope="openid devices:write"):
        home = tempfile.mkdtemp()
        stored_session(home, scope)
        return home

    def test_removes_the_computer_from_the_account_then_forgets_it_here(self):
        client, service = FakeClient(LINKED), self.service()
        self.assertTrue(devices.unlink(client, PROVIDER, opener=service, state_home=self.home()))
        deleted = next(r for r in service.requests if r[0].endswith("/api/oauth/me/devices/dev-1"))
        self.assertEqual(deleted[2]["Authorization"], "Bearer STORED")                       # the one stored sign-in
        self.assertEqual([m for m, _a in client.calls], ["Status", "Unlink"])               # account first, then here

    def test_a_computer_the_account_no_longer_knows_is_still_unlinked_here(self):
        client = FakeClient(LINKED)
        self.assertTrue(devices.unlink(client, PROVIDER, opener=self.service(404, "device_not_found"), state_home=self.home()))
        self.assertIn("Unlink", [m for m, _a in client.calls])

    def test_if_the_account_cannot_be_told_nothing_changes(self):
        for service, home, words in ((self.service(500, "weird"), self.home(), "HTTP 500"), (self.service(), tempfile.mkdtemp(), "Sign in"),
                                     (self.service(), self.home("openid"), "Sign in again")):
            client = FakeClient(LINKED)
            with self.assertRaisesRegex(devices.DevicesError, words):
                devices.unlink(client, PROVIDER, opener=service, state_home=home)
            self.assertNotIn("Unlink", [m for m, _a in client.calls])

    def test_a_computer_that_is_not_linked_needs_no_sign_in(self):
        client = FakeClient()
        self.assertTrue(devices.unlink(client, PROVIDER, opener=self.service(), state_home=tempfile.mkdtemp()))

    def test_unlink_here_never_contacts_the_account(self):
        client = FakeClient(LINKED)
        self.assertTrue(devices.unlink_here(client))
        self.assertEqual([m for m, _a in client.calls], ["Unlink"])

    def test_the_command_reports_what_happened(self):
        def run(**args):
            ns = type("Args", (), dict({"json": False, "yes": True, "local": False}, **args))()
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                return cli.cmd_devices_unlink(FakeClient(), ns), out.getvalue()
        self.assertIn("Unlinked on this computer", run(local=True)[1])


class ListDevices(unittest.TestCase):
    def service(self, answer, status=200):
        class Listing(FakeService):
            def __call__(self, request, timeout=0):
                self.requests.append((request.full_url, "", dict(request.header_items()), request.get_method()))
                if status != 200:
                    raise urllib.error.HTTPError(request.full_url, status, "x", {}, io.BytesIO(json.dumps({"error": "weird"}).encode()))
                return Reply(json.dumps(answer).encode())
        return Listing()

    def test_lists_the_accounts_computers_with_the_stored_sign_in(self):
        home = tempfile.mkdtemp()
        stored_session(home, "openid devices:read devices:write")
        service = self.service({"devices": [{"id": "a", "name": "Laptop", "report": None, "last_seen_at": 5}, {"nonsense": 1}]})
        found = devices.list_devices(PROVIDER, "https://id.example/", opener=service, state_home=home)
        self.assertEqual([d["id"] for d in found], ["a"])
        url, _b, headers, method = service.requests[0]
        self.assertEqual((url, method, headers["Authorization"]), ("https://id.example/api/oauth/me/devices", "GET", "Bearer STORED"))

    def test_needs_the_read_permission_and_a_sign_in(self):
        home = tempfile.mkdtemp()
        stored_session(home, "openid devices:write")                                     # write only: cannot list
        with self.assertRaisesRegex(devices.DevicesError, "see your computers"):
            devices.list_devices(PROVIDER, "https://id.example", opener=self.service({}), state_home=home)
        with self.assertRaisesRegex(devices.DevicesError, "Sign in to Ersa ID first"):
            devices.list_devices(PROVIDER, "https://id.example", opener=self.service({}), state_home=tempfile.mkdtemp())

    def test_a_refusal_or_nonsense_is_an_error(self):
        home = tempfile.mkdtemp()
        stored_session(home, "openid devices:read")
        with self.assertRaisesRegex(devices.DevicesError, "HTTP 500"):
            devices.list_devices(PROVIDER, "https://id.example", opener=self.service({}, 500), state_home=home)
        with self.assertRaisesRegex(devices.DevicesError, "cannot read"):
            devices.list_devices(PROVIDER, "https://id.example", opener=self.service({"devices": "no"}), state_home=home)


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

    def test_link_without_a_sign_in_says_to_sign_in_first(self):
        class Client(FakeClient):
            def one(self, interface, method, signature=None, *args, **kw):
                if method == "List":
                    return {"identity.issuer": "https://id.example", "identity.client_id": "ecp"}
                return super().one(interface, method, signature, *args, **kw)
        home = tempfile.mkdtemp()
        saved = os.environ.get("XDG_STATE_HOME")
        os.environ["XDG_STATE_HOME"] = home                      # an empty state folder: nobody is signed in
        try:
            with self.assertRaisesRegex(cli.UserError, "identity login"):
                self.run_cmd(cli.cmd_devices_link, Client())
        finally:
            if saved is None:
                del os.environ["XDG_STATE_HOME"]
            else:
                os.environ["XDG_STATE_HOME"] = saved

    def test_checkin(self):
        code, out = self.run_cmd(cli.cmd_devices_checkin, FakeClient())
        self.assertEqual((code, out.strip()), (0, "Reported."))


if __name__ == "__main__":
    unittest.main()
