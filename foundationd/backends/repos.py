"""Package repositories: definitions, signed metadata, dependency solving and verified downloads.

Built on libsolv (the solver and metadata reader that libzypp and dnf use) through its Python bindings;
there is no zypper and no libzypp on the system. Repositories are described in zypp's `.repo` file format
so the files are familiar:

    /usr/lib/foundation/repos.d/*.repo     shipped with the image (read-only)
    /etc/foundation/repos.d/*.repo         local changes; a section with the same alias replaces the shipped one
    /etc/foundation/keys/*.asc             signing keys of repositories added with `foundation repo add`

What is trusted, and how:
  * Every repository names its signing key(s) by file (`gpgkey=file:///usr/share/foundation/keys/...`); keys
    are never fetched from the network. `repomd.xml` must carry a valid detached signature from one of them
    (`repo_gpgcheck`), the other metadata files must match the checksums `repomd.xml` lists, and every RPM
    must match its checksum and carry a valid signature (`gpgcheck`) from a key of its own repository.
  * Only https and file URLs are accepted, response sizes are capped, and a repository without signature
    checks is refused unless it is a local file repository (development).
  * Nothing here installs anything: it resolves package names to the RPM files that would be needed on top of
    what the base already has, and downloads and verifies them. bundles.py turns those into an extension.
"""

import concurrent.futures
import configparser
import hashlib
import os
import re
import shutil
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from ..errors import Failed, InvalidArgument, NotFound, Unavailable
from .runner import run

VENDOR_DIR = "usr/lib/foundation/repos.d"
LOCAL_DIR = "etc/foundation/repos.d"
ALIAS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
MAX_REPOMD = 4 * 1024 * 1024
MAX_METADATA = 1024 * 1024 * 1024
MAX_RPM = 4 * 1024 * 1024 * 1024
TIMEOUT = 60
REPOMD_NS = {"r": "http://linux.duke.edu/metadata/repo"}
HASHES = {"sha256": "sha256", "sha512": "sha512", "sha384": "sha384", "sha224": "sha224", "sha": "sha1", "sha1": "sha1"}


# -- definitions -------------------------------------------------------------------------------

@dataclass
class Repo:
    alias: str
    name: str
    baseurl: str
    enabled: bool = True
    priority: int = 99                  # zypp's meaning: a lower number wins
    gpgcheck: bool = True               # RPM signatures
    repo_gpgcheck: bool = True          # repomd.xml signature
    keys: list = field(default_factory=list)   # key files (absolute paths on this system)
    vendor: bool = False                # shipped with the image (a local file may still override it)

    def describe(self):
        return {"alias": self.alias, "name": self.name, "url": self.baseurl, "enabled": self.enabled,
                "priority": self.priority, "signed": self.gpgcheck and self.repo_gpgcheck, "shipped": self.vendor}


def _boolean(value, default):
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "yes", "true", "on")


def _under(root, path):
    return os.path.join(root, path.lstrip("/")) if root not in ("", "/") else path


