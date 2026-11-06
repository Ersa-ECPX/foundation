"""systemd-boot loader variables, read from efivarfs. Read-only (writing goes through bootctl)."""

import os

LOADER_GUID = "4a67b082-0a4c-41cf-b6c7-440b29bb8c4f"


def read_loader_variable(name, efivars):
    """A loader variable as text (4 attribute bytes, then UTF-16LE), or None if it is not set."""
    try:
        with open(os.path.join(efivars, f"{name}-{LOADER_GUID}"), "rb") as handle:
            raw = handle.read()
    except OSError:
        return None
    return raw[4:].decode("utf-16-le", errors="replace").rstrip("\x00") or None


def booted_with_efi(efivars):
    return os.path.isdir(efivars)
