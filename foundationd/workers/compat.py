"""compat.* workers: legacy RPMs as system extensions."""

from ..backends import compat
from ..workerlib import worker


@worker("compat.inspect")
def inspect(ctx):
    report, _extras = compat.analyse(ctx.arg("path"), ctx.paths)
    return report


@worker("compat.install")
def install(ctx):
    return compat.install(ctx.arg("path"), ctx.arg("force", bool, False), ctx.paths, ctx)


@worker("compat.remove")
def remove(ctx):
    return compat.remove(ctx.arg("name"), ctx.paths, ctx)


@worker("compat.list")
def list_installed(ctx):
    return compat.list_installed(ctx.paths)


@worker("compat.revalidate")
def revalidate(ctx):
    return compat.revalidate(ctx.paths, ctx)
