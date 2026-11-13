"""The update feed: what the update source says is available, and downloading the archive it names.

`<ota.url>/latest-<arch>.json`: {"version": "0.1.1", "file": "ecp_0.1.1-x86_64.tar.zst", "size": 1234, "sha256": "...", "arch": "x86_64"}.
`file` is relative to the feed or an absolute address. The archive carries its own signed manifest, which updates.read_archive verifies;
the feed only says what is available and is not signed, so a wrong feed can at worst offer the wrong archive (which the signature
check then rejects) or an older signed one (a downgrade that nothing refuses yet).
"""

import hashlib
import ipaddress
import json
import os
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request

from ..errors import Failed, Unavailable

MAX_FEED = 1024 * 1024
MAX_ARCHIVE = 8 * 1024 ** 3


def url_allowed(url):
    """https from anywhere; plain http only from this machine or a private network (a test server)."""
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


def is_placeholder(base):
    """True for an update source that is not set up (empty, or the reserved .invalid name)."""
    host = urllib.parse.urlparse(base or "").hostname or ""
    return not base or host.endswith(".invalid")


def feed_url(base, arch):
    return base.rstrip("/") + f"/latest-{arch}.json"


def parse_feed(text, base, arch):
    """{"version", "url", "size", "sha256"} for this architecture, or Failed."""
    try:
        data = json.loads(text)
    except ValueError as error:
        raise Failed("the update feed is not valid") from error
    if not isinstance(data, dict) or not isinstance(data.get("version"), str) or not data["version"] \
            or not isinstance(data.get("file"), str) or not data["file"]:
        raise Failed("the update feed does not name a version and a file")
    if data.get("arch") not in (None, arch):
        raise Failed(f"the update feed is for {data.get('arch')}, this computer is {arch}")
    url = urllib.parse.urljoin(feed_url(base, arch), data["file"])
    if not url_allowed(url):
        raise Failed("the update feed points at an address that is not allowed (https only, or a private network)")
    size = data.get("size")
    sha = str(data.get("sha256") or "").lower()
    if sha and not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise Failed("the update feed has a malformed checksum")
    return {"version": data["version"], "url": url, "size": size if isinstance(size, int) and size > 0 else 0, "sha256": sha}


def version_key(version):
    return tuple(int(part) for part in re.findall(r"\d+", version or ""))


def is_newer(candidate, running):
    """Whether `candidate` is a newer version than `running` (number by number: 0.1.10 is newer than 0.1.9)."""
    if not running:
        return True
    return version_key(candidate) > version_key(running)


def fetch_text(url, limit=MAX_FEED, timeout=30):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            data = response.read(limit + 1)
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise Unavailable(f"cannot reach the update source {url}: {getattr(error, 'reason', error)}") from error
    if len(data) > limit:
        raise Failed("the update feed is larger than expected")
    return data.decode("utf-8", errors="replace")


def read_feed(base, arch, fetch=fetch_text):
    """The feed of the update source `base` for `arch`, parsed."""
    if is_placeholder(base):
        raise Unavailable("no update source is set up (the setting ota.url)")
    url = feed_url(base, arch)
    if not url_allowed(url):
        raise Unavailable("the update source must be an https address (plain http only from this computer or a private network)")
    return parse_feed(fetch(url), base, arch)


def download(url, directory, sha256="", limit=MAX_ARCHIVE, progress=lambda fraction: None, timeout=60):
    """Download `url` into a new file in `directory` and return its path; a checksum that does not match is an error."""
    try:
        response = urllib.request.urlopen(url, timeout=timeout)
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise Unavailable(f"cannot download the update: {getattr(error, 'reason', error)}") from error
    total = int(response.headers.get("Content-Length") or 0)
    handle = tempfile.NamedTemporaryFile(dir=directory, prefix="update-", suffix=".tar.zst", delete=False)
    digest, done = hashlib.sha256(), 0
    try:
        with handle, response:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                done += len(chunk)
                if done > limit:
                    raise Failed("the update is larger than the allowed size")
                digest.update(chunk)
                handle.write(chunk)
                if total:
                    progress(min(1.0, done / total))
            handle.flush()
            os.fsync(handle.fileno())
        if sha256 and digest.hexdigest() != sha256:
            raise Failed("the downloaded update does not match the checksum in the update feed")
    except OSError as error:
        os.unlink(handle.name)
        raise Unavailable(f"the download was interrupted: {error}") from error
    except BaseException:
        os.unlink(handle.name)
        raise
    return handle.name
