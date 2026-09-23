"""FakeRunner beyond the contract: state transitions and stop."""
import time
import unittest

from cousin_lib.delivery import Item
from cousin_lib.runner.fake import FakeRunner
from tests._hermetic import HermeticCase
from tests.runner._home import temp_home


class TestFakeRunner(HermeticCase):
    def setUp(self):
        super().setUp()
        self.home = temp_home(self)

    def test_state_transitions_are_in_the_stream(self):
        r = FakeRunner(self.home); self.addCleanup(lambda: r.stop(timeout=5))
        r.start()
        r.enqueue(Item("operator:priya", "chat", "x", sender="Priya"))
        time.sleep(0.3)
        states = [e["payload"]["to"] for e in r.events() if e["kind"] == "state"]
        self.assertEqual(states[:2], ["running", "idle"])

    def test_stop_is_terminal_and_idempotent(self):
        r = FakeRunner(self.home)
        r.start(); r.stop(timeout=5); r.stop(timeout=5)
        self.assertEqual(r.state(), "stopped")

    def test_rollover_declares_itself_not_implemented_in_this_phase(self):
        r = FakeRunner(self.home); self.addCleanup(lambda: r.stop(timeout=5))
        out = r.rollover("test")
        self.assertEqual(out["ok"], False)
        self.assertIn("phase 4", out["reason"])


if __name__ == "__main__":
    unittest.main()
