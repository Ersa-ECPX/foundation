"""Jobs and queries: running work in worker processes without blocking the daemon.

A *job* changes the system. It holds named locks, has an id and a D-Bus object, reports progress,
can be cancelled until it passes its point of no return, and is remembered after it finishes.
A *query* is a short read-only worker whose result goes straight back to the caller: no object,
no locks, a timeout.

Both run a worker kind from `workerlib.REGISTRY` in a child process (see workerlib for the
protocol). Nothing here blocks: the child is started, its standard output is read asynchronously
line by line, and its exit is awaited asynchronously.

This module does not know about D-Bus. It reports through a *notifier* with these methods:
    job_added(job)  job_changed(job, names)  job_signal(job, name, args)  job_removed(job)
"""

import collections
import json
import logging
import os
import signal
import tempfile
import time

from gi.repository import Gio, GLib

from .errors import Busy, NotCancellable, NotFound, by_name

log = logging.getLogger("foundationd.jobs")

KEEP_FINISHED = 50
LOG_LINES = 500
QUERY_TIMEOUT = 60       # seconds before a query worker is killed
CANCEL_GRACE = 10        # seconds between SIGTERM and SIGKILL

RUNNING, DONE, FAILED, CANCELLED = "running", "done", "failed", "cancelled"


class Job:
    def __init__(self, job_id, kind, locks, cleanup=None):
        self.cleanup = cleanup     # called once when the job ends, however it ends
        self.id = job_id
        self.kind = kind
        self.locks = tuple(locks)
        self.state = RUNNING
        self.step = ""
        self.step_title = ""
        self.progress = -1.0
        self.cancellable = True
        self.result = {}
        self.error_name = ""
        self.error_message = ""
        self.log = collections.deque(maxlen=LOG_LINES)
        self.started = time.time()
        self.finished = None
        self.process = None
        self.cancel_requested = False
        self.got_result = False     # a result event arrived
        self.error = None           # (name, message) from an error event

    @property
    def path(self):
        return f"/org/ersa/Foundation1/Job/{self.id}"

    @property
    def active(self):
        return self.state == RUNNING

    def to_record(self):
        return {
            "id": self.id, "kind": self.kind, "state": self.state, "step": self.step,
            "step_title": self.step_title, "progress": self.progress, "result": self.result,
            "error_name": self.error_name, "error_message": self.error_message,
            "log": list(self.log)[-200:], "started": self.started, "finished": self.finished,
        }

    @classmethod
    def from_record(cls, record):
        job = cls(int(record["id"]), str(record["kind"]), ())
        job.state = record.get("state", FAILED)
        job.step = record.get("step", "")
        job.step_title = record.get("step_title", "")
        job.progress = float(record.get("progress", -1.0))
        job.result = record.get("result") or {}
        job.error_name = record.get("error_name", "")
        job.error_message = record.get("error_message", "")
        job.log.extend(record.get("log") or [])
        job.started = record.get("started") or 0.0
        job.finished = record.get("finished")
        job.cancellable = False
        if job.state == RUNNING:  # the daemon died while it ran: say so rather than pretend
            job.state = FAILED
            job.error_name = "org.ersa.Foundation1.Error.Failed"
            job.error_message = "interrupted: foundationd stopped while this job was running"
        return job


class DirectLauncher:
    """Start a worker as a plain child of the daemon (it inherits the daemon's sandbox).

    `command` is the worker executable, to which the kind is appended. A launcher that starts each
    kind in its own systemd sandbox would replace this class; nothing else would change.
    """

    def __init__(self, command, env=None):
        self.command = list(command)
        self.env = dict(env or {})

    def start(self, kind):
        launcher = Gio.SubprocessLauncher.new(
            Gio.SubprocessFlags.STDIN_PIPE | Gio.SubprocessFlags.STDOUT_PIPE)
        for name, value in self.env.items():
            launcher.setenv(name, value, True)
        return launcher.spawnv(self.command + [kind])


