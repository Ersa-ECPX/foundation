"""Download an update archive: which addresses are accepted, and the download itself (shared by the command line and the updater window)."""

import hashlib
import ipaddress
import os
import tempfile
import urllib.error
import urllib.parse
import urllib.request


class FetchError(Exception):
    """The address is not accepted or the download failed; the message is for a person."""


def url_allowed(url):
    """https from anywhere; plain http only from this machine or a private network (a test server). The update is
    verified by its signature either way, but an unsigned one must not travel over the open internet unprotected."""
    parts = urllib.parse.urlparse(url)
    if parts.scheme == "https" and parts.hostname:
        return True
    if parts.scheme != "http" or not parts.hostname:
        return False
    try:
        address = ipaddress.ip_address(parts.hostname)
    except ValueError:
        return parts.hostname == "localhost"
    return address.is_private or address.is_loopback


def fetch(url, directory, limit=64 * 1024 ** 3, progress=None):
    """Download an update archive to a new file in `directory`. Returns its path. `progress(done, total)` gets bytes (total 0 if unknown)."""
    try:
        response = urllib.request.urlopen(url, timeout=60)
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise FetchError(f"cannot download {url}: {getattr(error, 'reason', error)}") from error
    total = int(response.headers.get("Content-Length") or 0)
    handle = tempfile.NamedTemporaryFile(dir=directory, prefix="update-", suffix=".tar.zst", delete=False)
    done = 0
    try:
        with handle:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                done += len(chunk)
                if done > limit:
                    raise FetchError("the update is larger than the allowed size")
                handle.write(chunk)
                if progress:
                    progress(done, total)
    except BaseException:
        os.unlink(handle.name)
        raise
    return handle.name


def fetch_text(url, limit=1024 * 1024, timeout=20):
    """A small text document (the update feed) as a string."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            data = response.read(limit + 1)
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise FetchError(f"cannot reach {url}: {getattr(error, 'reason', error)}") from error
    if len(data) > limit:
        raise FetchError("the update feed is larger than expected")
    return data.decode("utf-8", errors="replace")


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
