"""#253: each cousin's spend split into upkeep and work, measured from
the runner's stream (result + usage events) and the inbox rows a turn
answered. Hermetic: a temp home with a hand-written stream and inbox."""
import contextlib
import io
import json
import os
import sqlite3
import time
from unittest import mock

from cousin_lib import upkeep
from tests._hermetic import HermeticCase
from tests.runner._home import temp_home


class UpkeepCase(HermeticCase):
    def setUp(self):
        super().setUp()
        self.home = temp_home(self, runner="sdk")
        (self.home / "data" / "stream").mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.home / "data" / "inbox.db")
        self.addCleanup(self.db.close)
        self.db.execute("CREATE TABLE inbox (id INTEGER PRIMARY KEY, thread_id TEXT NOT NULL,"
                        " source TEXT NOT NULL, body TEXT NOT NULL)")
        self.events, self.now = [], time.time()

    def row(self, i, source, thread, body):
        self.db.execute("INSERT INTO inbox VALUES (?, ?, ?, ?)", (i, thread, source, body))
        self.db.commit()

    def turn(self, ids, cost, tokens=100, ago=60, background=False):
        ts = self.now - ago
        payload = {"inbox_ids": ids}
        if background:
            payload["background"] = True
        self.events.append({"ts": ts, "kind": "result", "payload": payload})
        self.events.append({"ts": ts, "kind": "usage", "payload": {"cost_usd": cost, "total": tokens}})

    def write(self):
        path = self.home / "data" / "stream" / "sdk-test.jsonl"
        path.write_text("".join(json.dumps(e) + "\n" for e in self.events))


