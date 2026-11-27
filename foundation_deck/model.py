"""Text and small decisions for Deck, kept apart from GTK so they can be tested without a display."""

import os
import re

PRODUCT = "Ersa Compute Platform"
ARCHIVE_SUFFIXES = (".tar.zst", ".tar", ".zst")


def slot_title(slot):
    return "Slot " + str(slot["name"]).upper()


def slot_subtitle(slot):
    """One line under a slot's name: what it holds and what it does."""
    if slot.get("live"):
        return "The installer medium's own system"
    parts = [f"Version {slot['version']}" if slot.get("version") else "Empty"]
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


def error_text(name, message):
    """What to tell the person when a call failed."""
    if name == "NotAuthorized":
        return "The administrator's password was not given, so nothing was changed."
    if name == "Busy":
        return "Another change is in progress. Try again when it has finished."
    if "FileNotFoundError" in (message or ""):
        return "This is not available on this system."
    return message or "That did not work."


def size_text(size):
    if not size:
        return ""
    return f"{size / 1024 ** 3:.1f} GB" if size >= 1024 ** 3 else f"{size / 1024 ** 2:.0f} MB"


def failed_update(slots):
    """The version of an update that was tried and did not start (its tries are used up and it is not running), or ''.
    The system then started the other slot again: this is what the person is told after a rollback."""
    for slot in slots:
        if (not slot.get("running") and not slot.get("live") and slot.get("version") and slot.get("entry")
                and slot.get("tries_left", -1) == 0):
            return slot["version"]
    return ""


def rollback_text(version):
    return (f"Version {version} did not start, so the previous system was started instead. "
            "The failed version stays in its slot until the next update replaces it.")


# The sidebar, in the order a person looks for things: (heading, [(page id, title, icon)]). The first group has no heading.
SECTIONS = (
    (None, (("overview", "Overview", "computer-symbolic"),)),
    ("Account", (("ersaid", "Ersa ID", "avatar-default-symbolic"),)),
    ("Software", (("updates", "Updates", "system-software-update-symbolic"),
                  ("bundles", "Bundles and Extensions", "extension-symbolic"),
                  ("repositories", "Repositories", "folder-remote-symbolic"),
                  ("appsources", "Application Sources", "system-software-install-symbolic"),
                  ("packages", "Base Packages", "package-symbolic"))),
    ("System", (("slots", "Boot Slots", "drive-harddisk-symbolic"),
                ("appearance", "Appearance", "applications-graphics-symbolic"),
                ("remote", "Remote Access", "network-wired-symbolic"),
                ("advanced", "Advanced", "preferences-other-symbolic"))),
    ("History", (("activity", "Activity", "document-open-recent-symbolic"),)),
)
SEARCH_TERMS = {
    "overview": "overview about version system status machine developer build license website foundation deck", "updates": "update upgrade install check restart automatic",
    "bundles": "bundles extensions sysext rpm codecs software add install", "repositories": "repositories sources packman signing key find package search",
    "appsources": "application sources flatpak remotes flathub apps store add signed key",
    "packages": "base packages rpm list installed", "slots": "boot slots rollback restart start a b default",
    "appearance": "boot splash light dark theme", "remote": "remote ssh login keys password network",
    "ersaid": "ersa id account sign in sign out profile name email photo picture login identity oidc",
    "advanced": "advanced settings config developer channel url", "activity": "activity history jobs log tasks",
}


def page_matches(name, title, query):
    query = (query or "").strip().lower()
    return not query or all(word in f"{title} {SEARCH_TERMS.get(name, '')}".lower() for word in query.split())


PAGES = tuple(page for _heading, pages in SECTIONS for page in pages)


def slot_actions(slot):
    """Which buttons a slot row gets: a list of 'once' and 'default'. Only a standby slot that can boot is offered."""
    if slot.get("live") or slot.get("running") or not slot.get("bootable"):
        return []
    return ["once"] if slot.get("default") else ["once", "default"]


def slot_can_release(slot):
    """A standby slot that holds something can be emptied; the running slot and the installer medium's never."""
    return not (slot.get("live") or slot.get("running")) and bool(slot.get("version"))


def slot_action_text(action):
    return {"once": "Start Once", "default": "Always Start"}[action]


