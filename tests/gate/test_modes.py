"""Gate mode is zero-tolerance; triage mode classifies and aggregates.

Same engine, two thresholds: the public tree has no legitimate mechanical
hits, while the triage over a private tree exists to describe them.
"""
import json
import pathlib
import tempfile
import unittest

from cousin_lib.gate.scanner import Scanner, manifest_lines, run_gate, triage_verdicts


class _TreeCase(unittest.TestCase):
    def _tree(self, files):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = pathlib.Path(tmp.name)
        for rel, content in files.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        return root


class TestGateMode(_TreeCase):
    def test_gate_passes_on_a_clean_tree(self):
        root = self._tree({"src/mod.py": "x = 1\n"})
        result = run_gate(root, name_terms=["zorblatt"])
        self.assertTrue(result.passed)
        self.assertEqual(result.hits, [])

    def test_gate_fails_on_a_mechanical_hit_too(self):
        # "It is only a docstring" is not a state the public tree may be in.
        root = self._tree({"src/mod.py": '"""zorblatt wrote this."""\n'})
        result = run_gate(root, name_terms=["zorblatt"])
        self.assertFalse(result.passed)
        self.assertEqual(len(result.hits), 1)


class TestTriage(_TreeCase):
    def test_structural_hit_outranks_mechanical_in_file_verdict(self):
        root = self._tree(
            {"src/mod.py": '# zorblatt drafted this\ngate = "zorblatt"\n'}
        )
        hits = Scanner(name_terms=["zorblatt"]).scan_tree(root)
        verdicts = triage_verdicts(hits)
        self.assertEqual(verdicts["src/mod.py"], "structural")

    def test_comment_only_file_verdict_is_mechanical(self):
        root = self._tree({"src/mod.py": "# zorblatt drafted this\nx = 1\n"})
        hits = Scanner(name_terms=["zorblatt"]).scan_tree(root)
        self.assertEqual(triage_verdicts(hits)["src/mod.py"], "mechanical")

    def test_manifest_lines_are_sorted_json_and_deterministic(self):
        root = self._tree(
            {
                "b.py": "u = 'zorblatt'\n",
                "a.py": "# zorblatt\nv = 'zorblatt'\n",
            }
        )
        hits = Scanner(name_terms=["zorblatt"]).scan_tree(root)
        lines = manifest_lines(hits)
        self.assertEqual(lines, manifest_lines(list(reversed(hits))))
        parsed = [json.loads(l) for l in lines]
        self.assertEqual(
            [(p["file"], p["line"]) for p in parsed],
            [("a.py", 1), ("a.py", 2), ("b.py", 1)],
        )


if __name__ == "__main__":
    unittest.main()
