"""Evidence that first-boot setup is complete.

GNOME Initial Setup writes ~/.config/gnome-initial-setup-done for the account it created when it
finishes. That file is the evidence. To keep it from being forged by an arbitrary local user, only
the file of an *administrator* (a member of the wheel group, which is how Initial Setup creates the
first account) counts, and it must be a regular file owned by that user (not a symlink to someone
else's file). An administrator can do anything anyway, so forging it gains them nothing.

Users come from the passwd file, not NSS: accounts that only exist in a directory service are not
considered. A different front end (a custom first-boot app, an MDM agent) does not use this: it calls
CompleteProvisioning.
"""

import os
import stat

SETUP_DONE = ".config/gnome-initial-setup-done"
MIN_UID = 1000
NOBODY = 65534
ADMIN_GROUP = "wheel"


def parse_passwd(text):
    """[(name, uid, gid, home)] from passwd-format text; malformed lines are skipped."""
    users = []
    for line in text.splitlines():
        fields = line.split(":")
        if len(fields) < 6 or not fields[2].isdigit() or not fields[3].isdigit():
            continue
        users.append((fields[0], int(fields[2]), int(fields[3]), fields[5]))
    return users


def admins(group_text, users):
    """Names of the members of the administrator group, including those whose primary group it is."""
    names, gid = set(), None
    for line in group_text.splitlines():
        fields = line.split(":")
        if len(fields) >= 4 and fields[0] == ADMIN_GROUP:
            names.update(member for member in fields[3].split(",") if member)
            if fields[2].isdigit():
                gid = int(fields[2])
    if gid is not None:
        names.update(name for name, _uid, group, _home in users if group == gid)
    return names


def evidence(passwd_path, group_path):
    """The name of an administrator whose setup-done file exists, or None."""
    try:
        with open(passwd_path, encoding="utf-8") as handle:
            users = parse_passwd(handle.read())
        with open(group_path, encoding="utf-8") as handle:
            wheel = admins(handle.read(), users)
    except OSError:
        return None
    for name, uid, _gid, home in users:
        if name not in wheel or not MIN_UID <= uid < NOBODY or not os.path.isabs(home):
            continue
        try:
            info = os.lstat(os.path.join(home, SETUP_DONE))
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode) and info.st_uid == uid:
            return name
    return None
