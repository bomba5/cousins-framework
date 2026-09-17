"""Static-file contract for the chat half of the console frontend.

Covers `chat.jsx`, `styles.css`, `manifest.webmanifest` and `favicon.svg`
under `cousin_lib/console_static/`: the files exist, every route the
chat view calls is one `docs/console-spec.md` defines, nothing the spec
dropped survives by name, and the contamination scanner finds nothing
in the directory. The shell files (`index.html`, `app.jsx`, ...) have
their own test module.
"""
import json
import os
import pathlib
import re
import unittest
import xml.etree.ElementTree as ET

from cousin_lib.gate.scanner import Scanner, load_denylist

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_STATIC = _REPO_ROOT / "cousin_lib" / "console_static"
_SPEC = _REPO_ROOT / "docs" / "console-spec.md"

_MINE = ("chat.jsx", "styles.css", "manifest.webmanifest", "favicon.svg")

# Names of surfaces the contract dropped. None may appear, in any case,
# in the files this module owns.
_DROPPED_NAMES = (
    "lightbox", "InlineVideo", "engagement", "presence",
    "favorite", "favourite", "MediaRecorder", "getUserMedia",
    "/api/chat/audio", "/api/chat/image", "/api/chat/video",
    "/effort", "has=", "agents-table", "kanban", "backlog",
)

# Generic patterns the contract lists as install specifics: a private
# address, a home path, a hard-coded locale, an em dash.
_GENERIC_PATTERNS = {
    "rfc1918 address": re.compile(
        r"\b(?:10|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}(?:\.\d{1,3})?\b"),
    "home path": re.compile(r"/home/[A-Za-z0-9_.-]+"),
    "hard-coded locale": re.compile(r"toLocale\w*\(\s*[\"'][a-z]{2}-[A-Z]{2}"),
    "em dash": re.compile("\u2014"),
}

_DENYLIST = pathlib.Path(
    os.environ.get("COUSIN_DENYLIST",
                   os.path.expanduser("~/.config/cousin-framework/denylist.txt")))


def _spec_routes():
    """Every `/api/...` route the spec defines under a heading.

    Returned as compiled patterns: a `<param>` segment matches one path
    segment. Mentions in prose (the not-ported list names dropped routes
    too) do not count; only headings define a route.
    """
    routes = []
    heading = re.compile(r"^#+ .*$", re.M)
    inside = re.compile(r"`(?:(?:GET|POST|DELETE)(?:/(?:GET|POST|DELETE))*\s+)?(/api/[^`?\s]+)")
    for h in heading.findall(_SPEC.read_text()):
        for path in inside.findall(h):
            rx = "^" + re.sub(r"<[^>]+>", r"[^/]+", re.escape(path).replace(r"\<", "<").replace(r"\>", ">")) + "$"
            routes.append((path, re.compile(rx)))
    return routes


def _called_routes(text):
    """The static prefix of every `/api/...` string literal in a JSX file.

    A path built into a variable and passed to fetch() later counts the
    same as one written inline: the literal is what names the route.
    """
    rx = re.compile(r"[`\"'](/api/[^`\"'?$]*)")
    return sorted(set(rx.findall(text)))


