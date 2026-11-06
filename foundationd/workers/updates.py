"""updates.* workers: installing a system image update."""

from ..backends import updates
from ..workerlib import worker


@worker("updates.install")
def install(ctx):
    return updates.install(ctx.paths, ctx.arg("path"), ctx, target=ctx.arg("target", str, ""),
                           unsigned=ctx.arg("unsigned", bool, False), force=ctx.arg("force", bool, False))


@worker("updates.pending")
def pending(ctx):
    return updates.pending(ctx.paths)
