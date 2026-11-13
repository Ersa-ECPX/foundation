"""PolkitAuthorizer: root is authorized without asking polkit, everyone else is asked (with fake bus connections)."""

import os
import sys
import unittest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))

try:
    import gi
    gi.require_version("Gio", "2.0")
    from gi.repository import GLib
except (ImportError, ValueError):
    gi = None

if gi:
    from foundationd.auth import PolkitAuthorizer


class FakeConnection:
    """Answers GetConnectionUnixUser with `uid` and CheckAuthorization with `authorized`; remembers what was called."""

    def __init__(self, uid, authorized=True, uid_fails=False):
        self.uid, self.authorized, self.uid_fails = uid, authorized, uid_fails
        self.methods = []

    def call(self, name, path, interface, method, parameters, reply_type, flags, timeout, cancellable, callback):
        self.methods.append(method)
        self._answer = method
        callback(self, method)

    def call_finish(self, result):
        if result == "GetConnectionUnixUser":
            if self.uid_fails:
                raise GLib.Error("no such connection")
            return GLib.Variant("(u)", (self.uid,))
        return GLib.Variant("((bba{ss}))", ((self.authorized, False, {}),))


@unittest.skipUnless(gi, "needs PyGObject")
class RootIsAuthorized(unittest.TestCase):
    def ask(self, connection):
        answers = []
        PolkitAuthorizer(connection).authorize(":1.7", "org.ersa.foundation.updates", lambda allowed, why: answers.append((allowed, why)))
        self.assertEqual(len(answers), 1, "the callback must run exactly once")
        return answers[0]

    def test_root_is_allowed_and_polkit_is_not_asked(self):
        connection = FakeConnection(uid=0, authorized=False)
        self.assertEqual(self.ask(connection), (True, ""))
        self.assertEqual(connection.methods, ["GetConnectionUnixUser"])

    def test_anyone_else_is_asked_and_polkit_decides(self):
        allowed = FakeConnection(uid=1000, authorized=True)
        self.assertEqual(self.ask(allowed), (True, ""))
        self.assertEqual(allowed.methods, ["GetConnectionUnixUser", "CheckAuthorization"])
        self.assertEqual(self.ask(FakeConnection(uid=1000, authorized=False)), (False, "not authorized"))

    def test_an_unknown_caller_is_asked_not_trusted(self):
        connection = FakeConnection(uid=0, authorized=False, uid_fails=True)
        self.assertEqual(self.ask(connection), (False, "not authorized"))
        self.assertEqual(connection.methods, ["GetConnectionUnixUser", "CheckAuthorization"])


if __name__ == "__main__":
    unittest.main()
