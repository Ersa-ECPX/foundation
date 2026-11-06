"""Schema, layered config and state: plain files, no D-Bus."""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from foundationd.config import API_FILE, ConfigStore  # noqa: E402
from foundationd.errors import InvalidArgument, ReadOnly, UnknownKey  # noqa: E402
from foundationd.schema import Schema  # noqa: E402
from foundationd.state import StateStore  # noqa: E402

SCHEMA = {
    "a.flag": {"type": "boolean", "default": False},
    "a.name": {"type": "string", "default": "one", "choices": ["one", "two"]},
    "a.count": {"type": "integer", "default": 3},
    "a.fixed": {"type": "string", "default": "x", "writable": False},
}


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.schema_dir = os.path.join(self.tmp.name, "schema.d")
        self.defaults = os.path.join(self.tmp.name, "defaults.d")
        self.conf = os.path.join(self.tmp.name, "conf.d")
        write(os.path.join(self.schema_dir, "10-test.json"), json.dumps(SCHEMA))
        self.schema = Schema.load(self.schema_dir)
        self.store = ConfigStore(self.schema, self.defaults, self.conf)


class SchemaTests(Base):
    def test_loads_keys_in_sorted_order(self):
        self.assertEqual([k.name for k in self.schema], ["a.count", "a.fixed", "a.flag", "a.name"])

    def test_later_schema_file_overrides_default(self):
        write(os.path.join(self.schema_dir, "20-variant.json"),
              json.dumps({"a.count": {"type": "integer", "default": 9}}))
        self.assertEqual(Schema.load(self.schema_dir).get("a.count").default, 9)

    def test_bad_default_in_schema_is_an_error(self):
        write(os.path.join(self.schema_dir, "30-bad.json"),
              json.dumps({"b.x": {"type": "string", "default": "nope", "choices": ["yes"]}}))
        with self.assertRaises(InvalidArgument):
            Schema.load(self.schema_dir)

    def test_unknown_type_in_schema_is_an_error(self):
        write(os.path.join(self.schema_dir, "30-bad.json"),
              json.dumps({"b.x": {"type": "float", "default": 1}}))
        with self.assertRaises(ValueError):
            Schema.load(self.schema_dir)

    def test_integer_key_rejects_boolean(self):
        with self.assertRaises(InvalidArgument):
            self.schema.get("a.count").validate(True)

    def test_string_must_be_single_line(self):
        # A newline in a value would let a caller inject a second key into the config file.
        with self.assertRaises(InvalidArgument):
            self.schema.get("a.name").validate("one\na.flag=true")

    def test_unknown_key(self):
        with self.assertRaises(UnknownKey):
            self.schema.get("nope")


class LayeringTests(Base):
    def test_defaults_come_from_schema(self):
        self.assertEqual(self.store.get("a.count"), 3)
        self.assertIs(self.store.get("a.flag"), False)

    def test_defaults_dir_overrides_schema_and_conf_overrides_defaults(self):
        write(os.path.join(self.defaults, "10-image.conf"), "a.count=5\na.flag=yes\n")
        write(os.path.join(self.conf, "50-admin.conf"), "a.count=7\n")
        self.assertEqual(self.store.get("a.count"), 7)
        self.assertIs(self.store.get("a.flag"), True)

    def test_lexical_order_within_a_directory(self):
        write(os.path.join(self.conf, "10-a.conf"), "a.count=1\n")
        write(os.path.join(self.conf, "20-b.conf"), "a.count=2\n")
        self.assertEqual(self.store.get("a.count"), 2)

    def test_comments_and_blank_lines_ignored(self):
        write(os.path.join(self.conf, "50.conf"), "# a.count=9\n\n; a.count=8\n  a.count = 4  \n")
        self.assertEqual(self.store.get("a.count"), 4)

    def test_bad_admin_line_is_skipped_not_fatal(self):
        write(os.path.join(self.conf, "50.conf"), "a.count=lots\na.nope=1\na.flag=true\n")
        self.assertEqual(self.store.get("a.count"), 3)
        self.assertIs(self.store.get("a.flag"), True)
        self.assertEqual(len(self.store.warnings), 2)

    def test_admin_file_sorting_after_api_file_wins(self):
        self.store.set("a.count", 10)
        write(os.path.join(self.conf, "95-admin.conf"), "a.count=11\n")
        self.assertEqual(self.store.get("a.count"), 11)


class WriteTests(Base):
    def test_set_persists_and_reports_change(self):
        self.assertTrue(self.store.set("a.count", 8))
        self.assertEqual(self.store.get("a.count"), 8)
        self.assertEqual(os.stat(os.path.join(self.conf, API_FILE)).st_mode & 0o777, 0o644)

    def test_set_same_value_reports_no_change(self):
        self.assertFalse(self.store.set("a.count", 3))

    def test_booleans_round_trip(self):
        self.store.set("a.flag", True)
        self.assertIs(self.store.get("a.flag"), True)
        self.store.set("a.flag", False)
        self.assertIs(self.store.get("a.flag"), False)

    def test_reset_restores_default_and_removes_empty_file(self):
        self.store.set("a.count", 8)
        self.assertTrue(self.store.reset("a.count"))
        self.assertEqual(self.store.get("a.count"), 3)
        self.assertFalse(os.path.exists(os.path.join(self.conf, API_FILE)))

    def test_reset_of_unset_key_is_a_no_op(self):
        self.assertFalse(self.store.reset("a.count"))

    def test_set_keeps_other_api_entries(self):
        self.store.set("a.count", 8)
        self.store.set("a.name", "two")
        self.store.reset("a.count")
        self.assertEqual(self.store.get("a.name"), "two")

    def test_set_does_not_touch_admin_files(self):
        admin = os.path.join(self.conf, "50-admin.conf")
        write(admin, "a.count=7\n")
        self.store.set("a.name", "two")
        with open(admin, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "a.count=7\n")

    def test_rejections(self):
        with self.assertRaises(UnknownKey):
            self.store.set("nope", 1)
        with self.assertRaises(ReadOnly):
            self.store.set("a.fixed", "y")
        with self.assertRaises(InvalidArgument):
            self.store.set("a.name", "three")
        with self.assertRaises(InvalidArgument):
            self.store.set("a.count", "many")
        self.assertFalse(os.path.exists(os.path.join(self.conf, API_FILE)))

    def test_no_temporary_files_left_behind(self):
        self.store.set("a.count", 8)
        self.assertEqual(os.listdir(self.conf), [API_FILE])

    def test_malformed_api_file_does_not_stop_set(self):
        write(os.path.join(self.conf, API_FILE), "garbage\na.count=oops\n")
        self.store.set("a.name", "two")
        self.assertEqual(self.store.get("a.name"), "two")


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = StateStore(self.tmp.name)

    def test_missing_file_means_not_provisioned(self):
        self.assertFalse(self.state.provisioned)

    def test_reads_provisioned(self):
        write(self.state.path, json.dumps({"system.provisioned": True}))
        self.assertTrue(self.state.provisioned)

    def test_corrupt_file_is_not_mistaken_for_provisioned(self):
        write(self.state.path, "{not json")
        self.assertFalse(self.state.provisioned)

    def test_wrong_type_is_ignored(self):
        write(self.state.path, json.dumps({"system.provisioned": "yes"}))
        self.assertFalse(self.state.provisioned)


if __name__ == "__main__":
    unittest.main()