def parse_repo_file(path, root="/", vendor=False):
    """The repositories of one `.repo` file. Raises InvalidArgument for a definition that is unsafe or broken."""
    parser = configparser.RawConfigParser(strict=False, interpolation=None)
    try:
        with open(path, encoding="utf-8") as handle:
            parser.read_file(handle)
    except (OSError, configparser.Error, UnicodeDecodeError) as error:
        raise InvalidArgument(f"cannot read {path}: {error}") from error
    found = []
    for alias in parser.sections():
        section = parser[alias]
        if not ALIAS.match(alias):
            raise InvalidArgument(f"{path}: unusable repository alias {alias!r}")
        url = (section.get("baseurl") or "").split()[0:1]
        if not url:
            raise InvalidArgument(f"{path}: repository {alias} has no baseurl")
        baseurl = url[0].rstrip("/") + "/"
        scheme = urllib.parse.urlparse(baseurl).scheme
        if scheme not in ("https", "file"):
            raise InvalidArgument(f"{path}: repository {alias}: only https and file URLs are accepted")
        gpgcheck = _boolean(section.get("gpgcheck"), True)
        repo_gpgcheck = _boolean(section.get("repo_gpgcheck"), True)
        if scheme != "file" and not (gpgcheck and repo_gpgcheck):
            raise InvalidArgument(f"{path}: repository {alias}: signature checks cannot be switched off for a remote repository")
        keys = []
        for key in (section.get("gpgkey") or "").split():
            if not key.startswith("file://"):
                raise InvalidArgument(f"{path}: repository {alias}: a gpgkey must be a file:// path")
            keys.append(_under(root, key[len("file://"):]))
        if (gpgcheck or repo_gpgcheck) and not keys:
            raise InvalidArgument(f"{path}: repository {alias} checks signatures but names no gpgkey")
        try:
            priority = int(section.get("priority", "99"))
        except ValueError as error:
            raise InvalidArgument(f"{path}: repository {alias}: priority must be a number") from error
        found.append(Repo(alias=alias, name=section.get("name") or alias, baseurl=baseurl,
                          enabled=_boolean(section.get("enabled"), True), priority=priority,
                          gpgcheck=gpgcheck, repo_gpgcheck=repo_gpgcheck, keys=keys, vendor=vendor))
    return found


def load_repos(paths):
    """Shipped repositories, then local ones; a local section replaces a shipped one of the same alias."""
    root = paths["root"]
    repos = {}
    for directory, vendor in ((_under(root, VENDOR_DIR), True), (_under(root, LOCAL_DIR), False)):
        try:
            names = sorted(name for name in os.listdir(directory) if name.endswith(".repo"))
        except OSError:
            continue
        for name in names:
            for repo in parse_repo_file(os.path.join(directory, name), root, vendor):
                repos[repo.alias] = repo
    return repos


def set_enabled(paths, alias, enabled):
    """Enable or disable a repository, by a local file that replaces the shipped definition."""
    repos = load_repos(paths)
    if alias not in repos:
        raise NotFound(f"no repository named {alias}")
    repo = repos[alias]
    if repo.enabled == enabled:
        return repo
    directory = _under(paths["root"], LOCAL_DIR)
    os.makedirs(directory, mode=0o755, exist_ok=True)
    keys = " ".join("file://" + (k[len(paths["root"].rstrip("/")):] if paths["root"] not in ("", "/") else k) for k in repo.keys)
    body = (f"[{repo.alias}]\nname={repo.name}\nenabled={1 if enabled else 0}\nbaseurl={repo.baseurl}\n"
            f"priority={repo.priority}\ngpgcheck={1 if repo.gpgcheck else 0}\nrepo_gpgcheck={1 if repo.repo_gpgcheck else 0}\n"
            f"gpgkey={keys}\n")
    target = os.path.join(directory, f"{alias}.repo")
    with tempfile.NamedTemporaryFile("w", dir=directory, suffix=".tmp", delete=False, encoding="utf-8") as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(handle.name, target)
    repo.enabled = enabled
    return repo


KEY_DIR = "etc/foundation/keys"
MAX_KEY = 256 * 1024


def _shipped_aliases(paths):
    found = set()
    directory = _under(paths["root"], VENDOR_DIR)
    try:
        names = sorted(n for n in os.listdir(directory) if n.endswith(".repo"))
    except OSError:
        return found
    for name in names:
        try:
            found.update(r.alias for r in parse_repo_file(os.path.join(directory, name), paths["root"], True))
        except InvalidArgument:
            pass
    return found


def key_info(text, runner=run):
    """The fingerprints and owners in an armoured public key. Raises InvalidArgument if it is not one."""
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_KEY or "\0" in text:
        raise InvalidArgument("the signing key is missing or too large")
    if "-----BEGIN PGP PUBLIC KEY BLOCK-----" not in text or "PRIVATE KEY" in text:
        raise InvalidArgument("the signing key must be an armoured PGP public key (.asc)")
    code, out, error = runner(["gpg", "--batch", "--no-tty", "--with-colons", "--show-keys", "-"], 30,
                              input_bytes=text.encode())
    if code != 0:
        raise InvalidArgument(f"cannot read the signing key: {error.strip()[:200]}")
    keys, current = [], None
    for line in out.splitlines():
        fields = line.split(":")
        if fields[0] == "pub":
            current = {"fingerprint": "", "owner": "", "expires": int(fields[6]) if len(fields) > 6 and fields[6].isdigit() else 0}
            keys.append(current)
        elif fields[0] == "fpr" and current is not None and not current["fingerprint"]:
            current["fingerprint"] = fields[9]
        elif fields[0] == "uid" and current is not None and not current["owner"]:
            current["owner"] = fields[9]
    if not keys:
        raise InvalidArgument("the file holds no public key")
    return keys


