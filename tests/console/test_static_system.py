"""system.jsx, the System view (WP-F): pinned by text, as the other
static tests pin theirs. It registers its view and a schedules panel in
the inspector through the seams, calls only routes routes_system.py
registers, keeps secrets in SecretField, asks twice (or for a typed name)
before anything destructive, offers only the console's existing restart
route, and uses the style tokens, never a colour literal."""
import pathlib
import re
import unittest

_STATIC = pathlib.Path(__file__).resolve().parents[2] / "cousin_lib" / "console_static"


def _read(name):
    return (_STATIC / name).read_text(encoding="utf-8")


def _component(text, name):
    start = text.index("function %s(" % name)
    rest = text[start + 1:]
    m = re.search(r"^(function \w+\(|class \w+ |const \w+ = )", rest, re.M)
    return text[start:start + 1 + (m.start() if m else len(rest))]


class SystemView(unittest.TestCase):
    def setUp(self):
        self.src = _read("system.jsx")

    def test_registers_its_view_and_the_inspector_panel(self):
        self.assertRegex(self.src, r'registerView\(\{ id: "system", label: "System"')
        self.assertRegex(self.src, r'registerSlot\("inspector\.panels", \{ id: "schedules"')
        view = _component(self.src, "SystemView")
        for panel in ("SupervisorPanel", "SchedulesPanel", "UsersPanel", "BackupPanel",
                      "AgentDefaultsPanel", "ConfigEditors"):
            self.assertIn("<%s" % panel, view, panel)

    def test_every_route_it_calls_is_registered(self):
        from cousin_lib.console import app, router
        from tests.console.test_static_files import _TEMPLATE_PARAM, _api_literals
        app.load_routes()
        patterns = [re.compile("^" + re.sub(r"\\\{[a-z_]+\\\}", "[^/]+", re.escape(p)) + "$")
                    for _m, p in router.routes()]
        used = _api_literals(self.src)
        self.assertGreater(len(used), 20)
        for literal in used:
            path = _TEMPLATE_PARAM.sub("x", literal.split("?", 1)[0])
            self.assertTrue(any(rx.match(path) for rx in patterns), literal)

    def test_secrets_go_through_the_write_only_field(self):
        for name in ("MediaEditor", "PeersEditor"):
            comp = _component(self.src, name)
            self.assertIn("<SecretField", comp, name)
            self.assertNotIn('type="password"', comp, name)
        users = _component(self.src, "UsersPanel")
        self.assertIn("<SecretField", users)          # a reset never shows the value
        # the add form's two password boxes are cleared before the request goes out
        add = users[users.index("const add"):users.index("const reset")]
        self.assertLess(add.index('setPw("")'), add.index("sysSend("))

    def test_destructive_actions_ask_twice_or_for_a_typed_name(self):
        confirm = _component(self.src, "ConfirmButton")
        self.assertIn("setArmed(true)", confirm)
        for name, needle in (("SupervisorPanel", 'label="stop"'),
                             ("SchedulesPanel", 'label="cancel"'),
                             ("MediaEditor", "remove [${kind}]"),
                             ("PeersEditor", 'label="remove peer"'),
                             ("AllowlistEditor", 'label="remove"')):
            comp = _component(self.src, name)
            self.assertIn("<ConfirmButton", comp, name)
            self.assertIn(needle, comp, name)
        users = _component(self.src, "UsersPanel")
        self.assertIn("disabled={typed !== u}", users)
        self.assertIn("confirm: typed", users)
        self.assertIn("confirm: a.confirm", _component(self.src, "SupervisorPanel"))

    def test_the_only_service_control_is_the_supervisor_and_the_console_restart(self):
        routes = set(re.findall(r'/api/(?:admin|cousins/[^/"`]+)/(restart|start|stop)\b', self.src))
        self.assertEqual(routes, {"restart"})
        self.assertIn("/api/admin/restart/framework", _component(self.src, "RestartOffer"))

    def test_no_colour_literals(self):
        self.assertNotRegex(self.src, r"#[0-9a-fA-F]{3,8}\b")
        self.assertNotIn("oklch(", self.src)
        self.assertNotIn("rgb(", self.src)

    def test_no_em_dash(self):
        self.assertNotIn("—", self.src)


if __name__ == "__main__":
    unittest.main()
