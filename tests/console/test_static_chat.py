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
import shutil
import subprocess
import unittest
import xml.etree.ElementTree as ET

from cousin_lib.gate.scanner import Scanner, load_denylist

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_STATIC = _REPO_ROOT / "cousin_lib" / "console_static"
_SPEC = _REPO_ROOT / "docs" / "console-spec.md"

_MINE = ("chat.jsx", "media.jsx", "styles.css", "manifest.webmanifest", "favicon.svg")

# Names of surfaces the contract dropped. None may appear, in any case,
# in the files this module owns. The media viewer and inline players came
# back on 2026-09-18 (docs/console-spec.md, "The chat media viewer"); the
# engagement pings, the recorder and the media-kind filter stay out.
_DROPPED_NAMES = (
    "engagement", "presence",
    "favorite", "favourite", "MediaRecorder", "getUserMedia",
    "/api/chat/audio", "/api/chat/image", "/api/chat/video",
    # the source injected a vendor slash command ("/effort <level>")
    # into the pane; the effort ROUTE (/api/cousins/<slug>/effort) is
    # on the contract and persists to cousin.toml instead
    "/effort ", "has=", "agents-table", "kanban", "backlog",
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
                   os.path.expanduser("~/.config/cousins-framework/denylist.txt")))


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
    same as one written inline: the literal is what names the route. A
    `${...}` segment in a template literal is one path parameter, the
    way the spec's `<slug>` is, so `/api/cousins/${slug}/effort` is
    checked as a whole route rather than as its static prefix.
    """
    rx = re.compile(r"[`\"'](/api/(?:[^`\"'?$]|\$\{[^}]*\})*)")
    return sorted(set(re.sub(r"\$\{[^}]*\}", "x", p)
                      for p in rx.findall(text)))


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
        for name in ("chat.jsx", "media.jsx", "styles.css"):
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



class SeenWatermarkLinkage(unittest.TestCase):
    def test_chat_stamps_the_same_seen_key_the_sidebar_reads(self):
        # app.jsx computes the unread dot from chatSeenKey(slug, user) in
        # data.jsx; a chat view stamping a differently spelled key means
        # the dot never clears.
        chat = (_STATIC / "chat.jsx").read_text()
        self.assertIn("chatSeenKey(", chat)
        self.assertNotIn("fw_chat_seen_", chat)


def _function_body(text, name):
    """The source of one top-level `function name(` up to the next one."""
    start = text.index("function %s(" % name)
    nxt = re.search(r"^function \w+\(", text[start + 1:], re.M)
    return text[start:start + 1 + nxt.start()] if nxt else text[start:]


def _css_block(css, selector):
    m = re.search(r"(?m)^%s\s*\{([^}]*)\}" % re.escape(selector), css)
    return m.group(1) if m else ""


class PaneFitsTheWindow(unittest.TestCase):
    """The live pane ends at the viewport's bottom edge."""

    def setUp(self):
        self.chat = (_STATIC / "chat.jsx").read_text()
        self.pane = _function_body(self.chat, "PaneView")
        self.css = (_STATIC / "styles.css").read_text()

    def test_shell_height_is_the_visible_viewport_not_the_layout_one(self):
        # 100vh on a mobile browser is the viewport with the toolbars
        # retracted, so the bottom of the shell sits under them
        shell = _css_block(self.css, ".shell")
        self.assertIn("height: 100vh;", shell, "no fallback for old engines")
        self.assertIn("height: 100dvh;", shell)
        self.assertLess(shell.index("100vh"), shell.index("100dvh"),
                        "the dvh line must come after its fallback")
        full = _css_block(self.css, "body.chat-fullscreen .main")
        self.assertIn("100dvh", full)

    def test_xterm_host_has_no_padding_the_fit_addon_would_miscount(self):
        # FitAddon sizes rows from the parent's computed height; under
        # `box-sizing: border-box` that height includes the padding, so a
        # padded host gets rows that hang below its bottom edge
        m = re.search(r"ref=\{hostRef\}\s*style=\{\{(.*?)\}\}", self.pane, re.S)
        self.assertIsNotNone(m, "the xterm host element is not found")
        self.assertNotIn("padding", m.group(1))

    def test_chat_view_carries_no_fixed_minimum_height(self):
        root = _function_body(self.chat, "ChatView")
        self.assertIsNone(re.search(r"minHeight:\s*[1-9]\d{2}", root),
                          "a fixed minimum pushes the pane under the window")

    def test_a_geom_frame_never_sets_rows_past_the_fitted_host(self):
        self.assertIsNone(re.search(r"term\.resize\([^)]*payload\.rows",
                                    self.pane),
                          "adopting tmux's rows overflows the host")


