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
        for panel in ("SysSupervisorPanel", "SchedulesPanel", "SysUsersPanel", "SysBackupPanel",
                      "SysAgentDefaultsPanel", "SysConfigEditors"):
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

    def test_it_shadows_no_other_files_globals(self):
        self.assertNotRegex(self.src, r"^function ConfirmButton\(", "explorer.jsx owns ConfirmButton")
        exported = re.search(r"Object\.assign\(window, \{([^}]*)\}", self.src).group(1)
        self.assertNotIn("ConfirmButton", re.sub(r"SysConfirmButton", "", exported))

    def test_a_mistyped_number_is_refused_and_a_key_goes_only_on_remove(self):
        parse = _component(self.src, "sysInputToValue")
        self.assertIn("Number.isFinite", parse)
        self.assertIn("Number.isInteger", parse)
        self.assertIn("remove: true", parse)
        form = _component(self.src, "SysTomlForm")
        self.assertIn("if (v.error)", form)
        self.assertIn("remove: true", form)
        agent = _component(self.src, "SysAgentDefaultsPanel")
        self.assertIn("remove", agent)
        self.assertNotIn(": null", agent)

    def test_the_form_keeps_edits_across_re_renders(self):
        form = _component(self.src, "SysTomlForm")
        self.assertIn("JSON.stringify", form)
        self.assertRegex(form, r"useMemo\([^;]*\[sig\]\)")

    def test_a_password_reset_is_typed_twice_and_sent_as_typed(self):
        users = _component(self.src, "SysUsersPanel")
        self.assertNotIn("<SecretField", users)
        reset = _component(self.src, "SysPasswordReset")
        self.assertEqual(reset.count('type="password"'), 2)
        self.assertNotIn(".trim()", reset)
        send = reset[reset.index("const send"):]
        self.assertLess(send.index('setPw("")'), send.index("onSubmit("))
        self.assertIn("pw !== pw2", send)
        # Enter takes the same guard as the button: never an empty password
        self.assertLess(send.index("if (!pw || !pw2) return;"), send.index("onSubmit("))
        self.assertIn('if (e.key === "Enter") send();', reset)
        self.assertIn("disabled={!pw || !pw2}", reset)

    def test_a_panel_that_cannot_load_says_so(self):
        for name in ("SysSupervisorPanel", "SysAgentDefaultsPanel", "SysConfigEditors"):
            self.assertIn("did not answer", _component(self.src, name), name)

    def test_secrets_go_through_the_write_only_field(self):
        for name in ("SysMediaEditor", "SysPeersEditor"):
            comp = _component(self.src, name)
            self.assertIn("<SecretField", comp, name)
            self.assertNotIn('type="password"', comp, name)
        users = _component(self.src, "SysUsersPanel")
        # the add form's two password boxes are cleared before the request goes out
        add = users[users.index("const add"):users.index("const reset")]
        self.assertLess(add.index('setPw("")'), add.index("sysSend("))

    def test_destructive_actions_ask_twice_or_for_a_typed_name(self):
        confirm = _component(self.src, "SysConfirmButton")
        self.assertIn("setArmed(true)", confirm)
        for name, needle in (("SysSupervisorPanel", 'label="stop"'),
                             ("SchedulesPanel", 'label="cancel"'),
                             ("SysMediaEditor", "remove [${kind}]"),
                             ("SysPeersEditor", 'label="remove peer"'),
                             ("SysAllowlistEditor", 'label="remove"')):
            comp = _component(self.src, name)
            self.assertIn("<SysConfirmButton", comp, name)
            self.assertIn(needle, comp, name)
        users = _component(self.src, "SysUsersPanel")
        self.assertIn("disabled={typed !== u}", users)
        self.assertIn("confirm: typed", users)
        self.assertIn("confirm: a.confirm", _component(self.src, "SysSupervisorPanel"))

    def test_the_only_service_control_is_the_supervisor_and_the_console_restart(self):
        routes = set(re.findall(r'/api/(?:admin|cousins/[^/"`]+)/(restart|start|stop)\b', self.src))
        self.assertEqual(routes, {"restart"})
        self.assertIn("/api/admin/restart/framework", _component(self.src, "SysRestartOffer"))

    def test_no_colour_literals(self):
        self.assertNotRegex(self.src, r"#[0-9a-fA-F]{3,8}\b")
        self.assertNotIn("oklch(", self.src)
        self.assertNotIn("rgb(", self.src)

    def test_no_em_dash(self):
        self.assertNotIn("\u2014", self.src)


class NoSharedGlobals(unittest.TestCase):
    """Every jsx file is a classic script: a top-level function, class or
    const is a global, and a second file declaring the same name replaces
    the first one's for every file (system.jsx's ConfirmButton once broke
    the Memory Explorer's). No two files may declare the same name."""

    def test_no_two_jsx_files_declare_the_same_top_level_name(self):
        seen = {}
        clashes = []
        decl = re.compile(r"^(?:async\s+)?(?:function|class|const|let|var)\s+([A-Za-z_$][\w$]*)", re.M)
        for path in sorted(_STATIC.glob("*.jsx")):
            for name in set(decl.findall(path.read_text(encoding="utf-8"))):
                if name in seen:
                    clashes.append("%s: %s and %s" % (name, seen[name], path.name))
                seen.setdefault(name, path.name)
        self.assertEqual(clashes, [])


if __name__ == "__main__":
    unittest.main()
