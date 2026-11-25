"""org.ersa.Foundation1.Apps: system-wide Flatpak applications (workers: apps.*)."""

from ..service import Method
from ..variants import reply_dict, reply_list
from . import validate

APPS = "org.ersa.Foundation1.Apps"
ACTION_APPS = "org.ersa.foundation.apps"
ACTION_SOURCES = "org.ersa.foundation.apps-sources"
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

    def remotes(call):
        return service.query(call, "apps.remotes", {}, reply_list)

    def inspect_remote(call):
        location, key = call.args
        return service.query(call, "apps.inspect_remote",
                             {"location": validate.remote_location(location), "key": validate.optional_key_text(key)}, reply_dict)

    def check_remote_add(call):
        name, location, key, title = call.args
        validate.remote_name(name)
        validate.remote_location(location)
        validate.optional_key_text(key)
        validate.optional_title(title)
        jobs.check_locks(LOCKS)

    def add_remote(call):
        name, location, key, title = call.args
        return service.start_job("apps.add_remote", {"name": validate.remote_name(name), "location": validate.remote_location(location),
                                                     "key": validate.optional_key_text(key), "title": validate.optional_title(title)}, LOCKS)

    def check_remote_name(call):
        validate.remote_name(call.args[0])
        jobs.check_locks(LOCKS)

    def set_remote_enabled(call):
        name, enabled = call.args
        return service.start_job("apps.set_remote_enabled", {"name": validate.remote_name(name), "enabled": bool(enabled)}, LOCKS)

    def remove_remote(call):
        return service.start_job("apps.remove_remote", {"name": validate.remote_name(call.args[0])}, LOCKS)

    def check_install_from(call):
        validate.remote_name(call.args[0])
        validate.app_id(call.args[1])
        jobs.check_locks(LOCKS)

    def install_from(call):
        remote, app = call.args
        return service.start_job("apps.install", {"id": validate.app_id(app), "remote": validate.remote_name(remote)}, LOCKS)

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
        (APPS, "InstallFrom"): Method(install_from, ACTION_APPS, check_install_from),
        (APPS, "ListRemotes"): Method(remotes),
        (APPS, "InspectRemote"): Method(inspect_remote),
        (APPS, "AddRemote"): Method(add_remote, ACTION_SOURCES, check_remote_add),
        (APPS, "SetRemoteEnabled"): Method(set_remote_enabled, ACTION_SOURCES, check_remote_name),
        (APPS, "RemoveRemote"): Method(remove_remote, ACTION_SOURCES, check_remote_name),
    }
