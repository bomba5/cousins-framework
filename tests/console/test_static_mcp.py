"""mcp.jsx: the MCP and policy panels. Pinned by text where the
contract is a string (the slots, the routes, the words that explain a
refusal), and the pure helpers run under node."""
import json
import pathlib
import re
import shutil
import subprocess
import unittest

_STATIC = pathlib.Path(__file__).resolve().parents[2] / "cousin_lib" / "console_static"


def _read(name):
    return (_STATIC / name).read_text(encoding="utf-8")


class McpJsx(unittest.TestCase):
    def setUp(self):
        self.src = _read("mcp.jsx")

    def test_panels_register_through_the_seams(self):
        self.assertIn('registerSlot("inspector.panels", { id: "mcp"', self.src)
        self.assertIn('registerSlot("inspector.panels", { id: "policy"', self.src)
        self.assertIn('registerSlot("settings.panels", { id: "mcp"', self.src)

    def test_it_calls_the_package_routes_and_the_fleet_restart(self):
        for path in ("/mcp/registry", "/copy-default", "/copy-example", "/api/mcp/registry",
                     "/mcp/servers", "/selftest", "/last-connection", "/approve", "/status",
                     "/policy", "/restart"):
            self.assertIn(path, self.src, path)
        # every write sends the etag it read
        self.assertGreaterEqual(self.src.count("etag: data.etag"), 3)

    def test_the_ui_says_why_a_secret_is_refused_and_why_the_handoff_is_kept(self):
        self.assertIn("in a private file, never on its command line", self.src)
        self.assertIn("which any user on this host can read", self.src)
        self.assertIn("every generation ends through it", self.src)
        self.assertIn("A guardrail, not a sandbox", self.src)
        self.assertIn("applies at the next start", self.src)

    def test_destructive_actions_need_a_typed_or_second_confirmation(self):
        self.assertIn('<TypedConfirm word="replace"', self.src)
        self.assertIn("confirm_loosening: true", self.src)
        self.assertIn("click again to restart", self.src)
        self.assertIn('<TypedConfirm word="approve"', self.src)
        self.assertIn("click again to remove", self.src)

    def test_approve_says_it_rewrites_the_whole_settings_file(self):
        self.assertIn("rewrites the whole harness settings file", self.src)
        self.assertIn("while {cousin.slug}'s session is stopped", self.src)
        self.assertNotIn("this home's entry only", self.src)
        self.assertIn('["tmux-legacy", "tmux"]', self.src)

    def test_patterns_compile_with_the_u_flag_like_the_plugin(self):
        self.assertIn('new RegExp(pattern, "u")', self.src)

    def test_stale_means_the_servers_stale_flag_and_servers_key_by_id(self):
        self.assertIn("d.stale", self.src)
        self.assertNotIn("d.etag) setStale", self.src)
        self.assertIn("key={d._id}", self.src)

    def test_no_colour_literals_and_no_em_dashes(self):
        self.assertNotRegex(self.src, r"#[0-9a-fA-F]{3,8}\b|rgba?\(|oklch\(")
        self.assertNotIn("—", self.src)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class Helpers(unittest.TestCase):
    def run_node(self, body):
        src = _read("mcp.jsx")
        start = src.index("// ---- pure helpers")
        end = src.index("// ---- end pure helpers")
        out = subprocess.run(["node", "-e", src[start:end] + body],
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_policy_removals(self):
        got = self.run_node("""
const before = {deny_tools: ["WebFetch", "WebSearch"], deny_bash_patterns: ["a"], ask: [], outbound_filter: true};
process.stdout.write(JSON.stringify([
  policyRemovals(before, {deny_tools: ["WebFetch"], deny_bash_patterns: ["a", "b"], ask: ["X"], outbound_filter: true}),
  policyRemovals(before, Object.assign({}, before, {outbound_filter: false})),
  policyRemovals(before, before).any,
]));""")
        self.assertEqual(got[0]["deny_tools"], ["WebSearch"])
        self.assertTrue(got[0]["any"])
        self.assertEqual(got[0]["deny_bash_patterns"], [])
        self.assertTrue(got[1]["outbound_filter"] and got[1]["any"])
        self.assertFalse(got[2])

    def test_handoff_names_and_js_regex(self):
        got = self.run_node("""
process.stdout.write(JSON.stringify([
  ["mcp__cousin__handoff", "mcp__cousin__*", "mcp__*", "*", "mcp__cousin__send", "Bash"]
    .map(e => mcpNamesTool(e, MCP_HANDOFF_TOOL)),
  jsRegexProblem("\\\\bgit\\\\s+push"), typeof jsRegexProblem("(?P<x>a)"),
  typeof jsRegexProblem("rm\\\\-rf"), jsRegexProblem("[a\\\\-z]"),
]));""")
        self.assertEqual(got[0], [True, True, True, True, False, False])
        self.assertIsNone(got[1])
        self.assertEqual(got[2], "string")
        # `\-` outside a class: fine without `u`, an error with it (the plugin's flag)
        self.assertEqual(got[3], "string")
        self.assertIsNone(got[4])

    def test_server_draft_body_and_suggestion(self):
        got = self.run_node("""
const view = {name: "notes", type: "stdio", command: "/opt/notes",
              args: [{value: "--root", masked: false}, {value: null, masked: true}],
              env: [{name: "NOTES_TOKEN", value: null, masked: true}, {name: "LANG", value: "C", masked: false}]};
const d = mcpServerDraft(view);
const fixed = mcpApplySuggestion(d, {server: "notes", field: "env", key: "NOTES_TOKEN", suggest: "${NOTES_TOKEN}"});
const arg = mcpApplySuggestion(fixed, {server: "notes", field: "args", key: 1, suggest: "${NOTES_ARG}"});
const other = mcpApplySuggestion(d, {server: "else", field: "env", key: "NOTES_TOKEN", suggest: "x"});
const masked = mcpServerDraft({name: "m", type: "stdio", command: null, command_masked: true});
process.stdout.write(JSON.stringify([d.args, mcpServerBody(arg), other === d, masked.command, masked._id,
  mcpServerBody({name: " h ", type: "http", url: "https://x", headers: [{name: "", value: ""}]})]));""")
        self.assertEqual(got[0], ["--root", None])
        self.assertEqual(got[1]["env"][0], {"name": "NOTES_TOKEN", "value": "${NOTES_TOKEN}"})
        self.assertEqual(got[1]["args"], ["--root", "${NOTES_ARG}"])
        self.assertTrue(got[2])
        self.assertEqual((got[3], got[4]), (None, "file:m"))
        self.assertEqual(got[5], {"name": "h", "type": "http", "url": "https://x", "headers": []})


if __name__ == "__main__":
    unittest.main()
