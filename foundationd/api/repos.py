"""org.ersa.Foundation1.Repos: package repositories (workers: repos.*), and the bundle methods of Compat."""

from ..service import Method
from ..variants import reply_list
from . import validate

REPOS = "org.ersa.Foundation1.Repos"
ACTION_REPOS = "org.ersa.foundation.repos"
LOCKS = ("repos",)


def register(service):
    jobs = service.jobs

    def list_all(call):
        return service.query(call, "repos.list", {}, reply_list)

    def search(call):
        return service.query(call, "repos.search", {"query": validate.search_term(call.args[0])}, reply_list)

    def info(call):
        return service.query(call, "repos.info", {"name": validate.package(call.args[0])}, reply_list)

    def check_enable(call):
        validate.alias(call.args[0])
        jobs.check_locks(LOCKS)

    def enable(call):
        alias, enabled = call.args
        return service.start_job("repos.enable", {"alias": validate.alias(alias), "enabled": bool(enabled)}, LOCKS)

    def check_refresh(call):
        validate.aliases(call.args[0])
        jobs.check_locks(LOCKS)

    def refresh(call):
        aliases, force = call.args
        return service.start_job("repos.refresh", {"aliases": validate.aliases(aliases), "force": bool(force)}, LOCKS)

    def inspect_key(call):
        return service.query(call, "repos.inspect_key", {"key": validate.key_text(call.args[0])}, reply_list)

    def check_add(call):
        alias, name, url, priority, key = call.args
        validate.alias(alias)
        validate.repo_url(url)
        validate.key_text(key)
        jobs.check_locks(LOCKS)

    def add(call):
        alias, name, url, priority, key = call.args
        return service.start_job("repos.add", {"alias": validate.alias(alias), "name": validate.description(name),
                                               "url": validate.repo_url(url), "priority": int(priority),
                                               "key": validate.key_text(key)}, LOCKS)

    def remove(call):
        return service.start_job("repos.remove", {"alias": validate.alias(call.args[0])}, LOCKS)

    return {
        (REPOS, "InspectKey"): Method(inspect_key),
        (REPOS, "Add"): Method(add, ACTION_REPOS, check_add),
        (REPOS, "Remove"): Method(remove, ACTION_REPOS, check_enable),
        (REPOS, "List"): Method(list_all),
        (REPOS, "Search"): Method(search),
        (REPOS, "Info"): Method(info),
        (REPOS, "SetEnabled"): Method(enable, ACTION_REPOS, check_enable),
        (REPOS, "Refresh"): Method(refresh, ACTION_REPOS, check_refresh),
    }
