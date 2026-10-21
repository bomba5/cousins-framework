"""The console keeps its view and cousin in the address, pinned by text
(the page compiles its JSX in the browser and has no build step).

The failure it holds off: every reload landed on Overview, so a phone
app whose WebView was restored after the system reclaimed it lost the
chat you had open.
"""
import pathlib
import re
import unittest

_APP = pathlib.Path(__file__).resolve().parents[2] / "cousin_lib" \
    / "console_static" / "app.jsx"


class ViewInAddress(unittest.TestCase):

    def setUp(self):
        src = _APP.read_text(encoding="utf-8")
        m = re.search(r"useEffectApp\(\(\) => \{\n(?:(?!\n  \}, \[).)*"
                      r"replaceState(?:(?!\n  \}, \[).)*\n  \}, \[([^\]]*)\]\);",
                      src, re.S)
        self.assertIsNotNone(m, "no effect writes the address")
        self.effect, self.deps = m.group(0), m.group(1)

    def test_follows_view_and_cousin(self):
        self.assertEqual([d.strip() for d in self.deps.split(",")],
                         ["view", "activeCousin"])
        self.assertIn('p.set("view", view)', self.effect)
        self.assertIn('p.set("cousin", activeCousin)', self.effect)

    def test_replaces_never_pushes(self):
        self.assertNotIn("pushState", self.effect)

    def test_keeps_the_other_parameters(self):
        # it starts from the current query, so ?user= and ?embed= survive
        self.assertIn("new URLSearchParams(window.location.search)", self.effect)


class SystemTabsInAddress(unittest.TestCase):
    """Loops, Settings and Accounts are System tabs since 3.57.0 (#383): an
    old address for one of them lands on its tab, the tab rides in the
    address while System is open, and their shortcuts still work."""

    def setUp(self):
        self.app = _APP.read_text(encoding="utf-8")
        self.system = (_APP.parent / "system.jsx").read_text(encoding="utf-8")

    def _function(self, src, name):
        start = src.index("function %s(" % name)
        return src[start:src.index("\n}\n", start)]

    def test_the_moved_views_left_the_sidebar(self):
        nav = self.app[self.app.index("const NAV = ["):self.app.index("];", self.app.index("const NAV = ["))]
        for vid in ("loops", "settings", "accounts"):
            self.assertNotIn('id: "%s"' % vid, nav)
        self.assertIn('const SYSTEM_TAB_OF = { loops: "loops", settings: "settings", accounts: "accounts" };',
                      self.app)

    def test_an_old_address_lands_on_its_tab_in_place(self):
        body = self._function(self.app, "startView")
        self.assertIn("SYSTEM_TAB_OF[asked]", body)
        self.assertIn('p.set("view", "system")', body)
        self.assertIn('p.set("tab", tab)', body)
        self.assertIn("replaceState", body)
        self.assertNotIn("pushState", body)
        self.assertIn("useStateApp(startView)", self.app)

    def test_the_tab_leaves_the_address_with_system(self):
        self.assertIn('if (view !== "system") p.delete("tab");', self.app)

    def test_the_shortcuts_open_the_tabs(self):
        self.assertIn('const SYSTEM_TAB_KEYS = { "5": "loops", "0": "settings" };', self.app)
        self.assertIn("openSystemTab(tab, setView)", self.app)
        body = self._function(self.app, "openSystemTab")
        self.assertIn('"fw-system-tab"', body)
        self.assertIn('"fw-system-tab"', self.system)

    def test_system_reads_and_writes_its_tab_in_the_address(self):
        start = self._function(self.system, "startSystemTab")
        self.assertIn('.get("tab")', start)
        view = self._function(self.system, "SystemView")
        self.assertIn('p.set("tab", t)', view)
        self.assertIn("replaceState", view)
        self.assertNotIn("pushState", view)


class MeetingInAddress(unittest.TestCase):
    """The open meeting follows the selection, so a reload reopens it."""

    def test_selected_meeting_written_back(self):
        src = (_APP.parent / "meetings.jsx").read_text(encoding="utf-8")
        m = re.search(r"useEffect\(\(\) => \{\n(?:(?!\n  \}, \[).)*"
                      r"replaceState(?:(?!\n  \}, \[).)*\n  \}, \[selectedId\]\);",
                      src, re.S)
        self.assertIsNotNone(m, "no effect writes ?meeting=")
        self.assertIn('p.set("meeting", String(selectedId))', m.group(0))
        self.assertNotIn("pushState", m.group(0))


if __name__ == "__main__":
    unittest.main()
