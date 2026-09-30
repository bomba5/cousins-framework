"""The console's plugin UI (plugins.jsx, the pane tabs and the chat strips
in chat.jsx). Pinned
by text, and the pure helpers run under node. The operator's rule: an
install with no plugin shows no plugin UI at all, no tab, no empty strip,
no empty "plugins" section; a tab only for a plugin that is installed,
valid, enabled in config/plugins.toml, enabled on the cousin, and has a
[console] page (the server's row decides all but the last; the route tests
pin that side)."""
import json
import pathlib
import re
import shutil
import subprocess
import unittest

_STATIC = pathlib.Path(__file__).resolve().parents[2] / "cousin_lib" / "console_static"


def _read(name):
    return (_STATIC / name).read_text(encoding="utf-8")


def _component(text, name):
    start = text.index("function %s(" % name)
    rest = text[start + 1:]
    m = re.search(r"^(function \w+\(|class \w+ |const \w+ = |registerSlot\()", rest, re.M)
    return text[start:start + 1 + (m.start() if m else len(rest))]


class Wiring(unittest.TestCase):
    def test_the_file_is_in_the_package_block(self):
        html = _read("index.html")
        tag = '<script type="text/babel" src="plugins.jsx"></script>'
        self.assertIn(tag, html)
        self.assertTrue(html.index('src="meetings.jsx"') < html.index(tag)
                        < html.index('src="app.jsx"'))

    def test_it_publishes_its_parts(self):
        js = _read("plugins.jsx")
        for name in ("pluginTabs", "PluginPaneTabs", "PluginFrame", "PluginsPanel",
                     "pluginStripKeys", "pluginStripClamp", "PluginChatStrip",
                     "PluginChatStrips"):
            self.assertRegex(js, r"Object\.assign\(window, \{[^}]*\b%s\b" % name, name)

    def test_the_settings_toggle_is_an_inspector_entry_writing_the_route(self):
        js = _read("plugins.jsx")
        self.assertRegex(js, r'registerSlot\("inspector\.lane", \{ id: "plugins"')
        panel = _component(js, "PluginsPanel")
        self.assertIn("`/api/cousins/${c.slug}/plugins`", panel)
        self.assertIn('apiSend("POST", `/api/cousins/${c.slug}/plugins`', panel)
        self.assertIn('type="checkbox"', panel)
        self.assertIn("{ enabled:", panel)


class NoPluginsNoUi(unittest.TestCase):
    def test_the_panel_is_absent_without_an_installed_plugin(self):
        panel = _component(_read("plugins.jsx"), "PluginsPanel")
        guard = "if (c.remote || !data || !(data.available || []).length) return null;"
        self.assertIn(guard, panel)
        # the guard comes before anything is rendered
        self.assertLess(panel.index(guard), panel.index("<SectionLabel"))

    def test_the_strip_is_absent_without_a_tab(self):
        chat = _read("chat.jsx")
        view = _component(chat, "ChatView")
        self.assertIn("paneShown && pluginTabList.length > 0 && window.PluginPaneTabs", view)
        # with no active plugin tab the pane is exactly the 2.0.0 pane
        self.assertIn("paneShown && !activeTab && (c.runner", view)
        self.assertIn('<RunnerPaneView key={c.slug} cousin={c} onClose={closePane} chatUser={chatUser} chatHidden={chatHidden} />',
                      view)
        strip = _component(_read("plugins.jsx"), "PluginPaneTabs")
        self.assertIn("if (!tabs || !tabs.length) return null;", strip)

    def test_the_chat_strip_is_absent_without_a_chat_plugin(self):
        view = _component(_read("chat.jsx"), "ChatView")
        self.assertIn('const pluginTabList = pluginTabAll.filter(t => t.placement !== "chat");',
                      view)
        self.assertIn('const chatStrips = embed || c.remote ? []'
                      ' : pluginTabAll.filter(t => t.placement === "chat");', view)
        self.assertIn("chatStrips.length > 0 && window.PluginChatStrips", view)
        strips = _component(_read("plugins.jsx"), "PluginChatStrips")
        self.assertIn("if (!tabs || !tabs.length) return null;", strips)

    def test_without_a_plugin_the_chat_column_is_the_2_1_0_one(self):
        # the chat column less the one guarded strip line is, token for token,
        # the column 2.0.0 and 2.1.0 render
        view = _component(_read("chat.jsx"), "ChatView")
        col = view[view.index('<div className="chat-col"'):view.index("{paneShown && !chatHidden && (")]
        # less what the chat layout adds since: the column's measuring ref and collapse
        # (the compact header, #126) and the narrow flag it hands the header
        col = col.replace(' ref={chatColRef} aria-hidden={chatHidden || undefined}', '', 1)
        col = col.replace(' narrow={narrowCol} />', ' />', 1)
        strip = re.search(r"\{chatStrips\.length > 0 && window\.PluginChatStrips && \(\s*"
                          r"<PluginChatStrips slug=\{c\.slug\} tabs=\{chatStrips\} />\s*\)\}",
                          col)
        self.assertIsNotNone(strip)
        col = col[:strip.start()] + col[strip.end():]
        self.assertEqual(col.split(), _CHAT_COL_2_1_0.split())

    def test_a_tab_is_a_same_origin_iframe_of_the_proxy(self):
        frame = _component(_read("plugins.jsx"), "PluginFrame")
        self.assertIn("<iframe", frame)
        self.assertIn("src={tab.url}", frame)
        self.assertIn(".plugin-frame", _read("styles.css"))


