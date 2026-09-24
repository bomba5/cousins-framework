"""The framework contract: generated, complete, deterministic."""
import pathlib
import re
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
        for needle in ("2026", "UTC", "/home/", "wren"):
            self.assertNotIn(needle, text)
        self.assertIsNone(re.search(r"[Gg]eneration:? ?\d", text))

    def test_ascii_only(self):
        contract.render(_registry(TRACKER_TOML), "1.12.0").encode("ascii")


class TestRunnerLaneDoctrine(HermeticCase):
    """#95: a runner cousin's identity and memories were written for the
    tmux lane (`cousin-reply`, `cousin-chat send`, `cousin-memory`). The
    contract names the tools as the way and the CLIs as the fallback only."""

    def setUp(self):
        super().setUp()
        self.text = contract.render(_registry(), "1.12.0")
        self.flat = " ".join(self.text.split())

    def test_the_tools_are_named_as_the_way_to_reply_send_and_remember(self):
        for needle in ("with the `reply` tool", "with the `send` tool",
                       "through the `memory` tool"):
            self.assertIn(needle, self.flat)

    def test_each_cli_is_named_and_overridden(self):
        for cli in ("`cousin-reply", "`cousin-chat send", "`cousin-memory"):
            self.assertIn(cli, self.text)

    def test_a_cli_is_never_the_instructed_path(self):
        """Every paragraph or bullet that names a cousin-* CLI frames it as
        the habit to drop or the fallback, never as the way."""
        for chunk in re.split(r"\n\s*\n|\n(?=- )", self.text):
            if re.search(r"`cousin-[a-z]", chunk):
                flat = " ".join(chunk.split()).lower()
                self.assertTrue(any(w in flat for w in ("never", "fallback", "not ")), chunk)

    def test_being_told_to_keep_something_is_a_remember_call_in_the_same_turn(self):
        """A person saying 'remember this' or stating a preference got a
        reply and no memory write: the doctrine named the tool, not when."""
        self.assertIn("asks you to keep something", self.flat)
        self.assertIn("in that same turn, before you reply", self.flat)
        # operator level only for the operator's words; nobody else's
        self.assertIn("at level `operator` when the operator said it", self.flat)
        self.assertIn("at the default level when anyone else did", self.flat)
        # an unasked remark is not a write (review: 'a fact about their
        # life' fired on incidental remarks)
        self.assertIn("A remark nobody asked you to keep is not a memory write", self.flat)
        self.assertNotIn("a fact about their life", self.flat)

    def test_being_asked_what_you_remember_searches_memory_first(self):
        """Asked 'what do you remember about me', a cousin read its memory
        files with sed instead of searching (09-24)."""
        self.assertIn("asked what you know or remember", self.flat)
        self.assertIn("`search` or `recall` first", self.flat)

    def test_a_long_shell_command_is_the_job_tools_run(self):
        """A runner cousin ran `cousin-job start shell` through Bash because
        the job tool had no way to launch a command (09-24)."""
        self.assertIn("A long shell command is the `job` tool's `run`", self.flat)
        self.assertIn("`run_in_background`", self.flat)
        self.assertIn("never `cousin-job` through Bash", self.flat)

    def test_the_fallback_is_only_for_a_missing_or_erroring_tool(self):
        self.assertIn("fallback only", self.flat)
        self.assertIn("missing", self.flat)
        self.assertIn("error", self.flat)


if __name__ == "__main__":
    unittest.main()
