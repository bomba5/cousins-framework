"""Static-file contract for the chat half of the console frontend.

Covers `chat.jsx`, `styles.css`, `manifest.webmanifest` and `favicon.svg`
under `cousin_lib/console_static/`: the files exist, every route the
chat view calls is one `docs/reference/console-api.md` defines, nothing the spec
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
_SPEC = _REPO_ROOT / "docs" / "reference" / "console-api.md"

_MINE = ("chat.jsx", "media.jsx", "styles.css", "manifest.webmanifest", "favicon.svg")

# Names of surfaces the contract dropped. None may appear, in any case,
# in the files this module owns. The media viewer and inline players are
# back (docs/reference/console-api.md, "The chat media viewer"); the
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

    def test_embed_pins_the_chat_full_screen_without_saving_it(self):
        # ?embed=1 hides the chrome through body.chat-fullscreen; ChatView's
        # own effect owns that class and runs after app.jsx's (ChatView
        # mounts later), so it must hold it while embedded; an app.jsx-side
        # add lost to it and the top bar showed
        chat = (_STATIC / "chat.jsx").read_text()
        self.assertIn('classList.toggle("chat-fullscreen", fullscreen || !!embed)', chat)
        self.assertRegex(chat, r'if \(!embed\) \{\s*try \{ localStorage\.setItem\("fw_chat_fullscreen"')
        app = (_STATIC / "app.jsx").read_text()
        self.assertNotIn('classList.add("chat-fullscreen"', app)

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



class ReadMarkLinkage(unittest.TestCase):
    """The read mark is the server's (#380): the chat pane moves it, the
    sidebar shows the counts it answers, and no browser keeps one of its
    own any more, or a chat read on one device stays unread on another."""

    def test_the_chat_moves_the_mark_and_hands_the_counts_to_the_sidebar(self):
        chat = (_STATIC / "chat.jsx").read_text()
        app = (_STATIC / "app.jsx").read_text()
        self.assertIn('"/api/chat/read"', chat)
        self.assertIn('"fw-chat-unread"', chat)
        self.assertIn('"fw-chat-unread"', app)
        self.assertIn('"/api/chat/unread"', app)
        self.assertIn('kind === "chat-read"', app)

    def test_no_browser_keeps_a_seen_time_of_its_own(self):
        chat = (_STATIC / "chat.jsx").read_text()
        self.assertNotIn("chatSeenKey(", chat)
        self.assertNotIn("fw_chat_seen_", chat)
        app = (_STATIC / "app.jsx").read_text()
        # app.jsx only reads the old key, once, to move it to the server
        body = app.split("const renderCousin", 1)[1].split("return (", 1)[0]
        self.assertNotIn("localStorage", body)


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
        self.assertIn("scrollToBottom", body)

    def test_a_frame_repaints_in_one_write_without_a_blank(self):
        # reset() blanked the screen and the write landed a render
        # later: a busy pane flickered twice a second.
        start = self.pane.index("const applyFrame = ")
        body = self.pane[start:self.pane.index("};", start)]
        self.assertNotIn("term.reset()", body)
        self.assertIn("term.write(FRAME_PREFIX + text", body)
        src = (_STATIC / "chat.jsx").read_text()
        prefix = src[src.index("const FRAME_PREFIX"):]
        prefix = prefix[:prefix.index(";")]
        for code in ("[2J", "[3J", "[H", "[0m", "[?1006l"):
            self.assertIn(code, prefix)

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


class ChatBesideThePane(unittest.TestCase):
    """The chat and the pane side by side: the
    runner's reasoning stream or the tmux terminal sits beside the chat,
    never in its place, on a screen wide enough for both; the interrupt
    is on the stream's side."""

    def setUp(self):
        self.chat = (_STATIC / "chat.jsx").read_text()
        self.css = (_STATIC / "styles.css").read_text()

    def test_both_columns_share_one_row(self):
        view = _function_body(self.chat, "ChatView")
        self.assertIn('className={"chat-split" + (paneShown ? " pane-open" : "") + (chatHidden ? " chat-collapsed" : "")}', view)
        self.assertIn('className="chat-col"', view)
        self.assertIn('className={`pane-col ${paneShown ? "open" : ""}`}', view)
        self.assertIn(".chat-split.pane-open .chat-col { flex: 1 1 0; }", self.css)
        # the pane takes the dragged share (--pane-w), half by default
        self.assertIn(".chat-split.pane-open .pane-col { flex: 0 0 var(--pane-w, 50%);", self.css)

    def test_the_split_is_dragged_kept_and_reset(self):
        view = _function_body(self.chat, "ChatView")
        self.assertIn('className="split-div" onPointerDown={onDividerDown} onDoubleClick={() => setPaneW(50)}', view)
        self.assertIn('style={{ "--pane-w": paneW + "%" }}', view)
        self.assertIn('localStorage.setItem("fw_pane_w"', view)
        self.assertIn("Math.min(80, Math.max(20,", view)
        # while dragging the columns do not animate, and a phone has no divider
        self.assertIn(".chat-split.dragging .chat-col, .chat-split.dragging .pane-col { transition: none; }", self.css)
        phone = self.css[self.css.index(".split-div { flex: none;"):]
        phone = phone[phone.index("@media (max-width: 820px)"):]
        self.assertIn(".split-div { display: none; }", phone[:phone.index("}\n}") + 3])

    def test_a_phone_still_gives_the_open_pane_the_width(self):
        phone = self.css[self.css.index("/* === Chat view (chat.jsx ChatView)"):]
        phone = phone[phone.index("@media (max-width: 820px)"):]
        self.assertIn(".chat-split.pane-open .chat-col { flex: 0 0 0; }", phone)

    def test_the_pane_opens_by_default_where_both_fit(self):
        view = _function_body(self.chat, "ChatView")
        self.assertIn('localStorage.getItem("fw_pane_side")', view)
        self.assertIn("window.innerWidth > 1100", view)

    def test_the_interrupt_is_in_the_streams_foot(self):
        pane = self.chat[self.chat.index("function RunnerPaneView("):]
        pane = pane[:pane.index("\n}\n")]
        foot = pane[pane.index('className="pane-foot"'):]
        self.assertIn("onClick={interrupt}", foot)
        self.assertIn("onClick={say}", foot)


