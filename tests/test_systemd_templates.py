"""The systemd unit templates under systemd/.

They are templates, not units: every install-specific value is a
placeholder the README tells you to substitute. Pinned: each parses
as INI, carries no absolute path (a path is an install fact), names
only placeholders from the documented set, is plain ASCII, and every
ExecStart runs a console script that pyproject actually declares.
"""
import configparser
import pathlib
import re
import tomllib
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_UNITS = _REPO_ROOT / "systemd"

REQUIRED_UNITS = {
    "cousin-loops.service",
    "cousin-sweep.service", "cousin-sweep.timer",
    "cousin-tool-surface.service", "cousin-tool-surface.timer",
    "cousin-console.service",
}
# R10: no per-cousin chat server, so neither its unit nor its watchdog's;
# row 70: no legacy session to start at boot (the supervisor starts runner
# cousins itself)
RETIRED_UNITS = {
    "cousin-chat-server@.service",
    "cousin-chat-watchdog.service", "cousin-chat-watchdog.timer",
    "cousin-start@.service",
}
PLACEHOLDERS = {"ROOT", "USER_BIN", "SYSTEM_PATH"}
_ABS_PATH = re.compile(r'(?:^|[=:\s"\'])/[A-Za-z0-9_]')
_PLACEHOLDER = re.compile(r"\{\{([A-Z_]+)\}\}")


def _units():
    return sorted(p for p in _UNITS.iterdir()
                  if p.suffix in (".service", ".timer"))


def _parse(path):
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    parser.read_string(path.read_text())
    return parser


class TestInventory(unittest.TestCase):
    def test_the_required_units_ship(self):
        names = {p.name for p in _units()}
        self.assertTrue(REQUIRED_UNITS <= names,
                        "missing: %s" % sorted(REQUIRED_UNITS - names))

    def test_no_retired_unit_ships(self):
        names = {p.name for p in _units()}
        self.assertEqual(RETIRED_UNITS & names, set())
        # the README names them only to disable them on an upgrade
        readme = (_UNITS / "README.md").read_text()
        for line in readme.splitlines():
            if any(stem in line for stem in ("cousin-chat-server", "cousin-chat-watchdog",
                                              "cousin-start@")):
                self.assertNotIn("enable", line.replace("disable", ""), line)
                self.assertFalse(line.startswith("|"), line)

    def test_every_timer_has_its_service(self):
        names = {p.name for p in _units()}
        for name in names:
            if name.endswith(".timer"):
                self.assertIn(name[:-len(".timer")] + ".service", names)

    def test_readme_names_every_unit_and_placeholder(self):
        readme = (_UNITS / "README.md").read_text()
        for unit in _units():
            self.assertIn(unit.name, readme)
        for key in PLACEHOLDERS:
            self.assertIn("{{%s}}" % key, readme)
        self.assertIn("sed", readme)
        self.assertIn("FRAMEWORK_ROOT", readme)


class TestEachUnit(unittest.TestCase):
    def setUp(self):
        self.scripts = tomllib.loads(
            (_REPO_ROOT / "pyproject.toml").read_text()
        )["project"]["scripts"]

    def test_parses_as_ini_with_a_description(self):
        for unit in _units():
            with self.subTest(unit=unit.name):
                parser = _parse(unit)
                self.assertIn("Unit", parser.sections())
                self.assertTrue(parser["Unit"].get("Description"))
                kind = "Timer" if unit.suffix == ".timer" else "Service"
                self.assertIn(kind, parser.sections())

    def test_no_absolute_paths_only_placeholders(self):
        for unit in _units():
            with self.subTest(unit=unit.name):
                text = unit.read_text()
                self.assertIsNone(_ABS_PATH.search(text),
                                  "absolute path in %s" % unit.name)
                found = set(_PLACEHOLDER.findall(text))
                self.assertTrue(found <= PLACEHOLDERS,
                                "unknown placeholders %s"
                                % sorted(found - PLACEHOLDERS))

    def test_ascii_only(self):
        for unit in list(_units()) + [_UNITS / "README.md"]:
            with self.subTest(file=unit.name):
                unit.read_bytes().decode("ascii")

    def test_every_execstart_runs_a_declared_console_script(self):
        for unit in _units():
            if unit.suffix != ".service":
                continue
            with self.subTest(unit=unit.name):
                exec_start = _parse(unit)["Service"]["ExecStart"]
                self.assertTrue(exec_start.startswith("{{USER_BIN}}/"),
                                exec_start)
                script = exec_start.split()[0].split("/", 1)[1]
                self.assertIn(script, self.scripts,
                              "%s is not a console script" % script)

    def test_services_carry_the_framework_root(self):
        for unit in _units():
            if unit.suffix != ".service":
                continue
            with self.subTest(unit=unit.name):
                text = unit.read_text()
                self.assertIn("FRAMEWORK_ROOT={{ROOT}}", text)
                # The user's ~/.local/bin (%h is systemd's home
                # specifier) sits before the system PATH: the agent
                # CLI's installer puts it there, and a flip started by
                # a unit must find it without a login shell's PATH.
                self.assertIn(
                    "PATH={{USER_BIN}}:%h/.local/bin:{{SYSTEM_PATH}}", text)
                # Unbuffered: a service's stdout is a pipe, and a
                # block-buffered status line never reaches the journal.
                self.assertIn("Environment=PYTHONUNBUFFERED=1", text)

    def test_timers_are_persistent_with_a_calendar(self):
        for unit in _units():
            if unit.suffix != ".timer":
                continue
            with self.subTest(unit=unit.name):
                timer = _parse(unit)["Timer"]
                self.assertEqual(timer.get("Persistent"), "true")
                self.assertTrue(timer.get("OnCalendar"))

    def test_sweep_is_weekly_and_tool_surface_is_daily(self):
        sweep = _parse(_UNITS / "cousin-sweep.timer")["Timer"]["OnCalendar"]
        daily = _parse(_UNITS / "cousin-tool-surface.timer")["Timer"][
            "OnCalendar"]
        self.assertTrue(sweep.startswith(("weekly", "Sun", "Mon", "Sat")),
                        sweep)
        self.assertTrue(daily.startswith(("daily", "*-*-*")), daily)

    def test_console_unit_is_a_restarting_service_on_a_stated_port(self):
        # The console owns nothing durable but sessions and the users
        # file, so a restart costs a login and nothing else: systemd may
        # restart it freely. The port is stated so the README's "open
        # http://<host>:8600" line and the unit agree.
        service = _parse(_UNITS / "cousin-console.service")["Service"]
        self.assertEqual(service["Type"], "simple")
        self.assertEqual(service["ExecStart"],
                         "{{USER_BIN}}/cousin-console --port 8600")
        self.assertEqual(service["Restart"], "on-failure")
        unit = _parse(_UNITS / "cousin-console.service")
        self.assertEqual(unit["Install"]["WantedBy"], "default.target")

if __name__ == "__main__":
    unittest.main()
