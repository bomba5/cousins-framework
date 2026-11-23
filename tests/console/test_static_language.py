"""The Language page (language.jsx): a view of its own through the seams,
the routes it calls, the reply mode written as a cousin setting, the
console's style rules, and its pure helpers under node."""
import json
import pathlib
import shutil
import subprocess
import unittest

_STATIC = pathlib.Path(__file__).resolve().parents[2] / "cousin_lib" / "console_static"


def _read(name):
    return (_STATIC / name).read_text(encoding="utf-8")


class LanguageJsx(unittest.TestCase):
    def setUp(self):
        self.src = _read("language.jsx")

    def test_registered_as_a_view(self):
        self.assertIn('registerView({ id: "language", label: "Language"', self.src)

    def test_it_calls_its_routes(self):
        self.assertIn('langSend("POST", "/api/language/check", body)', self.src)
        self.assertIn('apiGet("/api/language/config")', self.src)
        self.assertIn('langSend("PUT", "/api/language/config"', self.src)
        self.assertIn("{result.fixed}", self.src)
        self.assertIn("langSend(\"GET\", `/api/language/terms?${q}`)", self.src)
        self.assertIn('langSend("POST", "/api/language/groups", body())', self.src)
        self.assertIn('langSend("POST", "/api/language/derive", body())', self.src)
        self.assertIn('{tab === "glossary" && <LanguageGlossary />}', self.src)

    def test_the_reply_mode_goes_through_the_cousin_settings_route(self):
        self.assertIn("`/api/cousins/${encodeURIComponent(slug)}/settings`", self.src)
        self.assertIn('{ changes: { "plain_language.reply": value === "" ? null : value } }',
                      self.src)

    def test_the_inspector_settings_render_a_choice(self):
        agent = _read("agent.jsx")
        self.assertIn('if (row.type === "choice") {', agent)
        self.assertIn('"plain_language.reply": "plain language"', agent)

    def test_no_em_dash(self):
        self.assertNotIn("\u2014", self.src)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class Helpers(unittest.TestCase):
    def run_node(self, body):
        src = _read("language.jsx")
        start = src.index("// ---- pure helpers")
        end = src.index("// ---- end pure helpers")
        out = subprocess.run(["node", "-e", src[start:end] + body],
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_the_banned_box_round_trips(self):
        got = self.run_node("""
const list = languageBannedParse("leverage = use\\n\\n  utilize=use \\ngoing forward\\n = x");
console.log(JSON.stringify([list, languageBannedText(list)]));
""")
        self.assertEqual(got[0], [{"word": "leverage", "use": "use"},
                                  {"word": "utilize", "use": "use"},
                                  {"word": "going forward", "use": ""}])
        self.assertEqual(got[1], "leverage = use\nutilize = use\ngoing forward")

    def test_the_groups_box(self):
        got = self.run_node("""
console.log(JSON.stringify(languageGroupsParse("thread, conversation ,\\n\\n turn,round\\n , ")));
""")
        self.assertEqual(got, ["thread,conversation", "turn,round"])


if __name__ == "__main__":
    unittest.main()
