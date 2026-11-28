"""Reporting when the network comes up: the NetworkManager hook, the script it starts, and the timing rules."""

import os
import stat
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

import support  # noqa: E402,F401
from test_foundationd_boot import BootDaemon  # noqa: E402
from test_foundationd_bus import gi  # noqa: E402

from foundationd.backends import devices  # noqa: E402
from foundationd.errors import Unavailable  # noqa: E402

DISPATCHER = os.path.join(HERE, "..", "data", "networkmanager", "90-foundation-devices")
SCRIPT = os.path.join(HERE, "..", "data", "libexec", "foundation-devices-online")


class Hook(unittest.TestCase):
    """The hook decides only whether to start the unit: right events, linked computers, and it returns at once."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.log = os.path.join(self.dir.name, "calls")
        self.systemctl = os.path.join(self.dir.name, "systemctl")
        with open(self.systemctl, "w") as handle:
            handle.write(f'#!/bin/sh\necho "$@" >> {self.log}\n')
        os.chmod(self.systemctl, os.stat(self.systemctl).st_mode | stat.S_IXUSR)
        self.link = os.path.join(self.dir.name, "link.json")

    def run_hook(self, action, connectivity=None, linked=True):
        if linked:
            open(self.link, "w").close()
        elif os.path.exists(self.link):
            os.unlink(self.link)
        env = {"PATH": "/usr/bin:/bin", "ECP_LINK_FILE": self.link, "ECP_SYSTEMCTL": self.systemctl, "NM_DISPATCHER_ACTION": action}
        if connectivity:
            env["CONNECTIVITY_STATE"] = connectivity
        result = subprocess.run(["sh", DISPATCHER, "eth0", action], env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return open(self.log).read().split("\n")[:-1] if os.path.exists(self.log) else []

    def test_a_connection_coming_up_starts_the_unit(self):
        self.assertEqual(self.run_hook("up"), ["--no-block start foundation-devices-online.service"])

    def test_the_internet_becoming_reachable_starts_it_but_a_portal_does_not(self):
        self.assertEqual(len(self.run_hook("connectivity-change", "FULL")), 1)
        os.unlink(self.log)
        for state in ("PORTAL", "LIMITED", "NONE", "UNKNOWN"):
            self.assertEqual(self.run_hook("connectivity-change", state), [], state)

    def test_other_events_do_nothing(self):
        for action in ("down", "hostname", "vpn-up", "dhcp4-change", "pre-up", "dns-change"):
            self.assertEqual(self.run_hook(action), [], action)

    def test_a_computer_that_is_not_linked_starts_nothing(self):
        self.assertEqual(self.run_hook("up", linked=False), [])
        self.assertEqual(self.run_hook("connectivity-change", "FULL", linked=False), [])

    def test_it_is_a_plain_executable_shell_script(self):
        self.assertTrue(os.access(DISPATCHER, os.X_OK))
        self.assertEqual(subprocess.run(["sh", "-n", DISPATCHER]).returncode, 0)

    def test_the_unit_only_starts_on_a_linked_computer_and_never_on_the_installer_medium(self):
        unit = open(os.path.join(HERE, "..", "data", "systemd", "foundation-devices-online.service")).read()
        self.assertIn("ConditionPathExists=/var/lib/foundation/device/link.json", unit)
        self.assertIn("ConditionPathExists=!/usr/share/ersa/payload", unit)
        self.assertIn("ExecStart=/usr/libexec/foundation-devices-online", unit)


class Timing(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        release = os.path.join(self.dir.name, "os-release")
        open(release, "w").write('IMAGE_VERSION="1.0.2"\n')
        self.paths = {"state": self.dir.name, "os_release": release, "efivars": os.path.join(self.dir.name, "efi"),
                      "mountinfo": os.path.join(self.dir.name, "mountinfo"), "sysfs": self.dir.name, "dev": self.dir.name,
                      "tpm_class": os.path.join(self.dir.name, "none"), "tpmrm": os.path.join(self.dir.name, "none")}
        open(self.paths["mountinfo"], "w").close()
        runner = lambda cmd, timeout=60, input_bytes=None, env=None: (127, "", "")
        self.runner = runner
        devices.link(self.paths, "dev1", "https://id.example", runner)
        self.sent = []
        self.answer = (200, "", "")

    def sender(self, url, headers, data):
        self.sent.append(url)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    def go(self, at, **kwargs):
        return devices.checkin_if_due(self.paths, True, now=lambda: at, sender=self.sender, runner=self.runner, **kwargs)

    def test_the_scheduled_report_comes_with_every_run_of_the_six_hourly_update_check(self):
        """The timer runs about six hours apart (a little early at worst), and the interval is shorter than that: no run is skipped."""
        self.assertLess(devices.SCHEDULED_INTERVAL, 6 * 3600)
        self.assertGreater(devices.SCHEDULED_INTERVAL, 4 * 3600)
        self.assertEqual(self.go(0), "sent")
        self.assertEqual(self.go(5 * 3600 + 120), "sent")                    # the next run, 5 h 2 min later
        self.assertEqual(self.go(5 * 3600 + 300), "not due")

    def test_the_online_report_waits_only_fifteen_minutes(self):
        online = dict(interval=devices.ONLINE_GAP, quiet_unreachable=True)
        self.assertEqual(self.go(0, **online), "sent")
        self.assertEqual(self.go(600, **online), "not due")                  # a flapping connection cannot spam the account
        self.assertEqual(self.go(901, **online), "sent")

    def test_a_service_that_cannot_be_reached_yet_does_not_use_up_the_gap(self):
        """The name servers may not answer in the first second: the next network event must be able to try again."""
        online = dict(interval=devices.ONLINE_GAP, quiet_unreachable=True)
        self.answer = (0, "", "")
        with self.assertRaises(Unavailable):
            self.go(0, **online)
        self.assertEqual(devices.last(self.paths), {})
        self.answer = (200, "", "")
        self.assertEqual(self.go(5, **online), "sent")                       # the very next event

    def test_the_scheduled_report_still_backs_off_after_a_failure(self):
        self.answer = (0, "", "")
        with self.assertRaises(Unavailable):
            self.go(0)
        self.assertNotEqual(devices.last(self.paths), {})
        self.assertEqual(self.go(600), "not due")

    def test_other_failures_count_even_when_online(self):
        online = dict(interval=devices.ONLINE_GAP, quiet_unreachable=True)
        self.answer = (409, "device_replayed", "")
        with self.assertRaises(Exception):
            self.go(0, **online)
        self.assertNotEqual(devices.last(self.paths), {})


@unittest.skipUnless(gi, "needs PyGObject")
class Script(BootDaemon):
    def run_script(self):
        env = dict(os.environ, DBUS_SESSION_BUS_ADDRESS=self.address)
        result = subprocess.run([sys.executable, SCRIPT, "--bus", "session"], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_on_a_computer_that_is_not_linked_it_says_so_and_succeeds(self):
        self.assertEqual(self.run_script(), "not linked")

    def test_it_never_fails_the_unit(self):
        self.daemon.terminate()
        self.daemon.wait(5)
        env = dict(os.environ, DBUS_SESSION_BUS_ADDRESS="unix:path=/nonexistent/bus")
        result = subprocess.run([sys.executable, SCRIPT, "--bus", "session"], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0)
        self.assertIn("cannot report", result.stdout)


if __name__ == "__main__":
    unittest.main()
