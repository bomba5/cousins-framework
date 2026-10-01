"""The cousin template and its renderer.

The template is the ONLY definition of a new cousin's CLAUDE.md; an
unsubstituted placeholder in rendered output is a spawn failure, not a
TODO. Both halves of that contract are pinned here.
"""
import pathlib
import unittest

from cousin_lib.template import TemplateError, render_template

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_TEMPLATE = _REPO_ROOT / "templates" / "cousin-CLAUDE.template.md"

_FULL_VALUES = {
    "NAME": "Wren",
    "SLUG": "wren",
    "PORT": "8100",
    "ROLE_ONE_LINE": "example cousin",
    "ROLE_PARAGRAPH": "You are the example cousin for this framework.",
    "VOICE_GUIDE": "Plain and helpful. Address the operator directly.",
}


class TestRenderer(unittest.TestCase):
    def test_substitutes_every_placeholder(self):
        out = render_template("{{NAME}} ({{SLUG}}) on {{PORT}}", {
            "NAME": "Wren", "SLUG": "wren", "PORT": "8100",
        })
        self.assertEqual(out, "Wren (wren) on 8100")

    def test_leftover_placeholder_is_an_error_naming_the_key(self):
        with self.assertRaises(TemplateError) as ctx:
            render_template("hello {{VOICE_GUIDE}}", {})
        self.assertIn("VOICE_GUIDE", str(ctx.exception))

    def test_values_are_inserted_literally(self):
        out = render_template("{{ROLE_PARAGRAPH}}",
                              {"ROLE_PARAGRAPH": r"a \1 ${x} 50% deal"})
        self.assertEqual(out, r"a \1 ${x} 50% deal")

    def test_authoring_comments_are_stripped_from_the_render(self):
        # Template comments are guidance for template EDITORS ("replace
        # this", "do not ship without that") - persisting them into a
        # cousin's bedrock is drift-bait. Placeholders inside comments
        # are documentation, not omissions.
        out = render_template(
            "# {{NAME}}\n<!-- fill {{VOICE_GUIDE}} before saving -->\nx\n",
            {"NAME": "Wren"},
        )
        self.assertEqual(out, "# Wren\nx\n")


class TestNoChatServerInTheTemplate(unittest.TestCase):
    def test_the_template_names_no_chat_server_and_no_port(self):
        # template_sync pushes the template to every cousin, so a
        # line naming the retired chat server or its port would teach it
        text = _TEMPLATE.read_text()
        for word in ("{{PORT}}", "chat-server", "chat server",
                     "cousin-chat-watchdog", "_reply`"):
            self.assertNotIn(word, text)


class TestShippedTemplate(unittest.TestCase):
    def _render(self):
        return render_template(_TEMPLATE.read_text(), _FULL_VALUES)

    def test_renders_completely_with_the_six_documented_values(self):
        # This is also the null-operator proof: no operator key exists,
        # so the template cannot require a person to render.
        out = self._render()
        self.assertNotIn("{{", out)

    def test_carries_the_day_one_doctrine(self):
        out = self._render()
        for anchor in ("## Voice", "IN-CHARACTER", "OUT-OF-CHARACTER",
                       "## Hard rules", "## Memory",
                       "## Append your cousin-specific sections"):
            self.assertIn(anchor, out)

    def test_cli_table_covers_every_shipped_cousin_cli(self):
        # v1 shipped a memory subsystem the first template never
        # mentioned - producer tooling with nothing telling the cousin
        # to produce. The mechanism version of the fix: the list of
        # required rows is DERIVED from pyproject's console scripts, so
        # a new CLI cannot ship without its template row.
        import tomllib
        pyproject = tomllib.loads(
            (_REPO_ROOT / "pyproject.toml").read_text())
        clis = pyproject["project"]["scripts"].keys()
        self.assertGreaterEqual(len(clis), 10)
        out = self._render()
        for cli in clis:
            self.assertIn("`%s`" % cli, out,
                          "%s shipped without a template row" % cli)

    def test_memory_doctrine_names_the_layout_and_the_producer_loop(self):
        out = self._render()
        self.assertIn("memory/", out)
        self.assertIn("notes/", out)
        self.assertIn("cousin-memory decide", out)
        self.assertIn("cousin-memory search", out)

    def test_flip_carries_the_do_not_self_flip_caveat(self):
        out = self._render()
        self.assertIn("DO NOT run", out)

    def test_voice_section_carries_the_authored_never_improvised_rule(self):
        out = self._render()
        self.assertIn("authored, never improvised", out)

    def test_peer_reply_pitfall_is_stated(self):
        # The one instruction an earlier version's drifted copy got
        # backwards: peer replies go out via cousin-chat, and using the
        # own-surface reply path for a peer silently fails to deliver.
        out = self._render()
        self.assertIn("cousin-chat send", out)
        self.assertIn("DOES NOT deliver", out)

    def test_mcp_tools_are_named_as_the_preferred_interface(self):
        # The MCP tools were one table row; a cousin reading the CLI
        # table reached for the CLIs and the tools went unused.
        out = self._render()
        self.assertIn("## Tools: MCP first, CLIs as the fallback", out)
        for tool in ("mcp__cousin__memory", "mcp__cousin__send",
                     "mcp__cousin__job", "mcp__cousin__schedule"):
            self.assertIn(tool, out)

    def test_automatic_job_tracking_is_stated(self):
        out = self._render()
        self.assertIn("tracked automatically", out)
        self.assertIn("run_in_background", out)

    def test_job_run_is_the_doctrine_and_the_cli_is_only_the_fallback(self):
        # a tracked shell command goes through the job
        # tool's `run` command first; `cousin-job start shell` through
        # Bash is named only as what to use when the tool is missing.
        out = self._render()
        self.assertIn("mcp__cousin__job` - start, done, fail, list, show,"
                      " run", out)
        self.assertIn("goes through the job tool's `run` command", out)
        self.assertIn("When the tool is missing, the fallback", out)
        self.assertIn("cousin-job start shell", out)


if __name__ == "__main__":
    unittest.main()
