"""Argument checks shared by the API handlers.

Every string that reaches a worker or a command line is checked here against a narrow pattern, and
none may start with '-' (so it can never be mistaken for an option by a tool the worker runs).
"""

import re

from ..errors import InvalidArgument

_APP_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,254}$")
_SLOT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+~^-]{0,63}$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+._-]{0,63}$")
EXTENSION_KINDS = ("sysext", "confext")


def app_id(value):
    if not isinstance(value, str) or not _APP_ID.match(value):
        raise InvalidArgument(f"not a valid application id: {value!r}")
    return value


def slot(value):
    """A slot name: the part of the partition label after `ecp-root_`, or the full label."""
    if not isinstance(value, str) or not _SLOT.match(value):
        raise InvalidArgument(f"not a valid slot name: {value!r}")
    return value


def optional_slot(value):
    return slot(value) if value else ""


def name(value):
    if not isinstance(value, str) or not _NAME.match(value):
        raise InvalidArgument(f"not a valid name: {value!r}")
    return value


def kind(value):
    if value not in EXTENSION_KINDS:
        raise InvalidArgument(f"kind must be one of: {', '.join(EXTENSION_KINDS)}")
    return value


def search_term(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 200 or "\0" in value:
        raise InvalidArgument("the search term must be 1 to 200 characters")
    if value.startswith("-"):
        raise InvalidArgument("the search term must not start with '-'")
    return value.strip()


def device_path(value):
    """An absolute path with no traversal, as given for a disk. Whether it is a usable disk is checked
    by the worker against the real system."""
    if not isinstance(value, str) or not value.startswith("/") or len(value) > 4096 or "\0" in value \
            or ".." in value.split("/"):
        raise InvalidArgument(f"not a valid disk path: {value!r}")
    return value
