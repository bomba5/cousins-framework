"""The console's registration seams (WP0 of the UI parity work): a later
package adds routes and UI from files of its own, never by editing a
shared one. Pinned by text, and the registry's pure part run under node:

- app.py PACKAGE_ROUTE_MODULES: one stub module per package, loaded like
  the built-in ones; index.html's package block: one stub jsx per package,
  after the shared files and before app.jsx.
- ui.jsx registerSlot / <Slot name>: named slots in the Inspector and the
  Settings view; registerView: a NAV entry and its view.
- ui.jsx SecretField: the write-only secret input, extracted from the
  Telegram token box and the auth key box, which now use it.
- ui.jsx useLongOp / <LongOpStatus>: a long operation's state, from GET
  /api/cousins/<slug>/op and the re-dispatched `cousin-op` event."""
import importlib
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
    m = re.search(r"^(function \w+\(|class \w+ |const \w+ = )", rest, re.M)
    return text[start:start + 1 + (m.start() if m else len(rest))]


PACKAGES = ("agent", "migrate", "accounts", "mcp", "system")
PACKAGE_ROUTES = ("routes_agent", "routes_migrate", "routes_lifecycle",
                  "routes_accounts", "routes_mcp", "routes_system")


class RouteSeam(unittest.TestCase):
    def test_each_package_has_a_loaded_route_module(self):
        from cousin_lib.console import app
        names = ["cousin_lib.console.%s" % m for m in PACKAGE_ROUTES]
        self.assertEqual(app.PACKAGE_ROUTE_MODULES, names)
        for name in names:
            module = importlib.import_module(name)
            self.assertTrue(callable(getattr(module, "register", None)), name)
        import inspect
        self.assertIn("PACKAGE_ROUTE_MODULES", inspect.getsource(app.load_routes))


