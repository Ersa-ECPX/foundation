"""Bundles: one system extension built from many packages out of the repositories.

A bundle is a named extension (`/var/lib/extensions/<name>`) plus a manifest of what was asked for. Adding or
removing a package re-solves the whole bundle and rebuilds the extension: the new tree is built beside the
old one, swapped in, and merged; if the merge fails the previous tree comes back. Downloaded RPMs stay in
`<state>/bundles/<name>.rpms`, so a rebuild fetches only what is new, and the other bundles' RPMs count as
installed when a bundle is solved, so nothing is duplicated between bundles.

Everything a bundle contains came from a signed repository (repos.py checks checksums and signatures); no
scriptlet is ever run (compat.extract is a strict cpio reader); every file is labelled for SELinux for the path
it will have once merged (sellabel.py). Bundles share compat's bookkeeping (`<state>/compat/<name>.json`), so
`compat list`, `compat remove` and `compat revalidate` see them like any other extension.

The catalog (`/usr/lib/foundation/catalog.d/*.json`) is a list of ready-made bundles: a name, a description,
the repositories it needs and its package list.
"""

import json
import os
import re
import shutil
import tempfile
import time

from ..errors import Failed, InvalidArgument, NotFound, Unavailable
from . import compat, extensions, repos, sellabel
from .runner import run

CATALOG_DIR = "usr/lib/foundation/catalog.d"
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
PACKAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,127}$")
STALE_AFTER = 24 * 3600          # metadata older than this is refreshed before solving


# -- paths and manifests -----------------------------------------------------------------------

def _rpm_dir(paths, name):
    return os.path.join(paths["state"], "bundles", f"{name}.rpms")


def _meta(paths, name):
    for meta in compat.installed_metas(paths):
        if meta.get("name") == name:
            return meta
    return None


def _images_dir(paths, identity):
    """Where bundle trees built for one image are kept while that image is not the running one."""
    return os.path.join(paths["state"], "bundles", "images", identity)


def _archive(paths, name, meta):
    """Move a bundle's tree (active or disabled) and manifest into the store of the image it was built for."""
    identity = (meta.get("bundle") or {}).get("built_for")
    if not identity:
        return False
    for source in (compat._enabled_dir(paths, name), compat._disabled_dir(paths, name)):
        if os.path.lexists(source):
            target_dir = _images_dir(paths, identity)
            os.makedirs(target_dir, exist_ok=True)
            target = os.path.join(target_dir, name)
            shutil.rmtree(target, ignore_errors=True)
            os.rename(source, target)
            compat._write_json(os.path.join(target_dir, f"{name}.json"), meta)
            return True
    return False


def _restore(paths, name, identity, enabled=True):
    """Bring a stored tree for `identity` back as the active one. Returns its manifest, or None if there is none."""
    stored = os.path.join(_images_dir(paths, identity), name)
    try:
        with open(stored + ".json", encoding="utf-8") as handle:
            meta = json.load(handle)
    except (OSError, ValueError):
        return None
    if not os.path.isdir(stored):
        return None
    destination = compat._enabled_dir(paths, name)
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    shutil.rmtree(destination, ignore_errors=True)
    os.rename(stored, destination)
    os.unlink(stored + ".json")
    compat._write_json(compat._meta_path(paths, name), meta)
    return meta


def state_of(paths, meta):
    """current: built for this image and enabled. stale: built for another image (or no longer fits). disabled: otherwise off."""
    name = meta["name"]
    if compat.built_for_other_image(meta, paths):
        return "stale"
    if not os.path.isdir(compat._enabled_dir(paths, name)):
        return "disabled"
    release = os.path.join(compat._enabled_dir(paths, name), compat.RELEASE_DIR, f"extension-release.{name}")
    try:
        with open(release, encoding="utf-8") as handle:
            wanted = compat.release_file(name, meta.get("arch", ""), paths).splitlines()
            have = handle.read().splitlines()
    except OSError:
        return "stale"
    return "current" if all(line in have for line in wanted if not line.startswith("ARCHITECTURE=")) else "stale"


def _check_name(name):
    if not isinstance(name, str) or not NAME.match(name):
        raise InvalidArgument(f"unusable bundle name: {name!r}")
    return name


