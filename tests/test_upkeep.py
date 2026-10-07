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
        self.assertIn("upkeep 15% (with its own schedules 31%)", upkeep.format_measure("wren", m))

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
