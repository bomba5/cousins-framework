"""Bash position classification.

The CLIs ship, and in the source tree they are where interpreter paths and
framework roots hide. A bash hit that looks like a rename job and is
actually a behaviour change is the expensive mistake; full-line comments
are the only mechanical position, everything else is code.
"""
import unittest

from cousin_lib.gate.scanner import Scanner


class TestBashPositions(unittest.TestCase):
    def _scan(self, source, filename="tool.sh"):
        return Scanner(name_terms=["zorblatt"]).scan_text(source, filename)

    def test_full_line_comment_is_comment_position(self):
        (hit,) = self._scan("# maintained by zorblatt\nexec true\n")
        self.assertEqual(hit.position, "comment")

    def test_code_line_is_code_position(self):
        (hit,) = self._scan('OWNER="zorblatt"\n')
        self.assertEqual(hit.position, "code")

    def test_shebang_is_code_position(self):
        # an interpreter path in a shebang is behaviour, not prose
        (hit,) = self._scan("#!/usr/bin/env zorblatt\necho ok\n")
        self.assertEqual(hit.position, "code")

    def test_extensionless_file_with_bash_shebang_uses_bash_rules(self):
        source = "#!/usr/bin/env bash\n# zorblatt wrote this\n"
        (hit,) = self._scan(source, filename="bin/cousin-tool")
        self.assertEqual(hit.position, "comment")

    def test_extensionless_file_with_python_shebang_uses_python_rules(self):
        source = '#!/usr/bin/env python3\n"""zorblatt notes."""\n'
        (hit,) = self._scan(source, filename="bin/cousin-tool")
        self.assertEqual(hit.position, "docstring")


class TestDispatchDecisions(unittest.TestCase):
    """Markdown is prose by decision (docs are rewritten; a name there is a
    real hit but never a behaviour change). Anything unrecognized is
    unclassified, and unclassified is structural: unknown is unsafe."""

    def _scan(self, source, filename):
        return Scanner(name_terms=["zorblatt"]).scan_text(source, filename)

    def test_markdown_is_prose_deliberately(self):
        (hit,) = self._scan("zorblatt maintains this.\n", "README.md")
        self.assertEqual(hit.position, "prose")

    def test_unknown_file_type_is_unclassified(self):
        (hit,) = self._scan('owner = "zorblatt"\n', "cfg.toml")
        self.assertEqual(hit.position, "unclassified")

    def test_unclassified_hits_make_the_file_verdict_structural(self):
        from cousin_lib.gate.scanner import triage_verdicts

        hits = self._scan('owner = "zorblatt"\n', "cfg.toml")
        self.assertEqual(triage_verdicts(hits)["cfg.toml"], "structural")


if __name__ == "__main__":
    unittest.main()
