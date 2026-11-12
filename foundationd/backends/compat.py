"""Compat RPMs: run a legacy or third-party RPM as a system extension (sysext).

The immutable base is never modified. The RPM's payload is unpacked (no scriptlets run) into a
directory tree under /var/lib/extensions/<name>, with the `extension-release` file that makes
systemd-sysext accept it, and `systemd-sysext refresh` overlays it onto /usr and /opt.

An extension is *enabled* while its tree sits in /var/lib/extensions and *disabled* while it sits in
<state>/compat/disabled (moved, never deleted, so nothing is lost). Metadata about every installed
extension is kept in <state>/compat/<name>.json, which is what lets `revalidate` recheck them
against a new base image without unpacking anything again.

What is checked, honestly: the RPM's requirements are looked up in the base's RPM database by
capability *name* (version constraints are not compared); every ELF file's DT_NEEDED libraries must
exist in the base (per the linker cache) or in the RPM itself; files outside /usr and /opt are
dropped (and reported); files that would shadow a file of the base are reported as conflicts.
"""

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time

from ..errors import Failed, InvalidArgument, NotFound
from . import cpio, elf, extensions, osinfo, richdeps, scriptlets, sellabel
from .runner import check, run

log = logging.getLogger(__name__)

USRMERGE = {"bin": "usr/bin", "sbin": "usr/sbin", "lib": "usr/lib", "lib64": "usr/lib64"}
HIERARCHIES = ("usr", "opt")
ARCH = {"x86_64": "x86-64", "aarch64": "arm64"}
RELEASE_DIR = "usr/lib/extension-release.d"
ETC_FACTORY = "usr/share/foundation/etc"


# -- paths -------------------------------------------------------------------------------------

def _compat_dir(paths):
    return os.path.join(paths["state"], "compat")


def _meta_path(paths, name):
    return os.path.join(_compat_dir(paths), f"{name}.json")


def _enabled_dir(paths, name):
    return os.path.join(paths["root"], "var/lib/extensions", name)


def _disabled_dir(paths, name):
    return os.path.join(_compat_dir(paths), "disabled", name)


def _scratch(paths):
    directory = os.path.join(paths["state"], "staging")
    os.makedirs(directory, mode=0o700, exist_ok=True)
    return directory


# -- reading an RPM ----------------------------------------------------------------------------

RPM_MAGIC = b"\xed\xab\xee\xdb"


def require_rpm_file(path):
    """Refuse anything that is not an RPM before any tool sees it.

    `rpm -qp FILE` treats a plain-text FILE as a *manifest* and opens every path listed in it. This
    service runs as root on files supplied by callers, so a text file must never reach rpm.
    """
    try:
        with open(path, "rb") as handle:
            magic = handle.read(4)
    except OSError as error:
        raise InvalidArgument(f"cannot read the file: {error.strerror}") from error
    if magic != RPM_MAGIC:
        raise InvalidArgument("this is not an RPM file")


def read_rpm(path, runner=run):
    """Header facts and file/requirement lists of an RPM file, through the rpm tool."""
    require_rpm_file(path)
    header = check(["rpm", "-qp", "--nosignature", "--nodigest", "--qf",
                    "%{NAME}\n%{VERSION}\n%{RELEASE}\n%{ARCH}\n", path], 30, runner=runner).splitlines()
    if len(header) < 4:
        raise InvalidArgument("this is not a readable RPM file")
    name, version, release, arch = header[:4]
    listing = check(["rpm", "-qp", "--nosignature", "--nodigest", "--qf",
                     "[%{FILEMODES}\t%{FILENAMES}\n]", path], 30, runner=runner)
    files = []
    for line in listing.splitlines():
        mode, _, filename = line.partition("\t")
        if filename and mode.isdigit():
            files.append((int(mode), filename))
    requires = check(["rpm", "-qp", "--nosignature", "--nodigest", "--requires", path], 30, runner=runner).splitlines()
    provides = check(["rpm", "-qp", "--nosignature", "--nodigest", "--provides", path], 30, runner=runner).splitlines()
    scripts = check(["rpm", "-qp", "--nosignature", "--nodigest", "--scripts", path], 30, runner=runner)
    return {"name": name, "version": version, "release": release, "arch": arch, "files": files,
            "requires": requires, "provides": provides, "scriptlets": bool(scripts.strip())}