def _write_atomic(directory, target, body):
    os.makedirs(directory, mode=0o755, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=directory, suffix=".tmp", delete=False, encoding="utf-8") as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(handle.name, 0o644)
    os.replace(handle.name, target)


def add_repo(paths, alias, name, url, priority, key_text, runner=run):
    """Add a repository of the user's own: its definition and signing key go to /etc/foundation. It starts enabled."""
    if not ALIAS.match(alias or ""):
        raise InvalidArgument(f"unusable repository name: {alias!r}")
    if alias in load_repos(paths):
        raise InvalidArgument(f"a repository named {alias} already exists")
    if not isinstance(priority, int) or not 1 <= priority <= 999:
        raise InvalidArgument("the priority must be a number from 1 to 999")
    name = (name or alias).strip()
    if not name or len(name) > 120 or "\n" in name or "\0" in name:
        raise InvalidArgument("unusable repository description")
    key_info(key_text, runner)
    root = paths["root"]
    key_path = os.path.join(_under(root, KEY_DIR), f"{alias}.asc")
    repo_path = os.path.join(_under(root, LOCAL_DIR), f"{alias}.repo")
    key_url = "file:///" + KEY_DIR + f"/{alias}.asc"
    body = (f"[{alias}]\nname={name}\nenabled=1\nbaseurl={url}\npriority={priority}\ngpgcheck=1\nrepo_gpgcheck=1\n"
            f"gpgkey={key_url}\n")
    _write_atomic(os.path.dirname(key_path), key_path, key_text if key_text.endswith("\n") else key_text + "\n")
    try:
        _write_atomic(os.path.dirname(repo_path), repo_path, body)
        (repo,) = parse_repo_file(repo_path, root)
    except BaseException:
        for path in (repo_path, key_path):
            try:
                os.unlink(path)
            except OSError:
                pass
        raise
    return repo


def remove_repo(paths, alias, in_use=()):
    """Remove a repository added with add_repo. Shipped repositories can only be disabled."""
    repos = load_repos(paths)
    if alias not in repos:
        raise NotFound(f"no repository named {alias}")
    if alias in _shipped_aliases(paths):
        raise InvalidArgument(f"{alias} comes with the system and cannot be removed; turn it off with: foundation repo disable {alias}")
    if alias in in_use:
        raise InvalidArgument(f"bundles contain packages from {alias}; remove or rebuild them first")
    root = paths["root"]
    for path in (os.path.join(_under(root, LOCAL_DIR), f"{alias}.repo"), os.path.join(_under(root, KEY_DIR), f"{alias}.asc")):
        try:
            os.unlink(path)
        except OSError:
            pass
    shutil.rmtree(cache_dir(paths, alias), ignore_errors=True)
    return {"alias": alias, "removed": True}


# -- fetching ----------------------------------------------------------------------------------