def slot_confirm(slot, action):
    """(heading, body) to confirm a change of boot choice."""
    name = slot_title(slot)
    if action == "once":
        return (f"Start {name} once?",
                "The next restart starts that system. The one after goes back to the usual choice.")
    return (f"Always start {name}?",
            "Every restart starts that system until you choose another. A failed start is not rolled back automatically.")


def bundle_title(bundle):
    return bundle.get("name", "")


def bundle_subtitle(bundle):
    """One line: how many packages and whether it matches the running system."""
    count = len(bundle.get("packages", []))
    parts = [f"{count} package" + ("" if count == 1 else "s")]
    state = bundle.get("state", "")
    if not bundle.get("enabled", True):
        parts.append("turned off")
    elif state and state not in ("ready", "merged", "ok"):
        parts.append(state.replace("_", " "))
    return " · ".join(parts)


def extension_subtitle(extension):
    parts = [f"{extension.get('version', '')}".strip() or "version unknown"]
    if not extension.get("enabled", True):
        problems = extension.get("problems", [])
        parts.append("turned off" + (f": {problems[0]}" if problems else ""))
    return " · ".join(parts)


def catalog_notice(entry):
    """What to confirm before installing a catalog entry, or ''."""
    lines = []
    if entry.get("notice"):
        lines.append(entry["notice"])
    needs = entry.get("needs_enabling", [])
    if needs:
        lines.append("This turns on the package source: " + ", ".join(needs) + ".")
    return "\n\n".join(lines)


def update_status(available, pending):
    """(style, text) for the Updates page header."""
    if pending:
        return "available", pending_text(pending)
    if available and available.get("available"):
        size = size_text(available.get("size", 0))
        return "available", f"Version {available['version']} is available" + (f" ({size})" if size else "")
    return "", "Check to see whether an update is available."


def job_error(job_name, message):
    return error_text(job_name, message)


def shorten(text, limit=60):
    """The first sentence of a description, cut to `limit` characters."""
    text = (text or "").strip()
    first = text.split(". ")[0].rstrip(".")
    return first if len(first) <= limit else first[:limit - 1].rstrip() + "\u2026"


def package_names(packages):
    """Package names for a list: entries are 'name' or 'name-version' strings, or dicts with a name."""
    names = [p.get("name", "") if isinstance(p, dict) else str(p) for p in packages or []]
    return sorted(n for n in names if n)


# -- overview ------------------------------------------------------------------------------------

def splash_text(info):
    """A line under the boot splash choice, or ''."""
    if info and info.get("uefi") is False:
        return "This computer did not start through UEFI, so the boot splash cannot follow this choice."
    return ""


def about_rows(info, status):
    """[(label, value)] for the About group; every fact is optional."""
    os_info = info.get("os", {}) if info else {}
    rows = []
    for label, value in (("Name", os_info.get("name")), ("Version", os_info.get("image_version")),
                         ("Architecture", (info or {}).get("architecture"))):
        if value:
            rows.append((label, str(value)))
    return rows


# -- repositories --------------------------------------------------------------------------------

def repo_title(repo):
    return repo.get("name") or repo.get("alias", "")


def repo_subtitle(repo):
    parts = [repo.get("url", "")]
    parts.append("signed" if repo.get("signed") else "not signed")
    parts.append("comes with the system" if repo.get("shipped") else "added by you")
    age = repo.get("refreshed_seconds_ago", -1)
    parts.append("never refreshed" if age is None or age < 0 else "refreshed " + age_text(age))
    return " · ".join(p for p in parts if p)


def age_text(seconds):
    if seconds < 90:
        return "just now"
    if seconds < 3600 * 2:
        return f"{seconds // 60} minutes ago"
    if seconds < 86400 * 2:
        return f"{seconds // 3600} hours ago"
    return f"{seconds // 86400} days ago"


def repo_form_problem(alias, url, key):
    """Why the add-repository form cannot be used yet, or ''."""
    if not alias.strip():
        return "Give the repository a name."
    if not (url.startswith("https://") or url.startswith("/")):
        return "The address must start with https://."
    if "BEGIN PGP PUBLIC KEY BLOCK" not in key:
        return "Paste the repository's public signing key."
    return ""


def alias_from(name):
    """A short identifier made from a display name."""
    out = "".join(c.lower() if c.isalnum() else "-" for c in name.strip())
    return "-".join(part for part in out.split("-") if part)


