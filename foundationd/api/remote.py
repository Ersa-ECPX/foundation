"""org.ersa.Foundation1.Remote: the SSH server and the keys allowed to log in (workers: remote.*)."""

from ..service import Method
from ..variants import reply_dict, reply_list
from . import validate

REMOTE = "org.ersa.Foundation1.Remote"
ACTION_REMOTE = "org.ersa.foundation.remote"
LOCKS = ("remote",)


def register(service):
    jobs = service.jobs

    def status(call):
        return service.query(call, "remote.status", {}, reply_dict)

    def keys(call):
        (user,) = call.args
        return service.query(call, "remote.keys", {"user": validate.optional_username(user)}, reply_list)

    def check_ssh(call):
        jobs.check_locks(LOCKS)

    def set_ssh(call):
        enable, allow_password = call.args
        return service.start_job("remote.set_ssh", {"enable": bool(enable), "allow_password": bool(allow_password)}, LOCKS)

    def check_add(call):
        user, key = call.args
        validate.username(user)
        validate.public_key(key)
        jobs.check_locks(LOCKS)

    def add_key(call):
        user, key = call.args
        return service.start_job("remote.add_key", {"user": validate.username(user), "key": validate.public_key(key)}, LOCKS)

    def check_remove(call):
        user, fingerprint = call.args
        validate.username(user)
        validate.fingerprint(fingerprint)
        jobs.check_locks(LOCKS)

    def remove_key(call):
        user, fingerprint = call.args
        return service.start_job("remote.remove_key", {"user": validate.username(user),
                                                       "fingerprint": validate.fingerprint(fingerprint)}, LOCKS)

    return {
        (REMOTE, "GetStatus"): Method(status),
        (REMOTE, "ListKeys"): Method(keys),
        (REMOTE, "SetSsh"): Method(set_ssh, ACTION_REMOTE, check_ssh),
        (REMOTE, "AddKey"): Method(add_key, ACTION_REMOTE, check_add),
        (REMOTE, "RemoveKey"): Method(remove_key, ACTION_REMOTE, check_remove),
    }
