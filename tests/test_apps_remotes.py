"""Application sources (Flatpak remotes) of the system installation: the backend against a fake libflatpak."""

import base64
import contextlib
import io
import os
import sys
import types
import unittest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))

from foundationd.backends import flatpak as backend  # noqa: E402
from foundationd.errors import Failed, InvalidArgument, NotFound, Unavailable  # noqa: E402

KEY = b"\x99\x01binary-key"
REPO_FILE = ("[Flatpak Repo]\nTitle=Acme Apps\nUrl=https://apps.acme.example/repo\nComment=Acme's applications\n"
             "Homepage=https://acme.example\nGPGKey=" + base64.b64encode(KEY).decode() + "\n").encode()
GPG_OUT = "pub:-:255:22:AAAA1111BBBB2222:1700000000:::-:::scESC::::::23::0:\nfpr:::::::::ABCDEF0123456789ABCDEF0123456789ABCDEF01:\nuid:-::::1700000000::HASH::Acme Signing <sign@acme.example>::::::::::0:\n"


def gpg_runner(out=GPG_OUT, code=0):
    calls = []

    def runner(cmd, timeout=60, input_bytes=None):
        calls.append((cmd, input_bytes))
        return code, out, ""
    runner.calls = calls
    return runner


def fetcher(data):
    @contextlib.contextmanager
    def fetch(url, timeout=30):
        fetch.urls.append(url)
        yield io.BytesIO(data)
    fetch.urls = []
    return fetch


class Remote:
    def __init__(self, name, url="https://example/repo", title="", disabled=False):
        self.name, self.url, self.title, self.disabled, self.verify = name, url, title, disabled, True

    def get_name(self):
        return self.name

    def get_url(self):
        return self.url

    def get_title(self):
        return self.title

    def get_disabled(self):
        return self.disabled

    def get_gpg_verify(self):
        return self.verify

    def set_title(self, value):
        self.title = value

    def set_disabled(self, value):
        self.disabled = value

    def set_gpg_verify(self, value):
        self.verify = value


class Ref:
    def __init__(self, name, origin):
        self.name, self.origin = name, origin

    def get_name(self):
        return self.name

    def get_appdata_name(self):
        return self.name

    def get_appdata_version(self):
        return "1"

    def get_origin(self):
        return self.origin


class Fake:
    def __init__(self, remotes=(), refs=()):
        self.remote_objects = list(remotes)
        self.refs = list(refs)
        self.modified, self.removed, self.loaded = [], [], []
        fake = self

        class Installation:
            @staticmethod
            def new_system(cancellable):
                return fake

        class RemoteClass:
            @staticmethod
            def new_from_file(name, data):
                text = bytes(data.get_data()).decode()
                fake.loaded.append((name, text))
                url = next((line.split("=", 1)[1] for line in text.splitlines() if line.startswith("Url=")), "")
                title = next((line.split("=", 1)[1] for line in text.splitlines() if line.startswith("Title=")), "")
                return Remote(name, url, title)

        self.module = types.SimpleNamespace(Installation=Installation, Remote=RemoteClass, RefKind=types.SimpleNamespace(APP="app"),
                                            Error=types.SimpleNamespace(quark=lambda: "q"))

    # Installation API
    def list_remotes(self, cancellable):
        return list(self.remote_objects)

    def list_installed_refs_by_kind(self, kind, cancellable):
        return list(self.refs)

    def add_remote(self, remote, if_needed, cancellable):
        self.remote_objects.append(remote)

    def modify_remote(self, remote, cancellable):
        self.modified.append(remote.name)

    def remove_remote(self, name, cancellable):
        self.removed.append(name)
        self.remote_objects = [r for r in self.remote_objects if r.name != name]

    def api(self):
        return backend.Flatpak(flatpak=self.module)


class Parsing(unittest.TestCase):
    def test_a_repository_description(self):
        info = backend.parse_flatpakrepo(REPO_FILE)
        self.assertEqual((info["title"], info["url"], info["homepage"]), ("Acme Apps", "https://apps.acme.example/repo", "https://acme.example"))
        self.assertEqual(info["gpg_key"], KEY)

    def test_a_key_split_over_lines_is_read(self):
        text = base64.b64encode(KEY * 5).decode()
        data = f"[Flatpak Repo]\nUrl=https://x/repo\nGPGKey={text[:20]}\n {text[20:]}\n".encode()
        self.assertEqual(backend.parse_flatpakrepo(data)["gpg_key"], KEY * 5)

    def test_what_is_not_a_repository_description_is_refused(self):
        for data in (b"just text", b"[Other]\nUrl=x\n", b"\xff\xfe", b"[Flatpak Repo]\nUrl=https://x\nGPGKey=!!!\n"):
            with self.assertRaises(InvalidArgument, msg=data):
                backend.parse_flatpakrepo(data)

    def test_names_are_suggested_from_the_address(self):
        self.assertEqual(backend.suggested_name("https://dl.acme.example/acme.flatpakrepo"), "acme")
        self.assertEqual(backend.suggested_name("https://x/Some Odd!!.flatpakrepo"), "Some-Odd")
        self.assertEqual(backend.suggested_name("file:///"), "remote")


