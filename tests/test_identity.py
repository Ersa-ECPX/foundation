"""The OpenID Connect client: provider settings, discovery, the device flow, refresh, sign-out and the CLI commands, against a fake provider."""

import base64
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

import os as _os, sys as _sys  # noqa: E401
_sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), ".."))

import support  # noqa: E402,F401

from foundation import cli, identity  # noqa: E402


def jwt(claims):
    part = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{part({'alg': 'none'})}.{part(claims)}.x"


class Provider(BaseHTTPRequestHandler):
    """A tiny provider: discovery, a device code that is approved on the second poll, refresh with rotation, revocation."""
    state = {}

    def log_message(self, *_a):
        pass

    def reply(self, body, status=200):
        data = json.dumps(body).encode() if body is not None else b""
        self.send_response(status)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        base = f"http://127.0.0.1:{self.server.server_port}"
        if self.path != "/.well-known/openid-configuration":
            return self.reply({}, 404)
        meta = {"issuer": self.state.get("issuer", base), "token_endpoint": base + "/token",
                "device_authorization_endpoint": base + "/device", "revocation_endpoint": base + "/revoke",
                "scopes_supported": ["openid", "profile", "email", "offline_access"], "grant_types_supported": ["refresh_token"]}
        meta.update(self.state.get("meta", {}))
        self.reply({k: v for k, v in meta.items() if v is not None})

    def do_POST(self):
        form = urllib.parse.parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
        grant, st = form.get("grant_type", [""])[0], self.state
        st.setdefault("calls", []).append((self.path, {k: v[0] for k, v in form.items()}))
        base = f"http://127.0.0.1:{self.server.server_port}"
        if self.path == "/device":
            return self.reply({"device_code": "dev123", "user_code": "ABCD-EFGH", "verification_uri": base + "/activate",
                               "interval": 1, "expires_in": 600})
        if self.path == "/revoke":
            return self.reply(None)
        if grant.endswith("device_code"):
            st["polls"] = st.get("polls", 0) + 1
            if st.get("deny"):
                return self.reply({"error": "access_denied"}, 400)
            if st["polls"] < 2:
                return self.reply({"error": "authorization_pending"}, 400)
            claims = {"iss": st.get("issuer", base), "aud": "ersa-device", "sub": "u-1", "email": "ada@example.org",
                      "exp": time.time() + 3600}
            return self.reply({"access_token": "A1", "refresh_token": "R1", "expires_in": 300, "id_token": jwt(claims),
                               "scope": "openid profile email offline_access"})
        if grant == "refresh_token":
            if form["refresh_token"][0] != "R1" or st.get("revoked"):
                return self.reply({"error": "invalid_grant"}, 400)
            return self.reply({"access_token": "A2", "refresh_token": "R2", "expires_in": 300})
        self.reply({"error": "unsupported_grant_type"}, 400)


