"""Memory's validity and review gate, through the commands a person runs:
an obsoleted entry leaves the live set, opposite claims show as a tension
until one is settled, and a turn that writes more than [memory]
review_batch entries is held until reviewed."""
import contextlib
import io
import os
import pathlib
import tempfile
import time
import unittest
from unittest import mock

from cousin_lib import distill, memory, review_gate
from tests._hermetic import HermeticCase


def _home(case, extra=""):
    tmp = tempfile.TemporaryDirectory(); case.addCleanup(tmp.cleanup)
    home = pathlib.Path(tmp.name) / "cousins" / "wren"
    (home / "data").mkdir(parents=True)
    (home / "cousin.toml").write_text('[cousin]\nslug = "wren"\nname = "Wren"\n' + extra)
    p = mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": "/nonexistent/framework-root"})
    p.start(); case.addCleanup(p.stop)
    return home


def _cli(home, *argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        rc = memory.memory_main(["--home", str(home), *argv])
    return rc, out.getvalue()


def _views(home):
    distill.distill(home)
    return "".join(p.read_text() for p in memory.distilled_dir(home).glob("*.md"))


class TestValidity(HermeticCase):
    def test_obsolete_sets_valid_to_and_the_entry_leaves_the_live_set(self):
        home = _home(self)
        _cli(home, "remember", "boiler", "Mallory services the boiler in March.")
        _cli(home, "obsolete", "boiler", "--why", "the boiler was replaced")
        [row] = memory.validity(home)
        self.assertIsNotNone(row["valid_to"])
        self.assertEqual(memory.live_entries(home), [])

    def test_opposite_claims_are_a_tension_and_settling_one_clears_it(self):
        home = _home(self)
        _cli(home, "remember", "bins", "The bins go out on Monday.")
        _cli(home, "remember", "bins", "The bins go out on Thursday, not Monday.")
        rc, out = _cli(home, "tensions")
        self.assertIn("bins (2 live claims)", out)
        monday = memory.validity(home)[0]["id"]
        _cli(home, "obsolete", "bins", "--why", "the council moved it", "--entry", monday)
        self.assertEqual(_cli(home, "tensions")[1].strip(), "no tensions")


class TestReviewGate(HermeticCase):
    def _turn(self, home, n):
        review_gate.begin(home, now=time.time() - 1)          # the runner opens it at start
        for i in range(n):
            _cli(home, "remember", "pantry %d" % i, "Shelf %d holds the quokka tins." % i)
        return review_gate.gate(home, reviewer=None)

    def test_n_entries_in_one_turn_pass_straight_through(self):
        home = _home(self)
        self.assertEqual(self._turn(home, 3)["held"], [])
        self.assertIn("pantry 2", _views(home))

    def test_n_plus_1_are_held_and_reach_distilled_only_once_reviewed(self):
        home = _home(self)
        held = self._turn(home, 4)["held"]
        self.assertEqual(len(held), 4)
        self.assertNotIn("pantry", _views(home))
        with mock.patch("cousin_lib.accounts._inside_cousin_ancestry", return_value=None):
            self.assertEqual(_cli(home, "review", "--keep", *held)[0], 0)
        self.assertIn("pantry 3", _views(home))

    def test_n_comes_from_configuration(self):
        home = _home(self, "[memory]\nreview_batch = 4\n")
        self.assertEqual(self._turn(home, 4)["held"], [])


if __name__ == "__main__":
    unittest.main()
