"""Over-the-air updates: look at the update source, remember what is available, download and install the newest update.

The source is the feed at `ota.url` (feed.py). `check` asks it and records the answer in `update-check.json` in the state directory, which a
user session may read (the update window and the notification service use it); `install_latest` is the job that downloads the archive the
feed names and hands it to `updates.install`, which verifies its signature and writes it into the standby slot. Nothing here decides
*whether* to install by itself: the caller does (a person pressing Update, or the automatic update service when `ota.auto_update` is on).
"""

import json
import os
import time

from ..errors import InvalidArgument
from . import feed, osinfo, updates

CACHE_SECONDS = 30          # a second check this soon after the last one answers from the record, so a client cannot hammer the source


def check_path(paths):
    return os.path.join(paths["state"], "update-check.json")


def running_version(paths):
    return osinfo.os_release(paths["os_release"]).get("IMAGE_VERSION") or ""


def _write(paths, record):
    path = check_path(paths)
    temporary = path + ".tmp"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(record, handle)
    os.chmod(temporary, 0o644)          # a user session reads it
    os.replace(temporary, path)


def _read(paths):
    try:
        with open(check_path(paths), encoding="utf-8") as handle:
            record = json.load(handle)
    except (OSError, ValueError):
        return {}
    return record if isinstance(record, dict) else {}


def available(paths):
    """What the last check found, judged against what runs now: {} if no check was made, else
    {"available", "current", "version", "size", "checked"}. An update that is installed or older is no longer available."""
    record = _read(paths)
    if not record:
        return {}
    current = running_version(paths)
    version = str(record.get("version") or "")
    record.update({"current": current, "available": bool(version) and feed.is_newer(version, current)})
    return record


def check(paths, base, arch, now=time.time, fetch=feed.fetch_text):
    """Ask the update source and record the answer. Returns what `available` returns."""
    recent = _read(paths)
    if recent and now() - float(recent.get("checked", 0)) < CACHE_SECONDS and recent.get("source") == base:
        return available(paths)
    found = feed.read_feed(base, arch, fetch)
    _write(paths, {"version": found["version"], "size": found["size"], "checked": int(now()), "source": base})
    return available(paths)


class _Scaled:
    """A job context whose progress runs from `start` to `end` of the whole job (a step of a longer job)."""

    def __init__(self, ctx, start, end):
        self._ctx, self._start, self._end = ctx, start, end

    def progress(self, fraction):
        self._ctx.progress(self._start + (self._end - self._start) * max(0.0, min(1.0, fraction)))

    def __getattr__(self, name):
        return getattr(self._ctx, name)


def install_latest(paths, base, arch, ctx, runner=None, fetch=feed.fetch_text, opener=open, mount=None):
    """Download the newest update from the source and install it into the standby slot. The signature is always checked."""
    ctx.step("check", "Looking for the update")
    found = feed.read_feed(base, arch, fetch)
    current = running_version(paths)
    if not feed.is_newer(found["version"], current):
        raise InvalidArgument(f"this system is already up to date (version {current})")
    ctx.log(f"downloading version {found['version']} from {found['url']}")
    ctx.step("download", f"Downloading version {found['version']}")
    staging = os.path.join(paths["state"], "staging")
    os.makedirs(staging, mode=0o700, exist_ok=True)
    archive = feed.download(found["url"], staging, found["sha256"], progress=_Scaled(ctx, 0.0, 0.3).progress)
    try:
        kwargs = {"runner": runner} if runner else {}
        if mount is not None:
            kwargs["mount"] = mount
        return updates.install(paths, archive, _Scaled(ctx, 0.3, 1.0), unsigned=False, force=False, opener=opener, **kwargs)
    finally:
        try:
            os.unlink(archive)
        except OSError:
            pass
