"""System-wide Flatpak applications through libflatpak (GObject introspection).

Listing, installing, removing, updating and adding the remote use the library, which gives
structured errors and real progress. Searching and showing a remote application's details use the
`flatpak` command, because libflatpak has no search API (the CLI reads the appstream data); those
two are read-only.

The Flatpak module is injected so tests can use a fake.
"""

import base64
import configparser
import os
import re
import subprocess
import urllib.request

from ..errors import Failed, FoundationError, InvalidArgument, NotFound, Unavailable
from .runner import check, run

REMOTE_NAME = "flathub"
REMOTE_URL = "https://dl.flathub.org/repo/flathub.flatpakrepo"
DEFAULT_BRANCH = "stable"
INFO_KEYS = ("ID", "Ref", "Arch", "Branch", "Version", "License", "Origin", "Download", "Installed")
REMOTE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
MAX_REPO_FILE = 128 * 1024
MAX_KEY = 256 * 1024


def parse_flatpakrepo(data):
    """The fields of a .flatpakrepo file: title, url, comment, homepage and gpg_key (the key's bytes, empty if it has none)."""
    if len(data) > MAX_REPO_FILE:
        raise InvalidArgument("the repository description is too large")
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        parser.read_string(data.decode("utf-8"))
    except (UnicodeDecodeError, configparser.Error) as error:
        raise InvalidArgument(f"that is not a Flatpak repository description: {error}") from error
    if not parser.has_section("Flatpak Repo"):
        raise InvalidArgument("that is not a Flatpak repository description (no [Flatpak Repo] section)")
    section = parser["Flatpak Repo"]
    key = re.sub(r"\s+", "", section.get("GPGKey", ""))
    try:
        key_bytes = base64.b64decode(key, validate=True) if key else b""
    except ValueError as error:
        raise InvalidArgument("the repository's signing key is not valid") from error
    return {"title": section.get("Title", "").strip(), "url": section.get("Url", "").strip(),
            "comment": section.get("Comment", "").strip(), "homepage": section.get("Homepage", "").strip(), "gpg_key": key_bytes}


def _check_location(value):
    if not isinstance(value, str) or len(value) > 2048 or not value.startswith(("https://", "file:///")) \
            or any(c.isspace() or ord(c) < 32 for c in value):
        raise InvalidArgument("an application source must be an https:// address (or a file:/// path)")
    return value


def _read_location(location, fetch):
    """The bytes at an https:// address or a file:/// path (at most MAX_REPO_FILE)."""
    try:
        if location.startswith("file:///"):
            with open(location[len("file://"):], "rb") as handle:
                return handle.read(MAX_REPO_FILE + 1)
        with (fetch or urllib.request.urlopen)(location, timeout=30) as response:
            return response.read(MAX_REPO_FILE + 1)
    except OSError as error:
        raise Unavailable(f"cannot read {location}: {error}") from error


def key_summary(key, runner=run):
    """Fingerprints and owners of a public key given as bytes (binary or armoured). Refuses a secret key or no key."""
    if not key or len(key) > MAX_KEY or b"PRIVATE KEY" in key:
        raise InvalidArgument("the signing key is missing, too large, or a private key")
    code, out, error = runner(["gpg", "--batch", "--no-tty", "--with-colons", "--show-keys", "-"], 30, input_bytes=key)
    if code != 0:
        raise InvalidArgument(f"cannot read the signing key: {error.strip()[:200]}")
    keys, current = [], None
    for line in out.splitlines():
        fields = line.split(":")
        if fields[0] == "sec":
            raise InvalidArgument("that is a private key")
        if fields[0] == "pub":
            current = {"fingerprint": "", "owner": ""}
            keys.append(current)
        elif fields[0] == "fpr" and current is not None and not current["fingerprint"]:
            current["fingerprint"] = fields[9]
        elif fields[0] == "uid" and current is not None and not current["owner"]:
            current["owner"] = fields[9]
    if not keys:
        raise InvalidArgument("the file holds no public key")
    return keys


def _dearmor(text, dearmor=None):
    """The binary form of an armoured key (what a repository description carries)."""
    raw = text.encode() if isinstance(text, str) else text
    if b"BEGIN PGP PUBLIC KEY BLOCK" not in raw:
        return raw                                                    # already binary
    result = (dearmor or (lambda data: subprocess.run(["gpg", "--batch", "--no-tty", "--dearmor"], input=data, capture_output=True,
                                                       timeout=30)))(raw)
    if result.returncode != 0 or not result.stdout:
        raise InvalidArgument("cannot read the signing key")
    return result.stdout


def suggested_name(location):
    stem = os.path.basename(re.sub(r"^[a-z]+://", "", location).rstrip("/")).split(".")[0] or "remote"
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-.") or "remote"
    return cleaned[:63]


