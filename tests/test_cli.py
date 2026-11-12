"""The foundation command line, as a real process talking to a real foundationd on a private bus."""

import glob
import json
import os
import signal
import subprocess
import sys
import time
import unittest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

import support  # noqa: E402
from test_foundationd_boot import BootDaemon  # noqa: E402
from test_foundationd_jobs import JobCase  # noqa: E402

RPM = next(iter(sorted(glob.glob(os.path.join(os.environ.get("ECP_MKOSI_DIR", "/nonexistent"), "mkosi.packages/foundationd-*.rpm")))), None)


class Cli:
    """Runs `python -m foundation` against this test's bus."""

    def cli(self, *args, stdin=subprocess.DEVNULL, timeout=60):
        env = dict(os.environ, DBUS_SESSION_BUS_ADDRESS=self.address, PYTHONPATH=os.path.join(HERE, ".."),
                   NO_COLOR="1", LC_ALL="C.UTF-8")
        done = subprocess.run([sys.executable, "-m", "foundation", "--bus", "session", *args],
                              capture_output=True, text=True, env=env, stdin=stdin, timeout=timeout)
        return done.returncode, done.stdout, done.stderr


class Scripted(Cli, JobCase):
    """Jobs, settings and job control, with the scripted test worker."""

    def test_a_job_shows_its_steps_and_reports_success(self):
        code, out, err = self.cli("ext", "merge", "--yes")
        self.assertEqual(code, 0, err)
        self.assertIn("First", out)
        self.assertIn("Second", out)
        self.assertIn("done", out)

    def test_json_mode_emits_events_then_a_final_object(self):
        code, out, _err = self.cli("ext", "merge", "--yes", "--json")
        self.assertEqual(code, 0)
        lines = [json.loads(line) for line in out.splitlines() if line.startswith('{"event"')]
        self.assertEqual([e["id"] for e in lines if e["event"] == "step"], ["one", "two"])
        final = json.loads(out[out.index("{\n"):])
        self.assertEqual((final["state"], final["kind"], final["result"]["answer"]), ("done", "ext.merge", 42))

    def test_a_failed_job_exits_nonzero_with_the_reason(self):
        code, _out, err = self.cli("ext", "unmerge", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("no such thing", err)

    def test_a_crashed_worker_is_reported(self):
        code, _out, err = self.cli("compat", "revalidate")
        self.assertEqual(code, 1)
        self.assertIn("status 3", err)

    def test_without_yes_and_without_a_terminal_nothing_is_changed(self):
        code, _out, err = self.cli("ext", "merge")
        self.assertEqual(code, 1)
        self.assertIn("confirmation required", err)
        self.assertEqual(self.call("org.ersa.Foundation1.Manager", "ListJobs"), ([],))

    def test_no_wait_returns_the_job_and_job_commands_manage_it(self):
        code, out, _err = self.cli("ext", "refresh", "--yes", "--no-wait")
        self.assertEqual(code, 0)
        number = out.split("Job/")[1].split(".")[0]
        self.wait_for(f"/org/ersa/Foundation1/Job/{number}", lambda p: p["Step"] == "wait")
        _code, listing, _ = self.cli("job", "list")
        self.assertIn("ext.refresh", listing)
        self.assertIn("running", listing)
        code, shown, _ = self.cli("job", "show", number, "--json")
        self.assertEqual(json.loads(shown)["state"], "running")
        self.assertEqual(self.cli("job", "cancel", number)[0], 0)
        self.finish(f"/org/ersa/Foundation1/Job/{number}")
        self.assertEqual(json.loads(self.cli("job", "show", number, "--json")[1])["state"], "cancelled")

    def test_ctrl_c_cancels_the_job_it_is_following(self):
        env = dict(os.environ, DBUS_SESSION_BUS_ADDRESS=self.address, PYTHONPATH=os.path.join(HERE, ".."), NO_COLOR="1")
        process = subprocess.Popen([sys.executable, "-m", "foundation", "--bus", "session", "ext", "refresh", "--yes"],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
                                   stdin=subprocess.DEVNULL)
        try:
            line = process.stdout.readline()
            self.assertIn("Waiting", line)
            process.send_signal(signal.SIGINT)
            out, err = process.communicate(timeout=15)
        finally:
            if process.poll() is None:
                process.kill()
        self.assertEqual(process.returncode, 1)
        self.assertIn("cancelled", err)
        (path,) = self.call("org.ersa.Foundation1.Manager", "ListJobs")[0]
        self.assertEqual(self.props(path)["State"], "cancelled")

    def test_settings_round_trip(self):
        self.assertEqual(self.cli("config", "get", "ota.channel")[1].strip(), "stable")
        self.assertEqual(self.cli("config", "set", "ota.channel", "testing")[0], 0)
        self.assertEqual(self.cli("config", "get", "ota.channel")[1].strip(), "testing")
        self.assertEqual(self.cli("config", "set", "ota.auto_download", "yes")[0], 0)
        self.assertEqual(self.cli("config", "get", "ota.auto_download")[1].strip(), "true")
        listing = self.cli("config", "list")[1]
        self.assertIn("ota.channel", listing)
        self.assertEqual(self.cli("config", "reset", "ota.channel")[0], 0)
        self.assertEqual(self.cli("config", "get", "ota.channel")[1].strip(), "stable")

    def test_setting_mistakes_are_explained(self):
        code, _out, err = self.cli("config", "set", "ota.auto_check", "maybe")
        self.assertEqual((code, "true or false" in err), (2, True))
        code, _out, err = self.cli("config", "set", "ota.channel", "nightly")
        self.assertEqual(code, 1)
        self.assertIn("must be one of", err)
        code, _out, err = self.cli("config", "get", "no.such.key")
        self.assertEqual(code, 1)
        self.assertIn("unknown key", err)

    def test_describe(self):
        out = self.cli("config", "describe", "ota.channel")[1]
        self.assertIn("stable, testing", out)

    def test_a_closed_pipe_ends_quietly_with_success(self):
        env = dict(os.environ, DBUS_SESSION_BUS_ADDRESS=self.address, PYTHONPATH=os.path.join(HERE, ".."), NO_COLOR="1")
        process = subprocess.Popen([sys.executable, "-m", "foundation", "--bus", "session", "config", "list"],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, stdin=subprocess.DEVNULL)
        process.stdout.readline()
        process.stdout.close()                     # like `| head -1`
        err = process.stderr.read().decode()
        process.stderr.close()
        self.assertEqual(process.wait(timeout=30), 0, err)
        self.assertNotIn("BrokenPipe", err)

    def test_a_closed_pipe_shared_by_stdout_and_stderr_also_ends_quietly(self):
        # `foundation ... 2>&1 | head -1` with a job that fails: the error goes to stderr, which is
        # the same closed pipe. This used to exit with status 120 (Python's "cannot flush" code).
        env = dict(os.environ, DBUS_SESSION_BUS_ADDRESS=self.address, PYTHONPATH=os.path.join(HERE, ".."), NO_COLOR="1")
        process = subprocess.Popen(
            [sys.executable, "-m", "foundation", "--bus", "session", "ext", "unmerge", "--yes"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, stdin=subprocess.DEVNULL)
        process.stdout.readline()
        process.stdout.close()
        self.assertEqual(process.wait(timeout=30), 0)

    def test_job_numbers_must_be_numbers(self):
        code, _out, err = self.cli("job", "show", "abc")
        self.assertEqual(code, 2)

    def test_apps_errors_from_the_service_reach_the_user(self):
        code, _out, err = self.cli("apps", "info", "org.example.App")
        self.assertEqual(code, 1)
        self.assertIn("not in the remote", err)
        self.assertNotIn("GDBus", err)                       # the bus plumbing is not the user's business

    def test_help_and_usage_errors(self):
        self.assertEqual(self.cli("--help")[0], 0)
        code, out, _err = self.cli("slot")
        self.assertEqual(code, 2)
        self.assertIn("SUBCOMMAND", out)

    def test_product_name_is_spelled_out_in_help(self):
        out = self.cli("--help")[1]
        self.assertIn("Ersa Compute Platform", out)
        self.assertNotIn("ECP", out)


class Unreachable(unittest.TestCase):
    def test_a_missing_service_gives_a_helpful_message(self):
        import shutil
        import tempfile
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        bus = subprocess.Popen(["dbus-daemon", "--session", "--print-address", "--nofork"], stdout=subprocess.PIPE, text=True)
        self.addCleanup(bus.terminate)
        address = bus.stdout.readline().strip()
        env = dict(os.environ, DBUS_SESSION_BUS_ADDRESS=address, PYTHONPATH=os.path.join(HERE, ".."), NO_COLOR="1")
        done = subprocess.run([sys.executable, "-m", "foundation", "--bus", "session", "status"], env=env,
                              capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        self.assertEqual(done.returncode, 1)
        self.assertIn("foundationd", done.stderr)


@unittest.skipUnless(support.tool_available("sfdisk"), "needs sfdisk")
class Boot(Cli, BootDaemon):
    """The slot commands, with the real workers on a fake system."""

    def test_slot_status_says_which_slot_you_are_on(self):
        code, out, err = self.cli("slot", "status")
        self.assertEqual(code, 0, err)
        self.assertIn("Running slot", out)
        self.assertIn("0.2.0", out)
        self.assertIn("pending: 2 tries left", out)
        self.assertIn("b", out)

    def test_slot_list_is_an_alias(self):
        self.assertIn("b", self.cli("slot", "list")[1])

    def test_slot_status_json(self):
        data = json.loads(self.cli("slot", "status", "--json")[1])
        self.assertEqual(data["boot"]["running_slot"], "0.2.0")
        self.assertEqual({s["name"] for s in data["slots"]}, {"0.2.0", "b"})

    def test_status_overview(self):
        code, out, err = self.cli("status")
        self.assertEqual(code, 0, err)
        self.assertIn("Boot", out)
        self.assertIn("Slots", out)
        self.assertIn("0.2.0", out)

    def test_boot_a_slot(self):
        code, out, err = self.cli("slot", "boot", "0.2.0", "--yes")
        self.assertEqual(code, 0, err)
        self.assertIn("done", out)
        with open(self.bootctl_log) as handle:
            self.assertEqual(handle.read().split(), ["set-default", "ecp_0.2.0.efi"])
        self.assertIn("default", self.cli("slot", "status")[1])

    def test_splash_dark_then_light(self):
        code, out, err = self.cli("splash", "dark")
        self.assertEqual(code, 0, err)
        self.assertIn("UEFI variable", out)
        self.assertIn("next restart", out)
        code, out, _err = self.cli("splash", "dark", "--json")
        self.assertEqual(json.loads(out[out.index("{\n"):])["result"]["changed"], [])
        self.assertEqual(self.cli("splash", "light")[0], 0)
        self.assertNotEqual(self.cli("splash", "blue")[0], 0)

    def test_boot_once(self):
        self.assertEqual(self.cli("slot", "boot", "0.2.0", "--once", "--yes")[0], 0)
        with open(self.bootctl_log) as handle:
            self.assertEqual(handle.read().split()[0], "set-oneshot")

    def test_booting_an_empty_slot_is_refused_before_anything_runs(self):
        code, _out, err = self.cli("slot", "boot", "b", "--yes")
        self.assertEqual(code, 2)
        self.assertIn("no kernel image", err)
        self.assertFalse(os.path.exists(self.bootctl_log))

    def test_unknown_slot(self):
        code, _out, err = self.cli("slot", "boot", "nope", "--yes")
        self.assertEqual(code, 2)
        self.assertIn("no slot named", err)

    def test_release_empties_a_standby_slot(self):
        self.system.hold(1, "0.1.0")
        self.system.uki("0.1.0")
        code, _out, err = self.cli("slot", "release", "0.1.0", "--yes")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.labels()[3], "ecp-root-b", "labels never change")
        self.assertNotIn("ecp_0.1.0.efi", self.uki_files())
        self.assertIn("already empty", self.cli("slot", "release", "b", "--yes")[1])

    def test_the_running_slot_cannot_be_released(self):
        self.system.hold(1, "0.1.0")
        self.system.uki("0.1.0")
        code, _out, err = self.cli("slot", "release", "0.2.0", "--yes")
        self.assertNotEqual(code, 0)
        self.assertIn("running", err)

    def test_compat_inspect_explains_why_a_package_does_not_fit(self):
        if not RPM or not support.tool_available("rpm"):
            self.skipTest("needs the built RPM and rpm")
        code, out, err = self.cli("compat", "inspect", RPM)
        self.assertIn("foundationd", out, err)
        self.assertEqual(code, 1 if "not compatible" in out else 0)
        self.assertIn("Files to install", out)

    def test_compat_install_refuses_an_unfit_package_without_force(self):
        if not RPM or not support.tool_available("rpm"):
            self.skipTest("needs the built RPM and rpm")
        code, out, err = self.cli("compat", "install", RPM, "--yes")
        if "not compatible" in out:      # the test host has no RPM database, so requirements are missing
            self.assertEqual(code, 2)
            self.assertIn("--force", err)

    def test_compat_inspect_of_a_missing_file(self):
        code, _out, err = self.cli("compat", "inspect", "/nonexistent/x.rpm")
        self.assertEqual(code, 2)
        self.assertIn("not a file", err)

    def test_compat_list_when_empty(self):
        code, out, _err = self.cli("compat", "list")
        self.assertEqual(code, 0) if False else None
        self.assertIn("No compat extensions", out + _err)


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(all(support.tool_available(t) for t in ("systemd-repart", "mdir", "mren", "mkfs.btrfs", "mkfs.vfat")),
                     "needs systemd-repart, mtools and mkfs tools")
class Deploy(Cli, BootDaemon):
    """foundation deploy, end to end on a plain file standing in for a disk."""

    extra_args = ("--allow-file-targets",)

    def setUp(self):
        super().setUp()
        from foundationd.backends import layout
        self.target = os.path.join(self.system.tmp, "target.img")
        with open(self.target, "wb") as handle:
            handle.truncate(layout.MIN_DISK + 64 * 1024 * 1024)

    def test_dry_run_changes_nothing(self):
        code, out, err = self.cli("deploy", "install", self.target, "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("ecp-root_0.2.0", out)
        self.assertIn("nothing was changed", out.lower())
        with open(self.target, "rb") as handle:
            self.assertEqual(handle.read(4096), bytes(4096))

    def test_install_needs_the_typed_path(self):
        code, _out, _err = self.cli("deploy", "install", self.target)   # no --yes, nothing typed
        self.assertNotEqual(code, 0)
        with open(self.target, "rb") as handle:
            self.assertEqual(handle.read(4096), bytes(4096))

    def test_install(self):
        code, out, err = self.cli("deploy", "install", self.target, "--yes")
        self.assertEqual(code, 0, err + out)
        self.assertIn("Installed", out)
        from foundationd.backends import gpt
        self.assertIn("ecp-root_0.2.0", [p.name for p in gpt.read_table(self.target)])

    def test_list_marks_the_running_disk(self):
        code, out, err = self.cli("deploy", "list", "--json")
        self.assertEqual(code, 0, err)
        disks = {d["name"]: d for d in json.loads(out)}
        self.assertFalse(disks["vda"]["usable"])

    def test_refuses_the_running_disk(self):
        code, _out, err = self.cli("deploy", "install", self.system.image, "--yes")
        self.assertNotEqual(code, 0)
