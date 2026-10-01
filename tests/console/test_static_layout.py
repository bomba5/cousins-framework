"""The chat and its pane laid out, pinned by text (the console compiles its
JSX in the browser; there is no build step to test against): the compact
header on a narrow chat column, the status line that never wraps, the
divider's snap that collapses the chat under the pane and the say
box's "send as chat" while it is collapsed. The pure helpers run under
node, lifted out of chat.jsx as they are.
"""
import json
import pathlib
import re
import shutil
import subprocess
import unittest

_STATIC = pathlib.Path(__file__).resolve().parents[2] / "cousin_lib" \
    / "console_static"


def _read(name):
    return (_STATIC / name).read_text(encoding="utf-8")


def _function(text, name):
    """One top-level `function name(` up to the next top-level function."""
    start = text.index("function %s(" % name)
    nxt = re.search(r"^(async )?function \w+\(", text[start + 1:], re.M)
    return text[start:start + 1 + nxt.start()] if nxt else text[start:]


def _outside_media(css):
    """The stylesheet less every @media block: the rules every width gets."""
    out, i = [], 0
    for m in re.finditer(r"@media [^{]*\{", css):
        if m.start() < i:
            continue
        out.append(css[i:m.start()])
        depth, j = 1, m.end()
        while depth and j < len(css):
            depth += {"{": 1, "}": -1}.get(css[j], 0)
            j += 1
        i = j
    out.append(css[i:])
    return "".join(out)


class CompactHeaderOnANarrowColumn(unittest.TestCase):
    """The one-row header follows the chat column's width (a ResizeObserver
    on the column), not only the phone's viewport."""

    def setUp(self):
        self.chat = _read("chat.jsx")
        self.view = _function(self.chat, "ChatView")
        self.header = _function(self.chat, "ChatHeader")
        self.css = _read("styles.css")

    def test_the_threshold(self):
        self.assertIn("const CHAT_COMPACT_PX = 740;", self.chat)

    def test_the_column_is_measured_with_a_resize_observer(self):
        ui = _read("ui.jsx")
        hook = _function(ui, "useNarrowerThan")
        self.assertIn("new window.ResizeObserver(", hook)
        self.assertIn("setNarrow(w > 0 && w < px)", hook)
        self.assertRegex(ui, r"Object\.assign\(window, \{[^}]*useNarrowerThan")
        self.assertIn("useNarrowerThan(CHAT_COMPACT_PX)", self.view)
        self.assertIn('<div className="chat-col" ref={chatColRef}', self.view)
        self.assertIn("narrow={narrowCol} />", self.view)

    def test_compact_on_a_phone_or_a_narrow_column_never_in_an_embed(self):
        self.assertIn(
            "const compact = ((window.useMobileLayout ? useMobileLayout() : false)"
            " || !!narrow) && !embed;", self.header)
        self.assertIn("if (compact) {", self.header)
        self.assertIn('<div className="chat-header ch-mobile">', self.header)
        # the wide desktop return comes after the compact branches
        self.assertLess(self.header.index("if (compact) {"),
                        self.header.index('<div className="chat-header">'))

    def test_the_compact_row_is_styled_at_every_width(self):
        wide = _outside_media(self.css)
        self.assertIn(".chat-header.ch-mobile { flex-wrap: nowrap;", wide)
        self.assertRegex(wide, r"\.chat-header\.ch-mobile \.btn\.ch-icon \{[^}]*width: 36px;[^}]*height: 36px;")
        self.assertRegex(wide, r"\.ch-menu \{[^}]*position: absolute;")
        # the wide header's own rule is untouched
        self.assertIn(".chat-header {\n  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;", wide)


class StatusLineNeverWraps(unittest.TestCase):
    def test_the_desktop_status_is_one_line_cut_with_an_ellipsis(self):
        wide = _outside_media(_read("styles.css"))
        rule = re.search(r"\.main-header \.hdr-meta:not\(\.hdr-meta-m\) \{([^}]*)\}", wide)
        self.assertIsNotNone(rule)
        for decl in ("min-width: 0;", "overflow: hidden;", "text-overflow: ellipsis;",
                     "white-space: nowrap;", "display: block;"):
            self.assertIn(decl, rule.group(1), decl)
        # the path gives way too
        self.assertRegex(wide, r"\.main-header \.path \{[^}]*text-overflow: ellipsis;")