# -- remote access -------------------------------------------------------------------------------

def remote_summary(status):
    """(title, subtitle) for the SSH row."""
    if not status.get("installed", False):
        return "Not available", "The SSH server is not part of this system."
    if status.get("active"):
        text = "On"
    elif status.get("enabled"):
        text = "On at the next start"
    else:
        text = "Off"
    firewall = status.get("firewall", "")
    extra = {"open": "the firewall allows it", "closed": "the firewall blocks it", "unknown": ""}.get(firewall, "")
    return text, extra


def ssh_needs_a_way_in(has_keys, allow_password):
    """True when turning SSH on would be refused: nobody could sign in (no key anywhere and password sign-in off)."""
    return not has_keys and not allow_password


def key_title(key):
    return key.get("comment") or key.get("user", "")


def key_subtitle(key):
    return " · ".join(p for p in (key.get("user", ""), key.get("type", ""), key.get("fingerprint", "")) if p)


def key_form_problem(user, key):
    if not user.strip():
        return "Give the user name."
    words = key.strip().split()
    if len(words) < 2 or not (words[0].startswith("ssh-") or words[0].startswith("ecdsa-") or words[0].startswith("sk-")):
        return "Paste a public key (a line starting with ssh-ed25519, ssh-rsa and so on)."
    return ""


# -- advanced ------------------------------------------------------------------------------------

# Settings Deck does not list: they are set by a preseed or the command line, never edited here (the account provider).
HIDDEN_SETTING_PREFIXES = ("identity.",)


def setting_hidden(key):
    return key.startswith(HIDDEN_SETTING_PREFIXES)


GROUP_TITLES = {"ota": "Updates", "system": "System", "apps": "Applications"}


def config_group(key):
    prefix = key.split(".", 1)[0]
    return GROUP_TITLES.get(prefix, prefix.capitalize())


def config_title(key):
    return key.split(".", 1)[-1].replace("_", " ").capitalize()


# -- activity ------------------------------------------------------------------------------------

JOB_TITLES = {
    "updates.install": "Installed a system update", "updates.install_latest": "Installed the newest system update",
    "boot.set_default": "Changed which system starts", "boot.set_next": "Chose the next start",
    "boot.release": "Emptied a slot", "boot.splash": "Changed the boot splash",
    "apps.install": "Installed an application", "apps.remove": "Removed an application", "apps.update": "Updated applications",
    "apps.setup": "Set up the application source",
    "compat.install": "Installed an extension", "compat.remove": "Removed an extension", "compat.revalidate": "Checked extensions",
    "bundles.install": "Added packages to a bundle", "bundles.install_catalog": "Installed a bundle",
    "bundles.remove": "Removed a bundle", "bundles.update": "Updated bundles", "bundles.refresh": "Rebuilt bundles",
    "repos.enable": "Changed a repository", "repos.add": "Added a repository", "repos.remove": "Removed a repository",
    "repos.refresh": "Refreshed repositories", "remote.set_ssh": "Changed remote access",
    "remote.add_key": "Added an SSH key", "remote.remove_key": "Removed an SSH key",
    "deploy.run": "Installed the system on a disk", "ext.merge": "Merged extensions", "ext.unmerge": "Unmerged extensions",
    "ext.refresh": "Refreshed extensions",
}

JOB_STATES = {"done": "Done", "failed": "Failed", "cancelled": "Cancelled", "running": "Running", "queued": "Waiting"}


def job_title(kind):
    return JOB_TITLES.get(kind) or (kind or "Job").replace(".", " ").replace("_", " ").capitalize()


def job_subtitle(job):
    state = JOB_STATES.get(job.get("State", ""), job.get("State", ""))
    if job.get("State") == "failed" and job.get("ErrorMessage"):
        return f"{state}: {job['ErrorMessage']}"
    if job.get("State") == "running" and job.get("StepTitle"):
        return f"{state}: {job['StepTitle']}"
    return state


# -- extensions and files ------------------------------------------------------------------------

