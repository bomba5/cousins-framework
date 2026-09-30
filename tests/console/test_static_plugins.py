"""The console's plugin UI (plugins.jsx, the pane strip in chat.jsx). Pinned
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
        for name in ("pluginTabs", "PluginPaneTabs", "PluginFrame", "PluginsPanel"):
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
        self.assertIn('<RunnerPaneView key={c.slug} cousin={c} onClose={() => setPaneOpen(false)} />',
                      view)
        strip = _component(_read("plugins.jsx"), "PluginPaneTabs")
        self.assertIn("if (!tabs || !tabs.length) return null;", strip)

    def test_a_tab_is_a_same_origin_iframe_of_the_proxy(self):
        frame = _component(_read("plugins.jsx"), "PluginFrame")
        self.assertIn("<iframe", frame)
        self.assertIn("src={tab.url}", frame)
        self.assertIn(".plugin-frame", _read("styles.css"))


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
                                 "url": "/plugins/clock/page/wren"}]])

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
