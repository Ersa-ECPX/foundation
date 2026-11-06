"""org.ersa.Foundation1.Updates: install a system image update into the standby slot (workers: updates.*).

The archive arrives as a file descriptor and is copied into a private staging directory first, like an RPM for
Compat; the worker reads that copy. It takes the `slots` and `extensions` locks: while an image is being written
nothing else may change slots or merge extensions.
"""

import os

from ..errors import Failed, FoundationError
from ..service import DEFERRED, Method
from ..variants import reply_dict
from . import validate

UPDATES = "org.ersa.Foundation1.Updates"
ACTION_UPDATES = "org.ersa.foundation.updates"
LOCKS = ("slots", "extensions", "repos")


def _remove(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def register(service):
    jobs = service.jobs

    def check_locks(call):
        jobs.check_locks(LOCKS)

    def install(call):
        handle, target, unsigned, force = call.args
        if not isinstance(unsigned, bool) or not isinstance(force, bool):
            raise FoundationError("unsigned and force must be booleans")
        if target:
            validate.name(target)
        fd = call.take_fd(handle)

        def staged(path, error):
            if error:
                call.fail(Failed(f"could not read the update: {error}"))
                return
            try:
                body = service.start_job("updates.install", {"path": path, "target": target, "unsigned": unsigned, "force": force},
                                         LOCKS, cleanup=lambda: _remove(path))
            except FoundationError as failure:
                _remove(path)
                call.fail(failure)
                return
            call.reply(body)

        service.stage(fd, ".update", staged)
        return DEFERRED

    def pending(call):
        return service.query(call, "updates.pending", {}, reply_dict)

    return {
        (UPDATES, "Install"): Method(install, ACTION_UPDATES, check_locks),
        (UPDATES, "Pending"): Method(pending),
    }
