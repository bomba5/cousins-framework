"""CLI contract: exit codes are the gate's interface to CI."""
import contextlib
import io
import pathlib
import tempfile
import unittest

from cousin_lib.gate.cli import main


class TestCli(unittest.TestCase):
    def _tree(self, files):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = pathlib.Path(tmp.name)
        for rel, content in files.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        return root

    def test_gate_mode_exits_zero_on_clean_tree(self):
        root = self._tree({"a.py": "x = 1\n"})
        self.assertEqual(main(["--root", str(root)]), 0)

    def _denylist(self, terms):
        # The list lives OUTSIDE the scanned root, as it does in real use.
        d = self._tree({"terms.txt": "\n".join(terms) + "\n"})
        return d / "terms.txt"

    def test_gate_mode_exits_one_and_reports_on_hits(self):
        root = self._tree({"a.py": "who = 'zorblatt'\n"})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(
                ["--root", str(root), "--denylist", str(self._denylist(["zorblatt"]))]
            )
        self.assertEqual(code, 1)
        self.assertIn("a.py:1", out.getvalue())

    def test_triage_mode_prints_manifest_and_exits_zero(self):
        root = self._tree({"a.py": "# zorblatt\n"})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(
                [
                    "--root",
                    str(root),
                    "--denylist",
                    str(self._denylist(["zorblatt"])),
                    "--mode",
                    "triage",
                ]
            )
        self.assertEqual(code, 0)
        self.assertIn('"position": "comment"', out.getvalue())


if __name__ == "__main__":
    unittest.main()
