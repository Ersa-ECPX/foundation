"""The client package: terminal output, rendering helpers and the command parser (no daemon needed)."""

import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from foundation import cli, ui  # noqa: E402


class Tty(io.StringIO):
    encoding = "utf-8"

    def isatty(self):
        return True


class Output(unittest.TestCase):
    def test_visible_len_ignores_colour_and_counts_wide(self):
        self.assertEqual(ui.visible_len("\x1b[31mred\x1b[0m"), 3)
        self.assertEqual(ui.visible_len("日本"), 4)

    def test_no_color_when_not_a_tty(self):
        self.assertFalse(ui.color_enabled(io.StringIO()))
        self.assertEqual(ui.style("x", "red", stream=io.StringIO()), "x")

    def test_colour_on_a_tty_and_no_color_override(self):
        with mock.patch.dict(os.environ, {"TERM": "xterm"}, clear=False):
            os.environ.pop("NO_COLOR", None)
            self.assertTrue(ui.color_enabled(Tty()))
            os.environ["NO_COLOR"] = "1"
            self.assertFalse(ui.color_enabled(Tty()))

    def test_confirm_without_terminal_is_no(self):
        with mock.patch("sys.stdin", io.StringIO("")), mock.patch("sys.stderr", io.StringIO()):
            self.assertFalse(ui.confirm("Go?"))
            self.assertFalse(ui.confirm_typed("Go?", "x"))
        self.assertTrue(ui.confirm("Go?", assume_yes=True))
        self.assertTrue(ui.confirm_typed("Go?", "x", assume_yes=True))

    def test_typed_confirmation_needs_the_exact_name(self):
        class Terminal(io.StringIO):
            def isatty(self):
                return True
        for answer, expected in (("empty2\n", True), ("EMPTY2\n", False), ("yes\n", False), ("\n", False)):
            with mock.patch("sys.stdin", Terminal(answer)), mock.patch("builtins.input", return_value=answer.strip()):
                self.assertEqual(ui.confirm_typed("Overwrite?", "empty2"), expected, answer)

    def test_human_sizes(self):
        self.assertEqual([ui.human_size(n) for n in (0, 1023, 1024, 1536, 4 * 1024 ** 3)],
                         ["0 B", "1023 B", "1 KiB", "1.5 KiB", "4 GiB"])

    def test_progress_bar_is_clamped(self):
        self.assertIn("100%", ui.progress_bar(2.0))
        self.assertIn("  0%", ui.progress_bar(-1.0))
        self.assertEqual(len(ui.progress_bar(0.5)), len(ui.progress_bar(0.9)))

    def test_table_truncates_long_cells(self):
        out = io.StringIO()
        with redirect_stdout(out), mock.patch.object(ui, "terminal_width", return_value=40):
            ui.table(["A", "B"], [["x" * 100, "y"]])
        self.assertTrue(all(ui.visible_len(line) <= 40 for line in out.getvalue().splitlines()))


class Rendering(unittest.TestCase):
    SLOTS = [
        {"name": "0.2.0", "label": "ecp-root_0.2.0", "device": "/dev/vda1", "size": 4 * 1024 ** 3, "state": "running",
         "running": True, "default": True, "entry": "ecp_0.2.0.efi", "tries_left": -1, "bootable": True},
        {"name": "empty2", "label": "_empty", "device": "/dev/vda2", "size": 4 * 1024 ** 3, "state": "empty",
         "running": False, "default": False, "entry": "", "tries_left": -1, "bootable": False},
        {"name": "0.1.0", "label": "ecp-root_0.1.0", "device": "/dev/vda3", "size": 4 * 1024 ** 3, "state": "standby",
         "running": False, "default": False, "entry": "ecp_0.1.0+2.efi", "tries_left": 2, "bootable": True},
    ]

    def test_slot_rows_say_what_each_slot_is(self):
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            rows = cli.slot_rows(self.SLOTS)
        by_name = {row[0]: row for row in rows}
        self.assertEqual(by_name["0.2.0"][3:5], ["blessed", "default"])
        self.assertEqual(by_name["0.1.0"][3], "2 left")
        self.assertEqual(by_name["empty2"][2:4], ["-", "-"])
        self.assertEqual(by_name["0.2.0"][6], "4 GiB")

    def test_find_slot_by_name_or_label(self):
        self.assertEqual(cli.find_slot(self.SLOTS, "0.1.0")["device"], "/dev/vda3")
        self.assertEqual(cli.find_slot(self.SLOTS, "ecp-root_0.1.0")["name"], "0.1.0")
        with self.assertRaises(cli.UserError):
            cli.find_slot(self.SLOTS, "nope")

    def test_typed_values_follow_the_schema_type(self):
        self.assertEqual(cli.typed_value("b", "Yes").unpack(), True)
        self.assertEqual(cli.typed_value("b", "off").unpack(), False)
        self.assertEqual(cli.typed_value("i", "7").unpack(), 7)
        self.assertEqual(cli.typed_value("s", "stable").unpack(), "stable")
        for signature, text in (("b", "perhaps"), ("i", "seven")):
            with self.assertRaises(cli.UserError):
                cli.typed_value(signature, text)

    def test_job_numbers(self):
        self.assertEqual(cli.job_path("12"), "/org/ersa/Foundation1/Job/12")
        for bad in ("x", "1/2", "-1", ""):
            with self.assertRaises(cli.UserError):
                cli.job_path(bad)


class Parser(unittest.TestCase):
    def parse(self, *argv):
        return cli.build_parser().parse_args(argv)

    def test_the_commands_exist(self):
        for argv in (["status"], ["slot", "status"], ["slot", "list"], ["slot", "boot", "a", "--once"],
                     ["slot", "copy", "--to", "b"], ["slot", "release", "a"], ["reboot", "--firmware"],
                     ["apps", "install", "org.x"], ["compat", "install", "f.rpm", "--force"], ["ext", "merge", "--kind", "confext"],
                     ["config", "set", "k", "v"], ["job", "cancel", "3"], ["base", "gtk"]):
            self.assertTrue(callable(self.parse(*argv).func), argv)

    def test_copy_needs_a_target(self):
        with self.assertRaises(SystemExit), mock.patch("sys.stderr", io.StringIO()):
            self.parse("slot", "copy")

    def test_destructive_commands_accept_yes_and_the_rest_do_not_need_it(self):
        self.assertTrue(self.parse("slot", "copy", "--to", "b", "--yes").yes)
        self.assertFalse(hasattr(self.parse("slot", "status"), "yes"))

    def test_user_facing_text_spells_out_the_product_name(self):
        help_text = cli.build_parser().format_help()
        self.assertIn("Ersa Compute Platform", help_text)
        self.assertNotIn("ECP", help_text)
        for text in (cli.HINTS.values()):
            self.assertNotIn("ECP", text)


if __name__ == "__main__":
    unittest.main()
