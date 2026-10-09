"""The nightly crash harness (tests/crash_nightly.py) holds in the suite:
a round at each point it reaches, killed at its first hit, leaves stores
that a restart and a retry bring to every invariant."""
import unittest

from tests import crash_nightly
from tests._hermetic import HermeticCase


class TestNightlyRounds(HermeticCase):
    def test_every_point_the_scenario_reaches_recovers(self):
        for point in sorted(crash_nightly.POINTS):
            with self.subTest(point=point):
                out = crash_nightly.one_round_at("%s:1" % point)
                # job.exited kills the job's own runner, a child of the
                # scenario, which goes on
                self.assertEqual(out["killed"], point != "job.exited", out)
                self.assertEqual(out["broke"], [], out)

    def test_a_seed_picks_the_same_rounds(self):
        self.assertEqual([crash_nightly.pick("s", n) for n in range(5)],
                         [crash_nightly.pick("s", n) for n in range(5)])


if __name__ == "__main__":
    unittest.main()