def inspect_summary(report):
    """(ok, text) for a compatibility report from Compat.Inspect."""
    name = report.get("name", "this file")
    lines = []
    if report.get("compatible"):
        lines.append(f"{name} {report.get('version', '')} fits this system.".replace("  ", " "))
    else:
        lines.append(f"{name} does not fit this system.")
    for key, label in (("problems", ""), ("missing_requires", "Missing: "), ("missing_libraries", "Missing library: "),
                       ("conflicts", "Would replace: "), ("notes", "")):
        for item in report.get(key, []):
            lines.append(label + item)
    if report.get("scriptlets"):
        lines.append("Its install scripts are not run.")
    return bool(report.get("compatible")), "\n".join(lines)


def classify_rpm(path):
    path = (path or "").strip()
    if not path:
        return "Choose a file."
    if not path.endswith(".rpm"):
        return "That does not look like an RPM package."
    return ""


def package_line(package):
    return f"{package.get('name', '')}", package.get("version", "")


# The line under each page's headline (Updates sets its own: it shows the update state).
PAGE_BLURBS = {
    "updates": "Check to see whether an update is available.",
    "ersaid": "Your Ersa ID account on this computer.",
    "bundles": "System-level software such as codecs and tools.",
    "repositories": "Where the packages for bundles come from.",
    "appsources": "Where applications come from.",
    "packages": "Everything the system image contains.",
    "slots": "The two system copies, so an update can fall back.",
    "appearance": "How the computer starts up.",
    "remote": "Sign in from another computer.",
    "advanced": "Every setting, with what it does.",
    "activity": "What the system did, and how it went.",
}


# -- application sources (Flatpak remotes) -------------------------------------------------------

def remote_subtitle(remote):
    parts = [remote.get("url", "")]
    parts.append("signed" if remote.get("signed", True) else "not signed")
    apps = remote.get("apps", 0)
    parts.append(f"{apps} app" + ("" if apps == 1 else "s") + " installed" if apps else "no apps installed")
    if remote.get("default"):
        parts.append("comes with the system")
    return " · ".join(p for p in parts if p)


def remote_form_problem(name, location, key):
    """Why the add-source form cannot be used yet, or ''."""
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$", name.strip()):
        return "Give the source a short name: letters, digits, dots, dashes."
    if not (location.startswith("https://") or location.startswith("file:///")) or " " in location:
        return "The address must start with https://."
    if not location.endswith(".flatpakrepo") and "BEGIN PGP PUBLIC KEY BLOCK" not in key:
        return "Paste the source's public signing key (an address that is not a .flatpakrepo file needs one)."
    return ""


def suggest_remote_name(location):
    stem = re.sub(r"^[a-z]+://", "", location.strip()).rstrip("/").rsplit("/", 1)[-1].split(".")[0]
    return re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-.")[:63]


def remote_trust_text(info):
    """The body of the confirmation shown before a source is trusted."""
    lines = []
    if info.get("title"):
        lines.append(info["title"])
    lines.append(info.get("url", ""))
    for fingerprint, owner in zip(info.get("fingerprints", []), info.get("owners", []) + [""] * 9):
        lines.append(f"{owner}\n{fingerprint}".strip())
    lines.append("Applications from it are installed for everyone and run with the permissions they ask for. "
                 "Check the fingerprint with the publisher.")
    return "\n\n".join(l for l in lines if l)


def identity_view(settings, session):
    """What the Ersa ID page shows. `settings` is identity.settings_from(...); `session` the stored session or None.
    state: off (switched off), unset (no provider named), out (not signed in), in (signed in)."""
    if not settings.get("enabled", True):
        return {"state": "off", "name": "", "email": "", "initials": "", "provider": ""}
    issuer = settings.get("issuer", "")
    provider = issuer.split("://", 1)[-1].rstrip("/")
    if not settings.get("configured"):
        return {"state": "unset", "name": "", "email": "", "initials": "", "provider": provider}
    if not session:
        return {"state": "out", "name": "", "email": "", "initials": "", "provider": provider}
    claims = session.get("claims", {})
    email = claims.get("email", "")
    known = claims.get("name") or claims.get("preferred_username") or email.split("@")[0]
    name = known or "Signed in"
    words = [w for w in known.replace(".", " ").replace("_", " ").split() if w]
    initials = "".join(w[0] for w in (words[:2] if len(words) > 1 else words[:1])).upper()
    return {"state": "in", "name": name, "email": email, "initials": initials, "provider": provider,
            "picture": claims.get("picture", "")}
