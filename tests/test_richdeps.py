"""RPM rich (boolean) dependencies, including the exact lines from Google Chrome's package header."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from foundationd.backends import richdeps  # noqa: E402

CHROME_CURL = ("(libcurl.so()(64bit) or libcurl-gnutls.so.4()(64bit) or libcurl-nss.so.4()(64bit) "
               "or libcurl.so.4()(64bit))")
CHROME_GTK = "(libgtk-3.so.0()(64bit) or libgtk-4.so.1()(64bit))"


def provides(*names):
    have = set(names)
    return lambda name: name in have


class Tokens(unittest.TestCase):
    def test_parentheses_inside_names_stay_in_the_name(self):
        self.assertEqual(richdeps.tokenize("(libc.so.6()(64bit) or bash)"),
                         ["(", "libc.so.6()(64bit)", "or", "bash", ")"])

    def test_is_rich(self):
        self.assertTrue(richdeps.is_rich(CHROME_CURL))
        self.assertFalse(richdeps.is_rich("libc.so.6()(64bit)"))
        self.assertFalse(richdeps.is_rich("bash >= 5"))


class Chrome(unittest.TestCase):
    def test_any_one_libcurl_flavour_is_enough(self):
        self.assertTrue(richdeps.satisfied(CHROME_CURL, provides("libcurl.so.4()(64bit)")))
        self.assertTrue(richdeps.satisfied(CHROME_CURL, provides("libcurl-gnutls.so.4()(64bit)")))
        self.assertFalse(richdeps.satisfied(CHROME_CURL, provides("libfoo.so.1()(64bit)")))

    def test_gtk3_or_gtk4(self):
        self.assertTrue(richdeps.satisfied(CHROME_GTK, provides("libgtk-3.so.0()(64bit)")))
        self.assertTrue(richdeps.satisfied(CHROME_GTK, provides("libgtk-4.so.1()(64bit)")))
        self.assertFalse(richdeps.satisfied(CHROME_GTK, provides()))


class Operators(unittest.TestCase):
    def check(self, text, have, expected):
        self.assertEqual(richdeps.satisfied(text, provides(*have)), expected, text)

    def test_and_with_without(self):
        self.check("(a and b)", ["a", "b"], True)
        self.check("(a and b)", ["a"], False)
        self.check("(a with b)", ["a", "b"], True)
        self.check("(a without b)", ["a"], True)
        self.check("(a without b)", ["a", "b"], False)

    def test_if_and_unless(self):
        self.check("(a if b)", [], True)               # b absent: a is not needed
        self.check("(a if b)", ["b"], False)
        self.check("(a if b)", ["a", "b"], True)
        self.check("(a unless b)", ["b"], True)
        self.check("(a unless b)", [], False)
        self.check("(a if b else c)", ["b", "a"], True)
        self.check("(a if b else c)", ["c"], True)
        self.check("(a if b else c)", ["b"], False)

    def test_nesting_and_versions(self):
        self.check("((a or b) and c)", ["b", "c"], True)
        self.check("((a or b) and c)", ["b"], False)
        self.check("(foo >= 1.0 or bar)", ["foo"], True)

    def test_what_cannot_be_understood_is_none_not_true(self):
        for text in ("(a and", "(a or b and c)", "(a xor b)", "()", "(a b)", "(a or b) extra"):
            self.assertIsNone(richdeps.satisfied(text, provides("a", "b", "c")), text)


if __name__ == "__main__":
    unittest.main()
