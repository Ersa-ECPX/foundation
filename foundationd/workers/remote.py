"""remote.* workers: the SSH server and its keys."""

from ..backends import remote
from ..workerlib import worker


@worker("remote.status")
def status(ctx):
    return remote.status(ctx.paths)


@worker("remote.keys")
def keys(ctx):
    return remote.list_keys(ctx.paths, ctx.arg("user", str, ""))


@worker("remote.set_ssh")
def set_ssh(ctx):
    return remote.set_ssh(ctx.paths, ctx, ctx.arg("enable", bool), ctx.arg("allow_password", bool, False))


@worker("remote.add_key")
def add_key(ctx):
    ctx.step("add", "Adding the key")
    return remote.add_key(ctx.paths, ctx.arg("user"), ctx.arg("key"))


@worker("remote.remove_key")
def remove_key(ctx):
    ctx.step("remove", "Removing the key")
    return remote.remove_key(ctx.paths, ctx.arg("user"), ctx.arg("fingerprint"))