def _spec(item):
    """A package request as a dict, from a name or a dict."""
    if isinstance(item, str):
        item = {"name": item}
    name = item.get("name")
    if not isinstance(name, str) or not PACKAGE.match(name):
        raise InvalidArgument(f"unusable package name: {name!r}")
    spec = {"name": name}
    if item.get("repo"):
        if not isinstance(item["repo"], str) or not repos.ALIAS.match(item["repo"]):
            raise InvalidArgument(f"unusable repository name: {item['repo']!r}")
        spec["repo"] = item["repo"]
    if item.get("when_installed"):
        spec["when_installed"] = True
    return spec


# -- catalog -----------------------------------------------------------------------------------

def catalog(paths):
    """The ready-made bundles, as a list of dicts (name, title, description, notice, repos, packages)."""
    directory = os.path.join(paths["root"], CATALOG_DIR.lstrip("/")) if paths["root"] not in ("", "/") else "/" + CATALOG_DIR
    entries = []
    try:
        names = sorted(n for n in os.listdir(directory) if n.endswith(".json"))
    except OSError:
        return entries
    for filename in names:
        try:
            with open(os.path.join(directory, filename), encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, ValueError) as error:
            raise Failed(f"cannot read the catalog entry {filename}: {error}") from error
        entries.append({
            "name": _check_name(raw.get("name")), "title": str(raw.get("title", raw["name"])),
            "description": str(raw.get("description", "")), "notice": str(raw.get("notice", "")),
            "repos": [r for r in raw.get("repos", []) if repos.ALIAS.match(str(r))],
            "packages": [_spec(p) for p in raw.get("packages", [])]})
    return entries


def catalog_entry(paths, name):
    for entry in catalog(paths):
        if entry["name"] == name:
            return entry
    raise NotFound(f"no catalog entry named {name}")


# -- listing -----------------------------------------------------------------------------------

def list_bundles(paths):
    result = []
    for meta in compat.installed_metas(paths):
        bundle = meta.get("bundle")
        if not bundle:
            continue
        result.append({"name": meta["name"], "catalog": bundle.get("catalog") or "",
                       "requested": [s["name"] for s in bundle.get("specs", [])],
                       "packages": [f"{p['name']}-{p['evr']}" for p in bundle.get("packages", [])],
                       "repos": sorted({p["repo"] for p in bundle.get("packages", [])}),
                       "updated": bundle.get("updated", 0), "files": meta.get("files", 0),
                       "enabled": os.path.isdir(compat._enabled_dir(paths, meta["name"])),
                       "state": state_of(paths, meta), "built_for": bundle.get("built_for", ""),
                       "notes": [n["text"] for n in meta.get("scriptlet_notes", [])]})
    return result


def dependents(paths, name, runner=run):
    """Other bundles that use libraries only this bundle provides."""
    me = _meta(paths, name)
    if me is None:
        return []
    mine = set(me.get("provides_libs", [])) - compat.host_libraries(runner)
    found = []
    for meta in compat.installed_metas(paths):
        if meta.get("name") != name and meta.get("bundle") and mine & set(meta.get("needed_libs", [])):
            found.append(meta["name"])
    return found


# -- building ----------------------------------------------------------------------------------

def _others_rpms(paths, name):
    files = []
    for meta in compat.installed_metas(paths):
        if meta.get("name") == name or not meta.get("bundle"):
            continue
        files += [p["file"] for p in meta["bundle"].get("packages", []) if os.path.exists(p.get("file", ""))]
    return files


def _refresh_needed(paths, enabled, force, ctx, runner, fetcher=None, file_fetcher=None):
    kwargs = {}
    if fetcher:
        kwargs["fetcher"] = fetcher
    if file_fetcher:
        kwargs["file_fetcher"] = file_fetcher
    for index, repo in enumerate(enabled):
        age = repos.metadata_age(paths, repo.alias)
        if force or age is None or age > STALE_AFTER or repos.cached_primary(paths, repo.alias) is None:
            ctx.step("refresh", f"Updating the package list of {repo.name}")
            repos.refresh(repo, paths, runner, force=force, **kwargs)