class Flow(unittest.TestCase):
    def setUp(self):
        Provider.state = {}
        self.server = HTTPServer(("127.0.0.1", 0), Provider)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.issuer = f"http://127.0.0.1:{self.server.server_port}"
        self.provider = identity.settings_from({"identity.issuer": self.issuer + "/", "identity.client_id": "ersa-device"})
        self.home = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.home, ignore_errors=True))

    def login(self):
        started = identity.start_login(self.provider)
        session = identity.finish_login(self.provider, started, sleep=lambda _s: None)
        identity.save_session(session, self.home)
        return session

    def test_settings_default_to_enabled_and_need_issuer_and_client(self):
        empty = identity.settings_from({})
        self.assertEqual((empty["enabled"], empty["configured"]), (True, False))
        off = identity.settings_from({"identity.enabled": False, "identity.issuer": "https://id.example", "identity.client_id": "c"})
        self.assertEqual((off["enabled"], off["configured"], off["scopes"]), (False, True, identity.DEFAULT_SCOPES))

    def test_discovery_must_name_the_issuer_that_was_asked(self):
        self.assertEqual(identity.discover(self.issuer)["token_endpoint"], self.issuer + "/token")
        Provider.state["issuer"] = "https://elsewhere.example"
        with self.assertRaises(identity.IdentityError):
            identity.discover(self.issuer)

    def test_plain_http_to_the_open_internet_is_refused(self):
        with self.assertRaises(identity.IdentityError):
            identity.discover("http://id.example.org")

    def test_check_reports_each_need(self):
        rows = {name: ok for name, ok, _d in identity.check(self.provider)}
        self.assertTrue(all(rows.values()), rows)
        Provider.state["meta"] = {"device_authorization_endpoint": None, "revocation_endpoint": None}
        rows = {name: ok for name, ok, _d in identity.check(self.provider)}
        self.assertEqual((rows["Device sign-in"], rows["Revocation"], rows["Discovery"]), (False, False, True))

    def test_device_flow_waits_then_stores_a_private_session(self):
        started = identity.start_login(self.provider)
        self.assertEqual(started["user_code"], "ABCD-EFGH")
        waits = []
        session = identity.finish_login(self.provider, started, sleep=lambda _s: None, on_wait=lambda: waits.append(1))
        self.assertEqual((len(waits), session["access_token"], session["claims"]["email"]), (1, "A1", "ada@example.org"))
        self.assertIn("offline_access", session["scope"])                     # what the provider granted is kept
        path = identity.save_session(session, self.home)
        self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o600")
        self.assertEqual(identity.load_session(self.issuer, self.home)["claims"]["sub"], "u-1")

    def test_a_refused_sign_in_and_a_foreign_id_token_are_errors(self):
        Provider.state["deny"] = True
        with self.assertRaises(identity.IdentityError):
            identity.finish_login(self.provider, identity.start_login(self.provider), sleep=lambda _s: None)
        base = {"iss": self.issuer, "aud": "ersa-device", "exp": time.time() + 60}
        identity.id_claims(self.provider, jwt(base))
        for bad in ({"iss": "https://evil.example"}, {"aud": "someone-else"}, {"exp": time.time() - 3600}):
            with self.assertRaises(identity.IdentityError):
                identity.id_claims(self.provider, jwt({**base, **bad}))

    def test_the_access_token_is_refreshed_when_it_expires_and_rotates(self):
        self.login()
        self.assertEqual(identity.access_token(self.provider, self.home), "A1")
        later = lambda: time.time() + 400
        self.assertEqual(identity.access_token(self.provider, self.home, now=later), "A2")
        self.assertEqual(identity.load_session(self.issuer, self.home)["refresh_token"], "R2")

    def test_a_revoked_grant_signs_the_person_out(self):
        self.login()
        Provider.state["revoked"] = True
        self.assertIsNone(identity.access_token(self.provider, self.home, now=lambda: time.time() + 400))
        self.assertIsNone(identity.load_session(self.issuer, self.home))

    def test_logout_revokes_and_forgets(self):
        self.login()
        self.assertEqual(identity.logout(self.provider, self.home), "signed out")
        self.assertIsNone(identity.load_session(self.issuer, self.home))
        self.assertTrue(any(path == "/revoke" and form["token"] == "R1" for path, form in Provider.state["calls"]))
        self.assertEqual(identity.logout(self.provider, self.home), "not signed in")

    def test_logout_revokes_the_access_token_when_there_is_no_refresh_token(self):
        session = self.login()
        session["refresh_token"] = None
        identity.save_session(session, self.home)
        self.assertEqual(identity.logout(self.provider, self.home), "signed out")
        self.assertTrue(any(path == "/revoke" and form["token"] == "A1" and form["token_type_hint"] == "access_token"
                            for path, form in Provider.state["calls"]))

    def test_a_session_of_another_provider_is_not_used(self):
        self.login()
        other = identity.settings_from({"identity.issuer": self.issuer, "identity.client_id": "different-client"})
        self.assertIsNone(identity.access_token(other, self.home))


