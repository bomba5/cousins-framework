"""The gate gates its own repository.

This is the "runs from commit #1" rule made literal: the unit suite fails
if this tree ever contains an address literal, a home path, a secret shape,
or an opaque binary. Name terms come from the out-of-tree denylist at run
time; here the generic classes alone must hold the line.
"""
import pathlib
import unittest

from cousin_lib.gate.scanner import run_gate

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


class TestGateOnOwnTree(unittest.TestCase):
    def test_repository_passes_its_own_gate(self):
        result = run_gate(_REPO_ROOT)
        details = "\n".join(
            "%s:%s %s %r" % (h.file, h.line, h.kind, h.context)
            for h in result.hits
        )
        self.assertTrue(result.passed, "gate hits in own tree:\n" + details)


if __name__ == "__main__":
    unittest.main()
