"""A tool exists on both transports or on neither."""
import pathlib
import unittest

from cousin_lib import mcp_server
from cousin_lib.runner import tools
from tests._hermetic import HermeticCase
from tests.runner.test_tools import TRACKER_TOML

ROOT = pathlib.Path(__file__).resolve().parents[2]


class TestParity(HermeticCase):
    def _registry(self):
        text = mcp_server.shipped_default_registry(ROOT)
        return mcp_server.parse_registry(text, "shipped")

    def _registry_with_tracker(self):
        text = mcp_server.shipped_default_registry(ROOT) + TRACKER_TOML
        return mcp_server.parse_registry(text, "shipped + tracker")

    def test_stdio_and_in_process_agree_on_names_and_schemas(self):
        reg = self._registry()
        stdio = {t["name"]: t["inputSchema"] for t in mcp_server.list_tools(reg)}
        inproc = {t["name"]: t["inputSchema"] for t in tools.tool_definitions(reg)}
        for name, schema in stdio.items():
            self.assertEqual(inproc[name], schema, name)
        self.assertEqual(set(inproc) - set(stdio), {"reply", "handoff"})

    def test_every_shipped_command_has_an_in_process_handler(self):
        self.assertEqual(tools.missing_handlers(self._registry()), [])

    def test_the_handler_table_has_no_command_the_registry_lacks(self):
        reg = self._registry()
        for name, table in tools.HANDLERS.items():
            if name == "send":
                continue
            if name not in reg["tools"]:
                continue  # HANDLERS may carry a tool this registry does not enable
            commands = set(reg["tools"][name]["commands"])
            if reg["tools"][name]["kind"] == "job":
                commands |= {"status", "result"}
            extra = set(table) - commands
            self.assertEqual(extra, set(), "%s has handlers for %s the registry lacks" % (name, extra))

    def test_tracker_agrees_on_both_transports_when_the_registry_carries_it(self):
        reg = self._registry_with_tracker()
        stdio = {t["name"]: t["inputSchema"] for t in mcp_server.list_tools(reg)}
        inproc = {t["name"]: t["inputSchema"] for t in tools.tool_definitions(reg)}
        self.assertEqual(inproc["tracker"], stdio["tracker"])
        commands = set(reg["tools"]["tracker"]["commands"])
        extra = set(tools.HANDLERS["tracker"]) - commands
        self.assertEqual(extra, set())
        self.assertEqual(tools.missing_handlers(reg), [])


if __name__ == "__main__":
    unittest.main()