def _capability(line):
    return line.split(None, 1)[0] if line.strip() else ""


def remap(filename):
    """The path a packaged file gets inside the extension, or None if it cannot be installed."""
    parts = filename.lstrip("/").split("/", 1)
    first, rest = parts[0], parts[1] if len(parts) > 1 else ""
    if first in USRMERGE:
        return f"{USRMERGE[first]}/{rest}".rstrip("/")
    if first in HIERARCHIES:
        return filename.lstrip("/")
    return None


def host_libraries(runner=run):
    """Sonames the base can provide, from the dynamic linker cache."""
    code, out, _err = runner(["ldconfig", "-p"], 30)
    names = set()
    if code == 0:
        for line in out.splitlines():
            line = line.strip()
            if " => " in line and "(" in line:
                names.add(line.split(" ", 1)[0])
    return names


def provided_by_base(capability, runner=run):
    return runner(["rpm", "-q", "--whatprovides", capability], 15)[0] == 0


# -- scriptlets --------------------------------------------------------------------------------
#
# An RPM's scriptlets are never run: a shell script from a package, as root, on an immutable system, is exactly what an
# extension avoids. What they do is read from their text (scriptlets.py): users and groups become a sysusers.d file in the
# extension and services are preset after the merge, as the scripts did; anything else is reported, so a person knows
# what will be missing.

EFFECT_TEXT = {
    "users": "creates users or groups it could not translate, which are not created here",
    "services": "enables or starts services, which are not enabled here (start one with systemctl)",
    "caches": "rebuilds caches (the application list, MIME types, fonts or certificates), which are not rebuilt here, so a "
              "new file type or font may not be recognised until the next start",
    "alternatives": "sets up alternatives, which are not set up here",
    "other": "runs other commands, which are not run here",
}


def inspect_scriptlets(files, runner=run):
    """scriptlets.analyse() of each RPM file, plus its "package" name. A file rpm cannot read is left out."""
    results = []
    for path in files:
        code, out, _err = runner(["rpm", "-qp", "--nosignature", "--nodigest", "--queryformat",
                                  "NAME:%{NAME}-%{VERSION}-%{RELEASE}\\n", "--scripts", path], 30)
        if code != 0:
            continue
        lines = out.splitlines()
        name = os.path.basename(path)
        if lines and lines[0].startswith("NAME:"):
            name, out = lines[0][5:], "\n".join(lines[1:])
        results.append({"package": name, "file": path, **scriptlets.analyse(out)})
    return results


def apply_scriptlets(tree, name, files, runner=run):
    """Carry out what can be carried out of the RPM files' scriptlets in the extension `tree`: write their accounts as
    usr/lib/sysusers.d/foundation-<name>.conf. Returns {"notes": [...what is not done], "units": [...to preset],
    "accounts": n}."""
    results = inspect_scriptlets(files, runner)
    lines = []
    for kind in ("g ", "u ", "m "):
        for result in results:
            for line in result["sysusers"]:
                if line.startswith(kind) and line not in lines:
                    lines.append(line)
    if lines:
        target = os.path.join(tree, "usr/lib/sysusers.d")
        os.makedirs(target, exist_ok=True)
        with open(os.path.join(target, f"foundation-{name}.conf"), "w", encoding="utf-8") as handle:
            handle.write("# Users and groups the packages' scriptlets would have created, translated by foundation.\n"
                         + "\n".join(lines) + "\n")
    units, homes = [], []
    for result in results:
        units += [u for u in result["units"] if u not in units]
        homes += [h for h in result.get("homes", []) if h not in homes]
        if result.get("link_share"):
            code, listing, _err = runner(["rpm", "-qpl", "--nosignature", "--nodigest", result["file"]], 30)
            if code == 0:
                scriptlets.link_libreoffice_share(tree, listing)
    if homes:
        target = os.path.join(tree, "usr/lib/tmpfiles.d")
        os.makedirs(target, exist_ok=True)
        with open(os.path.join(target, f"foundation-{name}-homes.conf"), "w", encoding="utf-8") as handle:
            handle.write("# Home directories of the users the packages' scriptlets would have created with useradd -m.\n"
                         + "".join(f"d {home} 0755 {user} {group} -\n" for user, group, home in homes))
    notes = [{"package": r["package"], "effects": r["effects"],
              "text": f"{r['package']}: " + "; ".join(EFFECT_TEXT[e] for e in r["effects"])} for r in results if r["effects"]]
    return {"notes": notes, "units": units, "accounts": len([line for line in lines if line[0] == "u"])}


