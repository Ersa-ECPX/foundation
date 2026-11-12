"""The boot splash theme: light (the `ersa` Plymouth theme, the default) or dark (`ersa-dark`).

The choice is not kept in the kernel image. A signed image cannot be edited (that would break its signature, and Secure Boot
needs the signature), and editing the command line of every slot's image in place was fragile anyway. It lives in two places
that nothing signed depends on:

- a **UEFI variable**, `ErsaSplashDark-<GUID>`: present means dark, absent means light. The initrd looks at it before Plymouth
  starts (`ersa-splash-dark.service`, which only runs when the variable exists) and switches Plymouth's configuration to the dark
  theme, so the boot splash follows the choice. It is a few bytes of non-volatile memory, like systemd's own boot-entry variables.
- **`/etc/plymouth/plymouthd.conf`** of the installed system, which the splash shown at shutdown and restart reads (by then the
  initrd is gone). foundation edits only its `Theme=` line.

Nothing happens until the next boot: the splash that is showing is already chosen. A computer that did not start with UEFI has no
variables; the file is still written and the report says the boot splash cannot follow the choice there.
"""

import fcntl
import os
import struct

from ..errors import Failed, InvalidArgument

# A fixed vendor GUID for the variable. It only has to be unique to us; it is not a secret.
GUID = "7c1d3f52-8a4e-4b6a-9e0d-3b5f2a7c9e14"
VARIABLE = f"ErsaSplashDark-{GUID}"
ATTRIBUTES = 0x00000007                  # non-volatile, boot services access, runtime access
THEMES = {"light": "ersa", "dark": "ersa-dark"}
CONFIG = "etc/plymouth/plymouthd.conf"

FS_IOC_GETFLAGS, FS_IOC_SETFLAGS, FS_IMMUTABLE_FL = 0x80086601, 0x40086602, 0x10


def check(theme):
    if theme not in THEMES:
        raise InvalidArgument(f"unknown splash theme {theme!r}; use light or dark")
    return theme


def apply_theme(text, theme):
    """`plymouthd.conf` text with its Theme= line set for `theme` (added under [Daemon] when missing); other lines are kept."""
    wanted = f"Theme={THEMES[check(theme)]}"
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.strip().startswith("Theme="):
            if line.strip() == wanted:
                return text if text.endswith("\n") else text + "\n"
            lines[index] = wanted
            return "\n".join(lines) + "\n"
    for index, line in enumerate(lines):
        if line.strip() == "[Daemon]":
            lines.insert(index + 1, wanted)
            return "\n".join(lines) + "\n"
    return "\n".join(["[Daemon]", wanted] + lines) + "\n"


def theme_of(text):
    """"dark" when the configuration selects the dark theme, else "light"."""
    for line in text.splitlines():
        if line.strip() == f"Theme={THEMES['dark']}":
            return "dark"
    return "light"


# -- the UEFI variable -------------------------------------------------------------------------

def _variable(paths):
    return os.path.join(paths["efivars"], VARIABLE)


def _allow_changes(path):
    """efivarfs marks every variable immutable so that it is not deleted by accident; clear that on ours."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        flags = struct.unpack("i", fcntl.ioctl(fd, FS_IOC_GETFLAGS, bytes(8))[:4])[0]
        if flags & FS_IMMUTABLE_FL:
            fcntl.ioctl(fd, FS_IOC_SETFLAGS, struct.pack("i", flags & ~FS_IMMUTABLE_FL))
    except OSError:
        pass                              # not efivarfs (a test directory, say), or not allowed: the write says so
    finally:
        os.close(fd)


def has_variables(paths):
    return os.path.isdir(paths["efivars"])


def dark_variable(paths):
    return os.path.exists(_variable(paths))


def _set_variable(paths, dark):
    """Create or remove the variable. Returns True when something changed."""
    path = _variable(paths)
    exists = os.path.exists(path)
    if dark == exists:
        return False
    if dark:
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o644)
            try:
                os.write(fd, struct.pack("<I", ATTRIBUTES) + b"\x01")           # attributes and value in one write
            finally:
                os.close(fd)
        except OSError as error:
            raise Failed(f"cannot write the UEFI variable {VARIABLE}: {error.strerror}") from error
    else:
        _allow_changes(path)
        try:
            os.unlink(path)
        except OSError as error:
            raise Failed(f"cannot remove the UEFI variable {VARIABLE}: {error.strerror}") from error
    return True


# -- the configuration file ---------------------------------------------------------------------

def _write_config(paths, theme):
    """Set the theme in the installed system's plymouthd.conf. Returns True when the file changed."""
    path = os.path.join(paths["root"], CONFIG)
    try:
        with open(path, encoding="utf-8") as handle:
            old = handle.read()
    except OSError:
        old = ""
    new = apply_theme(old, theme)
    if new == old:
        return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + ".new"
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            handle.write(new)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as error:
        raise Failed(f"cannot write /{CONFIG}: {error.strerror}") from error
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return True


# -- the choice ---------------------------------------------------------------------------------

def set_theme(paths, theme, ctx):
    """Set the boot splash theme. Returns {"theme", "changed": [what was changed], "skipped": [{file, reason}]}."""
    check(theme)
    ctx.step("splash", f"Setting the {theme} boot splash")
    changed, skipped = [], []
    if has_variables(paths):
        if _set_variable(paths, theme == "dark"):
            changed.append(f"UEFI variable {VARIABLE}")
    else:
        skipped.append({"file": f"UEFI variable {VARIABLE}",
                        "reason": "this computer did not start with UEFI, so the boot splash cannot follow the choice"})
    ctx.progress(0.7)
    if _write_config(paths, theme):
        changed.append(f"/{CONFIG}")
    ctx.progress(1.0)
    return {"theme": theme, "changed": changed, "skipped": skipped}


def current(paths):
    """The theme that was chosen: {"theme": "light"|"dark", "uefi": bool} (the UEFI variable decides the boot splash)."""
    return {"theme": "dark" if dark_variable(paths) else "light", "uefi": has_variables(paths)}
