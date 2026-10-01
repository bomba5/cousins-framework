"""Receipt, protocol and priority (docs/reference/runners.md)."""
import unittest

from cousin_lib.delivery import Item
from cousin_lib.runner import base
from tests._hermetic import HermeticCase


class TestPriority(HermeticCase):
    def test_flip_comes_before_everything(self):
        self.assertLess(base.priority("flip", "system"),
                        base.priority("chat", "operator:priya"))

    def test_operator_chat_before_meeting_before_peer_chat(self):
        self.assertLess(base.priority("chat", "operator:priya"),
                        base.priority("meeting", "meeting:7"))
        self.assertLess(base.priority("meeting", "meeting:7"),
                        base.priority("chat", "peer:testa"))

    def test_peer_chat_before_schedule_before_loop(self):
        self.assertLess(base.priority("chat", "peer:testa"),
                        base.priority("schedule", "schedule"))
        self.assertLess(base.priority("schedule", "schedule"),
                        base.priority("loop", "loop:heartbeat"))

    def test_a_reaction_ranks_with_operator_chat(self):
        self.assertEqual(base.priority("reaction", "operator:priya"),
                         base.priority("chat", "operator:priya"))


class TestReceipt(HermeticCase):
    def test_receipt_is_frozen(self):
        r = base.Receipt(inbox_id=3, outcome="queued")
        with self.assertRaises(Exception):
            r.outcome = "delivered"

    def test_runner_error_is_an_exception(self):
        self.assertTrue(issubclass(base.RunnerError, Exception))


if __name__ == "__main__":
    unittest.main()
