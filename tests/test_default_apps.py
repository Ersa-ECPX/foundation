"""foundation-default-apps: the applications a computer starts with, offered once (the logic, with a fake foundationd)."""

import importlib.machinery
import importlib.util
import json
import os
import tempfile
import unittest

import support  # noqa: F401

HERE = os.path.dirname(__file__)
SCRIPT = os.path.join(HERE, "..", "data", "libexec", "foundation-default-apps")
BAZAAR = "io.github.kolunmi.Bazaar"


def load():
    loader = importlib.machinery.SourceFileLoader("default_apps", SCRIPT)
    spec = importlib.util.spec_from_loader("default_apps", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class Fake:
    """Stands in for foundationd: the setting, what is installed, and what a job ends with."""

    def __init__(self, setting=BAZAAR, installed=(), remote_ok=True, install_ok=True):
        self.setting, self.installed, self.remote_ok, self.install_ok = setting, list(installed), remote_ok, install_ok
        self.calls, self.jobs = [], {}

    def call(self, interface, method, signature=None, args=()):
        self.calls.append((method, args))
        if method == "Get":
            return (self.setting,)
        if method == "ListInstalled":
            return ([{"id": i} for i in self.installed],)
        job = f"/job/{len(self.jobs)}"
        self.jobs[job] = self.remote_ok if method == "SetupRemote" else self.install_ok
        if method == "Install" and self.install_ok:
            self.installed.append(args[0])
        return (job,)

    def wait(self, job):
        ok = self.jobs[job]
        return ok, "" if ok else "network is unreachable"

    def methods(self):
        return [m for m, _a in self.calls]


class DefaultApps(unittest.TestCase):
    def setUp(self):
        self.module = load()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = os.path.join(self.tmp.name, "state", "default-apps.json")

    def run_it(self, fake):
        return self.module.run(fake.call, fake.wait, self.state, lambda *_a: None)

    def offered(self):
        return json.load(open(self.state))["offered"] if os.path.exists(self.state) else []

    def test_the_app_store_is_the_default_setting(self):
        with open(os.path.join(HERE, "..", "data", "config", "schema.d", "10-foundation.json")) as handle:
            self.assertEqual(json.load(handle)["apps.default_apps"]["default"], BAZAAR)

    def test_a_missing_default_app_gets_the_source_and_is_installed_and_recorded(self):
        fake = Fake()
        self.assertEqual(self.run_it(fake), 0)
        self.assertEqual(fake.methods(), ["Get", "ListInstalled", "SetupRemote", "Install"])
        self.assertEqual(fake.calls[-1], ("Install", (BAZAAR,)))
        self.assertEqual(self.offered(), [BAZAAR])

    def test_an_app_that_is_already_there_is_only_recorded(self):
        fake = Fake(installed=[BAZAAR])
        self.assertEqual(self.run_it(fake), 0)
        self.assertNotIn("Install", fake.methods())
        self.assertEqual(self.offered(), [BAZAAR])

    def test_an_app_the_person_removed_is_not_installed_again(self):
        self.run_it(Fake())
        fake = Fake()                                  # removed since: not installed, but already offered
        self.assertEqual(self.run_it(fake), 0)
        self.assertEqual(fake.methods(), ["Get"])

    def test_no_network_fails_so_that_the_service_tries_again_and_records_nothing(self):
        fake = Fake(remote_ok=False)
        self.assertEqual(self.run_it(fake), 1)
        self.assertNotIn("Install", fake.methods())
        self.assertEqual(self.offered(), [])

    def test_a_failed_install_is_not_recorded(self):
        fake = Fake(install_ok=False)
        self.assertEqual(self.run_it(fake), 1)
        self.assertEqual(self.offered(), [])

    def test_an_empty_setting_does_nothing(self):
        fake = Fake(setting="")
        self.assertEqual(self.run_it(fake), 0)
        self.assertEqual(fake.methods(), ["Get"])

    def test_several_ids_and_a_bad_one(self):
        fake = Fake(setting=f"{BAZAAR}, org.gnome.Calculator ; rm -rf /")
        self.assertEqual(self.run_it(fake), 0)
        installs = [a[0] for m, a in fake.calls if m == "Install"]
        self.assertEqual(installs, [BAZAAR, "org.gnome.Calculator"])        # the bad words are not application ids and are ignored

    def test_ids_are_split_on_spaces_and_commas(self):
        self.assertEqual(self.module.ids_of("a.b.c, d.e.f  g.h.i"), ["a.b.c", "d.e.f", "g.h.i"])
        self.assertEqual(self.module.ids_of(""), [])


if __name__ == "__main__":
    unittest.main()
