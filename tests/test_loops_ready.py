"""Ready-file triggers inside the loops daemon.

An earlier version ran a separate watcher process for `<name>.ready`
files - a second owner of delivery, with its own tmux path and its
own dedup state. Here the trigger is a tick step: the daemon sees the
file, delivers through its normal path, and removes the file only
after delivery succeeded, like every other commit in the daemon.
"""
import time
import unittest

from cousin_lib.loops import tick
from tests.test_loops import LoopsCase


class ReadyCase(LoopsCase):
    @staticmethod
    def _early():
        # Before any 06:00 daily loop is due, so a delivery counted
        # here is the trigger's, never the schedule's.
        from datetime import datetime
        return datetime.now().replace(hour=1, minute=0,
                                      second=0).timestamp()

    def _ready(self, name, body="", slug="wren"):
        path = self.root / "cousins" / slug / ("%s.ready" % name)
        path.write_text(body)
        return path


class TestNamedLoopTrigger(ReadyCase):
    def test_ready_file_fires_the_named_loop_and_is_removed(self):
        self._cousin("wren", loops_toml=(
            '[[loops]]\nname = "report"\ndaily_at = "06:00"\n'
            'prompt = "write the report"\n'))
        path = self._ready("report")
        report = self._tick(now=self._early())
        self.assertEqual(len(self.delivered), 1)
        slug, text = self.delivered[0]
        self.assertEqual(slug, "wren")
        self.assertIn("write the report", text)
        self.assertIn("### report", text)
        self.assertFalse(path.exists())
        self.assertEqual(report["ready"], ["wren|report"])

    def test_failed_delivery_keeps_the_file_and_reports_once(self):
        self._cousin("wren", loops_toml=(
            '[[loops]]\nname = "report"\ndaily_at = "06:00"\n'
            'prompt = "write the report"\n'))
        path = self._ready("report")
        early = self._early()
        first = self._tick(now=early, deliver=lambda slug, text: False)
        self.assertTrue(path.exists())
        self.assertEqual(first["ready"], [])
        failures = [e for e in first["errors"] if "report.ready" in e]
        self.assertEqual(len(failures), 1)
        # Still failing: the file stays and the report line is not
        # repeated - once means once, not once per tick.
        second = self._tick(now=early + 30,
                            deliver=lambda slug, text: False)
        self.assertTrue(path.exists())
        self.assertEqual([e for e in second["errors"]
                          if "report.ready" in e], [])
        # Delivery works now: fired, removed.
        third = self._tick(now=early + 60)
        self.assertEqual(third["ready"], ["wren|report"])
        self.assertFalse(path.exists())
        self.assertEqual(len(self.delivered), 1)

    def test_ready_fire_does_not_move_the_loop_schedule(self):
        # A trigger is an extra fire, like a manual fire: the daily
        # schedule still owns its own day.
        self._cousin("wren", loops_toml=(
            '[[loops]]\nname = "daily"\ndaily_at = "06:00"\n'
            'prompt = "morning report"\n'))
        from datetime import datetime
        early = self._early()
        self._ready("daily")
        self._tick(now=early)
        self.assertEqual(len(self.delivered), 1)
        late = datetime.now().replace(hour=23, minute=0,
                                      second=0).timestamp()
        self._tick(now=late)
        self.assertEqual(len(self.delivered), 2)


class TestHeartbeatAndMessageTriggers(ReadyCase):
    def test_context_heartbeat_name_delivers_the_beat_and_commits(self):
        home = self._cousin("wren")
        (home / "STATUS.md").write_text("# Wren - STATUS\n")
        path = self._ready("context-heartbeat")
        self._tick()
        self.assertEqual(len(self.delivered), 1)
        _, text = self.delivered[0]
        self.assertIn("Context heartbeat", text)
        self.assertIn("STATUS.md", text)
        self.assertFalse(path.exists())
        # The delta committed: a second beat reports no changes.
        self.assertTrue((home / "data" / "heartbeat-mtimes.json").exists())
        self._ready("context-heartbeat")
        self._tick(now=time.time() + 10)
        _, text = self.delivered[1]
        self.assertIn("No identity files changed", text)

    def test_heartbeat_delta_is_held_when_delivery_fails(self):
        home = self._cousin("wren")
        (home / "STATUS.md").write_text("# Wren - STATUS\n")
        self._ready("context-heartbeat")
        self._tick(deliver=lambda slug, text: False)
        self.assertFalse((home / "data" / "heartbeat-mtimes.json").exists())

    def test_message_name_delivers_the_contents_literally(self):
        self._cousin("wren")
        path = self._ready("wren-message", "  plain line from outside\n")
        report = self._tick()
        self.assertEqual(self.delivered, [("wren", "plain line from outside")])
        self.assertFalse(path.exists())
        self.assertEqual(report["ready"], ["wren|wren-message"])

    def test_empty_message_is_removed_and_reported_never_delivered(self):
        self._cousin("wren")
        path = self._ready("wren-message", "\n")
        report = self._tick()
        self.assertEqual(self.delivered, [])
        self.assertFalse(path.exists())
        self.assertTrue(any("wren-message.ready" in e and "empty" in e
                            for e in report["errors"]))


class TestUnknownAndScope(ReadyCase):
    def test_unknown_name_is_removed_with_a_report_line(self):
        self._cousin("wren")
        path = self._ready("no-such-loop")
        report = self._tick()
        self.assertEqual(self.delivered, [])
        self.assertFalse(path.exists())
        self.assertTrue(any("no-such-loop.ready" in e
                            for e in report["errors"]))
        self.assertEqual(report["ready"], [])

    def test_dead_cousin_keeps_its_ready_file(self):
        # The liveness gate applies: a lingering pane with a dead chat
        # server receives nothing, and the trigger waits.
        self._cousin("wren", loops_toml=(
            '[[loops]]\nname = "report"\ndaily_at = "06:00"\n'
            'prompt = "write the report"\n'))
        path = self._ready("report")
        self._tick(is_alive=lambda slug: False)
        self.assertEqual(self.delivered, [])
        self.assertTrue(path.exists())

    def test_absent_files_mean_nothing_happens(self):
        self._cousin("wren")
        report = self._tick()
        self.assertEqual(self.delivered, [])
        self.assertEqual(report["ready"], [])
        self.assertEqual(report["errors"], [])

    def test_report_carries_the_ready_key_from_tick(self):
        self._cousin("wren")
        self.assertIn("ready", tick(deliver=self._deliver,
                                    is_alive=lambda s: True))


if __name__ == "__main__":
    unittest.main()
