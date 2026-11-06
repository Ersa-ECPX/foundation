"""Errors that map onto D-Bus error names under org.ersa.Foundation1.Error."""

ERROR_NAMESPACE = "org.ersa.Foundation1.Error"


class FoundationError(Exception):
    """Base class. `name` is the D-Bus error suffix returned to the client."""

    name = "Failed"

    def dbus_name(self):
        return f"{ERROR_NAMESPACE}.{self.name}"


class NotAuthorized(FoundationError):
    name = "NotAuthorized"


class InvalidArgument(FoundationError):
    name = "InvalidArgument"


class UnknownKey(FoundationError):
    name = "UnknownKey"


class ReadOnly(FoundationError):
    name = "ReadOnly"


class Busy(FoundationError):
    """A conflicting job holds a lock the request needs."""

    name = "Busy"


class NotFound(FoundationError):
    name = "NotFound"


class NotCancellable(FoundationError):
    name = "NotCancellable"


class Unavailable(FoundationError):
    """A tool or service the request depends on is not present or not usable on this system."""

    name = "Unavailable"


class Cancelled(BaseException):
    """Raised inside a worker when the broker asks it to stop. Not an Exception subclass, so a
    broad `except Exception` in a backend cannot swallow a cancellation."""


def by_name(name):
    """The FoundationError subclass for a D-Bus error suffix (falls back to FoundationError)."""
    for cls in FoundationError.__subclasses__():
        if cls.name == name:
            return cls
    return FoundationError


class Failed(FoundationError):
    name = "Failed"


class AlreadyProvisioned(FoundationError):
    """A setup-only call after first-boot setup has finished."""

    name = "AlreadyProvisioned"
