"""The loops additions the console contract names: the daemon-owned
fire log, request cancellation, disabled loops made visible, the
validated atomic save of a cousin's [[loops]], and next-due
computation with the daemon's own due logic."""
import json
import os
import pathlib
import tempfile
import tomllib
import unittest
from datetime import datetime
from unittest import mock

from cousin_lib.loops import (cancel_request, list_requests,
                              load_cousin_loops, next_due, read_fires,
                              save_cousin_loops, submit_request, tick,
                              validate_loops)


class LoopsCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        (self.root / "config").mkdir()
        patcher = mock.patch.dict(os.environ,
                                  {"FRAMEWORK_ROOT": str(self.root)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _cousin(self, slug="wren", loops_toml=""):
        home = self.root / "cousins" / slug
        (home / "data").mkdir(parents=True, exist_ok=True)
        (home / "cousin.toml").write_text(
            '[cousin]\nslug = "%s"\nname = "%s"\n'
            '[chat]\nport = 8100\n[heartbeat]\ncontext_beat_seconds = 0\n%s'
            % (slug, slug.capitalize(), loops_toml))
        return home


class TestFireLog(LoopsCase):
    def test_tick_appends_one_line_per_delivered_fire(self):
        self._cousin(loops_toml='[[loops]]\nname = "report"\n'
                                'interval_seconds = 60\nprompt = "go"\n')
        tick(deliver=lambda s, t: True, is_alive=lambda s: True,
             now=1000.0)
        rows = read_fires()
        self.assertEqual(rows, [{"ts": 1000.0, "cousin": "wren",
                                 "loop": "report"}])
        path = self.root / "data" / "loops-fires.jsonl"
        self.assertEqual(len(path.read_text().splitlines()), 1)

    def test_a_failed_delivery_writes_no_fire_line(self):
        self._cousin(loops_toml='[[loops]]\nname = "report"\n'
                                'interval_seconds = 60\nprompt = "go"\n')
        tick(deliver=lambda s, t: False, is_alive=lambda s: True,
             now=1000.0)
        self.assertEqual(read_fires(), [])

    def test_read_fires_skips_garbage_lines_and_respects_limit(self):
        path = self.root / "data" / "loops-fires.jsonl"
        path.parent.mkdir()
        path.write_text('{"ts": 1, "cousin": "a", "loop": "x"}\n'
                        'not json\n'
                        '{"ts": 2, "cousin": "a", "loop": "x"}\n')
        self.assertEqual([r["ts"] for r in read_fires()], [1, 2])
        self.assertEqual([r["ts"] for r in read_fires(limit=1)], [2])


class TestCancelRequest(LoopsCase):
    def test_marks_a_pending_row_cancelled_once(self):
        rid = submit_request("flip", cousin="wren",
                             payload={"fire_at": 1})
        self.assertTrue(cancel_request(rid))
        self.assertEqual(list_requests()[0]["status"], "cancelled")
        self.assertFalse(cancel_request(rid))
        self.assertFalse(cancel_request(99999))

    def test_a_cancelled_flip_is_not_fired_by_the_walker(self):
        self._cousin()
        rid = submit_request("flip", cousin="wren",
                             payload={"fire_at": 1.0})
        cancel_request(rid)
        flips = []
        tick(deliver=lambda s, t: True, is_alive=lambda s: True,
             now=1000.0, do_flip=lambda s: flips.append(s) or {"ok": True})
        self.assertEqual(flips, [])


class TestHeartbeatFireRequest(LoopsCase):
    def test_a_fire_request_named_context_heartbeat_delivers_the_beat(self):
        self._cousin()
        rid = submit_request("fire", cousin="wren",
                             payload={"loop": "context-heartbeat"})
        delivered = []
        tick(deliver=lambda s, t: delivered.append((s, t)) or True,
             is_alive=lambda s: True, now=1000.0)
        self.assertEqual(len(delivered), 1)
        self.assertIn("Context heartbeat", delivered[0][1])
        row = next(r for r in list_requests() if r["id"] == rid)
        self.assertEqual(row["status"], "done")
        self.assertIn("wren", (self.root / "data" / "loops-state.json")
                      .read_text())

    def test_an_unknown_loop_still_fails_loudly(self):
        self._cousin()
        rid = submit_request("fire", cousin="wren", payload={"loop": "nope"})
        tick(deliver=lambda s, t: True, is_alive=lambda s: True, now=1000.0)
        row = next(r for r in list_requests() if r["id"] == rid)
        self.assertEqual(row["status"], "failed")


class TestLoadIncludesDisabled(LoopsCase):
    def test_disabled_loops_are_visible_on_request_only(self):
        home = self._cousin(loops_toml=(
            '[[loops]]\nname = "on"\ninterval_seconds = 60\nprompt = "a"\n'
            '[[loops]]\nname = "off"\ninterval_seconds = 60\nprompt = "b"\n'
            'enabled = false\n'))
        loops, errors = load_cousin_loops(home)
        self.assertEqual([l["name"] for l in loops], ["on"])
        loops, errors = load_cousin_loops(home, include_disabled=True)
        self.assertEqual([l["name"] for l in loops], ["on", "off"])
        self.assertEqual(errors, [])


class TestValidateAndSave(LoopsCase):
    def test_validation_names_the_offending_index(self):
        errs = validate_loops([
            {"name": "ok", "interval_seconds": 60, "prompt": "x"},
            {"name": "Bad Name", "interval_seconds": 60, "prompt": "x"},
            {"name": "two", "interval_seconds": 60, "cron": "* * * * *",
             "prompt": "x"},
            {"name": "empty", "daily_at": "09:00", "prompt": " "},
            {"name": "ok", "interval_seconds": 60, "prompt": "dup"},
            {"name": "days", "daily_at": "09:00", "prompt": "x",
             "days": ["monday"]},
        ])
        self.assertEqual(len(errs), 5)
        for idx in (1, 2, 3, 4, 5):
            self.assertTrue(any("[%d]" % idx in e for e in errs), errs)
        self.assertEqual(validate_loops([]), [])
        self.assertTrue(validate_loops("not a list"))

    def test_save_replaces_the_array_atomically_and_keeps_the_rest(self):
        home = self._cousin(loops_toml=(
            '[[loops]]\nname = "old"\ninterval_seconds = 5\nprompt = "z"\n'
            '\n[runtime]\nsession_id = "abc"\n'))
        entries = [
            {"name": "report", "interval_seconds": 3600,
             "prompt": 'say "hi"\nthen stop', "enabled": True},
            {"name": "daily", "daily_at": "09:00", "days": ["mon", "fri"],
             "prompt": "morning", "enabled": False, "hidden": True},
        ]
        saved = save_cousin_loops(home, entries)
        data = tomllib.loads((home / "cousin.toml").read_text())
        self.assertEqual(data["runtime"]["session_id"], "abc")
        self.assertEqual(data["cousin"]["slug"], "wren")
        self.assertEqual([l["name"] for l in data["loops"]],
                         ["report", "daily"])
        self.assertEqual(data["loops"][0]["prompt"], 'say "hi"\nthen stop')
        self.assertEqual(data["loops"][1]["days"], ["mon", "fri"])
        self.assertFalse(data["loops"][1]["enabled"])
        self.assertTrue(data["loops"][1]["hidden"])
        self.assertEqual([l["name"] for l in saved], ["report", "daily"])
        self.assertFalse(list(home.glob("*.tmp")))

    def test_save_refuses_invalid_entries_without_touching_the_file(self):
        home = self._cousin(loops_toml=(
            '[[loops]]\nname = "old"\ninterval_seconds = 5\nprompt = "z"\n'))
        before = (home / "cousin.toml").read_text()
        with self.assertRaises(ValueError) as ctx:
            save_cousin_loops(home, [{"name": "x", "prompt": "p"}])
        self.assertIn("[0]", str(ctx.exception))
        self.assertEqual((home / "cousin.toml").read_text(), before)

    def test_save_an_empty_list_removes_every_loop(self):
        home = self._cousin(loops_toml=(
            '[[loops]]\nname = "old"\ninterval_seconds = 5\nprompt = "z"\n'))
        save_cousin_loops(home, [])
        data = tomllib.loads((home / "cousin.toml").read_text())
        self.assertNotIn("loops", data)


class TestNextDue(unittest.TestCase):
    def test_interval_and_never_fired(self):
        loop = {"name": "x", "interval_seconds": 600, "prompt": "p"}
        self.assertEqual(next_due(loop, 1000, now=1100), 1600)
        self.assertEqual(next_due(loop, 0, now=1100), 1100)

    def test_daily_at_respects_days_and_the_last_fire(self):
        loop = {"name": "x", "daily_at": "09:00", "prompt": "p",
                "days": ["mon"]}
        # 2026-09-16 is a Wednesday; next Monday 09:00 is 2026-09-21.
        now = datetime(2026, 9, 16, 12, 0).timestamp()
        due = datetime.fromtimestamp(next_due(loop, 0, now=now))
        self.assertEqual((due.year, due.month, due.day, due.hour),
                         (2026, 9, 21, 9))

    def test_cron_scans_forward_to_the_next_matching_minute(self):
        loop = {"name": "x", "cron": "30 14 * * *", "prompt": "p"}
        now = datetime(2026, 9, 16, 15, 0).timestamp()
        due = datetime.fromtimestamp(next_due(loop, 0, now=now))
        self.assertEqual((due.day, due.hour, due.minute), (17, 14, 30))


if __name__ == "__main__":
    unittest.main()