class Inspecting(unittest.TestCase):
    def test_a_repository_file_gives_title_address_and_the_key_fingerprint(self):
        fetch, runner = fetcher(REPO_FILE), gpg_runner()
        info, repo, key = backend.inspect_remote("https://acme.example/acme.flatpakrepo", "", fetch, runner)
        self.assertEqual(fetch.urls, ["https://acme.example/acme.flatpakrepo"])
        self.assertEqual((info["name"], info["title"], info["url"]), ("acme", "Acme Apps", "https://apps.acme.example/repo"))
        self.assertEqual(info["fingerprints"], ["ABCDEF0123456789ABCDEF0123456789ABCDEF01"])
        self.assertEqual(info["owners"], ["Acme Signing <sign@acme.example>"])
        self.assertTrue(info["from_file"])
        self.assertEqual((repo, key), (REPO_FILE, KEY))
        self.assertEqual(runner.calls[0][1], KEY)

    def test_an_unsigned_repository_file_is_refused(self):
        data = b"[Flatpak Repo]\nTitle=T\nUrl=https://x/repo\n"
        with self.assertRaisesRegex(InvalidArgument, "must be signed"):
            backend.inspect_remote("https://x/t.flatpakrepo", "", fetcher(data), gpg_runner())

    def test_a_plain_address_needs_its_key(self):
        with self.assertRaisesRegex(InvalidArgument, "must be signed"):
            backend.inspect_remote("https://x/repo", "", None, gpg_runner())
        info, repo, _key = backend.inspect_remote("https://x/repo", "-----BEGIN PGP PUBLIC KEY BLOCK-----\nabc\n", None, gpg_runner())
        self.assertEqual((info["url"], info["from_file"], repo), ("https://x/repo", False, b""))

    def test_only_https_and_local_files(self):
        for bad in ("http://x/a.flatpakrepo", "ftp://x/repo", "https://x/a b", "/etc/passwd", ""):
            with self.assertRaises(InvalidArgument, msg=bad):
                backend.inspect_remote(bad, "", fetcher(REPO_FILE), gpg_runner())

    def test_a_repository_file_that_points_at_plain_http_is_refused(self):
        data = REPO_FILE.replace(b"https://apps.acme.example/repo", b"http://apps.acme.example/repo")
        with self.assertRaises(InvalidArgument):
            backend.inspect_remote("https://acme.example/acme.flatpakrepo", "", fetcher(data), gpg_runner())

    def test_a_private_key_is_refused(self):
        with self.assertRaisesRegex(InvalidArgument, "private"):
            backend.inspect_remote("https://x/a.flatpakrepo", "", fetcher(REPO_FILE), gpg_runner("sec:u:255:22:AAAA:1700000000::::::\n"))
        with self.assertRaisesRegex(InvalidArgument, "private"):
            backend.key_summary(b"-----BEGIN PGP PRIVATE KEY BLOCK-----")

    def test_an_unreachable_address_is_unavailable(self):
        def fetch(url, timeout=30):
            raise OSError("no route")
        with self.assertRaises(Unavailable):
            backend.inspect_remote("https://x/a.flatpakrepo", "", fetch, gpg_runner())

    def test_a_local_file_can_be_read(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".flatpakrepo") as handle:
            handle.write(REPO_FILE)
            handle.flush()
            info, _r, _k = backend.inspect_remote("file://" + handle.name, "", None, gpg_runner())
        self.assertEqual(info["title"], "Acme Apps")


