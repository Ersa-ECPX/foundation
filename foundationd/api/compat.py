"""org.ersa.Foundation1.Compat: legacy RPMs as system extensions (workers: compat.*).

The RPM arrives as a file descriptor. It is copied into a private staging directory first (without
blocking the daemon), and the worker reads that copy. The daemon never opens a path a client chose.
"""

import os

from ..errors import Failed, FoundationError
from ..service import DEFERRED, Method
from ..variants import reply_dict, reply_list
from . import validate



COMPAT = "org.ersa.Foundation1.Compat"
ACTION_COMPAT = "org.ersa.foundation.compat"
LOCKS = ("extensions",)


def _remove(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def register(service):
    jobs = service.jobs

    def inspect(call):
        fd = call.take_fd(call.args[0])

        def staged(path, error):
            if error:
                call.fail(Failed(f"could not read the RPM: {error}"))
                return
            service.query(call, "compat.inspect", {"path": path}, reply_dict, cleanup=lambda: _remove(path))

        service.stage(fd, ".rpm", staged)
        return DEFERRED

    def check_locks(call):
        jobs.check_locks(LOCKS)

    def install(call):
        handle, force = call.args
        if not isinstance(force, bool):
            raise FoundationError("force must be a boolean")
        fd = call.take_fd(handle)

        def staged(path, error):
            if error:
                call.fail(Failed(f"could not read the RPM: {error}"))
                return
            try:
                body = service.start_job("compat.install", {"path": path, "force": force}, LOCKS,
                                         cleanup=lambda: _remove(path))
            except FoundationError as failure:
                _remove(path)
                call.fail(failure)
                return
            call.reply(body)

        service.stage(fd, ".rpm", staged)
        return DEFERRED

    def check_name(call):
        validate.name(call.args[0])
        jobs.check_locks(LOCKS)

    def remove(call):
        return service.start_job("compat.remove", {"name": validate.name(call.args[0])}, LOCKS)

    def list_all(call):
        return service.query(call, "compat.list", {}, reply_list)

    def revalidate(call):
        return service.start_job("compat.revalidate", {}, LOCKS)

    return {
        (COMPAT, "Inspect"): Method(inspect),
        (COMPAT, "Install"): Method(install, ACTION_COMPAT, check_locks),
        (COMPAT, "Remove"): Method(remove, ACTION_COMPAT, check_name),
        (COMPAT, "List"): Method(list_all),
        (COMPAT, "Revalidate"): Method(revalidate, ACTION_COMPAT, check_locks),
    }
