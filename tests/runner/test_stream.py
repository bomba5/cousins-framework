"""JSONL, monotonically numbered, readable while written."""
import json
import unittest

from cousin_lib.runner.stream import EventStream
from tests._hermetic import HermeticCase
from tests.runner._home import temp_home


class TestEventStream(HermeticCase):
    def setUp(self):
        super().setUp()
        self.home = temp_home(self)

    def test_append_numbers_from_one_and_tail_after_resumes_exactly(self):
        s = EventStream(self.home, "sess-1")
        self.assertEqual([s.append("state", {"to": "running"}), s.append("text", {"t": "hi"}),
                          s.append("result", {})], [1, 2, 3])
        self.assertEqual([e["seq"] for e in s.tail()], [1, 2, 3])
        self.assertEqual([e["seq"] for e in s.tail(after=2)], [3])
        self.assertEqual(list(s.tail(after=3)), [])

    def test_numbering_continues_across_instances(self):
        EventStream(self.home, "sess-1").append("a", {})
        self.assertEqual(EventStream(self.home, "sess-1").append("b", {}), 2)

    def test_a_truncated_last_line_is_skipped_not_fatal(self):
        s = EventStream(self.home, "sess-1")
        s.append("a", {}); s.append("b", {})
        with open(s.path, "a") as f:
            f.write('{"seq": 3, "kind": "c", "pay')   # the writer died mid-line
        self.assertEqual([e["kind"] for e in s.tail()], ["a", "b"])
        self.assertEqual(EventStream(self.home, "sess-1").append("d", {}), 3)
        self.assertEqual([e["kind"] for e in EventStream(self.home, "sess-1").tail()], ["a", "b", "d"])
        self.assertEqual([e["seq"] for e in EventStream(self.home, "sess-1").tail()], [1, 2, 3])

    def test_two_readers_do_not_disturb_the_writer(self):
        s = EventStream(self.home, "sess-1")
        s.append("a", {})
        r1 = s.tail(); r2 = s.tail()
        next(r1); next(r2)
        s.append("b", {})
        self.assertEqual([e["kind"] for e in EventStream(self.home, "sess-1").tail()], ["a", "b"])

    def test_events_carry_a_timestamp_and_the_payload(self):
        s = EventStream(self.home, "sess-1")
        s.append("tool", {"name": "Bash"})
        e = next(s.tail())
        self.assertIn("ts", e)
        self.assertEqual(e["payload"], {"name": "Bash"})
        with open(s.path) as f:
            self.assertEqual(json.loads(f.readline())["kind"], "tool")


if __name__ == "__main__":
    unittest.main()
