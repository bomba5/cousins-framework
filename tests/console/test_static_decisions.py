"""The Decisions view (decisions.jsx), pinned by text and, where node is
installed, by running its pure helpers: open decisions grouped by cousin
and then by group, the group's title, and an answer said back."""
import json
import pathlib
import shutil
import subprocess
import unittest

_STATIC = pathlib.Path(__file__).resolve().parents[2] / "cousin_lib" / "console_static"


def _read(name):
    return (_STATIC / name).read_text(encoding="utf-8")


def _helpers():
    text = _read("decisions.jsx")
    return text[text.index("// ---- decision helpers"):text.index("// ---- end decision helpers")]


class Markup(unittest.TestCase):
    def setUp(self):
        self.text = _read("decisions.jsx")

    def test_registered_as_a_view_using_its_routes(self):
        self.assertIn('registerView({ id: "decisions", label: "Decisions"', self.text)
        self.assertIn('apiGet("/api/decisions")', self.text)
        self.assertIn('apiSend("POST", `/api/decisions/${d.id}/answer`', self.text)
        self.assertNotIn("withdraw", self.text.split("// ---- end decision helpers")[1])
        # the open count is in the header (registerView takes no badge)
        self.assertIn("data-decisions-open", self.text)
        self.assertIn('"fw-decisions-refresh"', self.text)
        self.assertIn('kind === "decisions-refresh"', _read("app.jsx"))
        self.assertIn('apiSend("POST", "/api/decisions/redeliver"', self.text)

    def test_the_recommended_option_is_marked(self):
        self.assertIn("o.key === d.recommendation", self.text)
        self.assertIn("(recommended)", self.text)

    def test_no_em_dash(self):
        self.assertNotIn("\u2014", self.text)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class Helpers(unittest.TestCase):
    def run_node(self, body):
        out = subprocess.run(["node", "-e", _helpers() + body], capture_output=True,
                             text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_grouped_by_cousin_then_group_open_only(self):
        got = self.run_node("""
const rows = [
  {id: 5, cousin: "sam", status: "open", grp: ""},
  {id: 2, cousin: "wren", status: "open", grp: "setup"},
  {id: 3, cousin: "wren", status: "open", grp: ""},
  {id: 4, cousin: "wren", status: "open", grp: "setup"},
  {id: 1, cousin: "wren", status: "answered", grp: "setup"},
];
process.stdout.write(JSON.stringify(decisionGroups(rows).map(c =>
  [c.cousin, c.groups.map(g => [g.title, g.rows.map(r => r.id)])])));""")
        self.assertEqual(got, [["wren", [["group setup", [2, 4]], ["", [3]]]],
                               ["sam", [["", [5]]]]])

    def test_a_skill_stop_is_titled_by_skill_and_stop(self):
        got = self.run_node("""
process.stdout.write(JSON.stringify([
  decisionGroupTitle({skill: "board-check", stop: "review", grp: "board-check:review"}),
  decisionGroupTitle({grp: "setup"}), decisionGroupTitle({grp: ""})]));""")
        self.assertEqual(got, ["skill board-check, stop review", "group setup", ""])

    def test_what_an_answer_still_owes(self):
        got = self.run_node("""
process.stdout.write(JSON.stringify([
  decisionOwes({status: "answered", remembered: true, delivered: true}),
  decisionOwes({status: "answered", remembered: false, delivered: true}),
  decisionOwes({status: "answered", remembered: true, delivered: false}),
  decisionOwes({status: "open"}),
  decisionOwes({status: "answered", remembered: true, delivered: false,
                undeliverable: "the cousin's home is gone"})]));""")
        self.assertEqual(got, ["", "memory not written", "turn not delivered", "",
                               "undeliverable: the cousin's home is gone"])

    def test_answer_text(self):
        got = self.run_node("""
const opts = [{key: "a", text: "keep"}, {key: "b", text: "drop"}];
process.stdout.write(JSON.stringify([
  decisionAnswerText({options: opts, answer_option: "b", answer: "drop"}),
  decisionAnswerText({options: opts, answer_option: "a", answer: "for now"}),
  decisionAnswerText({options: opts, answer_option: null, answer: "neither"}),
]));""")
        self.assertEqual(got, ["b: drop", "a: keep (for now)", "neither"])


if __name__ == "__main__":
    unittest.main()
