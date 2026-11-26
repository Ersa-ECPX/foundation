"""Preseed files for unattended installations: parsing, the seed they produce, and the daemon's refusal of a bad one."""

import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

import support  # noqa: E402,F401
from foundationd import schema as schema_module  # noqa: E402
from foundationd.backends import deploy, preseed  # noqa: E402
from foundationd.errors import InvalidArgument  # noqa: E402
from test_foundationd_bus import BusCase, gi  # noqa: E402

SCHEMA = schema_module.Schema.load(os.path.join(HERE, "..", "data", "config", "schema.d"))
DEPLOY = "org.ersa.Foundation1.Deploy"


def zones():
    directory = tempfile.mkdtemp()
    os.makedirs(os.path.join(directory, "Europe"))
    open(os.path.join(directory, "Europe", "Berlin"), "w").close()
    open(os.path.join(directory, "UTC"), "w").close()
    return directory


class Parsing(unittest.TestCase):
    def parse(self, text):
        return preseed.parse(text, SCHEMA, zones())

    def test_installer_timezone_url_is_checked_and_not_written_to_the_system(self):
        self.assertEqual(self.parse("installer.timezone_url=https://tz.example/z\n")["config"], {})
        self.assertEqual(self.parse("installer.timezone_url=\n")["config"], {})
        with self.assertRaises(InvalidArgument):
            self.parse("installer.timezone_url=http://plain.example\n")

    def test_settings_language_timezone_and_hostname(self):
        result = self.parse("""
            # an office laptop
            ota.url = https://updates.example.org/ecp
            ota.auto_update = yes
            apps.default_apps = io.github.kolunmi.Bazaar org.mozilla.firefox
            deploy.language = de_DE.UTF-8
            deploy.timezone = Europe/Berlin
            deploy.hostname = laptop-042
        """)
        self.assertEqual(result["config"], {"ota.url": "https://updates.example.org/ecp", "ota.auto_update": "true",
                                            "apps.default_apps": "io.github.kolunmi.Bazaar org.mozilla.firefox"})
        self.assertEqual((result["language"], result["timezone"], result["hostname"]), ("de_DE.UTF-8", "Europe/Berlin", "laptop-042"))

    def test_an_empty_file_sets_nothing(self):
        self.assertEqual(self.parse("# nothing\n"), {"config": {}, "language": "", "timezone": "", "hostname": ""})

    def test_every_problem_is_reported_together(self):
        with self.assertRaises(InvalidArgument) as caught:
            self.parse("nope.key=1\nota.auto_update=maybe\nota.channel=beta\ndeploy.timezone=Mars/Base\ndeploy.hostname=Bad_Name\n"
                       "deploy.language=!!\nota.url=https://a\nota.url=https://b\n")
        message = str(caught.exception)
        for part in ("unknown setting: nope.key", "ota.auto_update", "ota.channel", "Mars/Base", "Bad_Name", "deploy.language",
                     "ota.url is given twice"):
            self.assertIn(part, message)

    def test_a_setting_that_is_not_writable_is_refused(self):
        read_only = schema_module.Schema({"x.fixed": schema_module.Key("x.fixed", "string", "v", writable=False)})
        with self.assertRaisesRegex(InvalidArgument, "read-only"):
            preseed.parse("x.fixed=w\n", read_only)

    def test_too_large_or_binary_input_is_refused(self):
        with self.assertRaises(InvalidArgument):
            self.parse("a=" + "x" * (preseed.MAX_PRESEED + 1))
        with self.assertRaises(InvalidArgument):
            self.parse("ota.url=https://a\0b")

    def test_the_written_file_lists_the_settings_sorted(self):
        text = preseed.config_text({"ota.url": "https://a", "apps.default_apps": ""})
        self.assertLess(text.index("apps.default_apps="), text.index("ota.url="))
        self.assertTrue(text.endswith("\n"))

    def test_a_host_name_may_not_start_or_end_with_a_dash(self):
        for bad in ("-a", "a-", "A", "a_b", "a" * 64, "a.b"):
            with self.assertRaises(InvalidArgument, msg=bad):
                self.parse(f"deploy.hostname={bad}\n")
        self.assertEqual(self.parse("deploy.hostname=a\n")["hostname"], "a")


class Seed(unittest.TestCase):
    def seed(self, **kw):
        stage = tempfile.mkdtemp()
        seed = deploy._make_seed(stage, zoneinfo=zones(), **kw)
        return os.path.join(seed, "var/lib/ecp/etc/upper")

    def test_the_files_land_in_the_etc_overlay(self):
        upper = self.seed(preseed={"config": {"ota.url": "https://a"}, "hostname": "laptop-1"})
        with open(os.path.join(upper, "hostname")) as handle:
            self.assertEqual(handle.read(), "laptop-1\n")
        path = os.path.join(upper, "foundation", "foundation.conf.d", "50-preseed.conf")
        with open(path) as handle:
            self.assertIn("ota.url=https://a", handle.read())
        self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o644")

    def test_without_a_preseed_nothing_extra_is_written(self):
        upper = self.seed()
        self.assertEqual(sorted(os.listdir(upper)), [])
        self.assertEqual(sorted(os.listdir(self.seed(preseed={"config": {}, "hostname": ""}))), [])

    def test_language_and_timezone_still_work(self):
        upper = self.seed(language="de_DE.UTF-8", timezone="UTC")
        self.assertEqual(sorted(os.listdir(upper)), ["locale.conf", "localtime"])

    def test_a_preseed_does_not_disturb_the_api_file_name(self):
        self.assertLess(preseed.CONFIG_FILE, "90-api.conf")             # the API's file sorts after it and wins


@unittest.skipUnless(gi, "needs PyGObject")
class Daemon(BusCase):
    def test_check_preseed_returns_what_would_be_set(self):
        (result,) = self.call(DEPLOY, "CheckPreseed", "(s)", "ota.channel=testing\ndeploy.hostname=box\n")
        self.assertEqual(result["config"], {"ota.channel": "testing"})
        self.assertEqual((result["hostname"], result["language"]), ("box", ""))

    def test_a_bad_preseed_is_refused_with_every_problem(self):
        name = self.error_of(DEPLOY, "CheckPreseed", "(s)", "nope=1\nota.channel=beta\n")
        self.assertEqual(name, "org.ersa.Foundation1.Error.InvalidArgument")

    def test_a_bad_preseed_stops_the_install_before_any_disk_is_touched(self):
        name = self.error_of(DEPLOY, "DeployPreseeded", "(sssss)", "/dev/sdz", "/dev/sdz", "", "", "nope=1\n")
        self.assertEqual(name, "org.ersa.Foundation1.Error.InvalidArgument")


if __name__ == "__main__":
    unittest.main()
