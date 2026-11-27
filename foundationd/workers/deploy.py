"""deploy.* workers: list disks, plan an installation (queries) and install (a job)."""

from ..backends import deploy, encryption
from ..workerlib import worker


def _allow_file(ctx):
    return bool(ctx.paths.get("allow_file_targets"))


@worker("deploy.disks")
def disks(ctx):
    return deploy.list_disks(ctx.paths)


@worker("deploy.plan")
def plan(ctx):
    return deploy.plan(ctx.paths, ctx.arg("disk"), _allow_file(ctx))


@worker("deploy.run")
def run(ctx):
    disk = ctx.arg("disk")
    secrets_path = ctx.arg("secrets_path", str, "")
    secrets = encryption.read_secrets_file(secrets_path) if secrets_path else None      # reads and deletes the staged file
    return deploy.deploy(ctx.paths, disk, ctx.arg("confirm"), ctx, allow_file=_allow_file(ctx),
                         language=ctx.arg("language") or "", timezone=ctx.arg("timezone") or "",
                         preseed=ctx.arg("preseed", dict, {}), secrets=secrets)