def _assemble(paths, name, files, runner):
    """A new extension tree from the downloaded RPMs. Returns (scratch, tree, dropped, scan)."""
    scratch = tempfile.mkdtemp(dir=compat._scratch(paths), prefix="bundle.")
    try:
        tree = os.path.join(scratch, name)
        os.mkdir(tree)
        for index, path in enumerate(files):
            compat.extract(path, tree)
        dropped = compat.apply_usrmerge(tree, name)
        done = compat.apply_scriptlets(tree, name, list(files), runner)
        done["etc"] = compat.stage_etc(tree, name)
        compat.touch_icon_themes(tree)
        release_dir = os.path.join(tree, compat.RELEASE_DIR)
        os.makedirs(release_dir, exist_ok=True)
        with open(os.path.join(release_dir, f"extension-release.{name}"), "w", encoding="utf-8") as handle:
            handle.write(compat.release_file(name, os.uname().machine, paths))
        contexts = sellabel.Contexts.load(paths["root"])
        if contexts is not None:
            sellabel.label_tree(tree, contexts)
        return scratch, tree, dropped, done
    except BaseException:
        shutil.rmtree(scratch, ignore_errors=True)
        raise


def _count_files(tree):
    return sum(len(files) for _d, _dirs, files in os.walk(tree))


def build(paths, ctx, name, specs, catalog_name="", allow_repos=(), force_refresh=False, runner=run,
          fetcher=None, file_fetcher=None, pool_factory=None):
    """Create or rebuild bundle `name` so that it provides `specs`. Returns a summary dict."""
    _check_name(name)
    specs = [_spec(s) for s in specs]
    if not specs:
        raise InvalidArgument("a bundle needs at least one package")
    existing = _meta(paths, name)
    if existing is not None and not existing.get("bundle"):
        raise InvalidArgument(f"{name} is an extension installed from an RPM file, not a bundle")
    if existing is None and os.path.lexists(compat._enabled_dir(paths, name)):
        raise InvalidArgument(f"{name} is already an extension that foundation does not manage")

    # Repositories the request needs must be enabled; a third-party one only with the caller's say-so.
    ctx.step("repositories", "Checking the package repositories")
    available = repos.load_repos(paths)
    needed = {s["repo"] for s in specs if s.get("repo")}
    for alias in sorted(needed):
        if alias not in available:
            raise NotFound(f"no repository named {alias}")
        if not available[alias].enabled:
            if alias not in allow_repos:
                raise InvalidArgument(f"the repository {alias} is not enabled; enable it (foundation repo enable {alias}) "
                                      "or confirm it when installing")
            ctx.log(f"enabling the repository {alias}")
            repos.set_enabled(paths, alias, True)
    enabled = repos.enabled(paths)
    if not enabled:
        raise Failed("no package repository is enabled")
    _refresh_needed(paths, enabled, force_refresh, ctx, runner, fetcher, file_fetcher)
    ctx.progress(0.15)

    ctx.step("solve", "Working out what is needed")
    others = _others_rpms(paths, name)
    pool = (pool_factory or repos.Pool)(enabled, paths, others)
    todo, skipped, _missing = pool.resolve(specs)
    if not todo:
        raise InvalidArgument("everything requested is already part of the base image"
                              + (f" (skipped: {', '.join(skipped)})" if skipped else ""))
    ctx.log("packages: " + ", ".join(f"{p['name']}-{p['evr']}" for p in todo))
    ctx.progress(0.25)

    ctx.step("download", f"Downloading {len(todo)} package(s)")
    cache = _rpm_dir(paths, name)
    downloaded = repos.download(todo, enabled, cache, runner, progress=lambda done, total: ctx.progress(0.25 + 0.35 * done / total),
                                **({"file_fetcher": file_fetcher} if file_fetcher else {}))
    ctx.progress(0.6)

    ctx.step("build", "Building the extension")
    scratch, tree, dropped, done = _assemble(paths, name, [p["file"] for p in downloaded], runner)
    notes = done["notes"]
    for note in notes:
        ctx.log("scriptlets not run: " + note["text"])
    try:
        required, _optional, provided = compat.scan_libraries(tree)
        files = _count_files(tree)
        ctx.progress(0.75)

        ctx.step("install", "Installing the extension")
        ctx.commit()
        destination = compat._enabled_dir(paths, name)
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        previous = None
        identity = compat.image_identity(paths)
        archived = bool(existing) and compat.built_for_other_image(existing, paths) and _archive(paths, name, existing)
        if os.path.lexists(destination):
            previous = os.path.join(scratch, "previous")
            os.rename(destination, previous)
        os.rename(tree, destination)
        meta = {"name": name, "version": f"{len(downloaded)} packages", "release": "", "arch": os.uname().machine,
                "files": files, "installed": int(time.time()), "scriptlets": bool(notes), "scriptlet_notes": notes, "units": done["units"], "etc_copies": done["etc"], "dropped": dropped,
                "forced": False, "problems": [], "requires": [], "provides": [],
                "needed_libs": sorted(lib for lib in required if lib not in provided),
                "optional_libs": [], "provides_libs": sorted(provided),
                "bundle": {"specs": specs, "catalog": catalog_name, "updated": int(time.time()), "built_for": identity,
                           "packages": [{"name": p["name"], "evr": p["evr"], "arch": p["arch"], "repo": p["repo"],
                                         "file": p["file"]} for p in downloaded]}}
        old_meta = existing
        shutil.rmtree(compat._disabled_dir(paths, name), ignore_errors=True)
        compat._write_json(compat._meta_path(paths, name), meta)

        ctx.step("merge", "Merging the extension")
        try:
            compat.refresh_extensions(paths, runner)
        except Failed as error:
            shutil.rmtree(destination, ignore_errors=True)
            if previous is not None:
                os.rename(previous, destination)
            if old_meta is not None:
                compat._write_json(compat._meta_path(paths, name), old_meta)
            else:
                try:
                    os.unlink(compat._meta_path(paths, name))
                except OSError:
                    pass
            if archived:
                _restore(paths, name, (existing["bundle"] or {}).get("built_for"))
            try:
                compat.refresh_extensions(paths, runner)
            except Failed:
                pass
            raise Failed(f"the extension could not be merged; the previous state was restored: {error}") from error
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    repos.forget(cache, [p["file"] for p in downloaded])
    return {"name": name, "packages": [f"{p['name']}-{p['evr']}" for p in downloaded], "skipped": skipped,
            "files": files, "merged": True, "notes": [n["text"] for n in notes]}