class JsxSeam(unittest.TestCase):
    def test_index_loads_each_package_file_after_the_shared_ones(self):
        html = _read("index.html")
        app_at = html.index('src="app.jsx"')
        shared_at = html.index('src="meetings.jsx"')
        for name in PACKAGES:
            tag = '<script type="text/babel" src="%s.jsx"></script>' % name
            self.assertIn(tag, html, name)
            self.assertTrue(shared_at < html.index(tag) < app_at, name)
            self.assertTrue((_STATIC / ("%s.jsx" % name)).is_file(), name)

    def test_ui_publishes_the_registry(self):
        ui = _read("ui.jsx")
        for name in ("registerSlot", "Slot", "registerView", "registeredViews",
                     "SecretField", "useLongOp", "LongOpStatus"):
            self.assertRegex(ui, r"Object\.assign\(window, \{[^}]*\b%s\b" % name, name)

    def test_the_inspector_and_settings_have_named_slots(self):
        inspector = _component(_read("cousins.jsx"), "Inspector")
        for name in ("inspector.lane", "inspector.panels", "inspector.actions"):
            self.assertIn('<Slot name="%s"' % name, inspector, name)
        self.assertIn("cousin={c}", inspector[inspector.index('<Slot name="inspector.panels"'):])
        settings = _component(_read("views.jsx"), "SettingsView")
        self.assertIn('<Slot name="settings.panels"', settings)

    def test_nav_takes_the_registered_views(self):
        app = _read("app.jsx")
        self.assertIn("registeredViews()", app)
        self.assertIn("navEntries()", app)
        # the view a package registered renders in the main body
        self.assertRegex(app, r"registeredView\(view\)")

    def test_a_package_view_can_never_shadow_a_built_in_one(self):
        app = _read("app.jsx")
        nav = app[app.index("const NAV = ["):app.index("];", app.index("const NAV = ["))]
        ids = re.findall(r'id: "([a-z-]+)"', nav)
        reserved = re.search(r"const RESERVED_VIEW_IDS = \[([^\]]*)\]", _read("ui.jsx")).group(1)
        for vid in ids + ["chat"]:
            self.assertIn('"%s"' % vid, reserved, vid)

    def test_the_op_event_is_re_dispatched(self):
        app = _read("app.jsx")
        self.assertIn('kind === "cousin-op"', app)
        self.assertIn('"fw-cousin-op"', app)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class Registry(unittest.TestCase):
    def run_node(self, body):
        ui = _read("ui.jsx")
        start = ui.index("// ---- seam registry")
        end = ui.index("// ---- end seam registry")
        out = subprocess.run(["node", "-e", "const window = globalThis;\n" + ui[start:end] + body],
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_slots_order_and_replace_by_id(self):
        got = self.run_node("""
registerSlot("inspector.panels", {id: "b", order: 20, render: () => "B"});
registerSlot("inspector.panels", {id: "a", order: 10, render: () => "A"});
registerSlot("inspector.panels", {id: "b", order: 5, render: () => "B2"});
registerSlot("settings.panels", {id: "x", render: () => "X"});
process.stdout.write(JSON.stringify({
  ids: slotEntries("inspector.panels").map(e => e.id),
  out: slotEntries("inspector.panels").map(e => e.render()),
  none: slotEntries("nobody").length,
}));""")
        self.assertEqual(got, {"ids": ["b", "a"], "out": ["B2", "A"], "none": 0})

    def test_a_bad_entry_is_refused(self):
        got = self.run_node("""
const errs = [];
for (const [n, e] of [["", {id: "a", render: () => 1}], ["s", {render: () => 1}],
                      ["s", {id: "a"}]]) {
  try { registerSlot(n, e); errs.push(null); } catch (err) { errs.push("refused"); }
}
try { registerView({id: "chat", label: "x", render: () => 1}); errs.push(null); }
catch (err) { errs.push("refused"); }
process.stdout.write(JSON.stringify(errs));""")
        self.assertEqual(got, ["refused"] * 4)

    def test_a_non_numeric_order_sorts_last(self):
        got = self.run_node("""
registerSlot("z", {id: "late", order: "soon", render: () => 1});
registerSlot("z", {id: "first", order: 1, render: () => 1});
registerSlot("z", {id: "dflt", render: () => 1});
registerSlot("z", {id: "nan", order: NaN, render: () => 1});
process.stdout.write(JSON.stringify(slotEntries("z").map(e => e.id)));""")
        self.assertEqual(got[:2], ["first", "dflt"])
        self.assertEqual(sorted(got[2:]), ["late", "nan"])

    def test_views_register_in_order(self):
        got = self.run_node("""
registerView({id: "system", label: "System", order: 60, render: () => 1});
registerView({id: "accounts", label: "Accounts", order: 50, render: () => 1});
process.stdout.write(JSON.stringify(registeredViews().map(v => v.id)));""")
        self.assertEqual(got, ["accounts", "system"])


class SecretFieldPin(unittest.TestCase):
    def setUp(self):
        self.src = _component(_read("ui.jsx"), "SecretField")

    def test_write_only(self):
        self.assertIn('type="password"', self.src)
        self.assertIn('autoComplete="off"', self.src)
        send = self.src[self.src.index("const send"):]
        send = send[:send.index("};")]
        self.assertLess(send.index('setDraft("")'), send.index("onSubmit("))
        # the draft is only ever the input's value, never rendered as text
        self.assertEqual(re.findall(r"\{draft\}", self.src), ["{draft}"])
        self.assertIn("value={draft}", self.src)

    def test_the_trimmed_value_is_sent(self):
        send = self.src[self.src.index("const send"):]
        self.assertIn("const value = draft.trim();", send)

    def test_a_slot_entry_starts_over_for_another_cousin(self):
        slot = _component(_read("ui.jsx"), "Slot")
        self.assertIn("props.cousin", slot)
        self.assertRegex(slot, r"key=\{[^}]*props\.cousin")

    def test_shows_set_or_the_last_four_only(self):
        text = _component(_read("ui.jsx"), "secretStateText")
        self.assertIn("last4", text)
        self.assertIn('"not set"', text)

    def test_the_token_box_uses_it(self):
        cousins = _read("cousins.jsx")
        telegram = _component(cousins, "TelegramPanel")
        for src, route in ((telegram, 'post("/token"'),):
            self.assertIn("<SecretField", src)
            self.assertIn(route, src)
            self.assertNotIn('type="password"', src)


class LongOpPin(unittest.TestCase):
    def test_the_hook_reads_the_route_and_the_event(self):
        ui = _read("ui.jsx")
        hook = _component(ui, "useLongOp")
        self.assertIn("/api/cousins/${slug}/op", hook)
        self.assertIn('"fw-cousin-op"', hook)
        status = _component(ui, "LongOpStatus")
        self.assertIn("useLongOp(", status)
        self.assertIn("op.stages", status)


if __name__ == "__main__":
    unittest.main()
