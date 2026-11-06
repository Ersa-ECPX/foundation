"""Facts about the running system. Read-only. No command lines: files and D-Bus only."""

import os
import re
import shlex

ROOT_LABEL = re.compile(r"^ecp-root_(?P<version>.+)$")
SERVICES = ["NetworkManager.service", "bluetooth.service", "firewalld.service", "gdm.service"]


def parse_os_release(text):
    data = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        try:
            parts = shlex.split(value)
        except ValueError:
            parts = [value.strip("\"'")]
        data[key] = parts[0] if parts else ""
    return data


def os_release(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return parse_os_release(handle.read())
    except OSError:
        return {}


def _unescape(field):
    """mountinfo escapes space, tab, newline and backslash as octal."""
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), field)


def mounts(mountinfo_path):
    """Mounted filesystems from /proc/self/mountinfo (the last mount on a target wins)."""
    result = {}
    try:
        with open(mountinfo_path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return result
    for line in lines:
        left, separator, right = line.partition(" - ")
        fields, tail = left.split(), right.split()
        if not separator or len(fields) < 6 or len(tail) < 3:
            continue
        options = fields[5].split(",")
        result[_unescape(fields[4])] = {
            "target": _unescape(fields[4]), "source": _unescape(tail[1]), "fstype": tail[0],
            "readonly": "ro" in options and "rw" not in options,
        }
    return result


def partition_name(device, sysfs):
    """The GPT partition name of a block device, from sysfs (the kernel reports it as PARTNAME)."""
    real = os.path.realpath(device)
    try:
        with open(os.path.join(sysfs, "class/block", os.path.basename(real), "uevent"), encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("PARTNAME="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def running_slot(paths):
    """The running root slot (device, label, version) or None if / is not a partition we know."""
    root = mounts(paths["mountinfo"]).get("/")
    if not root:
        return None
    label = partition_name(root["source"], paths["sysfs"])
    match = ROOT_LABEL.match(label)
    return {
        "device": os.path.realpath(root["source"]), "fstype": root["fstype"], "readonly": root["readonly"],
        "label": label, "version": match.group("version") if match else None,
    }


def unit_states(units, bus):
    """ActiveState of each unit from systemd's D-Bus API; 'unknown' if systemd cannot be asked."""
    from gi.repository import Gio, GLib

    try:
        connection = Gio.bus_get_sync(Gio.BusType.SYSTEM if bus == "system" else Gio.BusType.SESSION, None)
        reply = connection.call_sync(
            "org.freedesktop.systemd1", "/org/freedesktop/systemd1", "org.freedesktop.systemd1.Manager",
            "ListUnitsByNames", GLib.Variant("(as)", (units,)), GLib.VariantType("(a(ssssssouso))"),
            Gio.DBusCallFlags.NONE, 5000, None)
    except GLib.Error:
        return {unit: "unknown" for unit in units}
    (rows,) = reply.unpack()
    states = {row[0]: row[3] for row in rows}
    return {unit: states.get(unit, "unknown") for unit in units}
