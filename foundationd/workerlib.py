"""The protocol between the broker and a worker process, and the worker-side runtime.

A worker is a short-lived process started for one job (or one query). The broker writes a single
JSON line to its standard input:

    {"kind": "apps.install", "args": {...}, "paths": {...}}

and reads JSON lines from its standard output, one event each:

    {"event": "step", "id": "download", "title": "Downloading"}
    {"event": "progress", "fraction": 0.4}          # 0.0 to 1.0
    {"event": "log", "text": "..."}
    {"event": "uncancellable"}                      # the point of no return was passed
    {"event": "result", "data": {...}}              # success; data is JSON (dict or list)
    {"event": "error", "name": "NotFound", "message": "..."}

Anything a backend prints on its own cannot corrupt this channel: at start-up the real standard
output is moved to a private descriptor and descriptor 1 is pointed at standard error (the journal).

Cancellation is a SIGTERM from the broker. It raises `Cancelled` in the worker, so `finally` blocks
run and temporary state is cleaned up. After `ctx.commit()` the signal is ignored: from then on the
job runs to completion, and the broker has been told it can no longer be cancelled.
"""

import json
import os
import signal
import sys
import traceback

from .errors import Cancelled, FoundationError, InvalidArgument, Unavailable

REGISTRY = {}


def worker(kind):
    """Register a function as the implementation of a job or query kind."""
    def register(function):
        if kind in REGISTRY:
            raise RuntimeError(f"duplicate worker kind {kind}")
        REGISTRY[kind] = function
        return function
    return register


class Context:
    """What a worker function receives: its arguments, system paths, and a way to report."""

    def __init__(self, args, paths, out):
        self.args = args
        self.paths = paths
        self._out = out
        self._committed = False

    def _emit(self, **event):
        self._out.write(json.dumps(event, separators=(",", ":")) + "\n")
        self._out.flush()

    def step(self, step_id, title):
        self._emit(event="step", id=step_id, title=title)

    def progress(self, fraction):
        self._emit(event="progress", fraction=max(0.0, min(1.0, float(fraction))))

    def log(self, text):
        self._emit(event="log", text=str(text))

    def commit(self):
        """Mark the point of no return: ignore cancellation from now on."""
        if not self._committed:
            self._committed = True
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            self._emit(event="uncancellable")

    def arg(self, name, kind=str, default=None):
        """A required (default None) or optional argument, checked for type."""
        value = self.args.get(name, default)
        if value is None:
            raise InvalidArgument(f"missing argument: {name}")
        if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
            raise InvalidArgument(f"argument {name} must be {kind.__name__}")
        return value


def _raise_cancelled(signum, frame):
    raise Cancelled()


def main(kind, stdin=None, stdout=None):
    """Run one worker. Returns the process exit status."""
    stdin = stdin or sys.stdin
    # Keep the event channel private; stray prints from backends go to the journal instead.
    out = stdout or os.fdopen(os.dup(1), "w", encoding="utf-8")
    if stdout is None:
        os.dup2(2, 1)

    from . import workers  # noqa: F401  (importing registers the kinds)

    def emit(**event):
        out.write(json.dumps(event, separators=(",", ":")) + "\n")
        out.flush()

    signal.signal(signal.SIGTERM, _raise_cancelled)
    try:
        request = json.loads(stdin.readline())
        if request.get("kind") != kind:
            raise InvalidArgument("worker kind does not match the request")
        function = REGISTRY.get(kind)
        if function is None:
            raise Unavailable(f"unknown worker kind: {kind}")
        context = Context(request.get("args") or {}, request.get("paths") or {}, out)
        data = function(context)
        emit(event="result", data=data if data is not None else {})
        return 0
    except Cancelled:
        emit(event="error", name="Failed", message="cancelled")
        return 130
    except FoundationError as error:
        emit(event="error", name=error.name, message=str(error))
        return 1
    except Exception as error:  # noqa: BLE001
        traceback.print_exc()
        emit(event="error", name="Failed", message=f"internal error: {error.__class__.__name__}")
        return 1
