"""The contract's tool names per lane.

The SDK lane registers the tool server as `cousin`, so the model sees
`mcp__cousin__<tool>`; opencode prefixes an MCP server's tools with the
server name and one underscore, `cousin_<tool>`. The contract
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
# (1.19.0, whose 5e8b72f added "Tools, not the terminal CLIs"), plus the
# OTHER_SERVERS paragraph feat/runner-mcp-json (5edd58e) appends on the SDK
# lane, plus 1.21.0's memory triggers (remember when asked, search first) and
# the job tool's `run` line in "Tools, not the terminal CLIs", plus 3.8.0's
# `why` in the memory tool's command list, plus 3.27.1's handoff shapes in
# "Generations", plus 3.36.0's derived_from line in "Memory", plus
# 3.58.0's `skill` tool in the shipped registry, plus 3.60.0's `import`
# and `dry-run` in that tool's description and command list, plus 3.61.0's
# `check` there, minus 4.0.0's `import` (the operator's act now), plus
# 4.2.0's skills-hint paragraph in "How messages reach you" (the SDK lane's
# only, so opencode's bytes did not move with it): the bytes the
# SDK lane's renderer produces. A registry or static-text edit changes it on
# purpose: update the hash in the same commit, knowing every SDK and
# opencode cousin's prompt cache resets (the edit moves both lanes' bytes).
SDK_CONTRACT_SHA256 = "e12f04f96594e6eb7c0605e50af762a737517eebf212ff057a2af18c73483e01"


def as_opencode(sdk_text):
    """The SDK lane's contract or prompt with only the lane's names changed:
    the tool lines, the three names in "Tools, not the terminal CLIs", and
    which runner that section says the cousin is on. The OTHER_SERVERS
    paragraph is the SDK lane's only (opencode loads exactly one server),
    and so is SKILL_HINT (opencode loads no skills)."""
    sdk_text = sdk_text.replace("\n\n" + contract.OTHER_SERVERS.strip(), "")
    sdk_text = sdk_text.replace("\n\n" + contract.SKILL_HINT, "")
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
        text = contract.render(reg, "1.12.0", tool_name=opencode_name, runner=OPENCODE_RUNNER,
                               other_servers=None, skill_hint=False)
        for d in tools.tool_definitions(reg):
            self.assertIn("- `cousin_%s`: " % d["name"], text, d["name"])
        self.assertNotIn("mcp__", text)
        # Only the lane's names differ: same header, same text, same commands.
        sdk = contract.render(reg, "1.12.0")
        self.assertEqual(as_opencode(sdk), text)
        self.assertIn("(`cousin_reply`), never with `cousin-reply`", text)
        self.assertIn("You run on the opencode runner,", text)

    def test_the_call_form_paragraph_is_the_opencode_lane_s_only(self):
        """A model on opencode wrote the reply call out as text: its contract
        says, after "How you answer", that a call is made, not written, and
        names the reply tool as the lane serves it. The SDK lane's bytes do
        not move."""
        reg = _registry()
        plain = contract.render(reg, "1.12.0", tool_name=opencode_name, runner=OPENCODE_RUNNER,
                                other_servers=None)
        text = contract.render(reg, "1.12.0", tool_name=opencode_name, runner=OPENCODE_RUNNER,
                               other_servers=None, call_form=True)
        paragraph = contract.CALL_FORM.format(reply="cousin_reply")
        self.assertIn('like `cousin_reply({text: "..."})` in your text is only text', paragraph)
        self.assertIn("call the `cousin_reply` tool with `text`", paragraph)
        answer = plain.index("## How you answer")
        after = plain.index("\n\n", plain.index("\n\n", answer) + 2)
        self.assertEqual(text, plain[:after] + "\n\n" + paragraph + plain[after:])
        self.assertNotIn(paragraph, contract.render(reg, "1.12.0"))
        self.assertEqual(hashlib.sha256(contract.render(reg, "1.12.0").encode()).hexdigest(),
                         SDK_CONTRACT_SHA256)
        text.encode("ascii")

    def test_the_skills_hint_paragraph_is_the_sdk_lane_s_only(self):
        """The SDK lane's turns carry the skills hint (skills_hint.py), so its
        contract says what the line is and to use the skill; opencode loads
        no skills and the tmux kind has no per-turn context hook."""
        reg = _registry()
        sdk = contract.render(reg, "1.12.0")
        self.assertIn("\n\n" + contract.SKILL_HINT + "\n\n## How you answer", sdk)
        self.assertIn("instead of\nworking from memory", contract.SKILL_HINT)
        self.assertNotIn("[fw-skills]", contract.render(reg, "1.12.0", skill_hint=False))
        self.assertNotIn("[fw-skills]", contract.render(
            reg, "1.12.0", tool_name=opencode_name, runner=OPENCODE_RUNNER, other_servers=None,
            call_form=True, skill_hint=False))

    def test_every_tool_line_uses_the_naming_function(self):
        reg = _registry(TRACKER_TOML)
        seen = []
        text = contract.render(reg, "1.12.0", tool_name=lambda n: seen.append(n) or "X-%s" % n,
                               other_servers=None)
        # the three the "Tools, not the terminal CLIs" section names, then every tool line
        self.assertEqual(seen, ["reply", "send", "memory"]
                         + [d["name"] for d in tools.tool_definitions(reg)])
        self.assertNotIn("mcp__", text)
        lines = [l for l in text.splitlines() if l.startswith("- `")]
        self.assertEqual(len(lines), len(seen) - 3)
        self.assertTrue(all(re.match(r"- `X-[a-z_]+`: ", l) for l in lines), lines)

    def test_the_runner_label_is_passed_never_guessed(self):
        """The section's "You run on ..." is what the caller says, not a
        guess from the tool names."""
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
        oc = self.compose(tool_name=opencode_name, runner=OPENCODE_RUNNER, other_servers=None,
                          skill_hint=False)
        self.assertIn("- `cousin_reply`: ", oc)
        self.assertIn("- `cousin_handoff`: ", oc)
        self.assertNotIn("mcp__cousin__", oc)
        self.assertEqual(as_opencode(sdk), oc)

    def test_the_skills_hint_line_is_in_the_sdk_prompt_only(self):
        from cousin_lib.runner import prompt
        self.assertIn(contract.SKILL_HINT, self.compose())
        block = prompt.compose_context_block(self.home, root=self.root, registry=self.registry,
                                             version="1.12.0")
        self.assertNotIn("[fw-skills]", block)

    def test_the_opencode_prompt_carries_the_call_form(self):
        oc = self.compose(tool_name=opencode_name, runner=OPENCODE_RUNNER, other_servers=None,
                          call_form=True)
        self.assertIn(contract.CALL_FORM.format(reply="cousin_reply"), oc)
        self.assertNotIn("in your text is only text", self.compose())


if __name__ == "__main__":
    unittest.main()
