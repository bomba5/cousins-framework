"""The one way anything reaches a cousin (docs/design/agent-loop-runner.md)."""
import unittest

from cousin_lib import delivery
from cousin_lib.delivery import DeliveryError, Item


class TestThreadIds(unittest.TestCase):
    def test_a_keyed_kind_renders_kind_colon_key(self):
        self.assertEqual(delivery.thread_id("peer", "testa"), "peer:testa")

    def test_a_bare_kind_renders_alone(self):
        self.assertEqual(delivery.thread_id("schedule"), "schedule")
        self.assertEqual(delivery.thread_id("system"), "system")

    def test_a_keyed_kind_without_a_key_is_refused(self):
        with self.assertRaises(DeliveryError):
            delivery.thread_id("operator")

    def test_a_bare_kind_with_a_key_is_refused(self):
        with self.assertRaises(DeliveryError):
            delivery.thread_id("schedule", "x")

    def test_an_unknown_kind_is_refused(self):
        with self.assertRaises(DeliveryError):
            delivery.thread_id("pane", "wren")

    def test_parse_round_trips_and_keeps_colons_in_the_key(self):
        self.assertEqual(delivery.parse_thread("meeting:12"), ("meeting", "12"))
        self.assertEqual(delivery.parse_thread("loop:a:b"), ("loop", "a:b"))
        self.assertEqual(delivery.parse_thread("system"), ("system", ""))


class TestItem(unittest.TestCase):
    def test_an_item_validates_its_thread_and_source(self):
        with self.assertRaises(DeliveryError):
            Item(thread_id="nope:x", source="chat", body="hi")
        with self.assertRaises(DeliveryError):
            Item(thread_id="system", source="keyboard", body="hi")

    def test_attachments_are_frozen_into_a_tuple(self):
        item = Item(thread_id="operator:Sam", source="chat", body="hi",
                    attachments=["a.png"])
        self.assertEqual(item.attachments, ("a.png",))

    def test_the_three_outcomes_are_the_only_ones(self):
        self.assertEqual((delivery.DELIVERED, delivery.QUEUED,
                          delivery.FAILED),
                         ("delivered", "queued", "failed"))


if __name__ == "__main__":
    unittest.main()
