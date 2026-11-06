"""A client for the org.ersa.Foundation1 D-Bus API. No policy lives here: it only talks to foundationd.

The command line, and any future graphical client, are built on this. `wait` follows a job through
its signals and calls back with what happens; it never polls faster than once a second.
"""

import re
import signal

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

NAME = "org.ersa.Foundation1"
PATH = "/org/ersa/Foundation1"
PREFIX = "org.ersa.Foundation1."
ERROR_PREFIX = "org.ersa.Foundation1.Error."
JOB_INTERFACE = PREFIX + "Job"
LONG_TIMEOUT_MS = 600_000
JOB_SIGNALS = {"StepChanged": "(ss)", "ProgressChanged": "(d)", "Message": "(s)", "Finished": "(bs)"}


class ClientError(Exception):
    """A failed call. `name` is the error suffix (NotAuthorized, Busy, ...) or the raw D-Bus name."""

    def __init__(self, name, message):
        super().__init__(message)
        self.name = name
        self.message = message

    @classmethod
    def from_gerror(cls, error):
        remote = Gio.DBusError.get_remote_error(error)
        # "GDBus.Error:org.ersa.Foundation1.Error.Busy: text" -> "text"
        message = re.sub(r"^GDBus\.Error:\S+?:\s*", "", error.message)
        if remote and remote.startswith(ERROR_PREFIX):
            return cls(remote[len(ERROR_PREFIX):], message)
        if remote in ("org.freedesktop.DBus.Error.ServiceUnknown", "org.freedesktop.DBus.Error.NameHasNoOwner",
                      "org.freedesktop.DBus.Error.NoReply", "org.freedesktop.DBus.Error.Spawn.ChildExited"):
            return cls("Unreachable", "the Ersa Compute Platform management service (foundationd) is not "
                                      f"available: {message}")
        return cls(remote or "Failed", message)


class JobResult:
    def __init__(self, path, props):
        self.path = path
        self.state = props["State"]
        self.kind = props["Kind"]
        self.result = props["Result"]
        self.error_name = props["ErrorName"].removeprefix(ERROR_PREFIX)
        self.error_message = props["ErrorMessage"]

    @property
    def ok(self):
        return self.state == "done"

    @property
    def id(self):
        return self.path.rsplit("/", 1)[1]


class Collector:
    """Records job signals from the moment it is created, for every job.

    The service starts working as soon as it has replied with the job's path, so a fast job can
    emit signals before the caller has subscribed to that path. Subscribing to all jobs first and
    replaying the matching ones afterwards means no step is lost.
    """

    def __init__(self, connection):
        self._connection = connection
        self.events = []
        self._subscription = connection.signal_subscribe(
            NAME, JOB_INTERFACE, None, None, None, Gio.DBusSignalFlags.NONE, self._record)

    def _record(self, connection, sender, object_path, interface, name, parameters):
        self.events.append((object_path, name, parameters.unpack()))

    def close(self):
        if self._subscription is not None:
            self._connection.signal_unsubscribe(self._subscription)
            self._subscription = None


