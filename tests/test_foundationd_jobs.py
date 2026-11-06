"""Jobs, queries, locks, cancellation, persistence, staged files and reboot, through the real D-Bus API.

The daemon runs against tests/scripted_worker.py, which replaces some worker kinds with scripted
behaviour (see its docstring). Polkit is stubbed; the authorization path itself is covered in
test_foundationd_bus.py.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

from test_foundationd_bus import (BusCase, CONFIG, MANAGER, NAME, PATH, ROOT, gi)  # noqa: E402

if gi:
    from gi.repository import Gio, GLib

EXTENSIONS, BOOT, APPS, COMPAT = (f"org.ersa.Foundation1.{n}" for n in ("Extensions", "Boot", "Apps", "Compat"))
JOB = "org.ersa.Foundation1.Job"
WORKER = f"{sys.executable} {os.path.join(HERE, 'scripted_worker.py')}"
ERR = "org.ersa.Foundation1.Error."


class JobCase(BusCase):
    extra_args = ("--worker-command", WORKER)

    def start(self, interface, method, signature=None, *args):
        (path,) = self.call(interface, method, signature, *args)
        self.assertRegex(path, r"^/org/ersa/Foundation1/Job/\d+$")
        return path

    def props(self, path):
        result = self.conn.call_sync(NAME, path, "org.freedesktop.DBus.Properties", "GetAll",
                                     GLib.Variant("(s)", (JOB,)), None, Gio.DBusCallFlags.NONE, 5000, None)
        return result.unpack()[0]

    def wait_for(self, path, predicate, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            props = self.props(path)
            if predicate(props):
                return props
            time.sleep(0.05)
        self.fail(f"timed out; last properties: {self.props(path)}")

    def finish(self, path, timeout=10):
        return self.wait_for(path, lambda p: p["State"] != "running", timeout)

    def collect_signals(self):
        got = []
        self.conn.signal_subscribe(
            NAME, JOB, None, None, None, Gio.DBusSignalFlags.NONE,
            lambda c, sender, path, iface, signal, params: got.append((signal, params.unpack())))
        return got

    def restart(self):
        self._stop(self.daemon)
        self.daemon = self._start_daemon()
        self.addCleanup(self._stop, self.daemon)
        self._wait_for_name()

    def pump(self, until, timeout=5):
        deadline = time.monotonic() + timeout
        while not until() and time.monotonic() < deadline:
            GLib.MainContext.default().iteration(False)
            time.sleep(0.01)


class JobLifecycle(JobCase):
    def test_a_job_reports_steps_progress_result_and_finishes(self):
        signals = self.collect_signals()
        path = self.start(EXTENSIONS, "Merge", "(s)", "sysext")
        props = self.finish(path)
        self.assertEqual((props["Kind"], props["State"], props["Step"], props["StepTitle"]),
                         ("ext.merge", "done", "two", "Second"))
        self.assertEqual(props["Progress"], 1.0)
        self.assertEqual(props["Result"], {"answer": 42, "items": ["a", "b"]})
        self.assertEqual((props["ErrorName"], props["ErrorMessage"], props["Cancellable"]), ("", "", False))
        self.pump(lambda: any(s[0] == "Finished" for s in signals))
        names = [s[0] for s in signals]
        self.assertEqual([s[1] for s in signals if s[0] == "StepChanged"], [("one", "First"), ("two", "Second")])
        self.assertEqual([s[1][0] for s in signals if s[0] == "ProgressChanged"], [0.25, 0.75])
        self.assertIn(("Message", ("hello",)), signals)
        self.assertEqual([s[1] for s in signals if s[0] == "Finished"], [(True, "done")])
        self.assertLess(names.index("StepChanged"), names.index("Finished"))
        (lines,) = self.conn.call_sync(NAME, path, JOB, "Logs", GLib.Variant("(i)", (10,)), None,
                                       Gio.DBusCallFlags.NONE, 5000, None).unpack()
        self.assertEqual(lines, ["hello"])

    def test_listjobs_and_the_record_on_disk(self):
        path = self.start(EXTENSIONS, "Merge", "(s)", "sysext")
        self.finish(path)
        self.assertIn(path, self.call(MANAGER, "ListJobs")[0])
        record_dir = os.path.join(self.tmp.name, "state", "jobs")
        (name,) = os.listdir(record_dir)
        with open(os.path.join(record_dir, name)) as handle:
            record = json.load(handle)
        self.assertEqual((record["kind"], record["state"], record["result"]["answer"]), ("ext.merge", "done", 42))

    def test_a_failed_job_carries_the_error_name_and_message(self):
        props = self.finish(self.start(EXTENSIONS, "Unmerge", "(s)", "sysext"))
        self.assertEqual((props["State"], props["ErrorName"], props["ErrorMessage"]),
                         ("failed", ERR + "NotFound", "no such thing"))

    def test_a_crashing_worker_fails_the_job_instead_of_hanging_it(self):
        props = self.finish(self.start(COMPAT, "Revalidate"))
        self.assertEqual(props["State"], "failed")
        self.assertIn("status 3", props["ErrorMessage"])

    def test_jobs_are_numbered_and_the_daemon_keeps_serving_meanwhile(self):
        first = self.start(EXTENSIONS, "Refresh", "(s)", "sysext")      # sleeps
        self.assertEqual(self.call(CONFIG, "Get", "(s)", "ota.channel"), ("stable",))   # not blocked
        second = self.start(BOOT, "ReleaseSlot", "(s)", "x")
        self.assertLess(int(first.rsplit("/", 1)[1]), int(second.rsplit("/", 1)[1]))
        self.conn.call_sync(NAME, first, JOB, "Cancel", None, None, Gio.DBusCallFlags.NONE, 5000, None)


class Cancellation(JobCase):
    def cancel(self, path):
        return self.conn.call_sync(NAME, path, JOB, "Cancel", None, None, Gio.DBusCallFlags.NONE, 5000, None)

    def test_cancel_stops_a_running_job_and_frees_its_locks(self):
        path = self.start(EXTENSIONS, "Refresh", "(s)", "sysext")
        self.wait_for(path, lambda p: p["Step"] == "wait")
        self.assertTrue(self.props(path)["Cancellable"])
        started = time.monotonic()
        self.cancel(path)
        props = self.finish(path)
        self.assertEqual(props["State"], "cancelled")
        self.assertLess(time.monotonic() - started, 5)           # SIGTERM, not the 10 s SIGKILL fallback
        again = self.start(EXTENSIONS, "Merge", "(s)", "sysext")  # the lock was released
        self.assertEqual(self.finish(again)["State"], "done")

    def test_cancelling_a_finished_job_is_refused(self):
        path = self.start(EXTENSIONS, "Merge", "(s)", "sysext")
        self.finish(path)
        with self.assertRaises(GLib.Error) as raised:
            self.cancel(path)
        self.assertEqual(Gio.DBusError.get_remote_error(raised.exception), ERR + "NotCancellable")

    def test_after_the_point_of_no_return_cancel_is_refused(self):
        path = self.start(BOOT, "ReleaseSlot", "(s)", "x")
        props = self.wait_for(path, lambda p: not p["Cancellable"])
        self.assertEqual(props["State"], "running")
        with self.assertRaises(GLib.Error) as raised:
            self.cancel(path)
        self.assertEqual(Gio.DBusError.get_remote_error(raised.exception), ERR + "NotCancellable")
        self.assertEqual(self.finish(path)["State"], "done")      # it still ran to completion


class Locks(JobCase):
    def test_a_conflicting_job_fails_fast_with_busy(self):
        first = self.start(EXTENSIONS, "Refresh", "(s)", "sysext")
        self.wait_for(first, lambda p: p["Step"] == "wait")
        self.assertEqual(self.error_of(EXTENSIONS, "Merge", "(s)", "sysext"), ERR + "Busy")
        # The compat interface shares the 'extensions' lock; a different lock does not conflict.
        self.assertEqual(self.error_of(COMPAT, "Revalidate"), ERR + "Busy")
        other = self.start(BOOT, "ReleaseSlot", "(s)", "x")
        self.assertEqual(self.finish(other)["State"], "done")
        self.conn.call_sync(NAME, first, JOB, "Cancel", None, None, Gio.DBusCallFlags.NONE, 5000, None)


class Queries(JobCase):
    def test_a_list_result_becomes_an_array_of_dictionaries(self):
        (slots,) = self.call(BOOT, "GetSlots")
        self.assertEqual(slots, [{"name": "a", "size": 5, "running": True, "tries_left": -1},
                                 {"name": "b", "size": 6, "running": False}])

    def test_an_empty_list_keeps_its_type(self):
        self.assertEqual(self.call(APPS, "ListInstalled"), ([],))

    def test_a_dictionary_result(self):
        (state,) = self.call(BOOT, "GetBootState")
        self.assertEqual(state["assessment"], "good")

    def test_a_worker_error_reaches_the_caller_with_its_name(self):
        self.assertEqual(self.error_of(APPS, "GetInfo", "(s)", "org.example.App"), ERR + "NotFound")

    def test_a_crashing_query_worker_returns_an_error_instead_of_hanging(self):
        self.assertEqual(self.error_of(APPS, "Search", "(s)", "zen"), ERR + "Failed")

    def test_queries_do_not_create_jobs(self):
        self.call(BOOT, "GetSlots")
        self.assertEqual(self.call(MANAGER, "ListJobs"), ([],))

    def test_a_real_worker_for_system_facts(self):
        (info,) = self.call(MANAGER, "GetSystemInfo")
        self.assertIn("architecture", info)
        self.assertEqual(info["os"]["ecp"], False)


class Validation(JobCase):
    def test_bad_arguments_are_rejected_before_anything_starts(self):
        for method, signature, args in ((BOOT, "SetDefaultSlot", ("-rf",)), (BOOT, "SetNextBoot", ("a b",)),
                                        (APPS, "Install", ("../x",)), (APPS, "Remove", ("-y",)),
                                        (COMPAT, "Remove", ("a/b",)), (APPS, "GetInfo", ("",))):
            kind = "(s)"
            self.assertEqual(self.error_of(method, signature, kind, *args), ERR + "InvalidArgument", (signature, args))
        self.assertEqual(self.error_of(EXTENSIONS, "Merge", "(s)", "nonsense"), ERR + "InvalidArgument")
        self.assertEqual(self.error_of(APPS, "Search", "(s)", "-x"), ERR + "InvalidArgument")
        self.assertEqual(self.call(MANAGER, "ListJobs"), ([],))

    def test_unknown_job_object_has_no_methods(self):
        with self.assertRaises(GLib.Error):
            self.conn.call_sync(NAME, "/org/ersa/Foundation1/Job/999", JOB, "Cancel", None, None,
                                Gio.DBusCallFlags.NONE, 5000, None)


class Denied(JobCase):
    authorizer = "deny"

    def test_nothing_starts_without_authorization(self):
        self.assertEqual(self.error_of(EXTENSIONS, "Merge", "(s)", "sysext"), ERR + "NotAuthorized")
        self.assertEqual(self.error_of(BOOT, "CopySlot", "(ssb)", "", "b", True), ERR + "NotAuthorized")
        self.assertEqual(self.error_of(BOOT, "Reboot", "(b)", False), ERR + "NotAuthorized")
        self.assertEqual(self.call(MANAGER, "ListJobs"), ([],))

    def test_cancel_is_checked_against_polkit_before_anything_else(self):
        # A finished job record gives the daemon a job object even though nothing may start.
        record_dir = os.path.join(self.tmp.name, "state", "jobs")
        os.makedirs(record_dir, exist_ok=True)
        with open(os.path.join(record_dir, "00000009.json"), "w") as handle:
            json.dump({"id": 9, "kind": "ext.merge", "state": "done"}, handle)
        self.restart()
        with self.assertRaises(GLib.Error) as raised:
            self.conn.call_sync(NAME, "/org/ersa/Foundation1/Job/9", JOB, "Cancel", None, None,
                                Gio.DBusCallFlags.NONE, 5000, None)
        self.assertEqual(Gio.DBusError.get_remote_error(raised.exception), ERR + "NotAuthorized")
        # Reading the same job needs no authorization.
        self.assertEqual(self.props("/org/ersa/Foundation1/Job/9")["State"], "done")


class StagedFiles(JobCase):
    def send_fd(self, interface, method, signature, content, *extra):
        with tempfile.NamedTemporaryFile() as handle:
            handle.write(content)
            handle.flush()
            handle.seek(0)
            fds = Gio.UnixFDList.new()
            index = fds.append(handle.fileno())
            reply, _out = self.conn.call_with_unix_fd_list_sync(
                NAME, PATH, interface, method, GLib.Variant(signature, (index, *extra)), None,
                Gio.DBusCallFlags.NONE, 10000, fds, None)
        return reply.unpack()

    def staging(self):
        directory = os.path.join(self.tmp.name, "state", "staging")
        return os.listdir(directory) if os.path.isdir(directory) else []

    def test_inspect_reads_a_copy_of_the_descriptor_and_deletes_it(self):
        (report,) = self.send_fd(COMPAT, "Inspect", "(h)", b"hello rpm")
        self.assertEqual((report["size"], report["content"]), (9, "hello rpm"))
        self.assertEqual(self.staging(), [])

    def test_install_hands_the_staged_file_to_the_job_and_cleans_up_afterwards(self):
        (path,) = self.send_fd(COMPAT, "Install", "(hb)", b"payload", True)
        props = self.finish(path)
        self.assertEqual((props["State"], props["Result"]["content"], props["Result"]["force"]), ("done", "payload", True))
        self.assertEqual(self.staging(), [])

    def test_a_large_file_does_not_block_the_daemon(self):
        big = b"x" * (20 * 1024 * 1024)
        (report,) = self.send_fd(COMPAT, "Inspect", "(h)", big)
        self.assertEqual(report["size"], len(big))
        self.assertEqual(self.staging(), [])

    def test_the_staging_directory_is_private(self):
        self.send_fd(COMPAT, "Inspect", "(h)", b"x")
        self.assertEqual(os.stat(os.path.join(self.tmp.name, "state", "staging")).st_mode & 0o777, 0o700)

    def test_a_call_without_a_descriptor_fails_cleanly(self):
        with self.assertRaises(GLib.Error):
            self.conn.call_sync(NAME, PATH, COMPAT, "Inspect", GLib.Variant("(h)", (0,)), None,
                                Gio.DBusCallFlags.NONE, 5000, None)
        self.assertEqual(self.call(CONFIG, "Get", "(s)", "ota.channel"), ("stable",))


class Persistence(JobCase):
    def test_finished_jobs_survive_a_daemon_restart(self):
        path = self.start(EXTENSIONS, "Unmerge", "(s)", "sysext")
        self.finish(path)
        self.restart()
        self.assertIn(path, self.call(MANAGER, "ListJobs")[0])
        props = self.props(path)
        self.assertEqual((props["State"], props["ErrorName"]), ("failed", ERR + "NotFound"))

    def test_job_numbers_do_not_restart_at_one(self):
        first = self.start(EXTENSIONS, "Merge", "(s)", "sysext")
        self.finish(first)
        self.restart()
        second = self.start(EXTENSIONS, "Merge", "(s)", "sysext")
        self.assertGreater(int(second.rsplit("/", 1)[1]), int(first.rsplit("/", 1)[1]))

    def test_a_job_that_was_running_when_the_daemon_died_is_reported_as_interrupted(self):
        record_dir = os.path.join(self.tmp.name, "state", "jobs")
        os.makedirs(record_dir, exist_ok=True)
        with open(os.path.join(record_dir, "00000007.json"), "w") as handle:
            json.dump({"id": 7, "kind": "boot.copy", "state": "running", "step": "copy"}, handle)
        self.restart()
        props = self.props("/org/ersa/Foundation1/Job/7")
        self.assertEqual(props["State"], "failed")
        self.assertIn("interrupted", props["ErrorMessage"])
        self.assertFalse(props["Cancellable"])

    def test_old_jobs_are_pruned(self):
        for _ in range(3):
            self.finish(self.start(EXTENSIONS, "Merge", "(s)", "sysext"))
        self.assertEqual(len(self.call(MANAGER, "ListJobs")[0]), 3)   # well under the keep limit of 50


class IdleWithJobs(JobCase):
    idle_timeout = 2

    def test_the_daemon_does_not_exit_while_a_job_runs_and_exits_after(self):
        path = self.start(EXTENSIONS, "Refresh", "(s)", "sysext")
        time.sleep(4.5)
        self.assertIsNone(self.daemon.poll(), "the daemon exited with a job still running")
        self.conn.call_sync(NAME, path, JOB, "Cancel", None, None, Gio.DBusCallFlags.NONE, 5000, None)
        self.finish(path)
        self.assertEqual(self.daemon.wait(timeout=10), 0)


class RebootWithoutLogind(JobCase):
    def test_a_missing_logind_is_a_clear_error_not_a_hang(self):
        with self.assertRaises(GLib.Error) as raised:
            self.call(BOOT, "Reboot", "(b)", False)
        self.assertEqual(Gio.DBusError.get_remote_error(raised.exception), ERR + "Unavailable")
        message = raised.exception.message
        self.assertIn("cannot ask systemd-logind", message)
        self.assertEqual(message.count("GDBus.Error"), 1, message)    # only the bus's own prefix, none embedded


class Reboot(JobCase):
    def setUp(self):
        super().setUp()
        self.logind_log = os.path.join(self.tmp.name, "logind.log")
        env = dict(os.environ, DBUS_SESSION_BUS_ADDRESS=self.address, FAKE_LOGIND_LOG=self.logind_log)
        self.logind = subprocess.Popen([sys.executable, os.path.join(HERE, "fake_logind.py")], env=env,
                                       stdout=subprocess.PIPE, text=True)
        self.addCleanup(self._stop, self.logind)
        self.assertEqual(self.logind.stdout.readline().strip(), "ready")

    def calls(self):
        try:
            with open(self.logind_log) as handle:
                return handle.read().splitlines()
        except FileNotFoundError:
            return []

    def test_reboot_goes_through_logind_non_interactively(self):
        self.call(BOOT, "Reboot", "(b)", False)
        self.assertEqual(self.calls(), ["Reboot False"])

    def test_reboot_to_firmware_sets_the_flag_first(self):
        self.call(BOOT, "RebootToFirmware", "(b)", False)
        self.assertEqual(self.calls(), ["SetRebootToFirmwareSetup True", "Reboot False"])

    def test_a_running_job_blocks_reboot_unless_forced(self):
        path = self.start(EXTENSIONS, "Refresh", "(s)", "sysext")
        self.wait_for(path, lambda p: p["Step"] == "wait")
        self.assertEqual(self.error_of(BOOT, "Reboot", "(b)", False), ERR + "Busy")
        self.assertEqual(self.calls(), [])
        self.call(BOOT, "Reboot", "(b)", True)
        self.assertEqual(self.calls(), ["Reboot False"])
        self.conn.call_sync(NAME, path, JOB, "Cancel", None, None, Gio.DBusCallFlags.NONE, 5000, None)


if __name__ == "__main__":
    unittest.main()
