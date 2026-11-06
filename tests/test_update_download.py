"""`foundation update install URL`: which addresses are accepted, and the download itself."""

import functools
import http.server
import os
import shutil
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))

from foundation import cli  # noqa: E402


class Policy(unittest.TestCase):
    def test_https_from_anywhere_and_http_only_locally(self):
        for url in ("https://updates.example.org/a.tar.zst", "http://10.0.2.2:8000/a", "http://127.0.0.1:8000/a",
                    "http://192.168.1.5/a", "http://localhost:8000/a"):
            self.assertTrue(cli.update_url_allowed(url), url)
        for url in ("http://updates.example.org/a", "http://8.8.8.8/a", "ftp://10.0.0.1/a", "file:///etc/passwd", "https://", "x"):
            self.assertFalse(cli.update_url_allowed(url), url)


class Download(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        with open(os.path.join(self.tmp, "a.tar.zst"), "wb") as handle:
            handle.write(b"x" * 300000)
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=self.tmp)
        handler.log_message = lambda *a, **k: None
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/a.tar.zst"
        self.out = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.out, True)

    def test_the_file_is_downloaded_whole(self):
        seen = []
        path = cli.fetch_update(self.url, self.out, progress=lambda done, total: seen.append((done, total)))
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), b"x" * 300000)
        self.assertEqual(seen[-1], (300000, 300000))

    def test_a_missing_file_is_an_error_and_leaves_nothing(self):
        with self.assertRaisesRegex(cli.UserError, "cannot download"):
            cli.fetch_update(self.url.replace("a.tar", "nope.tar"), self.out)
        self.assertEqual(os.listdir(self.out), [])

    def test_a_download_over_the_limit_is_removed(self):
        with self.assertRaisesRegex(cli.UserError, "larger"):
            cli.fetch_update(self.url, self.out, limit=1000)
        self.assertEqual(os.listdir(self.out), [])


if __name__ == "__main__":
    unittest.main()
