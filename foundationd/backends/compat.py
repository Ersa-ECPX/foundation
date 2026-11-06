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
import os
import shutil
import subprocess
import tempfile
import time

from ..errors import Failed, InvalidArgument, NotFound
from . import cpio, elf, extensions, osinfo, richdeps, sellabel
from .runner import check, run

USRMERGE = {"bin": "usr/bin", "sbin": "usr/sbin", "lib": "usr/lib", "lib64": "usr/lib64"}
HIERARCHIES = ("usr", "opt")
ARCH = {"x86_64": "x86-64", "aarch64": "arm64"}
RELEASE_DIR = "usr/lib/extension-release.d"


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


def apply_usrmerge(root):
    """Move /bin, /sbin, /lib and /lib64 of an unpacked payload under /usr, and report what is left
    outside /usr and /opt (which is deleted: a sysext can only overlay those)."""
    for old, new in USRMERGE.items():
        source = os.path.join(root, old)
        if os.path.isdir(source) and not os.path.islink(source):
            shutil.copytree(source, os.path.join(root, new), symlinks=True, dirs_exist_ok=True)
            shutil.rmtree(source)
        elif os.path.lexists(source):
            os.unlink(source)
    dropped = []
    for entry in sorted(os.listdir(root)):
        if entry in HIERARCHIES:
            continue
        dropped.append("/" + entry)
        full = os.path.join(root, entry)
        shutil.rmtree(full) if os.path.isdir(full) and not os.path.islink(full) else os.unlink(full)
    return dropped


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
        notes.append(f"{len(outside)} file(s) outside /usr and /opt will not be installed (configuration, cron jobs, ...)")
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
        dropped = apply_usrmerge(tree)
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
            "dropped": dropped, "forced": bool(report["problems"]), "problems": report["problems"], **extras}
    _write_json(_meta_path(paths, name), meta)

    ctx.step("merge", "Merging the extension")
    ctx.progress(0.8)
    try:
        extensions.operate("sysext", "refresh", runner)
    except Failed as error:
        # Do not leave a broken extension behind: remove it and restore the previous merge.
        shutil.rmtree(_enabled_dir(paths, name), ignore_errors=True)
        try:
            os.unlink(_meta_path(paths, name))
        except OSError:
            pass
        try:
            extensions.operate("sysext", "refresh", runner)
        except Failed:
            pass
        raise Failed(f"the extension could not be merged and was removed again: {error}") from error
    return {"name": name, "version": report["version"], "merged": True, "dropped": dropped}


def remove(name, paths, ctx, runner=run):
    meta = _meta_path(paths, name)
    enabled, disabled = _enabled_dir(paths, name), _disabled_dir(paths, name)
    if not os.path.exists(meta):
        raise NotFound(f"{name} is not an installed compat extension")
    ctx.step("remove", f"Removing {name}")
    ctx.commit()
    shutil.rmtree(enabled, ignore_errors=True)
    shutil.rmtree(disabled, ignore_errors=True)
    os.unlink(meta)
    ctx.step("merge", "Updating the merged system")
    extensions.operate("sysext", "refresh", runner)
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
        extensions.operate("sysext", "refresh", runner)
    return {"checked": len(metas), "disabled": disabled, "enabled": enabled}
