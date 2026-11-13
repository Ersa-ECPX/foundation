"""The automatic update service (data/libexec/foundation-autoupdate) against a real foundationd on a private bus and a local web server."""

import functools
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

import support  # noqa: E402
from test_foundationd_boot import BootDaemon  # noqa: E402
from test_foundationd_bus import CONFIG, gi  # noqa: E402
from test_foundationd_jobs import MANAGER  # noqa: E402

if gi:
    from gi.repository import GLib

SCRIPT = os.path.join(HERE, "..", "data", "libexec", "foundation-autoupdate")


@unittest.skipUnless(gi, "needs PyGObject")
class AutoUpdate(BootDaemon):
    def setUp(self):
        super().setUp()
        with open(self.system.paths["os_release"], "w") as handle:           # the running system is 0.2.0
            handle.write("ID=opensuse-tumbleweed\nIMAGE_ID=ecp\nIMAGE_VERSION=0.2.0\n")
        self.www = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.www, True)
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=self.www)
        handler.log_message = lambda *a, **k: None
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.set("ota.url", f"http://127.0.0.1:{self.server.server_address[1]}")

    def set(self, key, value):
        kind = "b" if isinstance(value, bool) else "s"
        self.call(CONFIG, "Set", "(sv)", key, GLib.Variant(kind, value))

    def publish(self, version):
        """An update archive (unsigned: no signing key is installed on this fake system) and the feed that names it."""
        path = support.write_payload(self.www, version)
        name = os.path.basename(path)
        with open(os.path.join(self.www, f"latest-{os.uname().machine}.json"), "w") as handle:
            json.dump({"version": version, "file": "usr/share/ersa/payload/" + name, "size": os.path.getsize(path)}, handle)

    def run_service(self):
        env = dict(os.environ, DBUS_SESSION_BUS_ADDRESS=self.address)
        result = subprocess.run([sys.executable, SCRIPT, "--bus", "session"], env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def jobs(self):
        return self.call(MANAGER, "ListJobs")[0]

    def test_a_newer_update_is_announced_and_not_installed_when_automatic_installation_is_off(self):
        self.publish("0.3.0")
        output = self.run_service()
        self.assertIn("version 0.3.0 is available", output)
        self.assertIn("automatic installation is off", output)
        self.assertEqual(self.jobs(), [])
        with open(os.path.join(self.tmp.name, "state", "update-check.json")) as handle:
            self.assertEqual(json.load(handle)["version"], "0.3.0")

    def test_with_automatic_installation_on_the_job_runs_and_an_unsigned_update_is_still_refused(self):
        self.publish("0.3.0")
        self.set("ota.auto_update", True)
        output = self.run_service()
        self.assertIn("installing it", output)
        self.assertIn("was not installed", output)
        self.assertIn("no image signing key", output)           # downloaded, then refused: the signature check cannot be skipped over the air
        (job,) = self.jobs()
        self.assertEqual(self.props(job)["Kind"], "updates.install_latest")
        self.assertEqual(os.listdir(os.path.join(self.tmp.name, "state", "staging")), [], "the download is removed")

    def test_an_update_that_is_not_newer_is_left_alone(self):
        self.publish("0.2.0")
        self.set("ota.auto_update", True)
        self.assertIn("up to date", self.run_service())
        self.assertEqual(self.jobs(), [])

    def test_nothing_happens_when_automatic_checks_are_off(self):
        self.publish("0.3.0")
        self.set("ota.auto_check", False)
        self.assertIn("automatic checks are off", self.run_service())
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "state", "update-check.json")))

    def test_a_source_that_is_not_set_up_is_logged_and_not_an_error(self):
        self.set("ota.url", "https://updates.ersa.invalid/ecp")
        self.assertIn("no update source", self.run_service())


class Notify(unittest.TestCase):
    """data/libexec/foundation-update-notify: what it announces, and that it does not repeat itself."""

    def setUp(self):
        import importlib.machinery
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.module = importlib.machinery.SourceFileLoader("notify", os.path.join(HERE, "..", "data", "libexec", "foundation-update-notify")).load_module()
        m = self.module
        m.PENDING, m.CHECK = os.path.join(self.tmp, "pending.json"), os.path.join(self.tmp, "check.json")
        m.notified_path = lambda: os.path.join(self.tmp, "state", "notified")
        m.running_version = lambda: "0.2.0"
        self.sent = []
        m.notify = lambda title, body: self.sent.append((title, body))

    def write(self, name, data):
        with open(os.path.join(self.tmp, name), "w") as handle:
            json.dump(data, handle)

    def test_an_installed_update_is_announced_as_ready(self):
        self.write("pending.json", {"version": "0.3.0"})
        self.module.main()
        self.assertEqual(self.sent, [("Update ready", "Restart to start using version 0.3.0.")])

    def test_a_pending_record_of_the_running_version_says_nothing(self):
        self.write("pending.json", {"version": "0.2.0"})
        self.module.main()
        self.assertEqual(self.sent, [])

    def test_a_newer_available_update_is_announced_once(self):
        self.write("check.json", {"version": "0.3.0"})
        self.module.main()
        self.module.main()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][0], "Update available")
        self.write("check.json", {"version": "0.4.0"})
        self.module.main()
        self.assertEqual(len(self.sent), 2, "a still newer version is announced again")

    def test_an_older_or_same_version_is_not_announced(self):
        for version in ("0.2.0", "0.1.9", "0.1.10"):
            self.write("check.json", {"version": version})
            self.module.main()
        self.assertEqual(self.sent, [])

    def test_ready_wins_over_available(self):
        self.write("pending.json", {"version": "0.3.0"})
        self.write("check.json", {"version": "0.3.0"})
        self.module.main()
        self.assertEqual([title for title, _ in self.sent], ["Update ready"])


if __name__ == "__main__":
    unittest.main()
