"""Generating what an extension needs after it is merged, from data files in the image."""

import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))

from foundationd.backends import compat, postmerge  # noqa: E402


class Case(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.paths = {"root": self.root, "state": os.path.join(self.root, "var/lib/foundation"), "os_release": ""}
        self.calls = []

    def runner(self, cmd, timeout=60, input_bytes=None):
        self.calls.append(cmd)
        # a stand-in for glib-compile-schemas: produce the cache from the collected files
        if cmd[0] == "compile":
            target = cmd[1]
            with open(os.path.join(target, "gschemas.compiled"), "w") as handle:
                handle.write(",".join(sorted(n for n in os.listdir(target) if n != "gschemas.compiled")))
            return 0, "", ""
        return 1, "", "no such tool"

    def job(self, text=None):
        directory = os.path.join(self.root, postmerge.JOB_DIR)
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, "10-test.json"), "w") as handle:
            handle.write(text or json.dumps({"name": "schemas", "collect": ["usr/share/schemas/*.xml"],
                                             "run": ["compile", "{out}"]}))

    def extension(self, name, files, enabled=True):
        compat._write_json(compat._meta_path(self.paths, name), {"name": name})
        if enabled:
            for relative in files:
                path = os.path.join(compat._enabled_dir(self.paths, name), relative)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w") as handle:
                    handle.write(name)

    def result(self):
        path = os.path.join(postmerge.output_dir(self.paths, "schemas"), "gschemas.compiled")
        return open(path).read() if os.path.exists(path) else None


class Jobs(Case):
    def test_files_of_every_enabled_extension_are_gathered_and_processed(self):
        self.job()
        self.extension("a", ["usr/share/schemas/a.xml", "usr/share/other/ignored.xml"])
        self.extension("b", ["usr/share/schemas/b.xml"])
        self.extension("off", ["usr/share/schemas/off.xml"], enabled=False)
        self.assertEqual(postmerge.apply(self.paths, self.runner), {"schemas": 2})
        self.assertEqual(self.result(), "a.xml,b.xml")

    def test_the_output_is_replaced_and_then_removed_when_nothing_is_left(self):
        self.job()
        self.extension("a", ["usr/share/schemas/a.xml"])
        postmerge.apply(self.paths, self.runner)
        shutil.rmtree(compat._enabled_dir(self.paths, "a"))
        self.assertEqual(postmerge.apply(self.paths, self.runner), {"schemas": 0})
        self.assertIsNone(self.result())
        self.assertFalse(os.path.exists(postmerge.output_dir(self.paths, "schemas")))

    def test_a_failing_tool_leaves_no_half_made_output_and_no_staging_directory(self):
        self.job(json.dumps({"name": "schemas", "collect": ["usr/share/schemas/*.xml"], "run": ["missing", "{out}"]}))
        self.extension("a", ["usr/share/schemas/a.xml"])
        with self.assertLogs("foundationd.backends.postmerge", "WARNING"):
            self.assertEqual(postmerge.apply(self.paths, self.runner), {"schemas": 0})
        self.assertEqual(os.listdir(os.path.join(self.paths["state"], "generated")), [])

    def test_the_same_file_name_in_two_extensions_is_taken_once(self):
        self.job()
        self.extension("a", ["usr/share/schemas/x.xml"])
        self.extension("b", ["usr/share/schemas/x.xml"])
        self.assertEqual(postmerge.apply(self.paths, self.runner), {"schemas": 1})

    def test_no_job_files_means_nothing_happens(self):
        self.extension("a", ["usr/share/schemas/a.xml"])
        self.assertEqual(postmerge.apply(self.paths, self.runner), {})
        self.assertEqual(self.calls, [])

    def test_unusable_definitions_are_skipped_with_a_warning(self):
        for text in ('{"name": "../x", "collect": [], "run": ["a"]}', '{"name": "x", "collect": ["/abs/*"], "run": ["a"]}',
                     '{"name": "x", "collect": ["../up/*"], "run": ["a"]}', '{"name": "x", "collect": [], "run": []}', "not json"):
            self.job(text)
            with self.assertLogs("foundationd.backends.postmerge", "WARNING"):
                self.assertEqual(postmerge.jobs(self.paths), [], text)

    def test_the_shipped_definition_is_valid(self):
        shipped = os.path.join(HERE, "..", "data", "post-merge.d")
        os.makedirs(os.path.join(self.root, postmerge.JOB_DIR))
        shutil.copy(os.path.join(shipped, "10-gsettings.json"), os.path.join(self.root, postmerge.JOB_DIR))
        (job,) = postmerge.jobs(self.paths)
        self.assertEqual(job["run"], ["glib-compile-schemas", "{out}"])


@unittest.skipUnless(shutil.which("glib-compile-schemas"), "glib-compile-schemas is not installed")
class RealCompiler(Case):
    def test_a_schema_from_an_extension_is_found_next_to_the_system_ones(self):
        try:
            import gi
            gi.require_version("Gio", "2.0")
            from gi.repository import Gio
        except (ImportError, ValueError):
            self.skipTest("needs PyGObject")
        shipped = os.path.join(HERE, "..", "data", "post-merge.d")
        os.makedirs(os.path.join(self.root, postmerge.JOB_DIR))
        shutil.copy(os.path.join(shipped, "10-gsettings.json"), os.path.join(self.root, postmerge.JOB_DIR))
        self.extension("app", [])
        schemas = os.path.join(compat._enabled_dir(self.paths, "app"), "usr/share/glib-2.0/schemas")
        os.makedirs(schemas)
        with open(os.path.join(schemas, "org.example.foundationtest.gschema.xml"), "w") as handle:
            handle.write('<schemalist><schema id="org.example.foundationtest" path="/org/example/foundationtest/">'
                         '<key name="x" type="i"><default>7</default></key></schema></schemalist>')
        self.assertEqual(postmerge.apply(self.paths), {"gsettings": 1})
        out = postmerge.output_dir(self.paths, "gsettings")
        self.assertTrue(os.path.exists(os.path.join(out, "gschemas.compiled")))
        # a second process with the environment setting the image ships sees the schema
        import subprocess
        code = ("from gi.repository import Gio; s = Gio.SettingsSchemaSource.get_default(); "
                "print(s.lookup('org.example.foundationtest', True) is not None, "
                "s.lookup('org.gnome.desktop.interface', True) is not None)")
        found = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                               env={**os.environ, "GSETTINGS_SCHEMA_DIR": out}).stdout.strip()
        self.assertEqual(found, "True True", "the extension's schema is found and the system's still is")


if __name__ == "__main__":
    unittest.main()
