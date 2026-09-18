"""The console's static files: present, sterile, and on-contract.

Three things are pinned here. Every file the frontend tasks deliver
exists. Nothing private survives in them: the gate's generic classes
(an address, a home path, a secret shape, an opaque binary) plus the
out-of-tree denylist when the environment names one. And the wire the
views call is exactly the wire `docs/console-spec.md` states: every
`/api/...` literal in the files matches a route on that page, and none
matches a route the page lists as not ported. A dropped view leaves no
trace by name either.
"""
import os
import pathlib
import re
import unittest

from cousin_lib.gate.scanner import Scanner, load_denylist

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_STATIC = _REPO_ROOT / "cousin_lib" / "console_static"
_SPEC = _REPO_ROOT / "docs" / "console-spec.md"

# Frontend A (this task). Frontend B extends the list with chat.jsx,
# styles.css and the PWA assets.
REQUIRED = ["index.html", "app.jsx", "views.jsx", "cousins.jsx", "ui.jsx", "data.jsx",
            "media.jsx"]

# Names of dropped surfaces. A file that mentions one has ported a view
# the operator excluded, or a store the console must not own.
_DROPPED_WORDS = re.compile(
    r"\b(BacklogView|backlog|presence|engagement|agents|AgentsView)\b", re.IGNORECASE
)

_API_LITERAL = re.compile(r"[\"'`](/api/[^\"'`\s]*)[\"'`]")
_SPEC_TOKEN = re.compile(r"/api/[A-Za-z0-9_./<>-]*[A-Za-z0-9_>]")
_TEMPLATE_PARAM = re.compile(r"\$\{[^}]*\}")


def _static_files():
    if not _STATIC.is_dir():
        return []
    return sorted(p for p in _STATIC.iterdir() if p.is_file())


def _split_spec():
    text = _SPEC.read_text(encoding="utf-8")
    marker = "## Source routes not ported"
    head, _, tail = text.partition(marker)
    stop = tail.find("\n## ")
    dropped_section = tail if stop < 0 else tail[:stop]
    retained = set(_SPEC_TOKEN.findall(head)) | set(_SPEC_TOKEN.findall(tail[stop:] if stop >= 0 else ""))
    dropped = set(_SPEC_TOKEN.findall(dropped_section)) - retained
    return retained, dropped


def _segments(path):
    return path.split("?", 1)[0].strip("/").split("/")


def _matches(code_path, spec_path):
    code = _segments(code_path)
    spec = _segments(spec_path)
    if len(code) != len(spec):
        return False
    for c, s in zip(code, spec):
        param_in_spec = s.startswith("<") and s.endswith(">")
        param_in_code = _TEMPLATE_PARAM.fullmatch(c) is not None
        if param_in_spec:
            continue
        if param_in_code:
            return False
        if c != s:
            return False
    return True


def _api_literals(text):
    found = []
    for m in _API_LITERAL.finditer(text):
        found.append(m.group(1))
    return found


class StaticFilesPresent(unittest.TestCase):
    def test_every_required_file_exists_and_is_not_empty(self):
        for name in REQUIRED:
            path = _STATIC / name
            self.assertTrue(path.is_file(), "missing static file: %s" % path)
            self.assertGreater(path.stat().st_size, 0, "empty static file: %s" % path)

    def test_index_loads_every_jsx_through_babel_from_pinned_cdn_tags(self):
        html = (_STATIC / "index.html").read_text(encoding="utf-8")
        for name in REQUIRED:
            if name.endswith(".jsx"):
                self.assertRegex(
                    html, r'<script[^>]*type="text/babel"[^>]*src="%s"' % re.escape(name),
                    "index.html does not load %s through Babel" % name)
        for lib in ("react@18", "react-dom@18", "@babel/standalone@"):
            self.assertIn(lib, html, "index.html does not pin %s" % lib)
        self.assertRegex(html, r"react@18\.\d+\.\d+", "React tag is not pinned to a version")
        self.assertRegex(html, r"@babel/standalone@\d+\.\d+\.\d+", "Babel tag is not pinned to a version")


class StaticFilesBrand(unittest.TestCase):
    """The product name the operator sees: the top bar and the tab."""

    def test_page_title_is_the_product_name(self):
        html = (_STATIC / "index.html").read_text(encoding="utf-8")
        titles = re.findall(r"<title>(.*?)</title>", html, re.S)
        self.assertEqual(titles, ["cousins"])

    def test_top_bar_brand_reads_cousins_slash_slash_console(self):
        app = (_STATIC / "app.jsx").read_text(encoding="utf-8")
        m = re.search(r'<span className="brand">(.*?)</span>\s*\n', app)
        self.assertIsNotNone(m, "no brand span in app.jsx")
        self.assertEqual(
            m.group(1),
            'cousins<span className="dim">//</span>console')
        # the rendered text, markup stripped: the dim span's margin gives
        # the spaces around the slashes
        self.assertEqual(re.sub(r"<[^>]+>", " ", m.group(1)).split(),
                         ["cousins", "//", "console"])

    def test_no_view_sets_a_different_document_title(self):
        for path in _static_files():
            if path.suffix != ".jsx":
                continue
            text = path.read_text(encoding="utf-8")
            for m in re.finditer(r"document\.title\s*=([^;\n]*)", text):
                self.assertIn("cousins", m.group(1),
                              "%s sets a title off the product name" % path.name)


class StaticFilesSterile(unittest.TestCase):
    def test_gate_generic_classes_find_nothing(self):
        terms = []
        denylist = os.environ.get("COUSIN_GATE_DENYLIST")
        if denylist and os.path.isfile(denylist):
            terms = load_denylist(denylist)
        hits = Scanner(name_terms=terms).scan_tree(_STATIC) if _STATIC.is_dir() else []
        details = "\n".join("%s:%s %s %r" % (h.file, h.line, h.kind, h.context) for h in hits)
        self.assertFalse(hits, "gate hits in console_static:\n" + details)

    def test_no_dropped_surface_is_named(self):
        for path in _static_files():
            text = path.read_text(encoding="utf-8")
            hit = _DROPPED_WORDS.search(text)
            self.assertIsNone(
                hit, "%s names a dropped surface: %r" % (path.name, hit and hit.group(0)))

    def test_no_vendor_model_catalogue(self):
        rx = re.compile(r"claude-(haiku|sonnet|opus|fable)", re.IGNORECASE)
        for path in _static_files():
            self.assertIsNone(rx.search(path.read_text(encoding="utf-8")),
                              "%s carries a vendor model name" % path.name)

    def test_no_em_dash(self):
        for path in _static_files():
            self.assertNotIn("\u2014", path.read_text(encoding="utf-8"),
                             "%s contains an em dash" % path.name)


class StaticFilesOnContract(unittest.TestCase):
    def test_every_api_literal_is_a_specified_route_and_none_is_dropped(self):
        retained, dropped = _split_spec()
        self.assertIn("/api/events", retained)
        self.assertIn("/api/sidebar", dropped)
        problems = []
        seen = 0
        for path in _static_files():
            if path.suffix not in (".jsx", ".js", ".html"):
                continue
            for literal in _api_literals(path.read_text(encoding="utf-8")):
                seen += 1
                if any(_matches(literal, d) for d in dropped):
                    problems.append("%s calls a dropped route: %s" % (path.name, literal))
                elif not any(_matches(literal, r) for r in retained):
                    problems.append("%s calls an unspecified route: %s" % (path.name, literal))
        self.assertFalse(problems, "\n".join(problems))
        self.assertGreater(seen, 0, "no /api literal found; the views call nothing?")


if __name__ == "__main__":
    unittest.main()
