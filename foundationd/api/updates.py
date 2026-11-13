"""org.ersa.Foundation1.Updates: install a system image update into the standby slot (workers: updates.*).

The archive arrives as a file descriptor and is copied into a private staging directory first, like an RPM for
Compat; the worker reads that copy. It takes the `slots` and `extensions` locks: while an image is being written
nothing else may change slots or merge extensions.
"""

import os

from ..backends import updates as backend
from ..errors import Busy, Failed, FoundationError, InvalidArgument
from ..service import DEFERRED, Method
from ..variants import reply_dict
from . import validate
from .boot import reboot_now

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

    def feed_args():
        return {"url": service.config.get("ota.url"), "arch": os.uname().machine}

    def check(call):
        """Ask the update source (ota.url) whether a newer update exists. Reads only; no authorization."""
        return service.query(call, "updates.check", feed_args(), reply_dict)

    def available(call):
        return service.query(call, "updates.available", {}, reply_dict)

    def install_latest(call):
        return service.start_job("updates.install_latest", feed_args(), LOCKS)

    def restart(call):
        """Restart into the update that was just installed. It is part of the update (same authorization as Install) and works only
        while an update waits for a restart: it is not a general reboot (that is Boot.Reboot, another authorization)."""
        if not backend.pending({"state": service.paths.state}):
            raise InvalidArgument("no update is waiting for a restart")
        running = jobs.running_jobs()
        if running:
            raise Busy(f"{len(running)} job(s) are still running; wait for them")
        reboot_now(service, call)
        return DEFERRED

    return {
        (UPDATES, "Install"): Method(install, ACTION_UPDATES, check_locks),
        (UPDATES, "Restart"): Method(restart, ACTION_UPDATES),
        (UPDATES, "Check"): Method(check),
        (UPDATES, "Available"): Method(available),
        (UPDATES, "InstallLatest"): Method(install_latest, ACTION_UPDATES, check_locks),
        (UPDATES, "Pending"): Method(pending),
    }