class Profile(Flow):
    def test_userinfo_updates_the_session_only_for_the_same_person(self):
        session = self.login()
        session["userinfo_endpoint"] = self.issuer + "/userinfo"
        identity.save_session(session, self.home)

        def opener(info):
            return lambda request, timeout: __import__("io").BytesIO(json.dumps(info).encode())
        self.assertIsNone(identity.refresh_profile(self.provider, self.home, opener=opener({"sub": "someone-else", "name": "X"})))
        claims = identity.refresh_profile(self.provider, self.home, opener=opener(
            {"sub": "u-1", "name": "Ada Lovelace", "picture": "https://img.example/a.png", "groups": ["not kept"]}))
        self.assertEqual((claims["name"], claims["picture"], "groups" in claims), ("Ada Lovelace", "https://img.example/a.png", False))
        self.assertEqual(identity.load_session(self.issuer, self.home)["claims"]["name"], "Ada Lovelace")

    def test_picture_is_fetched_over_https_and_limited_in_size(self):
        import io
        ok = lambda request, timeout: io.BytesIO(b"\x89PNG...")
        big = lambda request, timeout: io.BytesIO(b"x" * (identity.MAX_PICTURE + 10))
        self.assertEqual(identity.fetch_picture("https://img.example/a.png", opener=ok), b"\x89PNG...")
        self.assertIsNone(identity.fetch_picture("https://img.example/a.png", opener=big))
        self.assertIsNone(identity.fetch_picture("http://img.example.org/a.png", opener=ok))
        self.assertIsNone(identity.fetch_picture("", opener=ok))


class Offer(Flow):
    def test_the_sign_in_is_offered_once_and_only_when_it_can_work(self):
        self.assertTrue(identity.offer_needed(self.provider, self.home))
        off = {**self.provider, "enabled": False}
        self.assertFalse(identity.offer_needed(off, self.home))                         # switched off by preseed
        self.assertFalse(identity.offer_needed(identity.settings_from({}), self.home))   # no provider named
        identity.mark_offered(self.home)                                                # "Not now" is final
        self.assertFalse(identity.offer_needed(self.provider, self.home))

    def test_nothing_is_offered_to_someone_already_signed_in(self):
        self.login()
        self.assertFalse(identity.offer_needed(self.provider, self.home))


class Units(unittest.TestCase):
    """The sign-in offer must never start without a screen: a display-less Deck process holds the application name and no later
    launch of Deck can open a window (seen in the first booted image)."""
    DIRECTORY = os.path.join(os.path.dirname(__file__), "..", "data", "systemd-user")

    def read(self, name):
        with open(os.path.join(self.DIRECTORY, name), encoding="utf-8") as handle:
            return handle.read()

    def test_the_offer_waits_for_the_graphical_session_and_needs_a_display(self):
        path, service = self.read("foundation-ersa-id-offer.path"), self.read("foundation-ersa-id-offer.service")
        self.assertIn("WantedBy=graphical-session.target", path)
        self.assertNotIn("WantedBy=default.target", path)
        for text in (path, service):
            self.assertIn("After=graphical-session.target", text)
        self.assertIn("ConditionEnvironment=|WAYLAND_DISPLAY", service)
        self.assertIn("ConditionEnvironment=|DISPLAY", service)


class Commands(unittest.TestCase):
    """The commands with a stub for the service's configuration."""

    class Stub:
        def __init__(self, values):
            self.values = values

        def one(self, interface, method, *args):
            assert (interface, method) == ("Config", "List")
            return self.values

    def run_cli(self, values, *argv):
        import contextlib
        import io
        args = cli.build_parser().parse_args(["identity", *argv])
        args.json = getattr(args, "json", False)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = args.func(self.Stub(values), args)
        return code, out.getvalue()

    def test_status_says_off_and_unconfigured_and_has_json(self):
        code, text = self.run_cli({"identity.enabled": False}, "status")
        self.assertIn("off", text)
        code, text = self.run_cli({}, "status", "--json")
        self.assertEqual(json.loads(text), {"enabled": True, "configured": False, "issuer": "", "client_id": "",
                                            "signed_in": False, "account": {}})

    def test_check_and_login_refuse_when_off_or_not_set_up(self):
        for values in ({"identity.enabled": False, "identity.issuer": "https://x.example", "identity.client_id": "c"}, {}):
            for command in ("check", "login"):
                with self.assertRaises(cli.UserError):
                    self.run_cli(values, command)


if __name__ == "__main__":
    unittest.main()
