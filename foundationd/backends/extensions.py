"""systemd-sysext and systemd-confext images.

Listing reads the search directories directly. Merging, unmerging and refreshing go through the
systemd tools, which are the only interface for them; `status` is read as JSON to learn what is
currently merged.
"""

import json
import os

from ..errors import Failed
from .runner import check, run

SEARCH = {
    "sysext": ("etc/extensions", "run/extensions", "var/lib/extensions", "usr/lib/extensions",
               "usr/local/lib/extensions"),
    "confext": ("etc/confexts", "run/confexts", "var/lib/confexts", "usr/lib/confexts",
                "usr/local/lib/confexts"),
}
TOOL = {"sysext": "systemd-sysext", "confext": "systemd-confext"}


def merged_names(kind, runner=run):
    """Names of the extensions that are currently merged, from `<tool> status --json=short`."""
    out = check([TOOL[kind], "status", "--json=short"], 30, runner=runner)
    try:
        data = json.loads(out)
    except ValueError as error:
        raise Failed(f"cannot read the output of {TOOL[kind]} status") from error
    names = set()
    for hierarchy in data if isinstance(data, list) else []:
        listed = hierarchy.get("extensions")
        if isinstance(listed, list):
            names.update(item if isinstance(item, str) else str(item.get("name", "")) for item in listed)
    names.discard("")
    return names


def discover(kind, root, managed_dir=None):
    """Every extension found in the search directories, first match winning like systemd does."""
    found, seen = [], set()
    for relative in SEARCH[kind]:
        directory = os.path.join(root, relative)
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            continue
        for filename in names:
            path = os.path.join(directory, filename)
            if filename.startswith("."):
                continue
            if os.path.islink(path):
                kind_of = "link"
            elif os.path.isdir(path):
                kind_of = "directory"
            elif filename.endswith(".raw"):
                kind_of = "image"
            else:
                continue
            name = filename[:-4] if kind_of == "image" else filename
            if name in seen:
                continue
            seen.add(name)
            found.append({"name": name, "kind": kind, "path": path, "type": kind_of,
                          "managed": bool(managed_dir) and kind == "sysext" and relative == "var/lib/extensions"
                          and os.path.exists(os.path.join(managed_dir, name + ".json"))})
    return found


def list_extensions(kind, root, managed_dir, runner=run):
    merged = merged_names(kind, runner)
    result = discover(kind, root, managed_dir)
    for item in result:
        item["merged"] = item["name"] in merged
    return result


def operate(kind, operation, runner=run):
    """merge, unmerge or refresh. Returns what is merged afterwards."""
    check([TOOL[kind], operation], 120, runner=runner)
    return {"kind": kind, "merged": sorted(merged_names(kind, runner))}
