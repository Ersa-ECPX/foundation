"""org.ersa.Foundation1.Extensions: systemd-sysext and systemd-confext images (workers: ext.*)."""

from ..service import Method
from ..variants import reply_list
from . import validate

EXTENSIONS = "org.ersa.Foundation1.Extensions"
ACTION_EXTENSIONS = "org.ersa.foundation.extensions"
LOCKS = ("extensions",)


def register(service):
    jobs = service.jobs

    def list_all(call):
        return service.query(call, "ext.list", {"kind": validate.kind(call.args[0])}, reply_list)

    def check(call):
        validate.kind(call.args[0])
        jobs.check_locks(LOCKS)

    def job(operation):
        def handler(call):
            return service.start_job(f"ext.{operation}", {"kind": validate.kind(call.args[0])}, LOCKS)
        return handler

    return {
        (EXTENSIONS, "List"): Method(list_all),
        (EXTENSIONS, "Merge"): Method(job("merge"), ACTION_EXTENSIONS, check),
        (EXTENSIONS, "Unmerge"): Method(job("unmerge"), ACTION_EXTENSIONS, check),
        (EXTENSIONS, "Refresh"): Method(job("refresh"), ACTION_EXTENSIONS, check),
    }