class Client:
    def __init__(self, bus="system"):
        bus_type = Gio.BusType.SYSTEM if bus == "system" else Gio.BusType.SESSION
        try:
            self.connection = Gio.bus_get_sync(bus_type, None)
        except GLib.Error as error:
            raise ClientError("Unreachable", f"cannot connect to the {bus} bus: {error.message}") from error

    # -- calls -----------------------------------------------------------------------------------

    def call(self, interface, method, signature=None, *args, path=PATH, fds=None, timeout=60_000):
        """Call a method; returns the unpacked reply as a tuple (empty for no output)."""
        parameters = GLib.Variant(signature, args) if signature else None
        try:
            if fds is not None:
                reply, _out = self.connection.call_with_unix_fd_list_sync(
                    NAME, path, PREFIX + interface if "." not in interface else interface, method, parameters,
                    None, Gio.DBusCallFlags.ALLOW_INTERACTIVE_AUTHORIZATION, LONG_TIMEOUT_MS, fds, None)
            else:
                reply = self.connection.call_sync(
                    NAME, path, PREFIX + interface if "." not in interface else interface, method, parameters, None,
                    Gio.DBusCallFlags.ALLOW_INTERACTIVE_AUTHORIZATION, timeout, None)
        except GLib.Error as error:
            raise ClientError.from_gerror(error) from None
        return reply.unpack() if reply is not None else ()

    def one(self, interface, method, signature=None, *args, **kw):
        """Like call(), returning the single output value."""
        return self.call(interface, method, signature, *args, **kw)[0]

    def properties(self, path, interface=JOB_INTERFACE):
        try:
            reply = self.connection.call_sync(
                NAME, path, "org.freedesktop.DBus.Properties", "GetAll", GLib.Variant("(s)", (interface,)), None,
                Gio.DBusCallFlags.NONE, 10_000, None)
        except GLib.Error as error:
            raise ClientError.from_gerror(error) from None
        return reply.unpack()[0]

    def status(self):
        return self.one("Manager", "GetStatus")

    # -- jobs ------------------------------------------------------------------------------------

    def collect(self):
        """Start recording job signals; pass the result to wait() after starting a job."""
        return Collector(self.connection)

    def start_and_wait(self, interface, method, signature, *args, on_event=None, fds=None):
        """Call a method that returns a job, follow it to the end and return the JobResult."""
        collector = self.collect()
        try:
            path = self.one(interface, method, signature, *args, fds=fds)
        except BaseException:
            collector.close()
            raise
        return self.wait(path, on_event, collector=collector)

    def wait(self, path, on_event=None, interrupt=None, collector=None):
        """Follow a job until it ends. `on_event(kind, *args)` receives:
             ("step", id, title)  ("progress", fraction)  ("message", text)  ("finished", ok, message)
        `interrupt()` is called once on Ctrl-C; it may cancel the job. Returns a JobResult."""
        emit = on_event or (lambda *a: None)
        loop = GLib.MainLoop()
        state = {"done": False, "step": None, "progress": None, "interrupted": False}

        def finish():
            if not state["done"]:
                state["done"] = True
                loop.quit()

        def handle(connection, sender, object_path, interface, name, parameters):
            args = parameters.unpack()
            if name == "StepChanged":
                state["step"] = args[0]
                emit("step", *args)
            elif name == "ProgressChanged":
                state["progress"] = args[0]
                emit("progress", args[0])
            elif name == "Message":
                emit("message", args[0])
            elif name == "Finished":
                emit("finished", *args)
                finish()

        subscription = self.connection.signal_subscribe(
            NAME, JOB_INTERFACE, None, path, None, Gio.DBusSignalFlags.NONE, handle)
        if collector is not None:
            # Replay what this job already said before we were listening, in order.
            for object_path, name, args in list(collector.events):
                if object_path == path:
                    handle(None, None, object_path, JOB_INTERFACE, name, GLib.Variant(JOB_SIGNALS[name], args))
            collector.close()

        def poll():  # a safety net: a signal sent before we subscribed, or lost, must not hang us
            try:
                props = self.properties(path)
            except ClientError:
                finish()
                return GLib.SOURCE_REMOVE
            if props["Step"] and props["Step"] != state["step"]:
                state["step"] = props["Step"]
                emit("step", props["Step"], props["StepTitle"])
            if props["Progress"] >= 0 and props["Progress"] != state["progress"]:
                state["progress"] = props["Progress"]
                emit("progress", props["Progress"])
            if props["State"] != "running":
                finish()
                return GLib.SOURCE_REMOVE
            return GLib.SOURCE_CONTINUE

        def on_sigint():
            if not state["interrupted"] and interrupt is not None:
                state["interrupted"] = True
                interrupt(path)
            return GLib.SOURCE_CONTINUE

        sigint = GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, on_sigint)
        try:
            if state["done"]:
                pass
            elif poll() == GLib.SOURCE_CONTINUE:
                GLib.timeout_add_seconds(1, poll)
                loop.run()
        finally:
            GLib.source_remove(sigint)
            self.connection.signal_unsubscribe(subscription)
        return JobResult(path, self.properties(path))

    def cancel(self, path):
        self.call(JOB_INTERFACE, "Cancel", path=path)

    def logs(self, path, count=0):
        return self.one(JOB_INTERFACE, "Logs", "(i)", count, path=path)
