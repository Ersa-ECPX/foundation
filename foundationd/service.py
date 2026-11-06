"""The D-Bus face of foundationd: object registration, dispatch, authorization, jobs and idle exit.

Interface definitions are not written here: they are read from org.ersa.Foundation1.xml, which is
the contract. A method that is not in that file cannot be called, and a method in that file with no
handler is a startup error, so the two cannot drift apart silently.

Handlers live in `foundationd.api.*`, one module per interface group. Each exports
`register(service) -> {(interface, method): Method(...)}`. A handler receives a `Call` and either
returns the reply (a GLib.Variant tuple, or None for no output), or returns DEFERRED and answers
later with `call.reply()` / `call.fail()`. The main loop never blocks: anything slow runs in a
worker process (see jobs.py).
"""

import logging
import os
import tempfile
from collections import namedtuple

from gi.repository import Gio, GLib

from . import __version__
from .errors import FoundationError, NotAuthorized
from .jobs import DirectLauncher, JobManager
from .paths import Paths
from .variants import dict_variant

log = logging.getLogger("foundationd")

BUS_NAME = "org.ersa.Foundation1"
OBJECT_PATH = "/org/ersa/Foundation1"
IFACE_JOB = "org.ersa.Foundation1.Job"
API_VERSION = "1"

# A handler returns this when it will answer later.
DEFERRED = object()

Method = namedtuple("Method", "handler action precheck", defaults=(None, None))

JOB_SIGNALS = {"StepChanged": "(ss)", "ProgressChanged": "(d)", "Message": "(s)", "Finished": "(bs)"}


class Call:
    """One incoming method call."""

    def __init__(self, service, sender, path, parameters, invocation):
        self.service = service
        self.sender = sender
        self.path = path
        self.parameters = parameters
        self.invocation = invocation
        self.args = parameters.unpack()
        self._answered = False

    def reply(self, body=None):
        if self._answered:
            return
        self._answered = True
        self.invocation.return_value(body)

    def fail(self, error):
        if self._answered:
            return
        self._answered = True
        if not isinstance(error, FoundationError):
            log.error("unexpected error: %r", error)
            error = FoundationError("internal error (see the journal)")
        self.invocation.return_dbus_error(error.dbus_name(), str(error))

    def take_fd(self, handle):
        """The file descriptor passed as D-Bus handle `handle` (index into the message's fd list).
        The caller owns the returned descriptor and must close it."""
        fd_list = self.invocation.get_message().get_unix_fd_list()
        if fd_list is None:
            raise FoundationError("no file descriptor was passed")
        return fd_list.get(handle)


