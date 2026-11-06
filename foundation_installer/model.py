"""Text and small decisions for the installer, with no GTK in it so they can be tested anywhere."""

from foundation.ui import human_size  # noqa: F401  (re-exported for the windows)

PRODUCT = "Ersa Compute Platform"

PARTITION_TITLES = {
    "ECP-ESP": "Boot",
    "ECP-RECOVERY": "Recovery",
    "_empty": "System slot B",
    "ECP-DATA": "Your files and settings",
}


def partition_title(label):
    if label in PARTITION_TITLES:
        return PARTITION_TITLES[label]
    if label.startswith("ecp-root_"):
        return "System slot A"
    return label


def disk_title(disk):
    return disk.get("model") or ("Removable disk" if disk.get("removable") else "Disk")


def disk_subtitle(disk):
    parts = [disk["path"], human_size(disk["size"])]
    if disk.get("removable"):
        parts.append("removable")
    if not disk.get("usable", True):
        parts.append(disk.get("reason") or "cannot be used")
    return " · ".join(parts)


def partition_size(partition):
    return human_size(partition["size"]) if partition["size"] else "The rest of the disk"


def erase_summary(plan):
    count = len(plan.get("will_erase", []))
    what = f"{plan['model']} ({plan['disk']}, {human_size(plan['size'])})" if plan.get("model") \
        else f"{plan['disk']} ({human_size(plan['size'])})"
    note = f" It currently holds {count} partition{'s' if count != 1 else ''}." if count else ""
    return f"Everything on {what} will be erased and cannot be recovered.{note}"


def failure_text(error):
    """One sentence for a failed call or job, from a ClientError or a JobResult."""
    name = getattr(error, "name", None) or getattr(error, "error_name", "")
    message = getattr(error, "message", None) or getattr(error, "error_message", "") or str(error)
    if name == "NotAuthorized":
        return "Permission was not granted to change the disk."
    if name == "Unreachable":
        return message
    if name == "Busy":
        return "Another installation is already running."
    return message or "The installation failed for an unknown reason."


def remaining_text(elapsed, fraction):
    """'About 3 minutes remaining', or '' until there is enough progress to estimate from."""
    if fraction < 0.05 or elapsed < 5 or fraction >= 1:
        return ""
    left = elapsed * (1 - fraction) / fraction
    if left < 60:
        return "Less than a minute remaining"
    minutes = round(left / 60)
    return f"About {minutes} minute{'s' if minutes != 1 else ''} remaining"
