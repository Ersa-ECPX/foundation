"""Every D-Bus call the command line makes uses the argument signature the contract declares."""

import os
import re
import sys
import unittest
import xml.etree.ElementTree as ET

HERE = os.path.dirname(__file__)
CLI = os.path.join(HERE, "..", "foundation", "cli.py")
XML = os.path.join(HERE, "..", "data", "dbus", "org.ersa.Foundation1.xml")


def contract():
    methods = {}
    for interface in ET.parse(XML).getroot().iter("interface"):
        short = interface.get("name").rsplit(".", 1)[1]
        for method in interface.iter("method"):
            args = "".join(a.get("type") for a in method.findall("arg") if a.get("direction", "in") == "in")
            methods[(short, method.get("name"))] = args
    return methods


class Signatures(unittest.TestCase):
    def test_calls_match_the_contract(self):
        with open(CLI, encoding="utf-8") as handle:
            source = handle.read()
        declared = contract()
        found = re.findall(r'"([A-Z][A-Za-z]+)", "([A-Z][A-Za-z]+)", (?:"(\([a-z0-9]*\))"|None)', source)
        self.assertGreater(len(found), 20, "the pattern no longer finds the calls")
        wrong = []
        for interface, method, signature in found:
            if (interface, method) not in declared:
                continue
            given = signature[1:-1] if signature else ""
            if given != declared[(interface, method)]:
                wrong.append(f"{interface}.{method}: the command line sends {given!r}, the contract says {declared[(interface, method)]!r}")
        self.assertEqual(wrong, [])


if __name__ == "__main__":
    unittest.main()
