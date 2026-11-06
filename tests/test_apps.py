"""The Flatpak backend against a fake libflatpak module (the real library is checked in the image)."""

import contextlib
import os
import sys
import unittest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))

from foundationd.backends import flatpak as backend  # noqa: E402
from foundationd.errors import Failed, NotFound, Unavailable  # noqa: E402


class Progress:
    def __init__(self):
        self.value, self.handlers = 0, []

    def connect(self, name, handler):
        self.handlers.append(handler)

    def get_progress(self):
        return self.value

    def set(self, value):
        self.value = value
        for handler in self.handlers:
            handler(self)


class Ref:
    def __init__(self, name, title=None, version="1.0", origin="flathub"):
        self.name, self.title, self.version, self.origin = name, title, version, origin

    def get_name(self):
        return self.name

    def get_appdata_name(self):
        return self.title

    def get_appdata_version(self):
        return self.version

    def get_origin(self):
        return self.origin

    def format_ref(self):
        return f"app/{self.name}/x86_64/stable"


class FakeError(Exception):
    def __init__(self, message, code):
        super().__init__(message)
        self.message, self.code = message, code

    def matches(self, quark, code):
        return quark == "flatpak-quark" and code == self.code


class FakeFlatpak:
    class RefKind:
        APP = "app"

    class Error:
        REF_NOT_FOUND, REMOTE_NOT_FOUND, NOT_INSTALLED = 1, 2, 3

        @staticmethod
        def quark():
            return "flatpak-quark"

    class Remote:
        created = []

        @classmethod
        def new_from_file(cls, name, data):
            cls.created.append((name, bytes(data.get_data())))
            return ("remote", name)

    @staticmethod
    def get_default_arch():
        return "x86_64"

    def __init__(self, installed=(), remotes=("flathub",), operations=2, fail_with=None):
        fake = self
        self.refs = list(installed)
        self.remote_names = list(remotes)
        self.operations = operations
        self.fail_with = fail_with
        self.added = []
        self.transactions = []
        self.updatable = []

        class Remote:
            def __init__(self, name):
                self.name = name

            def get_name(self):
                return self.name

            def get_url(self):
                return "https://example/repo"

        class Installation:
            @staticmethod
            def new_system(cancellable):
                return fake

        class Transaction:
            @staticmethod
            def new_for_installation(installation, cancellable):
                t = Transaction()
                t.handlers, t.actions = {}, []
                fake.transactions.append(t)
                return t

            def set_no_interaction(self, value):
                pass

            def connect(self, name, handler):
                self.handlers[name] = handler

            def add_install(self, remote, ref, subpaths):
                self.actions.append(("install", remote, ref))

            def add_uninstall(self, ref):
                self.actions.append(("uninstall", ref))

            def add_update(self, ref, subpaths, commit):
                self.actions.append(("update", ref))

            def get_operations(self):
                return [object()] * fake.operations

            def run(self, cancellable):
                self.handlers["ready"](self)
                for index in range(fake.operations):
                    progress = Progress()
                    self.handlers["new-operation"](self, object(), progress)
                    progress.set(50)
                    if fake.fail_with and index == 1:
                        self.handlers["operation-error"](self, object(), fake.fail_with, "")
                        raise fake.fail_with
                    progress.set(100)
                    self.handlers["operation-done"](self, object(), "commit", 0)

        self.Installation, self.Transaction = Installation, Transaction
        self._Remote = Remote

    # the Installation API used by the backend
    def list_installed_refs_by_kind(self, kind, cancellable):
        return list(self.refs)

    def list_installed_refs_for_update(self, cancellable):
        return list(self.updatable)

    def list_remotes(self, cancellable):
        return [self._Remote(name) for name in self.remote_names]

    def add_remote(self, remote, if_needed, cancellable):
        self.added.append(remote)
        self.remote_names.append(remote[1])


def make(**kw):
    fake = FakeFlatpak(**kw)
    module = type("Module", (), {"Installation": fake.Installation, "Transaction": fake.Transaction,
                                 "RefKind": FakeFlatpak.RefKind, "Error": FakeFlatpak.Error,
                                 "Remote": FakeFlatpak.Remote, "get_default_arch": staticmethod(FakeFlatpak.get_default_arch)})
    return backend.Flatpak(flatpak=module), fake


class Listing(unittest.TestCase):
    def test_installed_is_sorted_and_falls_back_to_the_id(self):
        api, _ = make(installed=[Ref("org.b", "B"), Ref("org.a", None, version=None)])
        self.assertEqual(api.installed(), [
            {"id": "org.a", "name": "org.a", "version": "", "origin": "flathub"},
            {"id": "org.b", "name": "B", "version": "1.0", "origin": "flathub"}])

    def test_remotes(self):
        api, _ = make(remotes=("flathub", "other"))
        self.assertEqual([r["name"] for r in api.remotes()], ["flathub", "other"])
        self.assertTrue(api.has_remote())
        self.assertFalse(make(remotes=())[0].has_remote())