def touch_icon_themes(tree):
    """Make GTK and GNOME Shell look at the icons an extension brings.

    The base image has an icon cache (`icon-theme.cache`) in each icon theme, and GTK trusts it for as long as it is not
    older than the theme's directory. The directory a merged system shows is the extension's one, whose time is when the
    package was built, usually before the base image's cache was made, so the old cache looked current and every icon the
    extension adds was "missing" (LibreOffice in the launcher had none). Giving the extension's theme directories the
    time of the build makes the cache look out of date, so GTK reads the directories instead."""
    icons = os.path.join(tree, "usr/share/icons")
    try:
        names = os.listdir(icons)
    except OSError:
        return 0
    touched = 0
    for name in names:
        path = os.path.join(icons, name)
        if os.path.isdir(path) and not os.path.islink(path):
            os.utime(path, None)
            touched += 1
    return touched


def tree_has(tree, directory, suffix):
    path = os.path.join(tree, directory)
    try:
        return any(n.endswith(suffix) for n in os.listdir(path))
    except OSError:
        return False


def apply_system_config(paths, runner=run, systemd=None):
    """After a merge: create the users and groups of sysusers.d files, the directories of tmpfiles.d files and enable the
    services (as `systemctl preset` would, honouring the preset files) that the enabled extensions brought. All of it
    is idempotent. A failure is logged, not raised: the merge itself worked."""
    sysusers = tmpfiles = False
    units = []
    for meta in installed_metas(paths):
        tree = _enabled_dir(paths, meta["name"])
        if os.path.isdir(tree):
            sysusers = sysusers or tree_has(tree, "usr/lib/sysusers.d", ".conf")
            tmpfiles = tmpfiles or tree_has(tree, "usr/lib/tmpfiles.d", ".conf")
            units += [u for u in meta.get("units", []) if u not in units]
    for wanted, command in ((sysusers, ["systemd-sysusers"]), (tmpfiles, ["systemd-tmpfiles", "--create"])):
        if wanted:
            code, _out, err = runner(command, 60)
            if code not in (0, 127):
                log.warning("%s failed: %s", command[0], (err or "").strip().splitlines()[-1:] or code)
    if units:
        from . import remote
        systemd = systemd or remote.System(paths.get("bus", "system"))
        for unit in units:
            try:
                systemd.preset(unit)
            except Failed as error:
                log.warning("could not preset %s: %s", unit, error)
        try:
            systemd.reload()
        except Failed as error:
            log.warning("could not reload systemd: %s", error)


# -- extraction --------------------------------------------------------------------------------