class Adding(unittest.TestCase):
    def add(self, fake, name="acme", location="https://acme.example/acme.flatpakrepo", key="", title="", data=REPO_FILE):
        return fake.api().add_custom_remote(name, location, key, title, fetch=fetcher(data), runner=gpg_runner())

    def test_a_repository_file_becomes_a_signed_enabled_source(self):
        fake = Fake([Remote("flathub")])
        result = self.add(fake)
        self.assertEqual(result["name"], "acme")
        added = fake.remote_objects[-1]
        self.assertEqual((added.name, added.url, added.verify, added.disabled), ("acme", "https://apps.acme.example/repo", True, False))
        self.assertEqual(fake.loaded[0][1].encode(), REPO_FILE)          # libflatpak reads the file as published

    def test_a_title_overrides_the_published_one(self):
        fake = Fake()
        self.add(fake, title="Work apps")
        self.assertEqual(fake.remote_objects[-1].title, "Work apps")

    def test_a_plain_address_with_a_key_is_wrapped_in_a_description(self):
        fake = Fake()
        armoured = "-----BEGIN PGP PUBLIC KEY BLOCK-----\nabc\n-----END PGP PUBLIC KEY BLOCK-----\n"
        dearmor = lambda data: types.SimpleNamespace(returncode=0, stdout=b"binary-from-armour")      # noqa: E731
        fake.api().add_custom_remote("mirror", "https://mirror.example/repo", armoured, "Mirror", fetch=None, runner=gpg_runner(), dearmor=dearmor)
        text = fake.loaded[0][1]
        self.assertIn("Url=https://mirror.example/repo", text)
        self.assertIn("GPGKey=" + base64.b64encode(b"binary-from-armour").decode(), text)
        self.assertEqual(fake.remote_objects[-1].title, "Mirror")

    def test_bad_and_taken_names_are_refused(self):
        fake = Fake([Remote("flathub")])
        for name in ("", "-x", "a b", "../x", "x" * 64):
            with self.assertRaises(InvalidArgument, msg=name):
                self.add(fake, name=name)
        with self.assertRaisesRegex(InvalidArgument, "exists already"):
            self.add(fake, name="flathub")

    def test_nothing_is_added_when_the_source_is_unsigned(self):
        fake = Fake()
        with self.assertRaises(InvalidArgument):
            self.add(fake, data=b"[Flatpak Repo]\nUrl=https://x/repo\n")
        self.assertEqual(fake.remote_objects, [])

    def test_a_library_failure_is_reported(self):
        fake = Fake()

        def boom(remote, if_needed, cancellable):
            raise RuntimeError("disk full")
        fake.add_remote = boom
        with self.assertRaisesRegex(Failed, "disk full"):
            self.add(fake)


class Changing(unittest.TestCase):
    def test_details_list_the_default_first_with_their_apps(self):
        fake = Fake([Remote("zeta"), Remote("flathub", "https://dl.flathub.org/repo", "Flathub"), Remote("acme", disabled=True, title="Acme")],
                    [Ref("a.b.c", "flathub"), Ref("d.e.f", "flathub"), Ref("g.h.i", "acme")])
        rows = fake.api().remote_details()
        self.assertEqual([r["name"] for r in rows], ["flathub", "acme", "zeta"])
        self.assertEqual((rows[0]["default"], rows[0]["apps"], rows[0]["title"]), (True, 2, "Flathub"))
        self.assertEqual((rows[1]["enabled"], rows[1]["apps"]), (False, 1))
        self.assertEqual(rows[2]["title"], "zeta")                        # no title: the name

    def test_a_source_is_turned_off_and_on(self):
        fake = Fake([Remote("acme")])
        self.assertEqual(fake.api().set_remote_enabled("acme", False), {"name": "acme", "enabled": False})
        self.assertTrue(fake.remote_objects[0].disabled)
        fake.api().set_remote_enabled("acme", True)
        self.assertFalse(fake.remote_objects[0].disabled)
        self.assertEqual(fake.modified, ["acme", "acme"])

    def test_an_unknown_source_is_not_found(self):
        with self.assertRaises(NotFound):
            Fake().api().set_remote_enabled("nope", True)
        with self.assertRaises(NotFound):
            Fake().api().remove_remote("nope")

    def test_a_source_with_installed_applications_is_not_removed(self):
        fake = Fake([Remote("acme")], [Ref("a.b.c", "acme")])
        with self.assertRaisesRegex(Failed, "a.b.c"):
            fake.api().remove_remote("acme")
        self.assertEqual(fake.removed, [])

    def test_an_unused_source_is_removed(self):
        fake = Fake([Remote("acme")])
        self.assertEqual(fake.api().remove_remote("acme"), {"name": "acme"})
        self.assertEqual(fake.removed, ["acme"])


if __name__ == "__main__":
    unittest.main()
