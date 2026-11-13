"""Text and small decisions for the updater window, kept apart from GTK so they can be tested without a display."""

import os

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


def classify_source(path):
    """('file', None) for an update archive that exists, else ('', why not)."""
    path = (path or "").strip()
    if not path:
        return "", "Choose a file."
    if not os.path.isfile(path):
        return "", "That file does not exist."
    if not path.endswith(ARCHIVE_SUFFIXES):
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


def size_text(size):
    if not size:
        return ""
    return f"{size / 1024 ** 3:.1f} GB" if size >= 1024 ** 3 else f"{size / 1024 ** 2:.0f} MB"