class Changing(unittest.TestCase):
    def test_install_reports_monotonic_progress_ending_at_one(self):
        api, fake = make()
        seen = []
        api.install("org.zen.Browser", "flathub", seen.append)
        self.assertEqual(fake.transactions[0].actions, [("install", "flathub", "app/org.zen.Browser/x86_64/stable")])
        self.assertEqual(seen, sorted(seen))
        self.assertEqual(seen[-1], 1.0)
        self.assertTrue(all(0 <= value <= 1 for value in seen))

    def test_install_without_the_remote(self):
        api, _ = make(remotes=())
        with self.assertRaisesRegex(Unavailable, "not configured"):
            api.install("org.x", "flathub", lambda f: None)

    def test_install_when_already_installed(self):
        api, _ = make(installed=[Ref("org.x")])
        with self.assertRaisesRegex(Failed, "already installed"):
            api.install("org.x", "flathub", lambda f: None)

    def test_uninstall_uses_the_installed_ref_and_unknown_is_not_found(self):
        api, fake = make(installed=[Ref("org.x")])
        api.uninstall("org.x", lambda f: None)
        self.assertEqual(fake.transactions[0].actions, [("uninstall", "app/org.x/x86_64/stable")])
        with self.assertRaises(NotFound):
            api.uninstall("org.nope", lambda f: None)

    def test_update_returns_what_was_updated_and_nothing_when_up_to_date(self):
        api, fake = make()
        self.assertEqual(api.update_all(lambda f: None), [])
        self.assertEqual(fake.transactions, [])                        # no transaction for nothing to do
        fake.updatable = [Ref("org.b"), Ref("org.a")]
        self.assertEqual(api.update_all(lambda f: None), ["org.a", "org.b"])

    def test_a_failing_operation_becomes_a_failure_with_its_message(self):
        api, _ = make(fail_with=FakeError("disk full", 99))
        with self.assertRaisesRegex(Failed, "disk full"):
            api.install("org.x", "flathub", lambda f: None)

    def test_flatpak_not_found_errors_map_to_not_found(self):
        api, _ = make(fail_with=FakeError("no such ref", FakeFlatpak.Error.REF_NOT_FOUND))
        with self.assertRaises(NotFound):
            api.install("org.x", "flathub", lambda f: None)

    def test_add_remote_downloads_the_description_and_is_idempotent(self):
        api, fake = make(remotes=())

        class Response(contextlib.AbstractContextManager):
            def read(self):
                return b"[Flatpak Repo]\nUrl=x\n"

            def __exit__(self, *exc):
                return False

        urls = []
        self.assertTrue(api.add_remote(fetch=lambda url, timeout: urls.append(url) or Response()))
        self.assertEqual(urls, [backend.REMOTE_URL])
        self.assertEqual(FakeFlatpak.Remote.created[-1], ("flathub", b"[Flatpak Repo]\nUrl=x\n"))
        self.assertFalse(api.add_remote(fetch=lambda *a, **k: self.fail("downloaded again")))

    def test_add_remote_network_failure(self):
        api, _ = make(remotes=())

        def broken(url, timeout):
            raise OSError("no route")
        with self.assertRaisesRegex(Unavailable, "no route"):
            api.add_remote(fetch=broken)


class CommandLineParts(unittest.TestCase):
    def runner(self, table):
        def run(cmd, timeout=60, input_bytes=None):
            for prefix, result in table.items():
                if tuple(cmd[:len(prefix)]) == prefix:
                    return result
            return 127, "", ""
        return run

    def test_search_skips_headers_and_reports_no_matches(self):
        out = "Application ID\tName\tDescription\tVersion\tRemotes\napp.zen\tZen\tA browser\t1.0\tflathub\n"
        api, _ = make()
        api._runner = self.runner({("flatpak", "search"): (0, out, "")})
        self.assertEqual(api.search("zen"), [{"id": "app.zen", "name": "Zen", "description": "A browser",
                                              "version": "1.0", "remote": "flathub"}])
        api._runner = self.runner({("flatpak", "search"): (0, "No matches found\n", "")})
        self.assertEqual(api.search("zzz"), [])

    def test_search_failure_is_a_failure(self):
        api, _ = make()
        api._runner = self.runner({("flatpak", "search"): (1, "", "network down")})
        with self.assertRaises(Failed):
            api.search("x")

    def test_missing_flatpak_tool(self):
        api, _ = make()
        api._runner = self.runner({})
        with self.assertRaises(Unavailable):
            api.search("x")

    def test_remote_info_keeps_known_keys_and_not_found_maps(self):
        api, _ = make()
        api._runner = self.runner({("flatpak", "remote-info"): (0, "ID: app.zen\nVersion: 1.2\nJunk: x\n", "")})
        self.assertEqual(api.remote_info("flathub", "app.zen"), {"ID": "app.zen", "Version": "1.2"})
        api._runner = self.runner({("flatpak", "remote-info"): (1, "", "error: not found")})
        with self.assertRaises(NotFound):
            api.remote_info("flathub", "app.nope")


class Loading(unittest.TestCase):
    def test_a_missing_typelib_is_unavailable_not_a_crash(self):
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "gi":
                raise ImportError("no gi")
            return real_import(name, *args, **kwargs)
        builtins.__import__ = fake_import
        try:
            with self.assertRaises(Unavailable):
                backend.load_flatpak()
        finally:
            builtins.__import__ = real_import


if __name__ == "__main__":
    unittest.main()