class Service:
    def __init__(self, xml, config, state, authorizer, paths=None, launcher=None, idle_timeout=60):
        self.config = config
        self.state = state
        self.authorizer = authorizer
        self.paths = paths or Paths()
        self.idle_timeout = idle_timeout
        self.connection = None
        self.exit_status = 0
        self._node = Gio.DBusNodeInfo.new_for_xml(xml)
        self._loop = GLib.MainLoop()
        self._pending = 0          # authorizations and file transfers in flight
        self._last_activity = GLib.get_monotonic_time()
        self._job_registrations = {}
        if launcher is None:
            launcher = DirectLauncher(["/usr/libexec/foundationd-worker"])
        self.jobs = JobManager(self, launcher, self.paths.worker_view(), self.paths.state)

        from . import api
        self.capabilities = []
        self._methods = {}
        for name, module in api.MODULES.items():
            self.capabilities.append(name)
            self._methods.update(module.register(self))
        self._check_contract()

    def _check_contract(self):
        declared = {(i.name, m.name) for i in self._node.interfaces for m in i.methods}
        missing = declared - set(self._methods)
        extra = set(self._methods) - declared
        if missing or extra:
            raise RuntimeError(f"contract mismatch: no handler for {sorted(missing)}, "
                               f"handler without contract entry for {sorted(extra)}")

    # -- lifecycle -------------------------------------------------------------------------------

    def run(self, connection):
        """Register on `connection`, own the bus name and run until idle or the name is lost."""
        self.connection = connection
        for interface in self._node.interfaces:
            if interface.name != IFACE_JOB:
                connection.register_object(
                    OBJECT_PATH, interface, self._on_method_call, self._on_get_property, None)
        self.jobs.load_history()
        Gio.bus_own_name_on_connection(
            connection, BUS_NAME, Gio.BusNameOwnerFlags.NONE, self._on_name_acquired, self._on_name_lost)
        if self.idle_timeout:
            GLib.timeout_add_seconds(1, self._check_idle)
        self._loop.run()
        return self.exit_status

    def quit(self, status=0):
        self.exit_status = status
        self._loop.quit()

    def _on_name_acquired(self, connection, name):
        log.info("acquired %s", name)

    def _on_name_lost(self, connection, name):
        log.error("lost or could not acquire %s", name)
        self.quit(1)

    def busy(self):
        return self._pending + self.jobs.busy()

    def _check_idle(self):
        idle_for = (GLib.get_monotonic_time() - self._last_activity) / 1_000_000
        if self.busy() == 0 and idle_for >= self.idle_timeout:
            log.info("idle for %d s, exiting (D-Bus activation restarts the daemon on demand)", idle_for)
            self.quit(0)
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def touch(self):
        self._last_activity = GLib.get_monotonic_time()

    # -- properties ------------------------------------------------------------------------------

    def _on_get_property(self, connection, sender, path, interface_name, property_name):
        if interface_name == IFACE_JOB:
            job = self.jobs.jobs.get(self._job_id(path))
            return self.job_property(job, property_name) if job else None
        if interface_name == "org.ersa.Foundation1.Manager":
            if property_name == "Version":
                return GLib.Variant("s", __version__)
            if property_name == "ApiVersion":
                return GLib.Variant("s", API_VERSION)
        return None

    @staticmethod
    def _job_id(path):
        try:
            return int(path.rsplit("/", 1)[1])
        except (IndexError, ValueError):
            return -1

    @staticmethod
    def job_property(job, name):
        values = {
            "Kind": GLib.Variant("s", job.kind), "State": GLib.Variant("s", job.state),
            "Step": GLib.Variant("s", job.step), "StepTitle": GLib.Variant("s", job.step_title),
            "Progress": GLib.Variant("d", job.progress), "Cancellable": GLib.Variant("b", job.cancellable),
            "Result": dict_variant(job.result), "ErrorName": GLib.Variant("s", job.error_name),
            "ErrorMessage": GLib.Variant("s", job.error_message),
        }
        return values.get(name)

    # -- the notifier interface used by JobManager -----------------------------------------------

    def job_added(self, job):
        if self.connection is None:
            return
        interface = self._node.lookup_interface(IFACE_JOB)
        self._job_registrations[job.id] = self.connection.register_object(
            job.path, interface, self._on_method_call, self._on_get_property, None)
        self.touch()

    def job_removed(self, job):
        registration = self._job_registrations.pop(job.id, None)
        if registration is not None and self.connection is not None:
            self.connection.unregister_object(registration)

    def job_changed(self, job, names):
        if self.connection is None:
            return
        changed = {name: self.job_property(job, name) for name in names}
        self.connection.emit_signal(
            None, job.path, "org.freedesktop.DBus.Properties", "PropertiesChanged",
            GLib.Variant("(sa{sv}as)", (IFACE_JOB, changed, [])))
        self.touch()

    def job_signal(self, job, name, args):
        if self.connection is not None:
            self.connection.emit_signal(None, job.path, IFACE_JOB, name, GLib.Variant(JOB_SIGNALS[name], args))

    def emit(self, interface, name, signature, args):
        """Emit a signal from the manager object."""
        if self.connection is not None:
            self.connection.emit_signal(None, OBJECT_PATH, interface, name, GLib.Variant(signature, args))

    # -- dispatch --------------------------------------------------------------------------------

    def _on_method_call(self, connection, sender, path, interface_name, method_name, parameters, invocation):
        self.touch()
        method = self._methods.get((interface_name, method_name))
        if method is None:  # cannot happen for registered interfaces; fail closed anyway
            invocation.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", method_name)
            return
        call = Call(self, sender, path, parameters, invocation)

        if method.action is None:
            self._run(method.handler, call)
            return

        # State-changing call: precheck, then ask polkit (asynchronously), then act. The handler
        # validates again, because time passes while a person types a password.
        if method.precheck is not None:
            try:
                method.precheck(call)
            except FoundationError as error:
                call.fail(error)
                return
            except Exception as error:  # noqa: BLE001
                log.exception("precheck failed")
                call.fail(error)
                return
        self._pending += 1
        answered = []

        def authorized(allowed, message):
            if answered:  # an authorizer must answer once; ignore a second answer
                return
            answered.append(True)
            self._pending -= 1
            self.touch()
            if not allowed:
                call.fail(NotAuthorized(message or "not authorized"))
                return
            self._run(method.handler, call)

        try:
            self.authorizer.authorize(sender, method.action, authorized)
        except Exception:  # noqa: BLE001 - a broken authorizer must deny, not hang the caller
            log.exception("authorizer failed for %s", method.action)
            authorized(False, "authorization failed")

    def _run(self, handler, call):
        try:
            result = handler(call)
        except FoundationError as error:
            call.fail(error)
        except Exception as error:  # noqa: BLE001 - never leak internals over the bus
            log.exception("unexpected error in %s", getattr(handler, "__name__", handler))
            call.fail(error)
        else:
            if result is not DEFERRED:
                call.reply(result)

    # -- helpers for handlers --------------------------------------------------------------------

    def query(self, call, kind, args, shape, cleanup=None):
        """Answer `call` with the result of a read-only worker. shape: reply_dict or reply_list.
        `cleanup()` runs when the worker has finished (for example to delete a staged file)."""
        def done(ok, payload):
            if cleanup is not None:
                cleanup()
            if ok:
                try:
                    call.reply(shape(payload))
                except Exception as error:  # noqa: BLE001 - a worker produced something unmappable
                    log.exception("cannot convert the result of %s", kind)
                    call.fail(error)
            else:
                name, message = payload
                from .errors import by_name
                call.fail(by_name(name)(message))

        self.jobs.query(kind, args, done)
        return DEFERRED

    def start_job(self, kind, args, locks=(), cleanup=None):
        """Start a job and return the reply body naming it."""
        job = self.jobs.start(kind, args, locks, cleanup)
        return GLib.Variant("(o)", (job.path,))

    def stage(self, fd, suffix, callback):
        """Copy a descriptor's contents into a private file without blocking the loop.

        callback(path, error) is called once. The caller owns the file and must delete it. Staged
        files are only ever read by workers, so a path chosen by a client is never opened by root.
        """
        directory = os.path.join(self.paths.state, "staging")
        os.makedirs(directory, mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)
        out_fd, path = tempfile.mkstemp(dir=directory, suffix=suffix)
        source = Gio.UnixInputStream.new(fd, True)
        target = Gio.UnixOutputStream.new(out_fd, True)
        self._pending += 1

        def spliced(stream, result):
            self._pending -= 1
            try:
                stream.splice_finish(result)
            except GLib.Error as error:
                try:
                    os.unlink(path)
                except OSError:
                    pass
                callback(None, error.message)
                return
            callback(path, None)

        target.splice_async(
            source, Gio.OutputStreamSpliceFlags.CLOSE_SOURCE | Gio.OutputStreamSpliceFlags.CLOSE_TARGET,
            GLib.PRIORITY_DEFAULT, None, spliced)