def extract(rpm_path, destination):
    """Unpack the payload into `destination` (safely, see cpio.py). Returns the entry count."""
    try:
        process = subprocess.Popen(["rpm2cpio", rpm_path], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError as error:
        from ..errors import Unavailable
        raise Unavailable("rpm2cpio is not installed") from error
    try:
        count = cpio.unpack(process.stdout, destination)
    finally:
        process.stdout.close()
        err = process.stderr.read().decode(errors="replace").strip()
        process.stderr.close()
        code = process.wait()
    if code != 0:
        raise Failed(f"rpm2cpio failed: {err or code}")
    return count


def tmpfiles_for_var(root):
    """The directories a payload had under /var, as tmpfiles.d lines. /var is machine state and nothing can be
    merged over it, so a package's empty state directories are created at boot instead. Files under /var are not
    carried (they would be state, not part of the package). Returns (lines, dropped_files)."""
    lines, files = [], 0
    top = os.path.join(root, "var")
    if os.path.islink(top) or not os.path.isdir(top):
        return [], 0
    for directory, subdirs, names in os.walk(top):
        subdirs.sort()
        files += len(names)
        relative = "/" + os.path.relpath(directory, root)
        if os.path.islink(directory):
            continue
        mode = os.stat(directory).st_mode & 0o7777
        if " " in relative or "%" in relative:
            continue
        lines.append(f"d {relative} {mode:04o} - - -")
    return lines, files


def apply_usrmerge(root, name=None):
    """Move /bin, /sbin, /lib and /lib64 of an unpacked payload under /usr. What is left is sorted out:

    - /usr and /opt stay (the sysext);
    - /etc stays where it is for now: `stage_etc` turns it into defaults that are copied into the writable /etc;
    - /var becomes tmpfiles.d lines inside the sysext (when `name` is given);
    - everything else is deleted and reported.
    Returns the list of what was dropped.
    """
    for old, new in USRMERGE.items():
        source = os.path.join(root, old)
        if os.path.isdir(source) and not os.path.islink(source):
            shutil.copytree(source, os.path.join(root, new), symlinks=True, dirs_exist_ok=True)
            shutil.rmtree(source)
        elif os.path.lexists(source):
            os.unlink(source)
    dropped = []
    if name:
        lines, files = tmpfiles_for_var(root)
        if lines:
            target = os.path.join(root, "usr/lib/tmpfiles.d")
            os.makedirs(target, exist_ok=True)
            with open(os.path.join(target, f"foundation-{name}.conf"), "w", encoding="utf-8") as handle:
                handle.write("# Created from the package's /var directories by foundation.\n" + "\n".join(lines) + "\n")
        if files:
            dropped.append(f"{files} file(s) under /var")
    for entry in sorted(os.listdir(root)):
        if entry in HIERARCHIES or entry == "etc":
            continue
        if entry == "var" and name:
            pass
        elif entry == "var":
            dropped.append("/var")
        else:
            dropped.append("/" + entry)
        full = os.path.join(root, entry)
        shutil.rmtree(full) if os.path.isdir(full) and not os.path.islink(full) else os.unlink(full)
    return dropped


def stage_etc(tree, name):
    """Turn the package's /etc files into defaults, and leave /etc itself empty.

    A system extension cannot carry /etc: only /usr and /opt are merged. A configuration extension (confext) would, but it
    needs the layer below it writable, and the kernel does not accept an overlay (our /etc is one) as that layer. So the files
    move to usr/share/foundation/etc/<name>/ inside the extension, and a tmpfiles.d file makes systemd-tmpfiles copy each one
    into the real, writable /etc **only if nothing is there yet** (`C`). What you edited, or the image ships, is never
    replaced; a file you leave alone is removed again with the extension (see `remove_etc_copies`).
    Returns the paths, relative to /etc, of the files that will be copied."""
    etc = os.path.join(tree, "etc")
    if not os.path.lexists(etc):
        return []
    if os.path.islink(etc) or not os.path.isdir(etc):
        os.unlink(etc)
        return []
    found = []
    for directory, subdirs, names in os.walk(etc):
        subdirs.sort()
        for entry in sorted(names + [d for d in subdirs if os.path.islink(os.path.join(directory, d))]):
            relative = os.path.relpath(os.path.join(directory, entry), etc)
            if any(c in relative for c in ' %\t\n"\\'):
                continue
            found.append(relative)
    factory = os.path.join(tree, ETC_FACTORY, name)
    os.makedirs(os.path.dirname(factory), exist_ok=True)
    shutil.move(etc, factory)
    if found:
        target = os.path.join(tree, "usr/lib/tmpfiles.d")
        os.makedirs(target, exist_ok=True)
        with open(os.path.join(target, f"foundation-{name}-etc.conf"), "w", encoding="utf-8") as handle:
            handle.write("# Defaults from the package's /etc files: copied only where nothing exists yet. Written by foundation.\n")
            for relative in found:
                handle.write(f"C /etc/{relative} - - - - /{ETC_FACTORY}/{name}/{relative}\n")
    return found


def remove_etc_copies(paths, name, copies, tree):
    """Remove the copies of an extension's /etc defaults that nobody changed (same content as the default); a file that was
    edited stays. `tree` is the extension's directory, which still holds the defaults. Returns the number removed."""
    import filecmp
    removed = 0
    for relative in copies:
        default = os.path.join(tree, ETC_FACTORY, name, relative)
        copy = os.path.join(paths["root"], "etc", relative)
        try:
            if os.path.islink(default):
                same = os.path.islink(copy) and os.readlink(copy) == os.readlink(default)
            else:
                same = os.path.isfile(copy) and not os.path.islink(copy) and filecmp.cmp(default, copy, shallow=False)
            if same:
                os.unlink(copy)
                removed += 1
        except OSError:
            continue
    return removed


def refresh_extensions(paths, runner=run, systemd=None):
    """Bring the merged system in line: refresh the extensions, then create what they need (users, directories, /etc
    defaults, services) and generate what has to be generated."""
    extensions.operate("sysext", "refresh", runner)
    apply_system_config(paths, runner, systemd)
    from . import postmerge
    postmerge.apply(paths, runner)


def scan_libraries(tree):
    """Classify the shared libraries an unpacked package asks for.

    Returns (required, optional, provided):
      required  sonames needed by a program in the package, or by a library such a program loads
                (directly or through other libraries of the package): the package cannot run without them;
      optional  sonames needed only by libraries that no program links against (plugins loaded on
                demand, like a browser's Qt theme shims): a missing one disables a feature, not the program;
      provided  every library name the package itself supplies.
    A package with no program in it (a pure library package) treats all its libraries as required.
    """
    entries, names = [], {}
    links = []
    for directory, _dirs, filenames in os.walk(tree):
        for filename in filenames:
            path = os.path.join(directory, filename)
            if os.path.islink(path):
                links.append(path)
                continue
            info = elf.read_dynamic(path)
            if info is None:
                continue
            entries.append(info)
            names[filename] = len(entries) - 1
            if info["soname"]:
                names[info["soname"]] = len(entries) - 1
    for path in links:  # libfoo.so.1 -> libfoo.so.1.2.3 names a library the package provides
        target = os.path.basename(os.path.realpath(path))
        names[os.path.basename(path)] = names.get(target)
    roots = [i for i, e in enumerate(entries) if e["interp"]] or list(range(len(entries)))
    seen, queue = set(roots), list(roots)
    while queue:
        for name in entries[queue.pop()]["needed"]:
            j = names.get(name)
            if j is not None and j not in seen:
                seen.add(j)
                queue.append(j)
    required = {n for i in seen for n in entries[i]["needed"]}
    optional = {n for i, e in enumerate(entries) if i not in seen for n in e["needed"]} - required
    return required, optional, set(names)


# -- analysis ----------------------------------------------------------------------------------

def installed_metas(paths):
    metas = []
    try:
        names = sorted(os.listdir(_compat_dir(paths)))
    except OSError:
        return metas
    for filename in names:
        if filename.endswith(".json"):
            try:
                with open(os.path.join(_compat_dir(paths), filename), encoding="utf-8") as handle:
                    metas.append(json.load(handle))
            except (OSError, ValueError):
                continue
    return metas


def _own_provides(rpm, paths):
    names = {_capability(line) for line in rpm["provides"]}
    for meta in installed_metas(paths):
        names.update(meta.get("provides", []))
    return names


def check_requirements(requires, own, runner=run):
    """The requirements the base does not provide, by capability name (or, for a rich dependency
    such as `(a or b)`, the whole expression when it is not satisfied or cannot be understood)."""
    def provided(capability):
        return capability in own or provided_by_base(capability, runner)

    missing = []
    for line in requires:
        line = line.strip()
        if not line:
            continue
        if richdeps.is_rich(line):
            if richdeps.satisfied(line, provided) is not True and line not in missing:
                missing.append(line)
            continue
        capability = _capability(line)
        if capability.startswith("rpmlib(") or capability in own or capability in missing:
            continue
        if not provided_by_base(capability, runner):
            missing.append(capability)
    return missing


def analyse(rpm_path, paths, runner=run):
    """Inspect an RPM without changing anything. Returns (report, extras for the metadata)."""
    rpm = read_rpm(rpm_path, runner)
    problems = []
    outside, conflicts, packaged = [], [], 0
    for mode, filename in rpm["files"]:
        if (mode & 0o170000) == 0o040000:
            continue
        mapped = remap(filename)
        if mapped is None:
            outside.append(filename)
            continue
        packaged += 1
        if os.path.lexists(os.path.join(paths["root"], mapped)):
            conflicts.append("/" + mapped)

    arch = rpm["arch"]
    machine = os.uname().machine
    if arch not in ("noarch", machine):
        problems.append(f"the RPM is built for {arch}, but this system is {machine}")

    own = _own_provides(rpm, paths)
    missing_requires = check_requirements(rpm["requires"], own, runner)

    scratch = tempfile.mkdtemp(dir=_scratch(paths), prefix="inspect.")
    try:
        extract(rpm_path, scratch)
        apply_usrmerge(scratch)
        needed, optional, provided = scan_libraries(scratch)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    for meta in installed_metas(paths):
        provided.update(meta.get("provides_libs", []))
    available = host_libraries(runner) | provided
    missing_libraries = sorted(lib for lib in needed if lib not in available)
    optional_libraries = sorted(lib for lib in optional if lib not in available)

    # Problems stop an install (unless forced). Notes are things worth knowing that do not.
    if missing_requires:
        problems.append(f"{len(missing_requires)} requirement(s) are not provided by the base")
    if missing_libraries:
        problems.append(f"{len(missing_libraries)} shared librar{'y is' if len(missing_libraries) == 1 else 'ies are'} not available")
    if conflicts:
        problems.append(f"{len(conflicts)} file(s) would replace files of the base")
    notes = []
    if optional_libraries:
        notes.append(f"{len(optional_libraries)} librar{'y' if len(optional_libraries) == 1 else 'ies'} used only by "
                     "plugins are not available; those features will not work")
    if outside:
        notes.append(f"{len(outside)} file(s) outside /usr and /opt: those in /etc become defaults that are copied into /etc "
                     "where nothing exists yet, directories in /var are created at boot, and anything else (such as /srv) is not installed")
    if rpm["scriptlets"]:
        notes.append("the package has scriptlets; they are not run")

    report = {
        "name": rpm["name"], "version": rpm["version"], "release": rpm["release"], "arch": arch,
        "files": packaged, "compatible": not problems, "problems": problems,
        "missing_requires": missing_requires, "missing_libraries": missing_libraries,
        "outside_usr": outside, "conflicts": conflicts, "scriptlets": rpm["scriptlets"],
        "optional_libraries": optional_libraries, "notes": notes,
    }
    extras = {
        "requires": sorted({_capability(r) for r in rpm["requires"]
                            if _capability(r) and not _capability(r).startswith("rpmlib(") and _capability(r) not in own}),
        "provides": sorted({_capability(p) for p in rpm["provides"] if _capability(p)}),
        "needed_libs": sorted(lib for lib in needed if lib not in provided),
        "optional_libs": sorted(lib for lib in optional if lib not in provided),
        "provides_libs": sorted(provided),
    }
    return report, extras


# -- installing --------------------------------------------------------------------------------

def image_identity(paths=None, os_release_path=None):
    """Which system image this is, as a short token ("ecp_0.1.0_20251101"). Bundles are built for exactly one."""
    host = osinfo.os_release(os_release_path or paths["os_release"])
    image, version = host.get("IMAGE_ID"), host.get("IMAGE_VERSION")
    parts = [image, version, host.get("VERSION_ID")] if image and version else [host.get("ID") or "linux", host.get("VERSION_ID") or host.get("BUILD_ID") or "unknown"]
    return re.sub(r"[^A-Za-z0-9._-]", "-", "_".join(p for p in parts if p))


def built_for_other_image(meta, paths):
    """True if this extension was built for a different image than the running one (it is only known for bundles)."""
    built = (meta.get("bundle") or {}).get("built_for")
    return bool(built) and built != image_identity(paths)


def release_file(name, rpm_arch, paths):
    """The extension-release content that makes systemd-sysext accept this extension."""
    host = osinfo.os_release(paths["os_release"])
    lines = [f"ID={host.get('ID', '_any')}"]
    if host.get("SYSEXT_LEVEL"):
        lines.append(f"SYSEXT_LEVEL={host['SYSEXT_LEVEL']}")
    elif host.get("VERSION_ID"):
        lines.append(f"VERSION_ID={host['VERSION_ID']}")
    if rpm_arch in ARCH:
        lines.append(f"ARCHITECTURE={ARCH[rpm_arch]}")
    return "\n".join(lines) + "\n"


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), mode=0o755, exist_ok=True)
    handle = tempfile.NamedTemporaryFile("w", dir=os.path.dirname(path), suffix=".tmp", delete=False, encoding="utf-8")
    with handle:
        json.dump(data, handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(handle.name, path)


def install(rpm_path, force, paths, ctx, runner=run):
    ctx.step("inspect", "Checking the package against the base image")
    report, extras = analyse(rpm_path, paths, runner)
    name = report["name"]
    if not name or any(c in name for c in "/\0") or name.startswith("."):
        raise InvalidArgument(f"unusable package name: {name!r}")
    if os.path.exists(_meta_path(paths, name)) or os.path.lexists(_enabled_dir(paths, name)):
        raise InvalidArgument(f"{name} is already installed as an extension; remove it first")
    if report["problems"] and not force:
        raise InvalidArgument("not compatible: " + "; ".join(report["problems"]) + " (use force to install anyway)")
    ctx.progress(0.2)

    ctx.step("unpack", "Unpacking the package")
    # The scratch directory is on /var, like the extensions directory, so the final rename is atomic.
    scratch = tempfile.mkdtemp(dir=_scratch(paths), prefix="new.")
    try:
        tree = os.path.join(scratch, name)
        os.mkdir(tree)
        extract(rpm_path, tree)
        dropped = apply_usrmerge(tree, name)
        done = apply_scriptlets(tree, name, [rpm_path], runner)
        done["etc"] = stage_etc(tree, name)
        touch_icon_themes(tree)
        release_dir = os.path.join(tree, RELEASE_DIR)
        os.makedirs(release_dir, exist_ok=True)
        with open(os.path.join(release_dir, f"extension-release.{name}"), "w", encoding="utf-8") as handle:
            handle.write(release_file(name, report["arch"], paths))
        # An overlay shows the extension's directories with the extension's labels, so they must be the labels the
        # merged paths have (sellabel.py), not those of /var/lib/extensions.
        contexts = sellabel.Contexts.load(paths["root"])
        if contexts is not None:
            sellabel.label_tree(tree, contexts)
        ctx.progress(0.6)

        ctx.step("install", "Installing the extension")
        ctx.commit()
        destination = _enabled_dir(paths, name)
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        os.rename(tree, destination)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    meta = {"name": name, "version": report["version"], "release": report["release"], "arch": report["arch"],
            "files": report["files"], "installed": int(time.time()), "scriptlets": report["scriptlets"],
            "scriptlet_notes": done["notes"], "units": done["units"], "etc_copies": done["etc"],
            "dropped": dropped, "forced": bool(report["problems"]), "problems": report["problems"], **extras}
    _write_json(_meta_path(paths, name), meta)

    ctx.step("merge", "Merging the extension")
    ctx.progress(0.8)
    try:
        refresh_extensions(paths, runner)
    except Failed as error:
        # Do not leave a broken extension behind: remove it and restore the previous merge.
        shutil.rmtree(_enabled_dir(paths, name), ignore_errors=True)
        try:
            os.unlink(_meta_path(paths, name))
        except OSError:
            pass
        try:
            refresh_extensions(paths, runner)
        except Failed:
            pass
        raise Failed(f"the extension could not be merged and was removed again: {error}") from error
    return {"name": name, "version": report["version"], "merged": True, "dropped": dropped,
            "notes": [n["text"] for n in done["notes"]]}


def remove(name, paths, ctx, runner=run):
    meta = _meta_path(paths, name)
    enabled, disabled = _enabled_dir(paths, name), _disabled_dir(paths, name)
    if not os.path.exists(meta):
        raise NotFound(f"{name} is not an installed compat extension")
    ctx.step("remove", f"Removing {name}")
    ctx.commit()
    with open(meta, encoding="utf-8") as handle:
        recorded = json.load(handle)
    units = recorded.get("units", [])
    for tree in (enabled, disabled):
        if os.path.isdir(tree):
            remove_etc_copies(paths, name, recorded.get("etc_copies", []), tree)
            break
    if units:
        from . import remote
        try:
            remote.System(paths.get("bus", "system")).disable_units(units)
        except Failed as error:
            log.warning("could not disable the services of %s: %s", name, error)
    shutil.rmtree(enabled, ignore_errors=True)
    shutil.rmtree(disabled, ignore_errors=True)
    os.unlink(meta)
    ctx.step("merge", "Updating the merged system")
    refresh_extensions(paths, runner)
    return {"name": name, "removed": True}


def list_installed(paths, runner=run):
    try:
        merged = extensions.merged_names("sysext", runner)
    except Failed:
        merged = set()
    result = []
    for meta in installed_metas(paths):
        name = meta["name"]
        enabled = os.path.isdir(_enabled_dir(paths, name))
        result.append({"name": name, "version": meta.get("version", ""), "release": meta.get("release", ""),
                       "arch": meta.get("arch", ""), "files": meta.get("files", 0), "enabled": enabled,
                       "merged": name in merged, "problems": [] if enabled else meta.get("problems", [])})
    return result


def revalidate(paths, ctx, runner=run):
    """Recheck every extension against the current base; disable those that no longer fit."""
    libraries = host_libraries(runner)
    metas = installed_metas(paths)
    ctx.step("check", "Checking extensions against the current base")
    disabled, enabled, changed = [], [], False
    for index, meta in enumerate(metas):
        name = meta["name"]
        others = set()
        for other in metas:
            if other is not meta:
                others.update(other.get("provides", []))
        missing = check_requirements(meta.get("requires", []), others, runner)
        available = libraries | {lib for other in metas if other is not meta for lib in other.get("provides_libs", [])}
        missing_libs = [lib for lib in meta.get("needed_libs", []) if lib not in available]
        problems = []
        if built_for_other_image(meta, paths):
            problems.append("built for a different system image; it is rebuilt for this one (foundation compat refresh)")
        if missing:
            problems.append(f"requirements no longer provided: {', '.join(missing)}")
        if missing_libs:
            problems.append(f"libraries no longer available: {', '.join(missing_libs)}")
        is_enabled = os.path.isdir(_enabled_dir(paths, name))
        if problems and is_enabled:
            os.makedirs(os.path.dirname(_disabled_dir(paths, name)), exist_ok=True)
            os.rename(_enabled_dir(paths, name), _disabled_dir(paths, name))
            disabled.append(name)
            changed = True
        elif not problems and not is_enabled and os.path.isdir(_disabled_dir(paths, name)):
            os.rename(_disabled_dir(paths, name), _enabled_dir(paths, name))
            enabled.append(name)
            changed = True
        meta["problems"] = problems
        _write_json(_meta_path(paths, name), meta)
        ctx.progress((index + 1) / max(1, len(metas)))
    if changed:
        ctx.step("merge", "Updating the merged system")
        refresh_extensions(paths, runner)
    return {"checked": len(metas), "disabled": disabled, "enabled": enabled}