class JobManager:
    def __init__(self, notifier, launcher, paths, state_dir, keep=KEEP_FINISHED):
        self.notifier = notifier
        self.launcher = launcher
        self.paths = paths
        self.jobs_dir = os.path.join(state_dir, "jobs")
        self.keep = keep
        self.jobs = {}              # id -> Job, oldest first by id
        self._locks = {}            # lock name -> job id
        self._queries = 0
        self._next_id = 1

    # -- bookkeeping -----------------------------------------------------------------------------

    def busy(self):
        """Work in flight (running jobs and queries): the daemon must not idle-exit under it."""
        return self._queries + sum(1 for job in self.jobs.values() if job.active)

    def running_jobs(self):
        return [job for job in self.jobs.values() if job.active]

    def get(self, job_id):
        try:
            return self.jobs[job_id]
        except KeyError:
            raise NotFound(f"no such job: {job_id}") from None

    def check_locks(self, locks):
        for name in locks:
            holder = self._locks.get(name)
            if holder is not None:
                raise Busy(f"'{name}' is in use by job {holder} ({self.jobs[holder].kind})")

    def load_history(self):
        """Re-create Job objects for the finished jobs remembered on disk."""
        try:
            names = sorted(os.listdir(self.jobs_dir))
        except FileNotFoundError:
            return
        for name in names:
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(self.jobs_dir, name), encoding="utf-8") as handle:
                    job = Job.from_record(json.load(handle))
            except (OSError, ValueError, KeyError, TypeError) as error:
                log.warning("ignoring unreadable job record %s: %s", name, error)
                continue
            self.jobs[job.id] = job
            self._next_id = max(self._next_id, job.id + 1)
            self.notifier.job_added(job)

    # -- jobs ------------------------------------------------------------------------------------

    def start(self, kind, args, locks=(), cleanup=None):
        """Start a job. Raises Busy if one of the locks is held. Returns the Job immediately.
        `cleanup()` runs once when the job ends (for example to delete a staged file)."""
        self.check_locks(locks)
        job = Job(self._next_id, kind, locks, cleanup)
        self._next_id += 1
        self.jobs[job.id] = job
        for name in job.locks:
            self._locks[name] = job.id
        self.notifier.job_added(job)
        try:
            self._spawn(job, kind, args, on_event=self._job_event, on_exit=self._job_exit)
        except Exception as error:  # noqa: BLE001 - could not even start the worker
            log.exception("cannot start worker for %s", kind)
            self._finish(job, FAILED, "org.ersa.Foundation1.Error.Unavailable",
                         f"cannot start the worker: {error}")
        return job

    def cancel(self, job_id):
        job = self.get(job_id)
        if not job.active:
            raise NotCancellable("the job has already finished")
        if not job.cancellable:
            raise NotCancellable("the job is past the point where it can be stopped safely")
        if job.cancel_requested:
            return
        job.cancel_requested = True
        job.log.append("cancel requested")
        if job.process is not None:
            job.process.send_signal(signal.SIGTERM)
            GLib.timeout_add_seconds(CANCEL_GRACE, self._kill_if_alive, job)

    @staticmethod
    def _kill_if_alive(job):
        if job.active and job.process is not None:
            log.warning("job %s ignored SIGTERM, killing it", job.id)
            job.process.force_exit()
        return GLib.SOURCE_REMOVE

    def _job_event(self, job, event):
        kind = event.get("event")
        if kind == "step":
            job.step, job.step_title = str(event.get("id", "")), str(event.get("title", ""))
            self.notifier.job_changed(job, ("Step", "StepTitle"))
            self.notifier.job_signal(job, "StepChanged", (job.step, job.step_title))
        elif kind == "progress":
            job.progress = float(event.get("fraction", -1.0))
            self.notifier.job_changed(job, ("Progress",))
            self.notifier.job_signal(job, "ProgressChanged", (job.progress,))
        elif kind == "log":
            text = str(event.get("text", ""))
            job.log.append(text)
            self.notifier.job_signal(job, "Message", (text,))
        elif kind == "uncancellable":
            job.cancellable = False
            self.notifier.job_changed(job, ("Cancellable",))
        elif kind == "result":
            job.result = event.get("data") if isinstance(event.get("data"), dict) else {"value": event.get("data")}
            job.got_result = True
        elif kind == "error":
            job.error = (str(event.get("name", "Failed")), str(event.get("message", "")))

    def _job_exit(self, job, status, signaled):
        error = job.error
        if job.cancel_requested and (signaled or error is not None or status != 0):
            self._finish(job, CANCELLED, "", "cancelled")
        elif error is not None:
            self._finish(job, FAILED, by_name(error[0])().dbus_name(), error[1])
        elif job.got_result and status == 0 and not signaled:
            self._finish(job, DONE, "", "done")
        else:
            how = f"was killed by signal {status}" if signaled else f"exited with status {status}"
            self._finish(job, FAILED, "org.ersa.Foundation1.Error.Failed", f"the worker {how} without a result")

    def _finish(self, job, state, error_name, message):
        job.state = state
        job.error_name = error_name
        job.error_message = message if state != DONE else ""
        job.cancellable = False
        job.finished = time.time()
        if state == DONE:
            job.progress = 1.0
        for name in job.locks:
            if self._locks.get(name) == job.id:
                del self._locks[name]
        if job.cleanup is not None:
            try:
                job.cleanup()
            except Exception:  # noqa: BLE001 - cleanup must never stop the job from being reported
                log.exception("cleanup for job %s failed", job.id)
            job.cleanup = None
        self._persist(job)
        self.notifier.job_changed(job, ("State", "Cancellable", "Progress", "Result", "ErrorName", "ErrorMessage"))
        self.notifier.job_signal(job, "Finished", (state == DONE, message))
        self._prune()

    # -- persistence -----------------------------------------------------------------------------

    def _persist(self, job):
        try:
            os.makedirs(self.jobs_dir, mode=0o755, exist_ok=True)
            handle = tempfile.NamedTemporaryFile("w", dir=self.jobs_dir, suffix=".tmp", delete=False, encoding="utf-8")
            with handle:
                json.dump(job.to_record(), handle)
            os.replace(handle.name, os.path.join(self.jobs_dir, f"{job.id:08d}.json"))
        except OSError as error:  # a full disk must not break the job that just finished
            log.warning("cannot record job %s: %s", job.id, error)

    def _prune(self):
        finished = [job for job in self.jobs.values() if not job.active]
        for job in sorted(finished, key=lambda j: j.id)[:max(0, len(finished) - self.keep)]:
            del self.jobs[job.id]
            self.notifier.job_removed(job)
            try:
                os.unlink(os.path.join(self.jobs_dir, f"{job.id:08d}.json"))
            except OSError:
                pass

    # -- queries ---------------------------------------------------------------------------------

    def query(self, kind, args, callback, timeout=QUERY_TIMEOUT):
        """Run a read-only worker and call callback(ok, data_or_(name, message)) once."""
        state = {"result": None, "error": None, "done": False}
        self._queries += 1

        def finish(ok, payload):
            if state["done"]:
                return
            state["done"] = True
            self._queries -= 1
            callback(ok, payload)

        class _Query:  # the shape _spawn expects for its bookkeeping
            id = f"query:{kind}"
            process = None

        query = _Query()

        def on_event(_q, event):
            if event.get("event") == "result":
                state["result"] = event.get("data")
            elif event.get("event") == "error":
                state["error"] = (str(event.get("name", "Failed")), str(event.get("message", "")))

        def on_exit(_q, status, signaled):
            if state["error"] is not None:
                finish(False, state["error"])
            elif state["result"] is not None and status == 0 and not signaled:
                finish(True, state["result"])
            else:
                finish(False, ("Failed", f"the worker exited with status {status} without a result"))

        try:
            self._spawn(query, kind, args, on_event, on_exit)
        except Exception as error:  # noqa: BLE001
            log.exception("cannot start query worker for %s", kind)
            finish(False, ("Unavailable", f"cannot start the worker: {error}"))
            return

        def expire():
            if not state["done"] and query.process is not None:
                log.warning("query %s timed out, killing it", kind)
                query.process.force_exit()
            return GLib.SOURCE_REMOVE

        GLib.timeout_add_seconds(timeout, expire)

    # -- the worker process ----------------------------------------------------------------------

    def _spawn(self, owner, kind, args, on_event, on_exit):
        process = self.launcher.start(kind)
        owner.process = process
        request = json.dumps({"kind": kind, "args": args, "paths": self.paths}) + "\n"

        stdin = process.get_stdin_pipe()
        stdin.write_all_async(request.encode(), GLib.PRIORITY_DEFAULT, None, self._stdin_written, stdin)

        reader = Gio.DataInputStream.new(process.get_stdout_pipe())
        progress = {"eof": False, "exited": False, "status": 0, "signaled": False}

        def maybe_done():
            if progress["eof"] and progress["exited"]:
                on_exit(owner, progress["status"], progress["signaled"])

        def on_line(stream, result):
            try:
                line, _length = stream.read_line_finish_utf8(result)
            except GLib.Error as error:
                log.warning("reading worker output failed: %s", error.message)
                line = None
            if line is None:
                progress["eof"] = True
                maybe_done()
                return
            try:
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise ValueError("not an object")
            except ValueError:
                log.warning("worker %s wrote a malformed line: %r", kind, line[:200])
            else:
                on_event(owner, event)
            stream.read_line_async(GLib.PRIORITY_DEFAULT, None, on_line)

        def on_wait(proc, result):
            try:
                proc.wait_finish(result)
            except GLib.Error:
                pass
            progress["signaled"] = proc.get_if_signaled()
            progress["status"] = proc.get_term_sig() if progress["signaled"] else proc.get_exit_status()
            progress["exited"] = True
            maybe_done()

        reader.read_line_async(GLib.PRIORITY_DEFAULT, None, on_line)
        process.wait_async(None, on_wait)

    @staticmethod
    def _stdin_written(stream, result, _stream):
        try:
            stream.write_all_finish(result)
        except GLib.Error as error:
            log.warning("could not send the request to the worker: %s", error.message)
        try:
            stream.close()
        except GLib.Error:
            pass
