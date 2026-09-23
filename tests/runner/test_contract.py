"""The framework contract: generated, complete, deterministic."""
import pathlib
import unittest

from cousin_lib import mcp_server
from cousin_lib.runner import contract, tools
from tests._hermetic import HermeticCase
from tests.runner.test_tools import TRACKER_TOML

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _registry(extra=""):
    return mcp_server.parse_registry(mcp_server.shipped_default_registry(ROOT) + extra, "t")


class TestContract(HermeticCase):
    def test_every_registry_tool_and_both_runner_tools_appear(self):
        reg = _registry(TRACKER_TOML)
        text = contract.render(reg, "1.12.0")
        for d in tools.tool_definitions(reg):
            self.assertIn("`mcp__cousin__%s`" % d["name"], text, d["name"])
        for cmd in reg["tools"]["memory"]["commands"]:
            self.assertIn(cmd, text)

    def test_a_disabled_tool_does_not_appear(self):
        reg = _registry(TRACKER_TOML)
        reg["tools"]["tracker"]["enabled"] = False
        self.assertNotIn("mcp__cousin__tracker", contract.render(reg, "1.12.0"))

    def test_deterministic_and_newline_terminated(self):
        reg = _registry()
        a, b = contract.render(reg, "1.12.0"), contract.render(_registry(), "1.12.0")
        self.assertEqual(a.encode(), b.encode())
        self.assertTrue(a.endswith("\n") and not a.endswith("\n\n"))

    def test_a_patch_release_does_not_change_a_byte(self):
        reg = _registry()
        self.assertEqual(contract.render(reg, "1.12.0"), contract.render(reg, "1.12.7"))
        self.assertNotEqual(contract.render(reg, "1.12.0"), contract.render(reg, "1.13.0"))

    def test_major_minor(self):
        self.assertEqual(contract.major_minor("1.12.0"), "1.12")
        self.assertEqual(contract.major_minor("2.0.0rc1"), "2.0")
        self.assertEqual(contract.major_minor("garbage"), "unknown")

    def test_the_contract_claims_precedence_over_identity_text(self):
        self.assertIn("this contract is right", contract.render(_registry(), "1.12.0"))

    def test_a_registry_text_change_changes_the_contract(self):
        a, b = _registry(), _registry()
        b["tools"]["memory"]["description"] = b["tools"]["memory"]["description"] + " Edited."
        self.assertNotEqual(contract.render(a, "1.12.0"), contract.render(b, "1.12.0"))

    def test_nothing_volatile(self):
        text = contract.render(_registry(), "1.12.0")
        for needle in ("2026", "UTC", "generation ", "Generation:", "/home/", "wren"):
            self.assertNotIn(needle, text)

    def test_ascii_only(self):
        contract.render(_registry(TRACKER_TOML), "1.12.0").encode("ascii")


if __name__ == "__main__":
    unittest.main()