class ChatMediaViewer(unittest.TestCase):
    """The media port (docs/reference/console-api.md, "The chat media viewer"):
    the on/off toggle and its browser key, kind detection, the inline
    players and the viewer with its keys, counter and links."""

    def setUp(self):
        self.media = (_STATIC / "media.jsx").read_text()
        self.chat = (_STATIC / "chat.jsx").read_text()
        self.css = (_STATIC / "styles.css").read_text()

    def test_index_loads_media_before_chat(self):
        html = (_STATIC / "index.html").read_text()
        at = html.index('<script type="text/babel" src="media.jsx"></script>')
        self.assertLess(at, html.index('src="chat.jsx"'),
                        "chat.jsx reads media.jsx's names off window")

    def test_toggle_key_and_default_on(self):
        self.assertIn('const MEDIA_PREF_KEY = "fw_chat_media";', self.media)
        read = _function_body(self.media, "readMediaShown")
        # only an explicit "0" hides; absent or unreadable storage shows
        self.assertIn('localStorage.getItem(MEDIA_PREF_KEY) !== "0"', read)
        self.assertRegex(read, r"catch \(e\) \{ return true; \}")
        write = _function_body(self.media, "writeMediaShown")
        self.assertIn("try {", write)
        self.assertIn('localStorage.setItem(MEDIA_PREF_KEY, on ? "1" : "0")', write)

    def test_header_toggle_reads_and_writes_the_preference(self):
        view = _function_body(self.chat, "ChatView")
        self.assertIn("React.useState(() => readMediaShown())", view)
        self.assertIn("writeMediaShown(mediaShown)", view)
        header = _function_body(self.chat, "ChatHeader")
        self.assertIn("setMediaShown(v => !v)", header)
        self.assertIn('mediaShown ? "media on" : "media off"', header)

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
    def test_a_poll_keeps_the_rows_older_than_its_window(self):
        # #315: loadMore prepends older rows; the next poll answered only
        # the newest window and replaced them.
        probe = _function_body(self.chat, "mergePolled") + """
const older = [{id: 1}, {id: 2}];
const win = [{id: 3, t: "old"}, {id: 4}];
const cases = [
  mergePolled([...older, ...win], [{id: 3, t: "new"}, {id: 4}, {id: 5}]),
  mergePolled([...older, ...win], [{id: 4}, {id: 5}]),
  mergePolled(win, [{id: 3}, {id: 4}]),
  mergePolled([...older, ...win], []),
].map(rows => rows.map(r => r.t ? r.id + r.t : r.id));
process.stdout.write(JSON.stringify(cases));
"""
        out = subprocess.run(["node", "-e", probe], capture_output=True,
                             text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout), [
            [1, 2, "3new", 4, 5],      # the window's rows are the poll's
            [1, 2, "3old", 4, 5],      # a row pushed out of the window stays
            [3, 4],
            [],                        # an empty answer is the poll's word
        ])

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_a_change_poll_updates_drops_and_appends(self):
        # #316: a poll answers only the rows changed since its cursor.
        probe = _function_body(self.chat, "mergeChanged") + """
const pane = [{id: 3}, {id: 5, t: "old"}, {id: 7}];
const live = (id, t, archived) => ({id, t, archived: archived ? 1 : 0});
const show = rows => rows.map(r => r.t ? r.id + r.t : r.id);
const same = mergeChanged(pane, [], [], false);
const cases = [
  show(mergeChanged(pane, [live(5, "new")], [], false)),
  show(mergeChanged(pane, [live(9, "new")], [], false)),
  show(mergeChanged(pane, [live(3, "", true), live(5, "", true)], [], false)),
  show(mergeChanged(pane, [], [7], false)),
  show(mergeChanged(pane, [live(1, "unseen")], [], false)),
  show(mergeChanged(pane, [live(4, "arch", true)], [], true)),
  show(mergeChanged([], [live(2, "a"), live(4, "b", true)], [], false)),
  same === pane,
];
process.stdout.write(JSON.stringify(cases));
"""
        out = subprocess.run(["node", "-e", probe], capture_output=True,
                             text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout), [
            [3, "5new", 7],            # a changed row is replaced in place
            [3, "5old", 7, "9new"],    # a new row lands after the pane
            [7],                       # archived rows leave the live view
            [3, "5old"],               # a deleted row leaves
            [3, "5old", 7],            # a row never paged in stays out
            [3, "4arch", "5old", 7],   # the archived view takes archived rows
            ["2a"],
            True,                      # nothing changed: same array, no render
        ])

    def test_the_poll_asks_for_changes_once_the_store_gives_a_cursor(self):
        body = _function_body(self.chat, "ChatBody")
        self.assertIn('rev = !searchActive && typeof d.rev === "number" ? d.rev : null;', body)
        self.assertIn("rev != null ? `${base}&changed=${rev}` : base", body)
        self.assertIn("if (asked && d.reset) {", body)
        self.assertIn("if (asked) setMessages(prev => mergeChanged(prev, list, d.gone || [], showArchived));", body)

    def test_the_first_pull_replaces_and_later_polls_merge(self):
        body = _function_body(self.chat, "ChatBody")
        self.assertIn("if (searchActive || fresh) setMessages(list);", body)
        self.assertIn("else setMessages(prev => mergePolled(prev, list));", body)

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
        for attr in ("muted", "loop", "playsInline"):
            self.assertIn(attr, tag)
        self.assertNotIn("controls", tag, "the inline preview is silent, the viewer has the controls")
        self.assertIn("IntersectionObserver", preview)
        # #314: no clip loads before it nears the view, or a long thread
        # queues every one of them ahead of the newest
        self.assertIn("src={near ? src : undefined}", tag)
        self.assertIn('preload={near ? "metadata" : "none"}', tag)
        self.assertIn("useState(!canObserve)", preview, "no observer: load at once")
        self.assertIn('closest(".chat-scroller")', preview)
        self.assertIn("rootMargin", preview)
        self.assertIn("visible.current", preview, "a clip already on screen plays once loaded")
        self.assertIn('className="chat-scroller"', self.chat)
        inline = _function_body(self.media, "InlineMedia")
        self.assertIn("[{media.kind} hidden]", inline)
        self.assertRegex(inline, r"<audio[^>]*controls")
        self.assertRegex(inline, r'<audio[^>]*preload="none"')
        bubble = _function_body(self.chat, "ChatBubble")
        self.assertIn("attachmentMedia(msg)", bubble)
        self.assertIn("<InlineMedia", bubble)
        self.assertIn("mediaShown", bubble)

    def test_viewer_handles_keys_counter_and_links(self):
        viewer = _function_body(self.media, "MediaViewer")
        for key in ('"Escape"', '"ArrowLeft"', '"ArrowRight"'):
            self.assertIn(key, viewer)
        self.assertIn('addEventListener("keydown"', viewer)
        self.assertIn('removeEventListener("keydown"', viewer)
        self.assertIn("{index + 1} / {count}", viewer)
        self.assertRegex(viewer, r"<video[^>]*controls[^>]*autoPlay")
        self.assertIn("onClick={onClose}", viewer, "a click outside closes")
        self.assertIn("media-viewer-prev", viewer)
        self.assertIn("media-viewer-next", viewer)
        self.assertRegex(viewer, r'href=\{current\.src\}[^>]*target="_blank"')
        self.assertRegex(viewer, r"href=\{current\.src\} download")
        # A ref callback re-runs on every poll render and would restart a
        # video the reader paused; the start is an effect keyed on the item.
        self.assertNotRegex(viewer, r"ref=\{\(el\)")
        self.assertIn("[currentSrc]", viewer)

    def test_chat_body_owns_the_viewer_and_snapshots_the_gallery(self):
        body = _function_body(self.chat, "ChatBody")
        self.assertIn("threadMedia(messages)", body)
        self.assertIn("<MediaViewer", body)
        self.assertIn("onOpenMedia={openMedia}", body)

    def test_viewer_styles_exist_with_a_phone_layout(self):
        for sel in (".media-viewer {", ".media-viewer-item {", ".media-viewer-bar {",
                    ".chat-media-hidden {", ".chat-media-audio {"):
            self.assertIn(sel, self.css)
        phone = self.css[self.css.rindex("@media (max-width: 540px)"):]
        self.assertIn(".media-viewer-item", phone)


