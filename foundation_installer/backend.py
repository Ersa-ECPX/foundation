"""Asynchronous access to foundationd for a GTK main loop. Callbacks run on the main loop.

The synchronous client in foundation.client is built for a terminal that waits; a window must not
block, so calls here use Gio's asynchronous API and job signals instead of a nested loop.
"""

from gi.repository import Gio, GLib

from foundation.client import (JOB_INTERFACE, JOB_SIGNALS, NAME, PATH, PREFIX, Client, ClientError,
                               JobResult)


class Backend:
    def __init__(self, bus="system", connection=None):
        if connection is None:
            connection = Client(bus).connection     # raises ClientError if the bus cannot be reached
        self.connection = connection                # tests pass a connection to a private bus

    def call(self, interface, method, signature, args, done, timeout=60_000, path=PATH):
        """done(values, None) with the unpacked reply tuple, or done(None, ClientError)."""
        def finished(connection, result):
            try:
                reply = connection.call_finish(result)
            except GLib.Error as error:
                done(None, ClientError.from_gerror(error))
                return
            done(reply.unpack() if reply is not None else (), None)

        name = interface if "." in interface else PREFIX + interface
        self.connection.call(NAME, path, name, method, GLib.Variant(signature, args) if signature else None,
                             None, Gio.DBusCallFlags.ALLOW_INTERACTIVE_AUTHORIZATION, timeout, None, finished)

    def properties(self, path, done):
        def got(values, error):
            done(values[0] if values else None, error)
        self.call("org.freedesktop.DBus.Properties", "GetAll", "(s)", (JOB_INTERFACE,), got, 10_000, path)

    def follow(self, start, on_event, done):
        """Start a job with start(callback_with_path_or_error) and follow it to the end.

        on_event(kind, *args) with kinds step(id, title), progress(fraction), message(text).
        done(JobResult, None) when it ends, or done(None, ClientError) if it could not be started.
        """
        follower = _Follower(self, on_event, done)
        follower.begin(start)
        return follower

    def close(self):
        pass


class _Follower:
    """Subscribes to every job's signals before the job exists, because foundationd starts working as
    soon as it has replied; the signals of other jobs are ignored once the path is known."""

    def __init__(self, backend, on_event, done):
        self.backend, self.on_event, self.done_cb = backend, on_event, done
        self.path, self.buffer, self.finished = None, [], False
        self.subscription = backend.connection.signal_subscribe(
            NAME, JOB_INTERFACE, None, None, None, Gio.DBusSignalFlags.NONE, self._signal)
        self.timer = 0

    def begin(self, start):
        def started(path, error):
            if error is not None:
                self._stop()
                self.done_cb(None, error)
                return
            self.path = path
            for object_path, name, args in self.buffer:
                self._handle(object_path, name, args)
            self.buffer = []
            if not self.finished:
                self.timer = GLib.timeout_add_seconds(2, self._poll)
        start(started)

    def _signal(self, connection, sender, object_path, interface, name, parameters):
        args = parameters.unpack()
        if self.path is None:
            self.buffer.append((object_path, name, args))
        else:
            self._handle(object_path, name, args)

    def _handle(self, object_path, name, args):
        if object_path != self.path or self.finished:
            return
        if name == "StepChanged":
            self.on_event("step", *args)
        elif name == "ProgressChanged":
            self.on_event("progress", args[0])
        elif name == "Message":
            self.on_event("message", args[0])
        elif name == "Finished":
            self._finish()

    def _poll(self):
        """A safety net for a signal that was lost: ask the job itself."""
        def got(props, error):
            if self.finished:
                return
            if error is not None or (props and props["State"] != "running"):
                self._finish()
        self.backend.properties(self.path, got)
        return GLib.SOURCE_CONTINUE if not self.finished else GLib.SOURCE_REMOVE

    def _stop(self):
        self.finished = True
        if self.subscription is not None:
            self.backend.connection.signal_unsubscribe(self.subscription)
            self.subscription = None
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = 0

    def _finish(self):
        if self.finished:
            return
        self._stop()

        def got(props, error):
            if error is not None:
                self.done_cb(None, error)
            else:
                self.done_cb(JobResult(self.path, props), None)
        self.backend.properties(self.path, got)


__all__ = ["Backend", "ClientError", "JOB_SIGNALS"]
