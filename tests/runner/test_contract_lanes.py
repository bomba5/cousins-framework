"""The contract's tool names per lane (phase 9 R7, Review Focus 5).

The SDK lane registers the tool server as `cousin`, so the model sees
`mcp__cousin__<tool>`; opencode prefixes an MCP server's tools with the
server name and one underscore, `cousin_<tool>` (Survey 5). The contract
names the tools the model actually has, so it takes a naming function,
and the SDK lane's bytes, which the prompt cache keys on, do not move."""
import hashlib
import pathlib
import re
import unittest

from cousin_lib import mcp_server
from cousin_lib.runner import contract, tools
from tests._hermetic import HermeticCase
from tests.runner.test_prompt import PromptCase
from tests.runner.test_tools import TRACKER_TOML

ROOT = pathlib.Path(__file__).resolve().parents[2]

# sha256 of contract.render(<shipped registry>, "1.12.0") on main 84d1a67
# (1.19.0, whose 5e8b72f added "Tools, not the terminal CLIs"), the bytes
# main's own renderer produces. A registry or static-text edit changes it on
# purpose: update the hash in the same commit, knowing every SDK cousin's
# prompt cache resets.
SDK_CONTRACT_SHA256 = "d74ff87d7313ffd8e448102bb1f6e9f3dc871063f6f9693233f1ec4477c036b2"


def as_opencode(sdk_text):
    """The SDK lane's contract or prompt with only the lane's names changed:
    the tool lines, the three names in "Tools, not the terminal CLIs", and
    which runner that section says the cousin is on."""
    return (sdk_text.replace("- `mcp__cousin__", "- `cousin_")
            .replace("(`mcp__cousin__", "(`cousin_")
            .replace("You run on the SDK runner,", "You run on the opencode runner,"))


OPENCODE_RUNNER = "the opencode runner"


def opencode_name(name):
    return "cousin_%s" % name


def _registry(extra=""):
    return mcp_server.parse_registry(mcp_server.shipped_default_registry(ROOT) + extra, "t")


class TestContractLanes(HermeticCase):
    def test_the_sdk_contract_bytes_do_not_move(self):
        reg = _registry()
        default = contract.render(reg, "1.12.0")
        self.assertEqual(hashlib.sha256(default.encode()).hexdigest(), SDK_CONTRACT_SHA256,
                         "the SDK lane's contract bytes moved")
        self.assertEqual(contract.render(reg, "1.12.0", tool_name=None), default)
        self.assertEqual(
            contract.render(reg, "1.12.0",
                            tool_name=lambda n: "mcp__%s__%s" % (mcp_server.SERVER_NAME, n)),
            default)

    def test_the_opencode_contract_names_cousin_tools(self):
        reg = _registry(TRACKER_TOML)
        text = contract.render(reg, "1.12.0", tool_name=opencode_name, runner=OPENCODE_RUNNER)
        for d in tools.tool_definitions(reg):
            self.assertIn("- `cousin_%s`: " % d["name"], text, d["name"])
        self.assertNotIn("mcp__", text)
        # Only the lane's names differ: same header, same text, same commands.
        sdk = contract.render(reg, "1.12.0")
        self.assertEqual(as_opencode(sdk), text)
        self.assertIn("(`cousin_reply`), never with `cousin-reply`", text)
        self.assertIn("You run on the opencode runner,", text)

    def test_every_tool_line_uses_the_naming_function(self):
        reg = _registry(TRACKER_TOML)
        seen = []
        text = contract.render(reg, "1.12.0", tool_name=lambda n: seen.append(n) or "X-%s" % n)
        # the three the "Tools, not the terminal CLIs" section names, then every tool line
        self.assertEqual(seen, ["reply", "send", "memory"]
                         + [d["name"] for d in tools.tool_definitions(reg)])
        self.assertNotIn("mcp__", text)
        lines = [l for l in text.splitlines() if l.startswith("- `")]
        self.assertEqual(len(lines), len(seen) - 3)
        self.assertTrue(all(re.match(r"- `X-[a-z_]+`: ", l) for l in lines), lines)

    def test_the_runner_label_is_passed_never_guessed(self):
        """Review round 2, minor 5: the section's "You run on ..." is what the
        caller says, not a guess from the tool names."""
        reg = _registry()
        self.assertIn("You run on the SDK runner,", contract.render(reg, "1.12.0"))
        self.assertIn("You run on the SDK runner,",
                      contract.render(reg, "1.12.0", tool_name=opencode_name))
        self.assertIn("You run on the fake runner,",
                      contract.render(reg, "1.12.0", runner="the fake runner"))

    def test_the_opencode_contract_is_deterministic_and_ascii(self):
        a = contract.render(_registry(TRACKER_TOML), "1.12.0", tool_name=opencode_name)
        b = contract.render(_registry(TRACKER_TOML), "1.12.7", tool_name=opencode_name)
        self.assertEqual(a.encode("ascii"), b.encode("ascii"))


class TestPromptLanes(PromptCase):
    def test_the_sdk_prompt_is_unchanged_by_the_default(self):
        self.assertEqual(self.compose(tool_name=None), self.compose())

    def test_the_opencode_prompt_names_cousin_tools_and_differs_only_there(self):
        sdk = self.compose()
        oc = self.compose(tool_name=opencode_name, runner=OPENCODE_RUNNER)
        self.assertIn("- `cousin_reply`: ", oc)
        self.assertIn("- `cousin_handoff`: ", oc)
        self.assertNotIn("mcp__cousin__", oc)
        self.assertEqual(as_opencode(sdk), oc)


if __name__ == "__main__":
    unittest.main()