def add_packages(paths, ctx, name, wanted, **kwargs):
    """Add packages (names, or dicts with a repo) to a bundle, creating it if needed."""
    _check_name(name)
    existing = _meta(paths, name)
    specs = list(existing["bundle"]["specs"]) if existing and existing.get("bundle") else []
    have = {s["name"] for s in specs}
    for item in wanted:
        spec = _spec(item)
        if spec["name"] not in have:
            specs.append(spec)
            have.add(spec["name"])
    catalog_name = existing["bundle"].get("catalog", "") if existing and existing.get("bundle") else ""
    return build(paths, ctx, name, specs, catalog_name=catalog_name, **kwargs)


def install_catalog(paths, ctx, entry_name, name="", **kwargs):
    entry = catalog_entry(paths, entry_name)
    kwargs.setdefault("allow_repos", tuple(entry["repos"]))
    return build(paths, ctx, name or entry["name"], entry["packages"], catalog_name=entry["name"], **kwargs)


def remove_packages(paths, ctx, name, names, runner=run, **kwargs):
    existing = _meta(paths, name)
    if existing is None or not existing.get("bundle"):
        raise NotFound(f"{name} is not a bundle")
    specs = existing["bundle"]["specs"]
    drop = set(names)
    unknown = sorted(drop - {s["name"] for s in specs})
    if unknown:
        raise NotFound(f"not part of {name}: {', '.join(unknown)} (only packages that were asked for can be removed; "
                       "their dependencies go with them)")
    left = [s for s in specs if s["name"] not in drop]
    if not left:
        return remove_bundle(paths, ctx, name, runner=runner)
    return build(paths, ctx, name, left, catalog_name=existing["bundle"].get("catalog", ""), runner=runner, **kwargs)


