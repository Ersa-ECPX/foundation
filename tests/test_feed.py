"""The update feed: what a source may say, and downloading the archive it names (foundationd/backends/feed.py)."""

import functools
import hashlib
import http.server
import os
import shutil
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))

from foundationd.backends import feed  # noqa: E402
from foundationd.errors import Failed, Unavailable  # noqa: E402


class Policy(unittest.TestCase):
    def test_https_from_anywhere_and_http_only_locally(self):
        for url in ("https://updates.example.org/a.tar.zst", "http://10.0.2.2:8000/a", "http://127.0.0.1:8000/a",
                    "http://192.168.1.5/a", "http://localhost:8000/a"):
            self.assertTrue(feed.url_allowed(url), url)
        for url in ("http://updates.example.org/a", "http://8.8.8.8/a", "ftp://10.0.0.1/a", "file:///etc/passwd", "https://", "x"):
            self.assertFalse(feed.url_allowed(url), url)

    def test_the_placeholder_update_source_is_recognised(self):
        self.assertTrue(feed.is_placeholder("https://updates.ersa.invalid/ecp"))
        self.assertTrue(feed.is_placeholder(""))
        self.assertFalse(feed.is_placeholder("https://updates.example.org/ecp"))

    def test_versions_compare_number_by_number(self):
        self.assertTrue(feed.is_newer("0.1.10", "0.1.9"))
        self.assertTrue(feed.is_newer("0.2.0", "0.1.9"))
        self.assertFalse(feed.is_newer("0.1.0", "0.1.0"))
        self.assertFalse(feed.is_newer("0.0.9", "0.1.0"))
        self.assertTrue(feed.is_newer("0.1.0", ""))


class Parsing(unittest.TestCase):
    def test_a_feed(self):
        text = '{"version": "0.1.1", "file": "ecp_0.1.1-x86_64.tar.zst", "size": 1610612736, "sha256": "' + "ab" * 32 + '"}'
        self.assertEqual(feed.parse_feed(text, "http://10.0.2.2:8000/", "x86_64"),
                         {"version": "0.1.1", "url": "http://10.0.2.2:8000/ecp_0.1.1-x86_64.tar.zst", "size": 1610612736, "sha256": "ab" * 32})
        self.assertEqual(feed.feed_url("https://u.example/ecp/", "aarch64"), "https://u.example/ecp/latest-aarch64.json")

    def test_an_absolute_file_address_is_used_as_it_is(self):
        text = '{"version": "1", "file": "https://github.com/x/y/releases/download/v1/a.tar.zst"}'
        self.assertEqual(feed.parse_feed(text, "https://u.example/ecp", "x86_64")["url"], "https://github.com/x/y/releases/download/v1/a.tar.zst")

    def test_a_bad_feed_is_refused(self):
        base = "https://u.example/ecp"
        for text, why in (("not json", "valid"), ("[]", "name a version"), ('{"version": "1"}', "name a version"),
                          ('{"version": "1", "file": "a", "arch": "aarch64"}', "aarch64"),
                          ('{"version": "1", "file": "http://evil.example/a"}', "not allowed"),
                          ('{"version": "1", "file": "a", "sha256": "xyz"}', "checksum")):
            with self.assertRaisesRegex(Failed, why):
                feed.parse_feed(text, base, "x86_64")

    def test_no_source_is_a_clear_error_and_nothing_is_fetched(self):
        def never(url):
            raise AssertionError("must not fetch")
        for base in ("", "https://updates.ersa.invalid/ecp"):
            with self.assertRaisesRegex(Unavailable, "no update source"):
                feed.read_feed(base, "x86_64", never)
        with self.assertRaisesRegex(Unavailable, "https"):
            feed.read_feed("http://updates.example.org", "x86_64", never)


class Downloading(unittest.TestCase):
    def setUp(self):
        self.www, self.out = tempfile.mkdtemp(), tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.www, True)
        self.addCleanup(shutil.rmtree, self.out, True)
        self.data = os.urandom(300000)
        with open(os.path.join(self.www, "a.tar.zst"), "wb") as handle:
            handle.write(self.data)
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=self.www)
        handler.log_message = lambda *a, **k: None
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/a.tar.zst"

    def test_the_file_is_downloaded_whole_and_progress_is_reported(self):
        seen = []
        path = feed.download(self.url, self.out, hashlib.sha256(self.data).hexdigest(), progress=seen.append)
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), self.data)
        self.assertEqual(seen[-1], 1.0)

    def test_a_checksum_that_does_not_match_removes_the_file(self):
        with self.assertRaisesRegex(Failed, "checksum"):
            feed.download(self.url, self.out, "00" * 32)
        self.assertEqual(os.listdir(self.out), [])

    def test_a_missing_file_and_an_oversized_one_leave_nothing(self):
        with self.assertRaises(Unavailable):
            feed.download(self.url.replace("a.tar", "nope.tar"), self.out)
        with self.assertRaisesRegex(Failed, "larger"):
            feed.download(self.url, self.out, limit=1000)
        self.assertEqual(os.listdir(self.out), [])


if __name__ == "__main__":
    unittest.main()
