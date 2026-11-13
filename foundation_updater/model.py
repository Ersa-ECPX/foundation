"""Text and small decisions for the updater window, kept apart from GTK so they can be tested without a display."""

import json
import os
import re
import urllib.parse

from foundation import fetch

PRODUCT = "Ersa Compute Platform"
ARCHIVE_SUFFIXES = (".tar.zst", ".tar", ".zst")


def slot_title(slot):
    return "Slot " + str(slot["name"]).upper()


def slot_subtitle(slot):
    """One line under a slot's name: what it holds and what it does."""
    parts = []
    if slot.get("live"):
        return "The installer medium's own system"
    parts.append(f"Version {slot['version']}" if slot.get("version") else "Empty")
    if slot.get("running"):
        parts.append("running now")
    if slot.get("default"):
        parts.append("starts by default")
    if slot.get("bootable") and not slot.get("running") and slot.get("tries_left", -1) >= 0:
        parts.append(f"{slot['tries_left']} start attempts left")
    return " · ".join(parts)


def running_title(state):
    version = state.get("running_version") or ""
    return f"Version {version}" if version else "Version unknown"


def running_subtitle(state):
    slot = state.get("running_slot") or ""
    return f"Running from slot {slot.upper()}" if slot else "The running slot is unknown"


def assessment(state):
    """(style, text) for the boot assessment: style is good, pending or unknown."""
    kind = state.get("assessment", "unknown")
    if kind == "good":
        return "good", "This start was confirmed good"
    if kind == "pending":
        return "pending", f"Waiting to be confirmed ({state.get('tries_left', 0)} attempts left)"
    return "unknown", "Not checked"


def pending_text(update):
    """The banner text when an installed update waits for a restart, or ''."""
    if not update:
        return ""
    version = update.get("version") or ""
    return f"Version {version} is installed. Restart to start using it." if version else "An update is installed. Restart to start using it."


def classify_source(text):
    """('url', None), ('file', None) or ('', problem) for what the person typed or chose."""
    text = (text or "").strip()
    if not text:
        return "", "Enter an address or choose a file."
    if text.startswith(("http://", "https://")):
        if not fetch.url_allowed(text):
            return "", "Updates are downloaded over https (plain http only from this computer or a private network)."
        return "url", None
    if not os.path.isfile(text):
        return "", "That file does not exist."
    if not text.endswith(ARCHIVE_SUFFIXES):
        return "", "That does not look like an update archive (.tar.zst)."
    return "file", None


def result_lines(result):
    """The facts about a finished install, one short line each."""
    lines = []
    version, slot = result.get("version"), result.get("slot")
    if version:
        lines.append(f"Version {version} was written to slot {str(slot).upper()}." if slot else f"Version {version} was installed.")
    lines.append("The signature was checked." if result.get("signed") else "The signature was not checked.")
    ready = result.get("bundles", {}).get("ready", [])
    if ready:
        lines.append("Your bundles were rebuilt for it: " + ", ".join(ready) + ".")
    lines.extend(result.get("notes", []))
    return lines


def error_text(name, message):
    """What to tell the person when the install could not run. The running system is never changed by a failed install."""
    if name == "NotAuthorized":
        return "The administrator's password was not given, so nothing was changed."
    if name == "Busy":
        return "Another change is in progress. Try again when it has finished."
    if name == "Unreachable":
        return message
    return message or "The update could not be installed."


def reboot_failed(name, message):
    return "The restart was not started: " + error_text(name, message)


# -- the update feed ---------------------------------------------------------------------------
#
# `<ota.url>/latest-<arch>.json`: {"version": "0.1.1", "file": "ecp_0.1.1-x86_64.tar.zst", "size": 1234, "sha256": "..."}.
# `file` is relative to the feed or an absolute address. The archive carries its own signed manifest, which foundationd
# verifies; the feed only says what is available, so a wrong feed can at worst offer the wrong archive.

class FeedError(Exception):
    """The feed could not be used; the message is for a person."""


def is_placeholder(base):
    host = urllib.parse.urlparse(base or "").hostname or ""
    return not base or host.endswith(".invalid")


def feed_url(base, arch):
    return base.rstrip("/") + f"/latest-{arch}.json"


def parse_feed(text, base, arch):
    """{"version", "url", "size", "sha256"} for this architecture, or FeedError."""
    try:
        data = json.loads(text)
    except ValueError as error:
        raise FeedError("the update feed is not valid") from error
    if not isinstance(data, dict) or not isinstance(data.get("version"), str) or not data["version"] \
            or not isinstance(data.get("file"), str) or not data["file"]:
        raise FeedError("the update feed does not name a version and a file")
    if data.get("arch") not in (None, arch):
        raise FeedError(f"the update feed is for {data.get('arch')}, this computer is {arch}")
    url = urllib.parse.urljoin(feed_url(base, arch), data["file"])
    if not fetch.url_allowed(url):
        raise FeedError("the update feed points at an address that is not allowed (https only, or a private network)")
    size = data.get("size")
    sha = str(data.get("sha256") or "").lower()
    if sha and not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise FeedError("the update feed has a malformed checksum")
    return {"version": data["version"], "url": url, "size": size if isinstance(size, int) and size > 0 else 0, "sha256": sha}


def version_key(version):
    return tuple(int(part) for part in re.findall(r"\d+", version or ""))


def is_newer(candidate, running):
    """Whether `candidate` is a newer version than `running` (versions compare number by number: 0.1.10 > 0.1.9)."""
    if not running:
        return True
    return version_key(candidate) > version_key(running)


def size_text(size):
    if not size:
        return ""
    return f"{size / 1024 ** 3:.1f} GB" if size >= 1024 ** 3 else f"{size / 1024 ** 2:.0f} MB"