def remove_bundle(paths, ctx, name, runner=run):
    blocking = dependents(paths, name, runner)
    if blocking:
        raise InvalidArgument(f"{', '.join(blocking)} use(s) libraries from {name}; remove that first")
    result = compat.remove(name, paths, ctx, runner)
    shutil.rmtree(_rpm_dir(paths, name), ignore_errors=True)
    return result


def update(paths, ctx, name="", runner=run, **kwargs):
    """Re-solve bundles against the newest repository metadata and rebuild those whose packages changed."""
    targets = [b for b in list_bundles(paths) if not name or b["name"] == name]
    if name and not targets:
        raise NotFound(f"{name} is not a bundle")
    results = []
    for index, bundle in enumerate(targets):
        meta = _meta(paths, bundle["name"])
        specs = meta["bundle"]["specs"]
        before = {f"{p['name']}-{p['evr']}" for p in meta["bundle"].get("packages", [])}
        outcome = build(paths, ctx, bundle["name"], specs, catalog_name=bundle["catalog"], force_refresh=(index == 0),
                        runner=runner, **kwargs)
        after = set(outcome["packages"])
        outcome["changed"] = sorted(after - before)
        results.append(outcome)
    return {"bundles": results}


# -- following the system image ------------------------------------------------------------------

def select(paths):
    """At boot, before systemd-sysext merges anything: make every bundle match the running image.

    A bundle built for this image (kept in the store while another image ran) is put back; one that only exists
    for another image is switched off, never merged, because its libraries may not match this base. Only renames,
    so it is quick and safe to run early. Returns {"restored": [...], "disabled": [...], "stale": [...]}.
    """
    identity = compat.image_identity(paths)
    restored, disabled, stale = [], [], []
    for meta in compat.installed_metas(paths):
        bundle = meta.get("bundle")
        if not bundle:
            continue
        name = meta["name"]
        if state_of(paths, meta) == "current":
            continue
        if os.path.isdir(os.path.join(_images_dir(paths, identity), name)):
            _archive(paths, name, meta)
            if _restore(paths, name, identity) is not None:
                restored.append(name)
                continue
        if os.path.isdir(compat._enabled_dir(paths, name)):
            os.makedirs(os.path.dirname(compat._disabled_dir(paths, name)), exist_ok=True)
            shutil.rmtree(compat._disabled_dir(paths, name), ignore_errors=True)
            os.rename(compat._enabled_dir(paths, name), compat._disabled_dir(paths, name))
            disabled.append(name)
        stale.append(name)
    from . import postmerge
    postmerge.apply(paths)
    marker = os.path.join(paths["state"], "bundles", "stale")
    if stale:
        os.makedirs(os.path.dirname(marker), exist_ok=True)
        with open(marker, "w", encoding="utf-8") as handle:
            handle.write("\n".join(stale) + "\n")
    else:
        try:
            os.unlink(marker)
        except OSError:
            pass
    return {"restored": restored, "disabled": disabled, "stale": stale}


def refresh_stale(paths, ctx, runner=run, **kwargs):
    """Rebuild every bundle that does not fit the running image; leave the ones that do alone."""
    todo = [m for m in compat.installed_metas(paths) if m.get("bundle") and state_of(paths, m) == "stale"]
    names = {m["name"] for m in todo}
    skipped = [b["name"] for b in list_bundles(paths) if b["name"] not in names]
    results, failed = [], []
    for index, meta in enumerate(todo):
        bundle = meta["bundle"]
        allow = tuple(sorted({p["repo"] for p in bundle.get("packages", [])}))
        try:
            outcome = build(paths, ctx, meta["name"], bundle["specs"], catalog_name=bundle.get("catalog", ""), allow_repos=allow,
                            force_refresh=(index == 0), runner=runner, **kwargs)
            results.append(outcome)
        except (Failed, InvalidArgument, NotFound, Unavailable) as error:
            ctx.log(f"{meta['name']} could not be rebuilt: {error}")
            failed.append({"name": meta["name"], "error": str(error)})
    marker = os.path.join(paths["state"], "bundles", "stale")
    if not failed:
        try:
            os.unlink(marker)
        except OSError:
            pass
    return {"bundles": results, "failed": failed, "skipped": skipped}