class TestMeasure(UpkeepCase):
    def test_the_split_and_the_kinds(self):
        self.row(1, "loop", "loop:daemon", "Context heartbeat. No identity files changed")
        self.row(2, "chat", "operator:ana", "hi")
        self.row(3, "boot", "system", "STATE DIGEST")
        self.row(4, "loop", "loop:daemon", "[cousin-schedule] Self-check: read the job log")
        self.row(5, "loop", "loop:daemon", "Context heartbeat. What changed")
        self.row(6, "chat", "operator:ana", "and this")
        self.turn([1], 1.0)
        self.turn([2], 3.0)
        self.turn([3], 1.0)
        self.turn([4], 2.0)
        self.turn([5, 6], 4.0)      # a heartbeat the operator's message joined is work
        self.turn([], 0.5)          # a result with no rows
        self.turn([], 1.5, background=True)     # the SDK woke it for a finished task
        self.write()
        m = upkeep.measure(self.home, days=7, now=self.now)
        self.assertEqual(m["turns"], 7)
        self.assertAlmostEqual(m["classes"]["upkeep"]["cost_usd"], 2.0)
        self.assertAlmostEqual(m["classes"]["self"]["cost_usd"], 2.0)
        self.assertAlmostEqual(m["classes"]["work"]["cost_usd"], 8.5)
        self.assertAlmostEqual(m["classes"]["other"]["cost_usd"], 0.5)
        self.assertAlmostEqual(m["upkeep_share"], 2.0 / 13.0)
        self.assertAlmostEqual(m["upkeep_or_self_share"], 4.0 / 13.0)
        self.assertEqual(set(m["kinds"]), {"heartbeat", "chat", "boot", "schedule", "none", "task"})
        self.assertIn("upkeep 31% (the framework's 15%, the rest its own schedules and job notices)", upkeep.format_measure("wren", m))

    def test_an_old_stream_marks_a_task_turn_by_its_notification(self):
        self.row(1, "chat", "operator:ana", "hi")
        self.events.append({"ts": self.now - 70, "kind": "system",
                            "payload": {"subtype": "task_notification"}})
        self.turn([], 1.5)          # written before the runner set `background`
        self.turn([], 0.5)          # no notification: still a row-less turn
        self.events.append({"ts": self.now - 50, "kind": "system",
                            "payload": {"subtype": "task_notification"}})
        self.turn([1], 2.0)         # a chat turn stays a chat turn
        self.write()
        m = upkeep.measure(self.home, days=7, now=self.now)
        self.assertEqual(set(m["kinds"]), {"task", "none", "chat"})
        self.assertAlmostEqual(m["classes"]["work"]["cost_usd"], 3.5)

    def test_the_window(self):
        self.row(1, "chat", "operator:ana", "hi")
        self.turn([1], 1.0, ago=10 * 86400)
        self.write()
        self.assertEqual(upkeep.measure(self.home, days=7, now=self.now)["turns"], 0)

    def test_a_cousin_with_no_stream_reports_nothing(self):
        m = upkeep.measure(self.home, days=7, now=self.now)
        self.assertEqual((m["turns"], m["upkeep_share"]), (0, None))
        self.assertIn("no costed turns", upkeep.format_measure("wren", m))

    def test_the_file_cache_sees_a_grown_file(self):
        self.row(1, "chat", "operator:ana", "hi")
        self.turn([1], 1.0)
        self.write()
        self.assertEqual(upkeep.measure(self.home, days=7, now=self.now)["turns"], 1)
        self.turn([1], 1.0)
        self.write()
        self.assertEqual(upkeep.measure(self.home, days=7, now=self.now)["turns"], 2)

    def test_the_cli_bounds_days(self):
        with mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": str(self.home.parent.parent)}), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(upkeep.upkeep_main(["--days", "0"]), 2)

    def test_the_cli_prints_one_cousin(self):
        self.row(1, "chat", "operator:ana", "hi")
        self.turn([1], 1.0)
        self.write()
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": str(self.home.parent.parent)}), \
                contextlib.redirect_stdout(out):
            rc = upkeep.upkeep_main([self.home.name, "--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.getvalue())[self.home.name]["turns"], 1)


class TestGenerationIdle(HermeticCase):
    """#280: a generation whose inbox since its start holds only upkeep
    rows is idle, and its daily flip is skipped."""

    def setUp(self):
        super().setUp()
        from cousin_lib.runner.inbox import Inbox
        self.home = temp_home(self)
        self.inbox = Inbox(self.home)

    def put(self, source, body, at):
        from cousin_lib.delivery import Item
        rec = self.inbox.put(Item("loop:daemon" if source == "loop" else "operator:priya",
                                  source, body, sender="x"))
        conn = sqlite3.connect(self.inbox.path)
        conn.execute("UPDATE inbox SET created_at=? WHERE id=?", (at, rec))
        conn.commit()
        conn.close()

    def test_heartbeats_and_the_boot_alone_are_idle(self):
        self.put("boot", "STATE DIGEST", 1000)
        self.put("loop", "Context heartbeat. nothing new", 2000)
        self.assertTrue(upkeep.generation_idle(self.home, 1000))

    def test_no_rows_at_all_is_idle(self):
        self.put("chat", "old work", 500)        # before the generation
        self.assertTrue(upkeep.generation_idle(self.home, 1000))

    def test_a_chat_row_is_work(self):
        self.put("loop", "Context heartbeat. nothing new", 2000)
        self.put("chat", "hello", 3000)
        self.assertFalse(upkeep.generation_idle(self.home, 1000))

    def test_a_schedule_it_set_itself_is_not_idle(self):
        self.put("loop", "[cousin-schedule] #4 check the build", 2000)
        self.assertFalse(upkeep.generation_idle(self.home, 1000))

    def test_no_inbox_is_never_idle(self):
        self.inbox.path.unlink()
        self.assertFalse(upkeep.generation_idle(self.home, 1000))



class TestAlarm(HermeticCase):
    """#288: the headline is upkeep plus the cousin's own schedules and job
    notices, and an alarm per cousin fails a health row over it."""

    def setUp(self):
        super().setUp()
        self.home = temp_home(self)

    def agent(self, extra):
        with open(self.home / "cousin.toml", "a") as fh:
            fh.write(extra)

    def m(self, share, cost=10.0):
        return {"cost_usd": cost, "headline_share": share, "turns": 5, "days": 7}

    def test_no_alarm_set_checks_nothing(self):
        self.assertIsNone(upkeep.alarm(self.home))

    def test_over_the_alarm_fails_with_the_numbers(self):
        self.agent("upkeep_alarm_percent = 50\n")
        with mock.patch.object(upkeep, "measure", return_value=self.m(0.63, 10.17)):
            ok, error = upkeep.alarm(self.home)
        self.assertFalse(ok)
        self.assertIn("63% of $10.17 over 7 days (alarm at 50%)", error)

    def test_under_the_alarm_or_under_a_dollar_is_ok(self):
        self.agent("upkeep_alarm_percent = 50\n")
        for m in (self.m(0.4), self.m(0.99, 0.5)):
            with mock.patch.object(upkeep, "measure", return_value=m):
                self.assertEqual(upkeep.alarm(self.home), (True, None))

    def test_the_headline_counts_self(self):
        self.assertIn("headline_share", upkeep.measure(self.home))
