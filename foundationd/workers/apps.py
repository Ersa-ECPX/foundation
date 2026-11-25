"""apps.* workers: Flatpak applications. Queries: list, search, info. Jobs: setup, install, remove, update."""

import contextlib
import signal

from ..backends import flatpak
from ..backends.flatpak import REMOTE_NAME, Flatpak
from ..errors import Cancelled, Unavailable
from ..workerlib import worker


@contextlib.contextmanager
def cancellable_on_sigterm():
    """A Gio.Cancellable that SIGTERM cancels. A Python signal handler cannot interrupt a blocking
    libflatpak call, but cancelling the Cancellable does stop the transaction cleanly."""
    from gi.repository import Gio

    cancellable = Gio.Cancellable()
    previous = signal.signal(signal.SIGTERM, lambda signum, frame: cancellable.cancel())
    try:
        yield cancellable
    except BaseException:
        if cancellable.is_cancelled():
            raise Cancelled() from None
        raise
    finally:
        signal.signal(signal.SIGTERM, previous)


@worker("apps.list")
def list_installed(ctx):
    return Flatpak().installed()


@worker("apps.search")
def search(ctx):
    return Flatpak().search(ctx.arg("term"))


@worker("apps.info")
def info(ctx):
    backend = Flatpak()
    if not backend.has_remote():
        raise Unavailable(f"the '{REMOTE_NAME}' remote is not configured")
    return backend.remote_info(REMOTE_NAME, ctx.arg("id"))


@worker("apps.setup")
def setup(ctx):
    ctx.step("remote", "Adding the Flathub application source")
    added = Flatpak().add_remote()
    return {"remote": REMOTE_NAME, "added": added}


@worker("apps.install")
def install(ctx):
    app_id, remote = ctx.arg("id"), ctx.arg("remote", str, REMOTE_NAME) or REMOTE_NAME
    ctx.step("install", f"Installing {app_id}")
    with cancellable_on_sigterm() as cancellable:
        Flatpak().install(app_id, remote, ctx.progress, cancellable)
    return {"id": app_id, "remote": remote}


@worker("apps.remotes")
def remotes(ctx):
    return Flatpak().remote_details()


@worker("apps.inspect_remote")
def inspect_remote(ctx):
    info, _repo, _key = flatpak.inspect_remote(ctx.arg("location"), ctx.arg("key", str, ""))
    return info


@worker("apps.add_remote")
def add_remote(ctx):
    ctx.step("add", "Adding the application source")
    return Flatpak().add_custom_remote(ctx.arg("name"), ctx.arg("location"), ctx.arg("key", str, ""), ctx.arg("title", str, ""))


@worker("apps.set_remote_enabled")
def set_remote_enabled(ctx):
    enabled = ctx.arg("enabled", bool, True)
    ctx.step("change", "Turning the application source " + ("on" if enabled else "off"))
    return Flatpak().set_remote_enabled(ctx.arg("name"), enabled)


@worker("apps.remove_remote")
def remove_remote(ctx):
    ctx.step("remove", "Removing the application source")
    return Flatpak().remove_remote(ctx.arg("name"))


@worker("apps.remove")
def remove(ctx):
    app_id = ctx.arg("id")
    ctx.step("remove", f"Removing {app_id}")
    with cancellable_on_sigterm() as cancellable:
        Flatpak().uninstall(app_id, ctx.progress, cancellable)
    return {"id": app_id}


@worker("apps.update")
def update(ctx):
    ctx.step("update", "Updating applications")
    with cancellable_on_sigterm() as cancellable:
        updated = Flatpak().update_all(ctx.progress, cancellable)
    return {"updated": updated}