def base_sonames(root):
    """Shared libraries found in the standard library directories of the image mounted at `root`."""
    names = set()
    for directory in ("usr/lib64", "usr/lib", "usr/lib/x86_64-linux-gnu", "usr/lib/aarch64-linux-gnu"):
        try:
            names.update(n for n in os.listdir(os.path.join(root, directory)) if ".so" in n)
        except OSError:
            pass
    return names


def prebuild(paths, ctx, new_root, runner=run, pool_factory=None, fetcher=None, file_fetcher=None):
    """Build every bundle for a system image that is installed but not running yet, so its first boot needs nothing.

    `new_root` is the new image mounted read-only. Each bundle is solved against that image's own package database,
    its libraries are checked against that image, and the finished tree goes into the store for that image;
    `select` puts it in place at the first boot of the image. A bundle that cannot be built (no network, a package
    gone, a library missing) is reported and left for `refresh_stale` after that boot. Returns
    {"identity", "ready": [...], "failed": [{"name", "error"}]}.
    """
    base = {**paths, "root": new_root, "os_release": os.path.join(new_root, "usr/lib/os-release")}
    identity = compat.image_identity(base)
    metas = [m for m in compat.installed_metas(paths) if m.get("bundle")]
    ready, failed = [], []
    if not metas:
        return {"identity": identity, "ready": ready, "failed": failed}
    enabled = repos.enabled(paths)
    try:
        if not enabled:
            raise Failed("no package repository is enabled")
        _refresh_needed(paths, enabled, True, ctx, runner, fetcher, file_fetcher)
    except (Failed, Unavailable) as error:
        return {"identity": identity, "ready": ready,
                "failed": [{"name": m["name"], "error": str(error)} for m in metas]}
    sonames = base_sonames(new_root)
    for index, meta in enumerate(metas):
        name, bundle = meta["name"], meta["bundle"]
        if bundle.get("built_for") == identity:
            ready.append(name)
            continue
        scratch = None
        try:
            ctx.log(f"building {name} for {identity}")
            pool = (pool_factory or repos.Pool)(enabled, base, _others_rpms(paths, name))
            todo, _skipped, _missing = pool.resolve(bundle["specs"])
            if not todo:
                raise Failed("everything in the bundle is now part of the new image")
            downloaded = repos.download(todo, enabled, _rpm_dir(paths, name), runner,
                                        **({"file_fetcher": file_fetcher} if file_fetcher else {}))
            scratch, tree, dropped, done = _assemble(base, name, [p["file"] for p in downloaded], runner)
            required, _optional, provided = compat.scan_libraries(tree)
            notes = done["notes"]
            others = {lib for m in metas if m is not meta for lib in m.get("provides_libs", [])}
            missing = sorted(lib for lib in required if lib not in provided and lib not in sonames and lib not in others)
            if missing:
                raise Failed("the new image no longer provides: " + ", ".join(missing))
            target = os.path.join(_images_dir(paths, identity), name)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.rmtree(target, ignore_errors=True)
            os.rename(tree, target)
            updated = json.loads(json.dumps(meta))
            updated["bundle"].update({"built_for": identity, "updated": int(time.time()),
                                      "packages": [{"name": p["name"], "evr": p["evr"], "arch": p["arch"], "repo": p["repo"],
                                                    "file": p["file"]} for p in downloaded]})
            updated.update({"files": _count_files(target), "dropped": dropped, "scriptlets": bool(notes), "scriptlet_notes": notes, "units": done["units"], "etc_copies": done["etc"],
                            "needed_libs": sorted(lib for lib in required if lib not in provided),
                            "provides_libs": sorted(provided), "installed": int(time.time())})
            compat._write_json(os.path.join(_images_dir(paths, identity), f"{name}.json"), updated)
            repos.forget(_rpm_dir(paths, name), [p["file"] for p in downloaded])
            ready.append(name)
        except (Failed, InvalidArgument, NotFound, Unavailable) as error:
            failed.append({"name": name, "error": str(error)})
        finally:
            if scratch:
                shutil.rmtree(scratch, ignore_errors=True)
        ctx.progress((index + 1) / len(metas))
    return {"identity": identity, "ready": ready, "failed": failed}