class SnapCollapsesTheChat(unittest.TestCase):
    """The divider still drags; let go near the chat's left edge and
    the chat collapses under the pane, a strip brings it back at its last
    width; near the right edge the pane closes. Kept per browser."""

    def setUp(self):
        self.chat = _read("chat.jsx")
        self.view = _function(self.chat, "ChatView")
        self.css = _read("styles.css")

    def test_the_drag_snaps_at_either_edge(self):
        self.assertIn("const SPLIT_SNAP_PX = 40;", self.chat)
        down = self.view[self.view.index("const onDividerDown"):self.view.index("window.addEventListener(\"pointerup\", up);")]
        self.assertIn("Math.min(80, Math.max(20,", down)   # the drag as it was
        self.assertIn("snap = splitSnap(m.clientX, r.left, r.right);", down)
        self.assertIn("const startW = paneW;", down)
        self.assertIn("if (snap) setPaneW(startW);", down)
        self.assertIn('if (snap === "chat") setChatCollapsed(true);', down)
        self.assertIn('if (snap === "pane") setPaneOpen(false);', down)

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_split_snap_behaves(self):
        src = self.chat[self.chat.index("const SPLIT_SNAP_PX"):self.chat.index("// POST /api/chat/send")]
        out = subprocess.run(["node", "-e", src + """
process.stdout.write(JSON.stringify([
  splitSnap(100, 100, 900), splitSnap(140, 100, 900), splitSnap(141, 100, 900),
  splitSnap(500, 100, 900), splitSnap(859, 100, 900), splitSnap(860, 100, 900),
  splitSnap(950, 100, 900), splitSnap(10, 100, 100), splitSnap(120, 100, 900, 10),
]));"""], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout),
                         ["chat", "chat", None, None, None, "pane", "pane", None, None])

    def test_the_collapse_and_the_last_width_are_kept(self):
        self.assertIn('localStorage.getItem("fw_chat_collapsed") === "1"', self.view)
        self.assertIn('localStorage.setItem("fw_chat_collapsed", chatCollapsed ? "1" : "0")', self.view)
        # the last width is the split's own key, left alone by the collapse
        self.assertIn('localStorage.setItem("fw_pane_w"', self.view)
        self.assertNotIn("setPaneW(0", self.view)

    def test_collapsed_only_under_an_open_pane_and_a_strip_restores(self):
        self.assertIn("const chatHidden = paneShown && chatCollapsed;", self.view)
        self.assertIn('(chatHidden ? " chat-collapsed" : "")', self.view)
        self.assertIn("{paneShown && !chatHidden && (", self.view)
        restore = self.view[self.view.index("{chatHidden && ("):]
        restore = restore[:restore.index(")}") + 2]
        self.assertIn('className="chat-restore"', restore)
        self.assertIn("onClick={() => setChatCollapsed(false)}", restore)
        # closing the pane brings the chat back
        self.assertIn("const closePane = () => { setPaneOpen(false); setChatCollapsed(false); };", self.view)
        self.assertIn("onClose={closePane}", self.view)

    def test_the_styles(self):
        wide = _outside_media(self.css)
        self.assertIn(".chat-split.chat-collapsed .chat-col { flex: 0 0 0; visibility: hidden; }", wide)
        self.assertIn(".chat-split.chat-collapsed .pane-col { flex: 1 1 auto;", wide)
        self.assertRegex(wide, r"\.chat-restore \{[^}]*width: 14px;")
        self.assertIn(".chat-split.dragging iframe { pointer-events: none; }", wide)
        self.assertIn(".chat-split.snap-chat .chat-col {", wide)

    def test_a_chat_plugin_strip_collapses_with_the_chat(self):
        col = self.view[self.view.index('<div className="chat-col"'):self.view.index("{paneShown && !chatHidden && (")]
        self.assertIn("<PluginChatStrips slug={c.slug} tabs={chatStrips} />", col)


class SendAsChatWhileCollapsed(unittest.TestCase):
    """While the chat is collapsed the say box can post a stored chat
    message, through the composer's own send; otherwise it only says."""

    def setUp(self):
        self.chat = _read("chat.jsx")
        self.pane = _function(self.chat, "RunnerPaneView")

    def test_the_toggle_renders_only_while_collapsed(self):
        self.assertIn("function RunnerPaneView({ cousin, onClose, chatUser, chatHidden }) {", self.chat)
        toggle = self.pane[self.pane.index("{chatHidden && ("):]
        toggle = toggle[:toggle.index("</label>")]
        self.assertIn('className="rp-as-chat"', toggle)
        self.assertIn('type="checkbox" checked={asChat}', toggle)
        self.assertIn("send as chat", toggle)
        self.assertEqual(self.pane.count('className="rp-as-chat"'), 1)
        self.assertIn("const sendAsChat = asChat && !!chatHidden;", self.pane)
        self.assertIn("chatUser={chatUser} chatHidden={chatHidden} />", _function(self.chat, "ChatView"))

    def test_send_as_chat_posts_as_the_composer_does(self):
        say = self.pane[self.pane.index("const say = async () => {"):]
        say = say[:say.index("\n  };\n")]
        self.assertLess(say.index("if (sendAsChat) {"), say.index("/say`"))
        self.assertIn("postChatMessage({ cousin: slug, user: chatUser, message: text })", say)
        self.assertIn("const res = await postChatMessage(body);", _function(self.chat, "ChatBody"))
        self.assertEqual(self.chat.count('fetch("/api/chat/send"'), 1)

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_post_chat_message_behaves(self):
        src = "async " + _function(self.chat, "postChatMessage")
        out = subprocess.run(["node", "-e", src + """
const calls = [];
const reply = [{ok: true, status: 200, body: {ok: true, id: 7}},
               {ok: false, status: 500, body: {error: "boom"}},
               {ok: true, status: 200, body: {ok: false}}];
global.fetch = async (url, init) => { calls.push([url, init.method, JSON.parse(init.body)]);
  const r = reply.shift(); return {ok: r.ok, status: r.status, json: async () => r.body}; };
(async () => {
  const got = [];
  for (let i = 0; i < 3; i++) got.push(await postChatMessage({cousin: "wren", user: "sam", message: "hi"}));
  process.stdout.write(JSON.stringify([got, calls[0]]));
})();"""], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        got, call = json.loads(out.stdout)
        self.assertEqual(got, [{"ok": True, "data": {"ok": True, "id": 7}},
                               {"ok": False, "error": "boom"},
                               {"ok": False, "error": "HTTP 200"}])
        self.assertEqual(call, ["/api/chat/send", "POST",
                                {"cousin": "wren", "user": "sam", "message": "hi"}])

    def test_the_label_and_placeholder_follow_the_toggle(self):
        self.assertIn('placeholder={sendAsChat ? `message @${slug}` : "say to the running turn"}', self.pane)
        self.assertIn('{sendAsChat ? "send" : "say"}', self.pane)


if __name__ == "__main__":
    unittest.main()
