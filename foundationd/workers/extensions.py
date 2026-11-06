"""ext.* workers: sysext and confext images."""

import os

from ..backends import extensions
from ..workerlib import worker


@worker("ext.list")
def list_all(ctx):
    return extensions.list_extensions(ctx.arg("kind"), ctx.paths["root"], os.path.join(ctx.paths["state"], "compat"))


def _operation(name, title):
    @worker(f"ext.{name}")
    def run_it(ctx):
        kind = ctx.arg("kind")
        ctx.step(name, f"{title} {kind} extensions")
        return extensions.operate(kind, name)
    return run_it


_operation("merge", "Merging")
_operation("unmerge", "Unmerging")
_operation("refresh", "Refreshing")