class MessageActions(unittest.TestCase):
    """A mouse right-click on a message is the browser's own menu (copy a
    selection, a link, an image); Reply, Copy and the reactions open from
    the bubble's hover button, or from a touch long-press on a phone."""

    def setUp(self):
        self.chat = (_STATIC / "chat.jsx").read_text()
        self.ui = (_STATIC / "ui.jsx").read_text()
        self.css = (_STATIC / "styles.css").read_text()
        self.bubble = _function_body(self.chat, "ChatBubble")

    def _node(self, probe):
        out = subprocess.run(["node", "-e", probe], capture_output=True,
                             text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_only_a_touch_long_press_takes_the_context_menu(self):
        probe = _function_body(self.chat, "contextMenuFromTouch") + """
process.stdout.write(JSON.stringify([
  contextMenuFromTouch({pointerType: "mouse"}, 0, 100000),
  contextMenuFromTouch({pointerType: "touch"}, 0, 100000),
  contextMenuFromTouch({pointerType: "pen"}, 0, 100000),
  contextMenuFromTouch({pointerType: "mouse"}, 99500, 100000),
  contextMenuFromTouch({}, 99500, 100000),
  contextMenuFromTouch({}, 1000, 100000),
  contextMenuFromTouch(null, 99900, 100000),
]));
"""
        self.assertEqual(self._node(probe), [False, True, False, False, True, False, True])

    def test_the_bubble_leaves_a_mouse_right_click_to_the_browser(self):
        handler = self.bubble[self.bubble.index("const onContextMenu"):]
        handler = handler[:handler.index("\n  };") + 5]
        self.assertIn("contextMenuFromTouch(e.nativeEvent, lastTouchAt.current, Date.now())",
                      handler)
        self.assertLess(handler.index("return;"), handler.index("e.preventDefault()"))
        self.assertIn("lastTouchAt.current = Date.now();", self.bubble)
        self.assertIn("onTouchStart={startLongPress}", self.bubble)

    def test_the_hover_button_opens_the_menu(self):
        self.assertRegex(self.bubble, r'className="chat-msg-more" aria-label="Message actions"'
                                      r'\s+title="[^"]+" onClick=\{onMoreClick\}')
        more = self.bubble[self.bubble.index("const onMoreClick"):]
        more = more[:more.index("\n  };")]
        # its click reaches the document (closing any open menu, this one
        # too) and the menu opens after it, never closed by its own click
        self.assertNotIn("stopPropagation", more)
        self.assertIn("if (menu) return;", more)
        self.assertIn("setTimeout(() => setMenu({ x, y: r.bottom + 4 }), 0);", more)
        self.assertIn('document.addEventListener("click", close);', self.bubble)
        self.assertIn("position: relative;", _css_block(self.css, ".chat-bubble"))
        self.assertIn("opacity: 0;", _css_block(self.css, ".chat-msg-more"))
        self.assertRegex(self.css, r"@media \(hover: hover\) \{\s*\.chat-bubble:hover "
                                   r"\.chat-msg-more \{ opacity: 1; \}")
        self.assertRegex(self.css, r"@media \(hover: none\) \{\s*\.chat-msg-more "
                                   r"\{ display: none; \}")

    def test_the_menu_copies_the_whole_message(self):
        menu = self.bubble[self.bubble.index("{menu && ("):]
        self.assertRegex(menu, r"\{msg\.message && \(\s*<button onClick=\{copyMessage\}")
        self.assertIn("&#10697; Copy", menu)
        self.assertIn('await copyText(msg.message || "")', self.bubble)
        self.assertIn('{copied && <span className="chat-copied">{copied}</span>}', self.bubble)

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_copy_text_uses_the_clipboard_api_or_falls_back_by_hand(self):
        probe = _function_body(self.ui, "copyText") + """
const calls = [];
function page(secure, clipboard) {
  globalThis.window = {isSecureContext: secure};
  Object.defineProperty(globalThis, "navigator", {configurable: true, writable: true,
    value: {clipboard: clipboard && {writeText: (t) => {
      calls.push(["api", t]);
      return clipboard === "ok" ? Promise.resolve() : Promise.reject(new Error("denied"));
    }}}});
  globalThis.document = {
    body: {appendChild() {}, removeChild() {}},
    createElement: () => ({style: {}, setAttribute() {},
                           select() { calls.push(["select", this.value]); }}),
    execCommand: (c) => { calls.push(["exec", c]); return true; },
  };
}
(async () => {
  const out = [];
  page(true, "ok"); out.push(await copyText("a"), calls.splice(0));
  page(false, "ok"); out.push(await copyText("b"), calls.splice(0));
  page(true, "denied"); out.push(await copyText("c"), calls.splice(0));
  page(true, null); out.push(await copyText("d"), calls.splice(0));
  process.stdout.write(JSON.stringify(out));
})();
"""
        self.assertEqual(self._node(probe), [
            True, [["api", "a"]],
            True, [["select", "b"], ["exec", "copy"]],
            True, [["api", "c"], ["select", "c"], ["exec", "copy"]],
            True, [["select", "d"], ["exec", "copy"]],
        ])

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_a_long_press_menu_never_sits_over_the_message(self):
        probe = _function_body(self.chat, "placeTouchMenu") + """
process.stdout.write(JSON.stringify([
  placeTouchMenu({top: 400, bottom: 500}, 150, 800, 450),   // room above
  placeTouchMenu({top: 100, bottom: 200}, 150, 800, 150),   // none above, room below
  placeTouchMenu({top: -100, bottom: 900}, 150, 800, 600),  // taller than the screen,
  placeTouchMenu({top: -100, bottom: 900}, 150, 800, 200),  // away from the finger
  placeTouchMenu({top: -10, bottom: 200}, 150, 100, 20),    // a screen smaller than the menu
]));
"""
        self.assertEqual(self._node(probe), [242, 208, 8, 642, 8])

    def test_a_long_press_opens_the_menu_beside_the_bubble_and_keeps_selection(self):
        handler = self.bubble[self.bubble.index("const onContextMenu"):]
        handler = handler[:handler.index("\n  };") + 5]
        # text keeps the phone's own selection; a link or a picture is held back
        self.assertIn('if (e.target && e.target.closest && e.target.closest("a, img, video"))'
                      ' e.preventDefault();', handler)
        self.assertIn("openMenuAtBubble(e.clientY);", handler)
        self.assertIn("openMenuAtBubble(t ? t.clientY : 0);", self.bubble)
        self.assertNotIn("setMenu({ x: t ? t.clientX", self.bubble)
        self.assertIn("ref={bubbleRef}", self.bubble)
        self.assertIn("<div ref={menuRef} onClick={e => e.stopPropagation()}", self.bubble)
        place = self.bubble[self.bubble.index("React.useLayoutEffect(() => {"):]
        place = place[:place.index("}, [menu]);")]
        self.assertIn("placed: true", place)
        # measured at full width: a fixed box at the bubble's left on the
        # right of a phone would shrink to the room left and wrap tall
        self.assertIn("setMenu({ x: 8, y: r.top, touchY, placed: false,", self.bubble)
        # the bottom is the visible one, above an open keyboard
        self.assertIn("const viewH = vv ? vv.offsetTop + vv.height : window.innerHeight;", place)
        self.assertIn("placeTouchMenu(a, box.height, viewH, menu.touchY)", place)

    def test_the_command_copy_button_shares_the_helper(self):
        button = _function_body((_STATIC / "cousins.jsx").read_text(), "CopyButton")
        self.assertIn("await copyText(text)", button)
        self.assertNotIn("execCommand", button)


if __name__ == "__main__":
    unittest.main()
