"""updates.* workers: installing a system image update."""

from ..backends import ota, updates
from ..workerlib import worker


@worker("updates.install")
def install(ctx):
    return updates.install(ctx.paths, ctx.arg("path"), ctx, target=ctx.arg("target", str, ""),
                           unsigned=ctx.arg("unsigned", bool, False), force=ctx.arg("force", bool, False))


@worker("updates.pending")
def pending(ctx):
    updates.clear_pending(ctx.paths)       # a restart that has happened is not pending any more
    return updates.pending(ctx.paths)


@worker("updates.check")
def check(ctx):
    return ota.check(ctx.paths, ctx.arg("url"), ctx.arg("arch"))


@worker("updates.available")
def available(ctx):
    return ota.available(ctx.paths)


@worker("updates.install_latest")
def install_latest(ctx):
    return ota.install_latest(ctx.paths, ctx.arg("url"), ctx.arg("arch"), ctx)
