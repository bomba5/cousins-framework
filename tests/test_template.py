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
        # to produce, the same species as an audited surface nothing
        # writes. The table must track pyproject's console scripts.
        out = self._render()
        for cli in ("cousin-chat", "cousin-reply", "cousin-chat-server",
                    "cousin-spawn", "cousin-gate", "cousin-memory",
                    "cousin-job", "cousin-schedule", "cousin-flip",
                    "cousin-self-portrait"):
            self.assertIn("`%s`" % cli, out)

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
        # The one instruction the source framework's drifted copy got
        # backwards: peer replies go out via cousin-chat, and using the
        # own-surface reply path for a peer silently fails to deliver.
        out = self._render()
        self.assertIn("cousin-chat send", out)
        self.assertIn("DOES NOT deliver", out)


if __name__ == "__main__":
    unittest.main()
