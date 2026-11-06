"""boot.* workers: slot and boot state (queries) and slot changes (jobs)."""

from ..backends import slots, splash
from ..workerlib import worker


@worker("boot.state")
def state(ctx):
    return slots.boot_state(ctx.paths)


@worker("boot.slots")
def list_slots(ctx):
    return slots.slots(ctx.paths)


@worker("boot.set_default")
def set_default(ctx):
    name = ctx.arg("slot")
    ctx.step("select", f"Setting slot {name} as the default")
    return slots.select(ctx.paths, name, once=False)


@worker("boot.set_next")
def set_next(ctx):
    name = ctx.arg("slot")
    ctx.step("select", f"Booting slot {name} at the next boot only")
    return slots.select(ctx.paths, name, once=True)


@worker("boot.copy")
def copy(ctx):
    return slots.copy(ctx.paths, ctx.arg("source", str, ""), ctx.arg("target"), ctx.arg("force", bool, False), ctx)


@worker("boot.release")
def release(ctx):
    return slots.release(ctx.paths, ctx.arg("slot"), ctx)


@worker("boot.splash")
def set_splash(ctx):
    return splash.set_theme(ctx.paths, ctx.arg("theme"), ctx)
