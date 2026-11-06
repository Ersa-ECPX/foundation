"""Authorization of state-changing calls.

An authorizer answers asynchronously: a polkit check can wait for a person to type a password, and
the daemon's main loop must keep serving other callers meanwhile. `authorize` therefore takes a
callback instead of returning a value. It is always called exactly once, with (allowed, message).

Both implementations fail closed: if polkit cannot be reached, the call is denied.
"""

import logging

log = logging.getLogger("foundationd.auth")

POLKIT_NAME = "org.freedesktop.PolicyKit1"
POLKIT_PATH = "/org/freedesktop/PolicyKit1/Authority"
POLKIT_IFACE = "org.freedesktop.PolicyKit1.Authority"
ALLOW_USER_INTERACTION = 1


class PolkitAuthorizer:
    """Ask polkit, identifying the caller by its unique bus name (not by pid, which can be reused)."""

    def __init__(self, connection):
        from gi.repository import Gio

        self._Gio = Gio
        self._connection = connection

    def authorize(self, sender, action_id, callback):
        from gi.repository import GLib

        subject = ("system-bus-name", {"name": GLib.Variant("s", sender)})
        parameters = GLib.Variant("((sa{sv})sa{ss}us)", (subject, action_id, {}, ALLOW_USER_INTERACTION, ""))

        def done(connection, result):
            try:
                reply = connection.call_finish(result)
                # The method has one out argument, a struct, so the body is a one-element tuple.
                ((is_authorized, _challenge, _details),) = reply.unpack()
            except Exception as error:  # noqa: BLE001 - any failure to ask means "no"
                log.warning("polkit check for %s failed: %s", action_id, error)
                callback(False, "authorization service unavailable")
                return
            callback(bool(is_authorized), "" if is_authorized else "not authorized")

        # A generous timeout: authentication may involve a person typing a password.
        self._connection.call(
            POLKIT_NAME, POLKIT_PATH, POLKIT_IFACE, "CheckAuthorization", parameters,
            GLib.VariantType("((bba{ss}))"), self._Gio.DBusCallFlags.ALLOW_INTERACTIVE_AUTHORIZATION,
            600_000, None, done)


class StubAuthorizer:
    """For tests and development only: allow or deny every request."""

    def __init__(self, allow=True):
        self.allow = allow
        self.requests = []

    def authorize(self, sender, action_id, callback):
        self.requests.append((sender, action_id))
        callback(self.allow, "" if self.allow else "not authorized")