class PaneScrollsBack(unittest.TestCase):
    """The pane keeps history and does not yank a reader to the bottom."""

    def setUp(self):
        self.pane = _function_body((_STATIC / "chat.jsx").read_text(),
                                   "PaneView")

    def test_the_stream_asks_for_history_beyond_the_screen(self):
        m = re.search(r"/api/pane/stream\?[^\"']*[\"']\s*\+[^;]*lines=(\d+)",
                      self.pane)
        self.assertIsNotNone(m, "the stream URL names no lines= count")
        self.assertGreaterEqual(int(m.group(1)), 1000)

    def test_scrollback_holds_the_captured_history(self):
        sb = int(re.search(r"scrollback:\s*(\d+)", self.pane).group(1))
        lines = int(re.search(r"lines=(\d+)", self.pane).group(1))
        self.assertGreater(sb, lines)

    def test_follow_state_comes_from_the_viewport_position(self):
        for token in ("followRef", "viewportY", "baseY"):
            self.assertIn(token, self.pane)

    def test_a_pane_frame_is_held_while_the_reader_is_scrolled_up(self):
        start = self.pane.index('addEventListener("pane"')
        handler = self.pane[start:self.pane.index("addEventListener", start + 1)]
        self.assertIn("followRef.current", handler)
        self.assertNotIn("term.reset()", handler,
                         "the handler rewrites the terminal unconditionally")
        self.assertLess(handler.index("followRef.current"),
                        handler.index("applyFrame("),
                        "the frame is applied before the follow check")
        self.assertIn("pendingRef.current = text", handler)

    def test_applying_a_frame_lands_on_its_last_line(self):
        start = self.pane.index("const applyFrame = ")
        body = self.pane[start:self.pane.index("};", start)]
        self.assertIn("term.reset()", body)
        self.assertIn("scrollToBottom", body)

    def test_returning_to_the_bottom_resumes_following(self):
        self.assertIn("scrollToBottom", self.pane)
        self.assertRegex(self.pane, r"onScroll|addEventListener\(\"scroll\"")


class PaneScrollsTheProgram(unittest.TestCase):
    """A full-screen program has no captured history: the wheel must
    reach it as its own mouse reports, and never as keys."""

    def setUp(self):
        self.pane = _function_body((_STATIC / "chat.jsx").read_text(),
                                   "PaneView")

    def test_a_frame_is_applied_with_the_state_it_came_with(self):
        self.assertIn("applyFrame(text, payload.state)", self.pane)
        self.assertIn("pendingStateRef.current = payload.state", self.pane)
        self.assertIn("applyFrame(next, nextState)", self.pane)

    def test_mouse_reports_are_mapped_to_pane_rows_before_sending(self):
        self.assertRegex(self.pane, r"const buf = toPaneRows\(inputBufRef\.current\)")
        self.assertIn("b.viewportY + Number(row) - (st.top || 0)", self.pane)

    def test_an_untracked_wheel_on_an_alternate_buffer_is_swallowed(self):
        start = self.pane.index("const onWheelCapture = ")
        body = self.pane[start:self.pane.index("};", start)]
        self.assertIn("appTracksMouse()", body)
        self.assertIn('"alternate"', body)
        self.assertIn("stopPropagation", body)
        self.assertRegex(self.pane, r'addEventListener\("wheel", onWheelCapture, \{ capture: true')

    def test_touch_drag_still_turns_into_wheel_reports(self):
        self.assertIn('"\\x1b[<" + (up ? 64 : 65)', self.pane)
        self.assertIn("mouseTrackingMode", self.pane)

    def test_mouse_tracking_is_read_from_the_frame_state_first(self):
        start = self.pane.index("const appTracksMouse = ")
        body = self.pane[start:self.pane.index("};", start)]
        self.assertLess(body.index("paneStateRef.current"),
                        body.index("mouseTrackingMode"))


