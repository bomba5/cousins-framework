"""The example cousin is a living render of the template.

An earlier version ran two definitions of cousin identity and they
drifted apart. This suite is the receipt that this repository cannot:
the checked-in example must equal a fresh render of the current
template, so editing one without the other fails the build.
"""
import pathlib
import tomllib
import unittest

from cousin_lib.template import render_template

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_WREN = _REPO_ROOT / "examples" / "wren"

# The canonical values Wren was generated with. Regenerate the example
# (see docs/cousins.md) when the template changes.
WREN_VALUES = {
    "NAME": "Wren",
    "SLUG": "wren",
    "PORT": 8100,
    "ROLE_ONE_LINE": "example cousin for this framework",
    "ROLE_PARAGRAPH": (
        "You are Wren, the example cousin that ships with this "
        "framework. You exist so a fresh install has a working, "
        "inspectable cousin on day one: your files are what "
        "cousin-spawn produces, nothing more."
    ),
    "VOICE_GUIDE": (
        "Plain, warm, and brief. Answer the question asked before "
        "adding anything else. Address the operator directly and "
        "by name when one is configured. No stage directions, no "
        "invented catchphrases."
    ),
}


class TestWren(unittest.TestCase):
    def test_claude_md_is_exactly_the_current_template_rendered(self):
        template = (
            _REPO_ROOT / "templates" / "cousin-CLAUDE.template.md"
        ).read_text()
        expected = render_template(template, WREN_VALUES)
        self.assertEqual(
            (_WREN / "CLAUDE.md").read_text(), expected,
            "examples/wren drifted from the template; regenerate it",
        )

    def test_voice_section_is_filled(self):
        text = (_WREN / "CLAUDE.md").read_text()
        self.assertIn("## Voice", text)
        voice_body = text.split("## Voice", 1)[1]
        self.assertIn("Plain, warm, and brief", voice_body)
        self.assertNotIn("{{", text)

    def test_cousin_toml_parses_with_matching_identity(self):
        cfg = tomllib.loads((_WREN / "cousin.toml").read_text())
        self.assertEqual(cfg["cousin"]["slug"], "wren")
        self.assertEqual(cfg["chat"]["port"], 8100)


if __name__ == "__main__":
    unittest.main()