class ChatStrip(unittest.TestCase):
    def setUp(self):
        self.strip = _component(_read("plugins.jsx"), "PluginChatStrip")

    def test_an_iframe_of_the_proxy_with_a_header_bar(self):
        self.assertIn("<iframe", self.strip)
        self.assertIn("src={tab.url}", self.strip)
        self.assertIn('className="plugin-strip-bar"', self.strip)
        self.assertIn("{tab.title}", self.strip)

    def test_collapse_pop_out_and_the_grip(self):
        self.assertIn("setCollapsed(v => !v)", self.strip)
        self.assertIn("aria-expanded={!collapsed}", self.strip)
        self.assertIn("window.open(tab.url,", self.strip)
        self.assertIn('className="plugin-strip-grip" onPointerDown={onGripDown}', self.strip)
        self.assertIn('closest(".chat-col")', self.strip)
        self.assertIn("pluginStripClamp(startH + m.clientY - startY, colH)", self.strip)

    def test_height_and_collapse_are_kept_per_cousin_and_plugin(self):
        self.assertIn("pluginStripKeys(slug, tab.name)", self.strip)
        for key in ("keys.height", "keys.collapsed"):
            self.assertIn("localStorage.getItem(%s)" % key, self.strip)
            self.assertIn("localStorage.setItem(%s," % key, self.strip)

    def test_the_styles_reuse_the_chat_look_and_keep_the_chat_below(self):
        css = _read("styles.css")
        for rule in (".plugin-strips {", ".plugin-strip {", ".plugin-strip-bar {",
                     ".plugin-strip-grip {", ".plugin-strip.collapsed .plugin-frame",
                     ".plugin-strip.dragging .plugin-frame { pointer-events: none; }"):
            self.assertIn(rule, css)
        block = css[css.index(".plugin-strips {"):]
        self.assertIn("max-height: 70%", block[:block.index("}")])

    def test_the_chat_keeps_its_tail_when_the_strip_resizes(self):
        body = _component(_read("chat.jsx"), "ChatBody")
        self.assertIn("new window.ResizeObserver(", body)
        self.assertIn("if (atBottomRef.current) el.scrollTop = el.scrollHeight;", body)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class PureHelpers(unittest.TestCase):
    def run_node(self, body):
        js = _read("plugins.jsx")
        start = js.index("// ---- pure helpers")
        end = js.index("// ---- end pure helpers")
        out = subprocess.run(["node", "-e", js[start:end] + body],
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_tabs_come_only_from_rows_with_a_console_tab(self):
        got = self.run_node("""
          const rows = [
            undefined, {}, { plugins: [] }, { plugins: null },
            { plugins: [{ name: "dial", tab: null }] },
            { plugins: [{ name: "clock", tab: { title: "Clock", url: "/plugins/clock/page/wren" } },
                        { name: "bell", tab: { title: "Bell", url: "https://elsewhere.example/" } }] },
          ];
          console.log(JSON.stringify(rows.map(pluginTabs)));
        """)
        self.assertEqual(got, [[], [], [], [], [],
                               [{"name": "clock", "title": "Clock",
                                 "url": "/plugins/clock/page/wren", "placement": "pane"}]])

    def test_a_tab_carries_its_placement_pane_by_default(self):
        got = self.run_node("""
          const row = { plugins: [
            { name: "bell", tab: { title: "Bell", url: "/plugins/bell/p", placement: "chat" } },
            { name: "clock", tab: { title: "Clock", url: "/plugins/clock/p", placement: "pane" } },
            { name: "dial", tab: { title: "Dial", url: "/plugins/dial/p" } },
            { name: "gong", tab: { title: "Gong", url: "/plugins/gong/p", placement: "side" } },
          ] };
          console.log(JSON.stringify(pluginTabs(row).map(t => [t.name, t.placement])));
        """)
        self.assertEqual(got, [["bell", "chat"], ["clock", "pane"], ["dial", "pane"],
                               ["gong", "pane"]])

    def test_the_strip_keys_are_per_cousin_and_plugin(self):
        got = self.run_node("""
          console.log(JSON.stringify([pluginStripKeys("wren", "bell"),
                                      pluginStripKeys("sam", "bell"),
                                      pluginStripKeys("wren", "clock")]));
        """)
        self.assertEqual(got[0], {"height": "fw_plugin_strip_h:wren:bell",
                                  "collapsed": "fw_plugin_strip_collapsed:wren:bell"})
        flat = [v for keys in got for v in keys.values()]
        self.assertEqual(len(flat), len(set(flat)))

    def test_the_height_stays_between_120_px_and_70_percent_of_the_column(self):
        got = self.run_node("""
          console.log(JSON.stringify([
            pluginStripClamp(300, 1000), pluginStripClamp(50, 1000), pluginStripClamp(900, 1000),
            pluginStripClamp(null, 1000), pluginStripClamp("", 1000), pluginStripClamp("x", 1000),
            pluginStripClamp("260", 0), pluginStripClamp(5000, 0), pluginStripClamp(300, 100),
          ]));
        """)
        self.assertEqual(got, [300, 120, 700, 240, 240, 240, 260, 5000, 120])

    def test_a_draft_differs_by_set_not_order(self):
        got = self.run_node("""
          console.log(JSON.stringify([
            pluginDraftChanged(["clock", "dial"], ["dial", "clock"]),
            pluginDraftChanged([], ["clock"]),
            pluginDraftChanged(["clock"], []),
            pluginDraftChanged(undefined, []),
          ]));
        """)
        self.assertEqual(got, [False, True, True, False])


# The chat column of ChatView as 2.0.0 and 2.1.0 have it (whitespace aside).
_CHAT_COL_2_1_0 = """
        <div className="chat-col">
          <ChatHeader cousin={c} chatUser={chatUser} paneOpen={paneShown} setPaneOpen={setPaneOpen} search={search} setSearch={setSearch} onArchive={onArchive} fullscreen={fullscreen} setFullscreen={embed ? null : setFullscreen} embed={embed} showArchived={showArchived} setShowArchived={setShowArchived} mediaShown={mediaShown} setMediaShown={setMediaShown} />
          {fullscreen && (
            <button className="chat-fullscreen-exit"
                    onClick={() => setFullscreen(false)}
                    title="exit fullscreen">x exit</button>
          )}
          {/* key by slug: remount ChatBody on cousin switch so its messages +
              draft state reset to empty. Without this React reuses the instance
              and the previous cousin's messages render until the new fetch lands
              -- a cross-cousin content leak between private chats. */}
          <ChatBody key={c.slug + "|" + chatUser} cousin={c} search={search} setSearch={setSearch} chatUser={chatUser} showArchived={showArchived} mediaShown={mediaShown} />
          {toast && <div className="chat-toast">{toast}</div>}
        </div>
"""
