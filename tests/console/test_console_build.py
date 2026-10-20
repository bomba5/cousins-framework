"""The console build (cousin_lib/console/build.py): the committed
compiled copy is current, the vendored libraries are the pinned ones,
and static.py serves the compiled files only while they match the
sources (docs/development.md, "The console build")."""
import json
import pathlib
import re
import tempfile
import unittest

from cousin_lib.console import build, static

_STATIC = build.STATIC_DIR

_PAGE = (
    '<html><head><script src="vendor/react.js"></script></head><body>\n'
    '<!-- app:start\n  the app -->\n'
    '<script src="https://unpkg.com/@babel/standalone@7.29.0/babel.min.js"'
    ' integrity="sha384-x" crossorigin="anonymous"></script>\n'
    '<script type="text/babel" src="data.jsx"></script>\n'
    '<script type="text/babel" src="app.jsx"></script>\n'
    '<!-- app:end -->\n</body></html>'
)


class CommittedBuild(unittest.TestCase):
    def test_the_compiled_copy_is_current(self):
        why = build.stale(_STATIC)
        self.assertEqual(why, "", "%s: run python -m cousin_lib.console.build"
                         " and commit compiled/" % why)

    def test_every_source_the_page_names_is_compiled(self):
        sources = build.page_sources(_STATIC)
        self.assertEqual((sources[0], sources[-1]), ("data.jsx", "app.jsx"))
        manifest = build.read_manifest(_STATIC)
        self.assertEqual(sorted(manifest["files"]), sorted(sources))
        self.assertEqual(manifest["esbuild"], build.ESBUILD)
        for source in sources:
            self.assertTrue((_STATIC / build.compiled_name(source)).is_file())

    def test_vendored_libraries_are_the_pinned_ones(self):
        for name, _url, pinned in build.LIBS:
            data = (_STATIC / build.VENDOR / name).read_bytes()
            self.assertEqual(build.sri(data), pinned, name)

    def test_the_served_page_fetches_nothing_from_outside(self):
        _, _, body = static.serve_static("/")
        page = body.decode()
        self.assertNotIn("@babel/standalone", page)
        self.assertNotIn('type="text/babel"', page)
        self.assertEqual(re.findall(r'(?:src|href)="(https?:[^"]+)"', page),
                         build.CDN_LIBS)
        for url in build.CDN_LIBS:
            self.assertRegex(page, r'data-lib="\w+" src="%s" integrity="sha384-'
                             % re.escape(url))
        for name in ("data", "chat", "app"):
            self.assertRegex(page, r'<script src="compiled/%s\.js\?v=[0-9a-f]{12}">'
                             % name)
        for name, _url, _pin in build.LIBS:
            self.assertIn('"vendor/%s?v=' % name, page)


class ServedPage(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        (self.root / "index.html").write_text(_PAGE)
        (self.root / "data.jsx").write_text("const d = <b/>;")
        (self.root / "app.jsx").write_text("const a = <i/>;")
        (self.root / "compiled").mkdir()
        (self.root / "compiled" / "data.js").write_text('const d = React.createElement("b");')
        (self.root / "compiled" / "app.js").write_text('const a = React.createElement("i");')
        self.write_manifest()

    def write_manifest(self):
        files = {}
        for name in ("data", "app"):
            files[name + ".jsx"] = {
                "source": build.stamp((self.root / (name + ".jsx")).read_bytes()),
                "output": build.stamp((self.root / "compiled" / (name + ".js")).read_bytes()),
            }
        (self.root / "compiled" / "manifest.json").write_text(
            json.dumps({"esbuild": build.ESBUILD, "files": files}))

    def page(self):
        status, _, body = static.serve_static("/", root=self.root)
        self.assertEqual(status, 200)
        return body.decode()

    def assert_compiled(self, page):
        self.assertNotIn("babel", page)
        self.assertRegex(page, r'<script src="compiled/data\.js\?v=[0-9a-f]{12}"></script>\n'
                               r'<script src="compiled/app\.js\?v=[0-9a-f]{12}"></script>')
        self.assertIn('src="vendor/react.js?v=', page)

    def assert_as_written(self, page):
        self.assertIn("@babel/standalone", page)
        self.assertIn('<script type="text/babel" src="data.jsx?v=', page)
        self.assertNotIn("compiled/", page)

    def test_a_current_build_stands_in_for_the_sources(self):
        self.assertEqual(build.stale(self.root), "")
        self.assert_compiled(self.page())

    def test_an_edited_source_falls_back_to_the_browser_compiling(self):
        (self.root / "app.jsx").write_text("const a = <u/>;")
        self.assertIn("app.jsx changed", build.stale(self.root))
        self.assert_as_written(self.page())
        self.write_manifest()   # rebuilt
        self.assert_compiled(self.page())

    def test_a_touched_output_or_no_manifest_falls_back(self):
        (self.root / "compiled" / "data.js").write_text("broken")
        self.assertIn("does not match", build.stale(self.root))
        self.assert_as_written(self.page())
        (self.root / "compiled" / "manifest.json").unlink()
        self.assertIn("no compiled/manifest.json", build.stale(self.root))
        self.assert_as_written(self.page())

    def test_a_source_the_manifest_does_not_know_falls_back(self):
        (self.root / "index.html").write_text(_PAGE.replace(
            '<!-- app:end -->',
            '<script type="text/babel" src="new.jsx"></script>\n<!-- app:end -->'))
        (self.root / "new.jsx").write_text("const n = 1;")
        self.assertIn("new.jsx is not compiled", build.stale(self.root))
        self.assert_as_written(self.page())

    def test_a_page_without_an_app_block_is_served_as_written(self):
        (self.root / "index.html").write_text(
            '<script type="text/babel" src="app.jsx"></script>')
        self.assertIn('type="text/babel" src="app.jsx?v=', self.page())


if __name__ == "__main__":
    unittest.main()
