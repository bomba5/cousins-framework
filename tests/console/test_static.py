"""Static serving for the console: an allowlisted directory, a traversal
check, index fallback for `/`, and a cache-busting stamp on index.html's
local script and stylesheet references (docs/reference/console-api.md, "Static
files")."""
import gzip
import hashlib
import os
import pathlib
import tempfile
import unittest

from cousin_lib.console import static


class StaticCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name) / "static"
        self.root.mkdir()
        (self.root / "index.html").write_text(
            '<html><head>'
            '<script src="https://cdn.example.invalid/react.js"></script>'
            '<link rel="stylesheet" href="styles.css">'
            '<link rel="manifest" href="/manifest.webmanifest">'
            '<script type="text/babel" src="app.jsx"></script>'
            '<a href="#top">top</a>'
            '</head><body></body></html>'
        )
        (self.root / "styles.css").write_text("body{}")
        (self.root / "app.jsx").write_text("const x = 1;")
        (self.root / "manifest.webmanifest").write_text("{}")
        (self.root / "notes.py").write_text("secrets = 1")
        (self.root.parent / "secret.txt").write_text("outside")

    def _headers(self, headers):
        return {k.lower(): v for k, v in headers}

    def test_root_serves_index_with_version_stamps_on_local_refs_only(self):
        status, headers, body = static.serve_static("/", root=self.root)
        self.assertEqual(status, 200)
        text = body.decode()
        h = self._headers(headers)
        self.assertTrue(h["content-type"].startswith("text/html"))
        self.assertEqual(h["cache-control"], "no-store")
        self.assertIn('src="app.jsx?v=', text)
        self.assertIn('href="styles.css?v=', text)
        self.assertIn('href="/manifest.webmanifest?v=', text)
        self.assertIn('src="https://cdn.example.invalid/react.js"', text)
        self.assertIn('href="#top"', text)

    def test_stamp_is_the_referenced_files_content_hash(self):
        stamp = hashlib.sha256(b"const x = 1;").hexdigest()[:12]
        _, _, body = static.serve_static("/index.html", root=self.root)
        self.assertIn('src="app.jsx?v=%s"' % stamp, body.decode())

    def test_stamp_follows_a_change_with_the_same_mtime(self):
        os.utime(self.root / "app.jsx", (1000, 1000))
        _, _, before = static.serve_static("/", root=self.root)
        (self.root / "app.jsx").write_text("const x = 22;")
        os.utime(self.root / "app.jsx", (1000, 1000))
        _, _, after = static.serve_static("/", root=self.root)
        self.assertNotEqual(self._stamp_of(before, "app.jsx"),
                            self._stamp_of(after, "app.jsx"))

    def _stamp_of(self, page, ref):
        import re
        return re.search(r'%s\?v=([0-9a-f]+)"' % re.escape(ref),
                         page.decode()).group(1)

    def test_current_stamp_is_cached_for_good_any_other_is_not(self):
        _, _, page = static.serve_static("/", root=self.root)
        stamp = self._stamp_of(page, "app.jsx")
        _, headers, body = static.serve_static("/app.jsx?v=" + stamp,
                                               root=self.root)
        self.assertEqual(body, b"const x = 1;")
        self.assertEqual(self._headers(headers)["cache-control"],
                         static.IMMUTABLE)
        # an old page asking for an old stamp gets today's file, so it
        # must not be cached under that old URL
        for path in ("/app.jsx?v=000000000000", "/app.jsx?v=1000",
                     "/app.jsx"):
            _, headers, _ = static.serve_static(path, root=self.root)
            self.assertEqual(self._headers(headers)["cache-control"],
                             "no-store", path)
        # the page itself never is, whatever it is asked with
        _, headers, _ = static.serve_static("/index.html?v=" + stamp,
                                            root=self.root)
        self.assertEqual(self._headers(headers)["cache-control"],
                         "no-store")

    def test_text_is_gzipped_when_accepted(self):
        big = "const filler = 1;\n" * 200
        (self.root / "big.js").write_text(big)
        _, headers, body = static.serve_static(
            "/big.js", root=self.root, accept_encoding="br, gzip;q=0.8")
        h = self._headers(headers)
        self.assertEqual(h["content-encoding"], "gzip")
        self.assertEqual(h["vary"], "Accept-Encoding")
        self.assertEqual(h["content-length"], str(len(body)))
        self.assertEqual(gzip.decompress(body).decode(), big)
        for refused in ("", "br", "gzip;q=0", "identity"):
            _, headers, body = static.serve_static(
                "/big.js", root=self.root, accept_encoding=refused)
            h = self._headers(headers)
            self.assertNotIn("content-encoding", h, refused)
            self.assertEqual(body.decode(), big)
            self.assertEqual(h["vary"], "Accept-Encoding")

    def test_small_text_and_images_go_as_they_are(self):
        (self.root / "icon.png").write_bytes(b"\x89PNG" + b"0" * 4096)
        for path in ("/styles.css", "/icon.png"):
            _, headers, _ = static.serve_static(path, root=self.root,
                                                accept_encoding="gzip")
            self.assertNotIn("content-encoding", self._headers(headers),
                             path)
        _, headers, _ = static.serve_static("/icon.png", root=self.root)
        self.assertNotIn("vary", self._headers(headers))

    def test_a_rewritten_file_is_served_fresh(self):
        static.serve_static("/app.jsx", root=self.root)
        (self.root / "app.jsx").write_text("const y = 2;")
        _, _, body = static.serve_static("/app.jsx", root=self.root)
        self.assertEqual(body, b"const y = 2;")

    def test_allowlisted_file_is_served_with_its_type_and_no_store(self):
        status, headers, body = static.serve_static("/styles.css",
                                                    root=self.root)
        self.assertEqual(status, 200)
        self.assertEqual(body, b"body{}")
        h = self._headers(headers)
        self.assertTrue(h["content-type"].startswith("text/css"))
        self.assertEqual(h["cache-control"], "no-store")
        self.assertEqual(h["content-length"], "6")

    def test_traversal_is_403_and_leaks_nothing(self):
        for path in ("/../secret.txt", "/%2e%2e/secret.txt",
                     "/sub/../../secret.txt"):
            status, _, body = static.serve_static(path, root=self.root)
            self.assertEqual(status, 403, path)
            self.assertNotIn(b"outside", body)

    def test_missing_file_and_disallowed_suffix_are_404(self):
        status, _, _ = static.serve_static("/missing.css", root=self.root)
        self.assertEqual(status, 404)
        status, _, body = static.serve_static("/notes.py", root=self.root)
        self.assertEqual(status, 404)
        self.assertNotIn(b"secrets", body)

    def test_error_bodies_are_json(self):
        import json
        status, headers, body = static.serve_static("/nope.js",
                                                    root=self.root)
        self.assertEqual(status, 404)
        self.assertEqual(self._headers(headers)["content-type"],
                         "application/json")
        self.assertIn("error", json.loads(body))

    def test_missing_index_is_404_not_a_crash(self):
        (self.root / "index.html").unlink()
        status, _, _ = static.serve_static("/", root=self.root)
        self.assertEqual(status, 404)

    def test_default_root_is_the_console_static_package_dir(self):
        expected = (pathlib.Path(static.__file__).resolve().parent.parent
                    / "console_static")
        self.assertEqual(static.STATIC_DIR, expected)

    def test_a_directory_is_not_served(self):
        (self.root / "sub.js").mkdir()
        status, _, _ = static.serve_static("/sub.js", root=self.root)
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
