"""Static serving for the console: an allowlisted directory, a traversal
check, index fallback for `/`, and a cache-busting stamp on index.html's
local script and stylesheet references (docs/reference/console-api.md, "Static
files")."""
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

    def test_stamp_is_the_referenced_files_mtime(self):
        os.utime(self.root / "app.jsx", (1000, 1000))
        _, _, body = static.serve_static("/index.html", root=self.root)
        self.assertIn('src="app.jsx?v=1000"', body.decode())

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