def fetch(url, limit):
    """The body of an https or file URL, at most `limit` bytes."""
    scheme = urllib.parse.urlparse(url).scheme
    if scheme not in ("https", "file"):
        raise Failed(f"refusing to fetch {url}: only https and file URLs are allowed")
    request = urllib.request.Request(url, headers={"User-Agent": "foundationd"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310 (scheme checked above)
            data = response.read(limit + 1)
    except (urllib.error.URLError, OSError, ValueError) as error:
        reason = getattr(error, "reason", error)
        raise Failed(f"cannot download {url}: {reason}") from error
    if len(data) > limit:
        raise Failed(f"{url} is larger than the {limit} bytes allowed")
    return data


def fetch_file(url, destination, limit):
    """Download to a file (streaming) and return its sha256-independent size; the caller verifies the checksum."""
    scheme = urllib.parse.urlparse(url).scheme
    if scheme not in ("https", "file"):
        raise Failed(f"refusing to fetch {url}: only https and file URLs are allowed")
    request = urllib.request.Request(url, headers={"User-Agent": "foundationd"})
    size = 0
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response, open(destination, "wb") as out:  # noqa: S310
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise Failed(f"{url} is larger than the {limit} bytes allowed")
                out.write(chunk)
    except (urllib.error.URLError, OSError, ValueError) as error:
        reason = getattr(error, "reason", error)
        raise Failed(f"cannot download {url}: {reason}") from error
    return size


def digest(path, algorithm):
    name = HASHES.get(algorithm)
    if name is None:
        raise Failed(f"unsupported checksum type {algorithm!r}")
    hasher = hashlib.new(name)
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


# -- signatures --------------------------------------------------------------------------------

def verify_detached(data, signature, key_files, runner=run):
    """True if `signature` is a valid detached GPG signature of `data` by one of the keys.

    Only a good signature counts: gpg's own exit status is 0 for a signature by an expired or revoked key (it only warns),
    so the status lines decide. VALIDSIG must be there and none of EXPSIG (the signature expired), EXPKEYSIG (the key
    expired), REVKEYSIG (the key was revoked) or BADSIG."""
    with tempfile.TemporaryDirectory(prefix="gnupg.") as home:
        os.chmod(home, 0o700)
        base = ["gpg", "--batch", "--no-tty", "--homedir", home]
        for key in key_files:
            code, _out, error = runner(base + ["--import", key], 30)
            if code != 0:
                raise Failed(f"cannot read the signing key {key}: {error.strip()[:200]}")
        data_path, sig_path = os.path.join(home, "data"), os.path.join(home, "data.asc")
        with open(data_path, "wb") as handle:
            handle.write(data)
        with open(sig_path, "wb") as handle:
            handle.write(signature)
        code, out, error = runner(base + ["--status-fd", "1", "--verify", sig_path, data_path], 30)
        words = {line.split()[1] for line in (out or "").splitlines() if line.startswith("[GNUPG:] ") and len(line.split()) > 1}
        if code != 0 or "VALIDSIG" not in words:
            return False
        return not words & {"EXPSIG", "EXPKEYSIG", "REVKEYSIG", "BADSIG", "ERRSIG"}


def verify_rpms(files, key_files, runner=run):
    """Raise Failed unless every RPM file carries a valid signature from one of the keys (rpmkeys, private keyring)."""
    if not files:
        return
    with tempfile.TemporaryDirectory(prefix="rpmkeys.") as db:
        for key in key_files:
            code, _out, error = runner(["rpmkeys", "--dbpath", db, "--import", key], 30)
            if code != 0:
                raise Failed(f"cannot import the signing key {key}: {error.strip()[:200]}")
        for path in files:
            code, out, error = runner(["rpmkeys", "--dbpath", db, "--checksig", path], 120)
            text = (out + error).lower()
            if code != 0 or "not ok" in text or "missing keys" in text or "signatures ok" not in text:
                raise Failed(f"{os.path.basename(path)} does not carry a valid signature from the repository's key")


# -- metadata ----------------------------------------------------------------------------------

def cache_dir(paths, alias):
    return os.path.join(paths["state"], "repos", alias)


def parse_repomd(data):
    """{type: {"href", "checksum": (type, value), "size"}} and the revision, from repomd.xml."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as error:
        raise Failed(f"repomd.xml is not valid XML: {error}") from error
    entries = {}
    for item in root.findall("r:data", REPOMD_NS):
        kind = item.get("type")
        location = item.find("r:location", REPOMD_NS)
        checksum = item.find("r:checksum", REPOMD_NS)
        if kind is None or location is None or checksum is None:
            continue
        href = location.get("href", "")
        if href.startswith("/") or ".." in href.split("/"):
            raise Failed(f"repomd.xml names an unsafe location: {href!r}")
        entries[kind] = {"href": href, "checksum": (checksum.get("type", ""), (checksum.text or "").strip())}
    revision = root.findtext("r:revision", default="", namespaces=REPOMD_NS)
    return entries, revision


def refresh(repo, paths, runner=run, fetcher=fetch, file_fetcher=fetch_file, force=False):
    """Bring the cached metadata of one repository up to date. Returns {"alias", "changed", "revision"}."""
    directory = cache_dir(paths, repo.alias)
    os.makedirs(directory, mode=0o755, exist_ok=True)
    repomd = fetcher(repo.baseurl + "repodata/repomd.xml", MAX_REPOMD)
    if repo.repo_gpgcheck:
        signature = fetcher(repo.baseurl + "repodata/repomd.xml.asc", MAX_REPOMD)
        if not verify_detached(repomd, signature, repo.keys, runner):
            raise Failed(f"the metadata of {repo.alias} is not signed by the repository's key; refusing to use it")
    entries, revision = parse_repomd(repomd)
    primary = entries.get("primary")
    if primary is None:
        raise Failed(f"{repo.alias} lists no package metadata")
    marker = os.path.join(directory, "repomd.xml")
    try:
        with open(marker, "rb") as handle:
            unchanged = handle.read() == repomd
    except OSError:
        unchanged = False
    base = os.path.basename(primary["href"])
    primary_name = "primary" + (base[base.find(".xml"):] if ".xml" in base else ".xml")
    cached = os.path.join(directory, primary_name)
    if unchanged and os.path.exists(cached) and not force:
        return {"alias": repo.alias, "changed": False, "revision": revision}
    partial = cached + ".part"
    file_fetcher(repo.baseurl + primary["href"], partial, MAX_METADATA)
    kind, expected = primary["checksum"]
    if digest(partial, kind) != expected:
        os.unlink(partial)
        raise Failed(f"the package list of {repo.alias} does not match the checksum its signed repomd.xml lists")
    for old in os.listdir(directory):
        if old.startswith("primary.") and old != primary_name and not old.endswith(".part"):
            os.unlink(os.path.join(directory, old))
    os.replace(partial, cached)
    with open(marker, "wb") as handle:
        handle.write(repomd)
    with open(os.path.join(directory, "refreshed"), "w", encoding="utf-8") as handle:
        handle.write(str(int(time.time())))
    return {"alias": repo.alias, "changed": True, "revision": revision}


def cached_primary(paths, alias):
    directory = cache_dir(paths, alias)
    try:
        for name in sorted(os.listdir(directory)):
            if name.startswith("primary.xml") and not name.endswith(".part"):
                return os.path.join(directory, name)
    except OSError:
        pass
    return None


def metadata_age(paths, alias):
    """Seconds since the repository's metadata was refreshed, or None if it never was."""
    try:
        with open(os.path.join(cache_dir(paths, alias), "refreshed"), encoding="utf-8") as handle:
            return max(0, int(time.time()) - int(handle.read().strip()))
    except (OSError, ValueError):
        return None


# -- the solver --------------------------------------------------------------------------------

def _solv():
    try:
        import solv  # noqa: PLC0415
    except ImportError as error:
        raise Unavailable("the package solver is not installed (the python solv bindings)") from error
    return solv


class Pool:
    """The installed packages of the base, the enabled repositories, and a solver over them.

    `extra_rpms` are RPM files that count as installed too (the packages of the other bundles).
    """

    def __init__(self, repos, paths, extra_rpms=(), solv=None):
        self.solv = solv or _solv()
        self.repos = repos
        self.paths = paths
        s = self.solv
        self.pool = s.Pool()
        self.pool.setarch()
        self.pool.set_loadcallback(None)
        self.installed = self.pool.add_repo("@System")
        self._load_installed(paths, extra_rpms)
        self.pool.installed = self.installed
        self.by_alias = {}
        for repo in repos:
            primary = cached_primary(paths, repo.alias)
            if primary is None:
                raise Unavailable(f"the package list of {repo.alias} has not been downloaded yet; refresh the repositories first")
            handle = self.pool.add_repo(repo.alias)
            stream = s.xfopen(primary)
            try:
                if not handle.add_rpmmd(stream, None, 0):
                    raise Failed(f"cannot read the package list of {repo.alias}")
            finally:
                stream.close()
            handle.priority = 100 - repo.priority      # libsolv: a higher number wins; zypp: a lower one
            self.by_alias[repo.alias] = handle
        self.pool.addfileprovides()
        self.pool.createwhatprovides()

    def _load_installed(self, paths, extra_rpms):
        s = self.solv
        root = paths["root"]
        if root not in ("", "/"):
            self.pool.set_rootdir(root)
        if not self.installed.add_rpmdb():
            raise Failed("cannot read the base's RPM database")
        for path in extra_rpms:
            if self.installed.add_rpm(path, s.Repo.REPO_REUSE_REPODATA | s.Repo.REPO_NO_INTERNALIZE) is None:
                raise Failed(f"cannot read {os.path.basename(path)}")
        self.installed.internalize()

    def _info(self, solvable):
        s = self.solv
        location = solvable.lookup_location()
        checksum = solvable.lookup_checksum(s.SOLVABLE_CHECKSUM)
        return {"name": solvable.name, "evr": solvable.evr, "arch": solvable.arch, "repo": solvable.repo.name,
                "location": location[0] if location else "",
                "checksum": [checksum.typestr(), checksum.hex()] if checksum else ["", ""],
                "size": solvable.lookup_num(s.SOLVABLE_DOWNLOADSIZE) or 0,
                "summary": solvable.lookup_str(s.SOLVABLE_SUMMARY) or ""}

    def is_installed(self, name):
        return any(True for _ in self.pool.whatprovides(self.pool.str2id(name)) if _.repo == self.installed)

    def _selection(self, name, repo_alias=None):
        s = self.solv
        selection = self.pool.select(name, s.Selection.SELECTION_NAME | s.Selection.SELECTION_PROVIDES)
        if selection.isempty():
            return None
        if repo_alias:
            handle = self.by_alias.get(repo_alias)
            if handle is None:
                return None
            only = self.pool.Selection()
            only.add_raw(s.Job.SOLVER_SOLVABLE_REPO | s.Job.SOLVER_SETREPO, handle.id)
            selection.filter(only)
            if selection.isempty():
                return None
        return selection

    def resolve(self, wanted):
        """The packages to add on top of what is installed so that every wanted one is. `wanted` is a list of
        {"name", "repo" (optional: only from this repository), "when_installed" (optional: skip unless the base
        already has it)}. Returns (to_install, skipped, missing) where the first is a list of package dicts."""
        s = self.solv
        jobs, skipped, missing = [], [], []
        for item in wanted:
            name = item["name"]
            if item.get("when_installed") and not self.is_installed(name):
                skipped.append(name)
                continue
            selection = self._selection(name, item.get("repo"))
            if selection is None:
                missing.append(name)
                continue
            jobs += selection.jobs(s.Job.SOLVER_INSTALL)
        if missing:
            raise NotFound("not found in the enabled repositories: " + ", ".join(missing))
        if not jobs:
            return [], skipped, []
        solver = self.pool.Solver()
        solver.set_flag(s.Solver.SOLVER_FLAG_IGNORE_RECOMMENDED, 1)
        solver.set_flag(s.Solver.SOLVER_FLAG_ALLOW_VENDORCHANGE, 1)
        solver.set_flag(s.Solver.SOLVER_FLAG_ALLOW_DOWNGRADE, 1)
        problems = solver.solve(jobs)
        if problems:
            lines = []
            for problem in problems:
                lines.append(str(problem))
            raise Failed("the packages cannot be installed together: " + "; ".join(lines))
        transaction = solver.transaction()
        return [self._info(solvable) for solvable in transaction.newsolvables()], skipped, []

    def search(self, query, limit=200):
        """Packages whose name contains `query` (case-insensitive), best version per repository."""
        s = self.solv
        selection = self.pool.select(f"*{query}*", s.Selection.SELECTION_NAME | s.Selection.SELECTION_GLOB | s.Selection.SELECTION_FLAT)
        best = {}
        for solvable in selection.solvables():
            if solvable.repo == self.installed:
                continue
            key = (solvable.name, solvable.repo.name)
            old = best.get(key)
            if old is None or solvable.evrcmp(old) > 0:
                best[key] = solvable
        return [self._info(best[key]) for key in sorted(best)][:limit]

    def info(self, name):
        found = self.search_exact(name)
        if not found:
            raise NotFound(f"no package named {name}")
        return found

    def search_exact(self, name):
        s = self.solv
        selection = self.pool.select(name, s.Selection.SELECTION_NAME | s.Selection.SELECTION_FLAT)
        best = {}
        for solvable in selection.solvables():
            key = solvable.repo.name
            old = best.get(key)
            if old is None or solvable.evrcmp(old) > 0:
                best[key] = solvable
        result = []
        for key in sorted(best):
            info = self._info(best[key])
            info["installed"] = best[key].repo == self.installed
            result.append(info)
        return result


# -- downloading -------------------------------------------------------------------------------

def package_url(repo, location):
    """Where a package is downloaded from. Normally the repository's address plus the location in its signed metadata;
    a repository may instead list absolute https addresses (the metadata on one host, the packages as release assets on
    another). Either way the file must match the checksum in the signed metadata and carry a valid RPM signature, so
    the host that serves it is not trusted for anything."""
    if location.startswith("https://"):
        return location
    if "://" in location or location.startswith("/") or ".." in location.split("/"):
        raise Failed(f"unsafe package location: {location!r}")
    return repo.baseurl + location


def download(packages, repos, directory, runner=run, file_fetcher=fetch_file, workers=6, progress=None):
    """Download and verify the RPMs of a resolved list into `directory`; returns the packages with a "file" added.

    A file already there with the right checksum is reused, so adding to a bundle fetches only what is new.
    Several files are fetched at once (`workers`); `progress(done, total)` is called as each one finishes.
    """
    os.makedirs(directory, mode=0o755, exist_ok=True)
    by_alias = {repo.alias: repo for repo in repos}
    plan = []
    for package in packages:
        repo = by_alias.get(package["repo"])
        if repo is None:
            raise Failed(f"{package['name']} comes from {package['repo']}, which is not an enabled repository")
        filename = os.path.basename(package["location"])
        if not filename.endswith(".rpm") or not re.match(r"^[A-Za-z0-9._+~-]+$", filename):
            raise Failed(f"unsafe package file name: {filename!r}")
        plan.append((package, repo, os.path.join(directory, filename)))

    def fetch_one(item):
        package, repo, target = item
        kind, expected = package["checksum"]
        if os.path.exists(target) and kind and digest(target, kind) == expected:
            return
        partial = target + ".part"
        file_fetcher(package_url(repo, package["location"]), partial, MAX_RPM)
        if kind and digest(partial, kind) != expected:
            os.unlink(partial)
            raise Failed(f"{os.path.basename(target)} does not match the checksum in the repository metadata")
        os.replace(partial, target)

    finished = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(fetch_one, item) for item in plan]
        try:
            for future in concurrent.futures.as_completed(futures):
                future.result()
                finished += 1
                if progress:
                    progress(finished, len(plan))
        except BaseException:
            for future in futures:
                future.cancel()
            raise
    per_repo, result = {}, []
    for package, repo, target in plan:
        per_repo.setdefault(repo.alias, []).append(target)
        result.append({**package, "file": target})
    for alias, files in per_repo.items():
        repo = by_alias[alias]
        if repo.gpgcheck:
            verify_rpms(files, repo.keys, runner)
    return result


def forget(directory, keep):
    """Remove cached RPM files in `directory` that are not in `keep` (paths)."""
    try:
        names = os.listdir(directory)
    except OSError:
        return
    wanted = {os.path.basename(path) for path in keep}
    for name in names:
        if name not in wanted:
            try:
                os.unlink(os.path.join(directory, name))
            except OSError:
                pass


def enabled(paths):
    return [repo for repo in load_repos(paths).values() if repo.enabled]
