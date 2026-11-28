"""devices.* workers: this computer's link to an Ersa ID account and its health check-ins."""

from ..backends import devices
from ..workerlib import worker


@worker("devices.status")
def status(ctx):
    return devices.status(ctx.paths, ctx.arg("enabled", bool, True), ctx.arg("url"))


@worker("devices.report")
def report(ctx):
    return devices.report(ctx.paths)


@worker("devices.prepare_link")
def prepare_link(ctx):
    return {"public_key": devices.public_key(ctx.paths)}


@worker("devices.sign_link")
def sign_link(ctx):
    return devices.sign_link(ctx.paths, ctx.arg("challenge"))


@worker("devices.complete_link")
def complete_link(ctx):
    devices.link(ctx.paths, ctx.arg("id"), ctx.arg("url"))
    return {}


@worker("devices.unlink")
def unlink(ctx):
    return {"was_linked": devices.unlink(ctx.paths)}


@worker("devices.checkin")
def checkin(ctx):
    return {"result": devices.checkin_if_due(ctx.paths, ctx.arg("enabled", bool, True), ctx.arg("due", bool, True))}