def inspect_remote(location, key_text="", fetch=None, runner=run):
    """What an application source is, before it is trusted: a .flatpakrepo address (its title, address and signing key come from
    the file) or a plain repository address with its public key given as text. A source without a signing key is refused: every
    application source must be signed. Returns name (a suggestion), title, url, comment, homepage, fingerprints, owners and
    description (the .flatpakrepo bytes to use, or b'' for a plain address) is kept inside the backend, not returned."""
    location = _check_location(location)
    info, repo_bytes, key = {"comment": "", "homepage": ""}, b"", b""
    if location.endswith(".flatpakrepo"):
        repo_bytes = _read_location(location, fetch)
        parsed = parse_flatpakrepo(repo_bytes)
        info.update(title=parsed["title"], url=_check_location(parsed["url"]) if parsed["url"] else "", comment=parsed["comment"],
                    homepage=parsed["homepage"])
        if not info["url"]:
            raise InvalidArgument("the repository description has no address")
        key = key_text.strip().encode() if key_text.strip() else parsed["gpg_key"]
    else:
        info.update(title="", url=location)
        key = key_text.strip().encode()
    if not key:
        raise InvalidArgument("an application source must be signed: give its public signing key")
    keys = key_summary(key, runner)
    info.update(name=suggested_name(location), fingerprints=[k["fingerprint"] for k in keys], owners=[k["owner"] for k in keys],
                from_file=bool(repo_bytes))
    return info, repo_bytes, key


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

    def remote_details(self):
        """Every application source with what a person wants to see: name, title, url, enabled, signed, default, apps (installed from it)."""
        installed = self.installed()
        rows = []
        for remote in self.installation.list_remotes(None):
            name = remote.get_name()
            title = getattr(remote, "get_title", lambda: "")() or ""
            rows.append({"name": name, "title": title or name, "url": remote.get_url() or "",
                         "enabled": not getattr(remote, "get_disabled", lambda: False)(),
                         "signed": bool(getattr(remote, "get_gpg_verify", lambda: True)()),
                         "default": name == REMOTE_NAME, "apps": sum(1 for a in installed if a["origin"] == name)})
        return sorted(rows, key=lambda r: (not r["default"], r["name"]))

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

    def add_custom_remote(self, name, location, key_text="", title="", fetch=None, runner=run, dearmor=None):
        """Add a signed application source of the person's own, system-wide. Returns its details."""
        if not isinstance(name, str) or not REMOTE_NAME_PATTERN.match(name):
            raise InvalidArgument(f"not a usable name for an application source: {name!r}")
        if self.has_remote(name):
            raise InvalidArgument(f"an application source named {name} exists already")
        title = (title or "").strip()
        if len(title) > 120 or "\n" in title:
            raise InvalidArgument("the title must be one line of up to 120 characters")
        info, repo_bytes, key = inspect_remote(location, key_text, fetch, runner)
        from gi.repository import GLib
        if repo_bytes and not key_text.strip():
            remote = self.f.Remote.new_from_file(name, GLib.Bytes.new(repo_bytes))
        else:
            lines = ["[Flatpak Repo]", f"Title={title or info['title'] or name}", f"Url={info['url']}",
                     "GPGKey=" + base64.b64encode(_dearmor(key, dearmor)).decode()]
            remote = self.f.Remote.new_from_file(name, GLib.Bytes.new("\n".join(lines).encode()))
        if title:
            remote.set_title(title)
        remote.set_gpg_verify(True)
        remote.set_disabled(False)
        try:
            self.installation.add_remote(remote, False, None)
        except Exception as error:  # noqa: BLE001 - GLib.Error
            raise Failed(f"cannot add the application source: {error}") from error
        return {"name": name, "url": info["url"], "fingerprints": info["fingerprints"]}

    def _find_remote(self, name):
        for remote in self.installation.list_remotes(None):
            if remote.get_name() == name:
                return remote
        raise NotFound(f"there is no application source named {name}")

    def set_remote_enabled(self, name, enabled):
        remote = self._find_remote(name)
        remote.set_disabled(not enabled)
        try:
            self.installation.modify_remote(remote, None)
        except Exception as error:  # noqa: BLE001 - GLib.Error
            raise Failed(f"cannot change the application source: {error}") from error
        return {"name": name, "enabled": bool(enabled)}

    def remove_remote(self, name):
        self._find_remote(name)
        using = [a["id"] for a in self.installed() if a["origin"] == name]
        if using:
            raise Failed(f"{len(using)} application(s) are installed from {name} ({', '.join(using[:5])}); remove them first")
        try:
            self.installation.remove_remote(name, None)
        except Exception as error:  # noqa: BLE001 - GLib.Error
            raise Failed(f"cannot remove the application source: {error}") from error
        return {"name": name}

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
