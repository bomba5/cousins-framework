"""The gate gates its own repository.

This is the "runs from commit #1" rule made literal: the unit suite fails
if this tree ever contains an address literal, a home path, a secret shape,
or an opaque binary. Name terms come from the out-of-tree denylist at run
time; here the generic classes alone must hold the line.
"""
import pathlib
import tempfile
import unittest

from cousin_lib.gate.scanner import Scanner, run_gate

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


class TestGateOnOwnTree(unittest.TestCase):
    def test_repository_passes_its_own_gate(self):
        result = run_gate(_REPO_ROOT)
        details = "\n".join(
            "%s:%s %s %r" % (h.file, h.line, h.kind, h.context)
            for h in result.hits
        )
        self.assertTrue(result.passed, "gate hits in own tree:\n" + details)

    def test_dot_directories_are_not_a_blind_spot(self):
        # CI workflows and other dotfiles can carry secrets; a future
        # _SKIP_DIRS change that excluded .github would silently stop
        # gating them. Plant a term under a dot-directory and require
        # the scan to find it.
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            planted = root / ".github" / "workflows"
            planted.mkdir(parents=True)
            (planted / "ci.yml").write_text("# zqxjkbanned marker\n")
            hits = Scanner(name_terms=["zqxjkbanned"]).scan_tree(root)
            self.assertTrue(
                any(".github" in h.file for h in hits),
                "the scanner does not reach dot-directories")


if __name__ == "__main__":
    unittest.main()