class ChatMediaViewer(unittest.TestCase):
    """The media port (docs/console-spec.md, "The chat media viewer")."""

    def setUp(self):
        self.media = (_STATIC / "media.jsx").read_text()
        self.chat = (_STATIC / "chat.jsx").read_text()
        self.css = (_STATIC / "styles.css").read_text()

    def test_index_loads_media_before_chat(self):
        html = (_STATIC / "index.html").read_text()
        at = html.index('<script type="text/babel" src="media.jsx"></script>')
        self.assertLess(at, html.index('src="chat.jsx"'),
                        "chat.jsx reads media.jsx's names off window")

    def test_kind_prefers_the_api_then_the_row_then_the_extension(self):
        body = _function_body(self.media, "attachmentMedia")
        api = body.index("msg.attachment && msg.attachment.kind")
        row = body.index("msg.attachment_kind")
        ext = body.index("mediaKindFromUrl(src)")
        self.assertLess(api, row)
        self.assertLess(row, ext)
        norm = _function_body(self.media, "normalizeMediaKind")
        self.assertIn('k === "audio" || k === "voice"', norm)
        for kind, ext in (("image", '"png"'), ("image", '"webp"'),
                          ("video", '"mp4"'), ("video", '"webm"'),
                          ("audio", '"mp3"'), ("audio", '"ogg"')):
            self.assertRegex(self.media, r"%s: \[[^\]]*%s" % (kind, ext))

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_kind_detection_behaves(self):
        # The pure helpers run under node, lifted out of the file as is.
        src = self.media[self.media.index("const _MEDIA_EXT"):
                         self.media.index("// Inline video preview")]
        probe = src + """
const cases = [
  mediaKindFromUrl("/api/chat/media/s/video/a_1_b.mp4"),
  mediaKindFromUrl("/x/clip.WEBM?v=1#t"),
  mediaKindFromUrl("/api/chat/inbound/s/12.png"),
  mediaKindFromUrl("/x/voice.mp3"),
  mediaKindFromUrl("data:image/png;base64,AAAA"),
  mediaKindFromUrl("/x/readme.txt"),
  mediaKindFromUrl(""),
  attachmentMedia({attachment: {url: "/x/a.bin", kind: "video"}}),
  attachmentMedia({attachment: {url: "/x/a.bin"}, attachment_kind: "voice"}),
  attachmentMedia({attachment: {url: "/x/a.ogg"}}),
  attachmentMedia({image: "data:image/jpeg;base64,AA"}),
  attachmentMedia({image: "/not/a/data/uri.png"}),
  attachmentMedia({message: "text only"}),
];
process.stdout.write(JSON.stringify(cases));
"""
        out = subprocess.run(["node", "-e", probe], capture_output=True,
                             text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout), [
            "video", "video", "image", "audio", "image", None, None,
            {"src": "/x/a.bin", "kind": "video"},
            {"src": "/x/a.bin", "kind": "audio"},
            {"src": "/x/a.ogg", "kind": "audio"},
            {"src": "data:image/jpeg;base64,AA", "kind": "image"},
            None, None,
        ])

    def test_inline_players_by_kind(self):
        preview = _function_body(self.media, "LoopingPreview")
        tag = re.search(r"<video.*?/>", preview, re.S).group(0)
        for attr in ("muted", "loop", "playsInline", 'preload="metadata"'):
            self.assertIn(attr, tag)
        self.assertNotIn("controls", tag, "the inline preview is silent, the viewer has the controls")
        self.assertIn("IntersectionObserver", preview)
        inline = _function_body(self.media, "InlineMedia")
        self.assertRegex(inline, r"<audio[^>]*controls")
        bubble = _function_body(self.chat, "ChatBubble")
        self.assertIn("attachmentMedia(msg)", bubble)
        self.assertIn("<InlineMedia", bubble)



if __name__ == "__main__":
    unittest.main()