class StaticChatFiles(unittest.TestCase):
    def test_every_file_of_this_task_exists_and_is_not_empty(self):
        for name in _MINE:
            p = _STATIC / name
            self.assertTrue(p.is_file(), "%s is missing" % p)
            self.assertGreater(p.stat().st_size, 0, "%s is empty" % p)

    def test_chat_jsx_defines_the_contract_components(self):
        text = (_STATIC / "chat.jsx").read_text()
        for name in ("ChatView", "ChatHeader", "ChatBody", "ChatBubble",
                     "PaneView", "renderMarkdown"):
            self.assertRegex(text, r"function %s\(" % name,
                             "%s is not defined in chat.jsx" % name)
        self.assertIn("ChatView", text.split("Object.assign(window")[-1],
                      "ChatView is not exported on window")

    def test_every_route_chat_jsx_calls_is_defined_by_the_spec(self):
        routes = _spec_routes()
        self.assertTrue(routes, "no routes parsed from the spec headings")
        called = _called_routes((_STATIC / "chat.jsx").read_text())
        self.assertTrue(called, "chat.jsx calls no /api route at all")
        for path in called:
            self.assertTrue(any(rx.match(path) for _, rx in routes),
                            "%s is not a route the spec defines" % path)

    def test_chat_jsx_calls_the_chat_and_pane_routes(self):
        called = _called_routes((_STATIC / "chat.jsx").read_text())
        for must in ("/api/messages", "/api/search", "/api/chat/send",
                     "/api/chat/archive", "/api/chat/reactions",
                     "/api/pane/stream", "/api/pane/input", "/api/pane/resize"):
            self.assertIn(must, called)

    def test_reactions_carry_an_action_and_search_reads_messages(self):
        text = (_STATIC / "chat.jsx").read_text()
        # the cousin server's /api/reactions takes `action`; the console
        # forwards it, so the view sends one
        self.assertRegex(text, r"action:\s*\w+")
        # the source view read `results`; the ported route answers `messages`
        self.assertNotIn("d.results", text)
        # attachments render through the console's inbound-file projection
        self.assertIn("attachment", text)

    def test_nothing_the_spec_dropped_survives_by_name(self):
        for name in ("chat.jsx", "styles.css"):
            text = (_STATIC / name).read_text().lower()
            for dropped in _DROPPED_NAMES:
                self.assertNotIn(dropped.lower(), text,
                                 "%s still names dropped %r" % (name, dropped))

    def test_no_generic_private_pattern_in_owned_files(self):
        for name in _MINE:
            text = (_STATIC / name).read_text()
            for label, rx in _GENERIC_PATTERNS.items():
                m = rx.search(text)
                self.assertIsNone(m, "%s carries a %s: %r" % (name, label, m and m.group(0)))

    def test_the_scanner_finds_nothing_in_the_static_directory(self):
        hits = Scanner(name_terms=[]).scan_tree(_STATIC)
        self.assertEqual([], ["%s:%s %s" % (h.file, h.line, h.term) for h in hits])

    def test_the_external_denylist_finds_nothing_when_present(self):
        if not _DENYLIST.is_file():
            self.skipTest("no external denylist on this machine")
        terms = load_denylist(_DENYLIST)
        hits = Scanner(name_terms=terms).scan_tree(_STATIC)
        self.assertEqual([], ["%s:%s" % (h.file, h.line) for h in hits])

    def test_styles_fetch_nothing_external_and_style_no_dropped_component(self):
        css = (_STATIC / "styles.css").read_text()
        self.assertNotRegex(css, r"@import\s+url\(\s*['\"]?https?://",
                            "styles.css imports from the network")
        self.assertNotRegex(css, r"url\(\s*['\"]?https?://")
        # message avatars are keyed on the row's type, never on a slug
        self.assertLessEqual(set(re.findall(r"\.msg\.(\w+)", css)), {"me", "op", "cousin"})
        # log colours come from a hash of the slug, not from a rule per slug
        self.assertIsNone(re.search(r"\.logtail \.c\.\w+", css))
        for cls in ("agents-table", "kanban"):
            self.assertNotIn(cls, css)
        self.assertIn(".chat-bubble", css)
        self.assertIn(".xterm", css)

    def test_manifest_is_valid_and_its_icons_exist(self):
        m = json.loads((_STATIC / "manifest.webmanifest").read_text())
        for key in ("name", "short_name", "start_url", "display", "icons"):
            self.assertIn(key, m)
        self.assertTrue(m["icons"], "manifest lists no icon")
        for icon in m["icons"]:
            src = icon["src"].lstrip("/")
            self.assertTrue((_STATIC / src).is_file(), "icon %s is missing" % src)

    def test_favicon_is_a_plain_svg(self):
        root = ET.parse(_STATIC / "favicon.svg").getroot()
        self.assertTrue(root.tag.endswith("svg"), root.tag)
        text = (_STATIC / "favicon.svg").read_text()
        self.assertNotIn("<image", text)
        self.assertNotIn("<text", text)


if __name__ == "__main__":
    unittest.main()
