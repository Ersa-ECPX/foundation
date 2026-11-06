"""org.ersa.Foundation1.Apps: system-wide Flatpak applications (workers: apps.*)."""

from ..service import Method
from ..variants import reply_dict, reply_list
from . import validate

APPS = "org.ersa.Foundation1.Apps"
ACTION_APPS = "org.ersa.foundation.apps"
LOCKS = ("flatpak",)


def register(service):
    jobs = service.jobs

    def list_installed(call):
        return service.query(call, "apps.list", {}, reply_list)

    def search(call):
        return service.query(call, "apps.search", {"term": validate.search_term(call.args[0])}, reply_list)

    def info(call):
        return service.query(call, "apps.info", {"id": validate.app_id(call.args[0])}, reply_dict)

    def check_id(call):
        validate.app_id(call.args[0])
        jobs.check_locks(LOCKS)

    def check_locks(call):
        jobs.check_locks(LOCKS)

    def setup(call):
        return service.start_job("apps.setup", {}, LOCKS)

    def install(call):
        return service.start_job("apps.install", {"id": validate.app_id(call.args[0])}, LOCKS)

    def remove(call):
        return service.start_job("apps.remove", {"id": validate.app_id(call.args[0])}, LOCKS)

    def update(call):
        return service.start_job("apps.update", {}, LOCKS)

    return {
        (APPS, "ListInstalled"): Method(list_installed),
        (APPS, "Search"): Method(search),
        (APPS, "GetInfo"): Method(info),
        (APPS, "SetupRemote"): Method(setup, ACTION_APPS, check_locks),
        (APPS, "Install"): Method(install, ACTION_APPS, check_id),
        (APPS, "Remove"): Method(remove, ACTION_APPS, check_id),
        (APPS, "Update"): Method(update, ACTION_APPS, check_locks),
    }
