"""The one way anything reaches a cousin (docs/design/agent-loop-runner.md)."""
import unittest

from cousin_lib import delivery
from cousin_lib.delivery import DeliveryError, Item
import os
import pathlib
import tempfile
from datetime import datetime, timezone
from cousin_lib.server.injection import compose_delivery


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


class TestTmuxRender(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = pathlib.Path(tmp.name)
        (self.home / "data").mkdir()
        self.marker = self.home / "data" / ".last-user-msg"
        self.now = datetime(2026, 8, 6, 5, 30, tzinfo=timezone.utc)
        self.backend = delivery.TmuxBackend()

    def test_chat_is_byte_identical_to_compose_delivery(self):
        self.marker.touch()
        ago = self.now.timestamp() - 12 * 60
        os.utime(self.marker, (ago, ago))
        item = Item(thread_id="operator:Sam", source="chat", sender="Sam",
                    body="two\nlines  here", attachments=("/tmp/a.png",))
        expected = compose_delivery("Sam", "two\nlines  here",
                                    marker_path=self.marker,
                                    attachments=("/tmp/a.png",), now=self.now)
        self.assertEqual(self.backend.render(self.home, item, now=self.now),
                         expected)

    def test_chat_context_rides_as_the_old_recall_suffix_did(self):
        item = Item(thread_id="operator:Sam", source="chat", sender="Sam",
                    body="where is the plan", context="[fw-recall] plan.md")
        expected = compose_delivery(
            "Sam", "where is the plan" + " " + "[fw-recall] plan.md",
            marker_path=self.marker, now=self.now)
        self.assertEqual(self.backend.render(self.home, item, now=self.now),
                         expected)

    def test_a_chat_hook_line_is_composed_like_chat(self):
        item = Item(thread_id="system", source="hook", sender="fw-hook",
                    body="rotated")
        expected = compose_delivery("fw-hook", "rotated",
                                    marker_path=self.marker, now=self.now)
        self.assertEqual(self.backend.render(self.home, item, now=self.now),
                         expected)

    def test_a_schedule_gets_its_prefix(self):
        item = Item(thread_id="schedule", source="schedule", body="check CI")
        self.assertEqual(self.backend.render(self.home, item),
                         "[cousin-schedule] check CI")

    def test_every_other_source_is_typed_exactly_as_given(self):
        for source, thread in (("reaction", "operator:Sam"),
                               ("loop", "loop:digest"),
                               ("meeting", "meeting:7"),
                               ("flip", "system"), ("boot", "system")):
            body = "[x] line one\nline two"
            item = Item(thread_id=thread, source=source, body=body)
            self.assertEqual(self.backend.render(self.home, item), body)


if __name__ == "__main__":
    unittest.main()
