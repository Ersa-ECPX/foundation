"""System-wide Flatpak applications through libflatpak (GObject introspection).

Listing, installing, removing, updating and adding the remote use the library, which gives
structured errors and real progress. Searching and showing a remote application's details use the
`flatpak` command, because libflatpak has no search API (the CLI reads the appstream data); those
two are read-only.

The Flatpak module is injected so tests can use a fake.
"""

import urllib.request

from ..errors import Failed, FoundationError, NotFound, Unavailable
from .runner import check

REMOTE_NAME = "flathub"
REMOTE_URL = "https://dl.flathub.org/repo/flathub.flatpakrepo"
DEFAULT_BRANCH = "stable"
INFO_KEYS = ("ID", "Ref", "Arch", "Branch", "Version", "License", "Origin", "Download", "Installed")


def load_flatpak():
    try:
        import gi
        gi.require_version("Flatpak", "1.0")
        from gi.repository import Flatpak
    except (ImportError, ValueError) as error:
        raise Unavailable("libflatpak (typelib-1_0-Flatpak-1_0) is not installed") from error
    return Flatpak


def _rows(output, columns, header_words=()):
    rows = []
    for line in output.splitlines():
        cells = [c.strip() for c in line.split("\t")]
        if len(cells) < columns or not cells[0]:
            continue
        if cells[0].lower() in header_words or cells[0].lower().startswith("no matches"):
            continue
        rows.append(cells[:columns])
    return rows


class Flatpak:
    def __init__(self, flatpak=None, runner=None):
        self.f = flatpak or load_flatpak()
        self._runner = runner
        self._installation = None

    @property
    def installation(self):
        if self._installation is None:
            try:
                self._installation = self.f.Installation.new_system(None)
            except Exception as error:  # GLib.Error: no system installation
                raise Unavailable(f"cannot open the system Flatpak installation: {error}") from error
        return self._installation

    # -- reading ---------------------------------------------------------------------------------

    def installed(self):
        refs = self.installation.list_installed_refs_by_kind(self.f.RefKind.APP, None)
        return sorted(({"id": ref.get_name(), "name": ref.get_appdata_name() or ref.get_name(),
                        "version": ref.get_appdata_version() or "", "origin": ref.get_origin() or ""}
                       for ref in refs), key=lambda app: app["id"])

    def remotes(self):
        return [{"name": r.get_name(), "url": r.get_url() or ""} for r in self.installation.list_remotes(None)]

    def has_remote(self, name=REMOTE_NAME):
        return any(r["name"] == name for r in self.remotes())

    def search(self, term):
        args = ["flatpak", "search", "--columns=application,name,description,version,remotes", term]
        out = self._check(args, 60)
        return [dict(zip(("id", "name", "description", "version", "remote"), r))
                for r in _rows(out, 5, ("application", "application id"))]

    def remote_info(self, remote, app_id):
        out = self._check(["flatpak", "remote-info", "--system", remote, app_id], 60, missing=app_id)
        info = {}
        for line in out.splitlines():
            key, sep, value = line.partition(":")
            if sep and key.strip() in INFO_KEYS:
                info[key.strip()] = value.strip()
        return info

    def _check(self, cmd, timeout, missing=None):
        from .runner import run
        try:
            return check(cmd, timeout, runner=self._runner or run)
        except Failed as error:
            if missing:
                raise NotFound(f"{missing} was not found in the remote") from error
            raise

    # -- changing --------------------------------------------------------------------------------

    def add_remote(self, fetch=None):
        """Add the Flathub remote. Returns True if it was added, False if it was already there."""
        if self.has_remote():
            return False
        try:
            with (fetch or urllib.request.urlopen)(REMOTE_URL, timeout=30) as response:
                data = response.read()
        except OSError as error:
            raise Unavailable(f"cannot download the Flathub remote description: {error}") from error
        from gi.repository import GLib  # only needed here; the fake module test passes `fetch` too
        remote = self.f.Remote.new_from_file(REMOTE_NAME, GLib.Bytes.new(data))
        try:
            self.installation.add_remote(remote, True, None)
        except Exception as error:  # noqa: BLE001 - GLib.Error
            raise Failed(f"cannot add the remote: {error}") from error
        return True

    def _installed_ref(self, app_id):
        for ref in self.installation.list_installed_refs_by_kind(self.f.RefKind.APP, None):
            if ref.get_name() == app_id:
                return ref
        return None

    def install(self, app_id, remote, report, cancellable=None):
        if not self.has_remote(remote):
            raise Unavailable(f"the '{remote}' remote is not configured; add it first")
        if self._installed_ref(app_id) is not None:
            raise Failed(f"{app_id} is already installed")
        ref = f"app/{app_id}/{self.f.get_default_arch()}/{DEFAULT_BRANCH}"
        transaction = self._transaction()
        transaction.add_install(remote, ref, None)
        self._run(transaction, report, cancellable)

    def uninstall(self, app_id, report, cancellable=None):
        ref = self._installed_ref(app_id)
        if ref is None:
            raise NotFound(f"{app_id} is not installed")
        transaction = self._transaction()
        transaction.add_uninstall(ref.format_ref())
        self._run(transaction, report, cancellable)

    def update_all(self, report, cancellable=None):
        """Update everything that has an update. Returns the application ids that were updated."""
        refs = self.installation.list_installed_refs_for_update(cancellable)
        if not refs:
            return []
        transaction = self._transaction()
        for ref in refs:
            transaction.add_update(ref.format_ref(), None, None)
        self._run(transaction, report, cancellable)
        return sorted({ref.get_name() for ref in refs})

    def _transaction(self):
        transaction = self.f.Transaction.new_for_installation(self.installation, None)
        transaction.set_no_interaction(True)
        return transaction

    def _run(self, transaction, report, cancellable):
        """Run a transaction, mapping its progress to a single 0..1 fraction."""
        state = {"total": 1, "done": 0, "error": None}

        def ready(_transaction):
            state["total"] = max(1, len(_transaction.get_operations()))
            return True

        def new_operation(_transaction, operation, progress):
            def changed(p):
                report((state["done"] + p.get_progress() / 100.0) / state["total"])
            progress.connect("changed", changed)

        def operation_done(_transaction, operation, commit, result):
            state["done"] += 1
            report(state["done"] / state["total"])

        def operation_error(_transaction, operation, error, details):
            state["error"] = error.message if hasattr(error, "message") else str(error)
            return False  # stop at the first failed operation

        transaction.connect("ready", ready)
        transaction.connect("new-operation", new_operation)
        transaction.connect("operation-done", operation_done)
        transaction.connect("operation-error", operation_error)
        try:
            transaction.run(cancellable)
        except Exception as error:  # noqa: BLE001 - GLib.Error
            raise self._translate(error, state["error"]) from error

    def _is(self, error, name):
        """True if `error` is the Flatpak error code `name` (checked against Flatpak's domain)."""
        code = getattr(self.f.Error, name, None)
        quark = getattr(self.f.Error, "quark", None)
        return code is not None and quark is not None and hasattr(error, "matches") \
            and error.matches(quark(), code)

    def _translate(self, error, detail):
        message = detail or getattr(error, "message", None) or str(error)
        if isinstance(error, FoundationError):
            return error
        if any(self._is(error, name) for name in ("REF_NOT_FOUND", "REMOTE_NOT_FOUND", "NOT_INSTALLED")):
            return NotFound(message)
        return Failed(message)
