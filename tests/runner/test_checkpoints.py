"""The two checkpoint files, written in Python the way the harness
shell hooks (hooks/session_checkpoint.sh, hooks/pre_compact.sh) write
them: same headings, same sources, the event stream's tail in place of
a terminal capture."""
import json
import os
import pathlib
import tempfile
from datetime import datetime, timezone
from unittest import mock

from cousin_lib.runner import checkpoints
from tests._hermetic import HermeticCase

NOW = datetime(2026, 9, 23, 10, 0, 0, tzinfo=timezone.utc)


class CheckpointCase(HermeticCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.home = pathlib.Path(tmp.name) / "wren"
        (self.home / "data").mkdir(parents=True)

    def _write(self, rel, text):
        path = self.home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path


class TestSessionCheckpoint(CheckpointCase):
    def test_writes_the_three_headings_and_the_activity_line(self):
        self._write("data/last-activity.txt", "2026-09-23T09:58: wiring the kettle sensor\n")
        path = checkpoints.write_session_checkpoint(self.home, slug="wren", now=NOW)
        self.assertEqual(path, self.home / "data" / "session-checkpoint.md")
        text = path.read_text()
        self.assertTrue(text.startswith("# Session checkpoint - wren - 2026-09-23T10:00:00+0000"))
        for heading in ("## What was happening", "## Open work (from STATUS.md)",
                        "## Last decisions"):
            self.assertIn(heading, text)
        self.assertIn("wiring the kettle sensor", text)

    def test_open_work_comes_from_state_json_else_the_status_open_loops_block(self):
        self._write("STATUS.md", "# Wren - STATUS\n\n## Open loops\n"
                    "- **Toki's printer queue** stalls on job 3\n- [x] closed item\n\n"
                    "## Parked\n- nothing here\n\n## Open loops (gen 3)\n- old history\n")
        text = checkpoints.write_session_checkpoint(self.home, slug="wren", now=NOW).read_text()
        self.assertIn("Toki's printer queue", text)
        self.assertNotIn("old history", text); self.assertNotIn("nothing here", text)
        self._write("data/state.json", json.dumps({"open_loops": [
            {"text": "Sam's backup rotation", "done": False, "partial": True},
            {"text": "finished thing", "done": True, "partial": False}]}))
        text = checkpoints.write_session_checkpoint(self.home, slug="wren", now=NOW).read_text()
        self.assertIn("- [~] Sam's backup rotation", text)
        self.assertNotIn("finished thing", text); self.assertNotIn("Toki's printer queue", text)

    def test_includes_the_last_five_decisions(self):
        lines = [json.dumps({"topic": "t%d" % i, "decision": "choice %d" % i,
                             "reasoning": "because %d" % i}) for i in range(7)]
        self._write("data/decisions.jsonl", "\n".join(lines) + "\n")
        text = checkpoints.write_session_checkpoint(self.home, slug="wren", now=NOW).read_text()
        for i in range(2, 7):
            self.assertIn("- [t%d] choice %d (why: because %d)" % (i, i, i), text)
        self.assertNotIn("choice 1", text)
        # The script's `tail -n 5` then parse: a torn line in the tail is skipped.
        with open(self.home / "data" / "decisions.jsonl", "a") as fh:
            fh.write("not json\n")
        text = checkpoints.write_session_checkpoint(self.home, slug="wren", now=NOW).read_text()
        self.assertNotIn("not json", text); self.assertIn("choice 6", text)


class TestPreCompactCheckpoint(CheckpointCase):
    def test_writes_its_headings_and_the_newest_stream_tail(self):
        self._write("data/last-activity.txt", "reviewing Priya's patch\n")
        self._write("STATUS.md", "## Open loops\n- Mallory's audit\n")
        self._write("CLAUDE.md", "one\ntwo\nthree\n")
        old = self._write("data/stream/old.jsonl", json.dumps(
            {"seq": 1, "ts": 1.0, "kind": "text", "payload": {"text": "stale session"}}) + "\n")
        os.utime(old, (1000, 1000))
        events = [{"seq": i, "ts": float(i), "kind": "text" if i % 2 else "tool",
                   "payload": {"text": "event number %02d" % i} if i % 2 else
                   {"name": "Bash", "input": {"command": "echo %02d" % i}}}
                  for i in range(1, 26)]
        self._write("data/stream/new.jsonl", "".join(json.dumps(e) + "\n" for e in events))
        path = checkpoints.write_pre_compact_checkpoint(self.home, slug="wren", now=NOW)
        self.assertEqual(path, self.home / "data" / "pre-compact-checkpoint.md")
        text = path.read_text()
        self.assertTrue(text.startswith("# Pre-compaction checkpoint - wren - "))
        for heading in ("## Current activity", "## Recent decisions", "## Open loops",
                        "## Recent events", "## Files on disk"):
            self.assertIn(heading, text)
        self.assertIn("reviewing Priya's patch", text); self.assertIn("Mallory's audit", text)
        self.assertIn("- CLAUDE.md: 3 lines", text)
        self.assertIn("text: event number 25", text); self.assertIn("text: event number 07", text)
        self.assertIn("tool: ", text)
        self.assertNotIn("event number 05", text); self.assertNotIn("stale session", text)


    def test_a_long_stream_is_read_from_its_end_only(self):
        pad = "x" * 100
        self._write("data/stream/big.jsonl", "".join(json.dumps(
            {"seq": i, "ts": float(i), "kind": "text", "payload": {"text": "n%04d %s" % (i, pad)}})
            + "\n" for i in range(3000)))
        with mock.patch.object(checkpoints, "_read", wraps=checkpoints._read) as read:
            text = checkpoints.write_pre_compact_checkpoint(self.home, slug="wren", now=NOW).read_text()
        self.assertNotIn("big.jsonl", [pathlib.Path(c.args[0]).name for c in read.call_args_list])
        self.assertIn("text: n2999", text); self.assertIn("text: n2980", text)
        self.assertNotIn("n2979", text)


class TestEmptyHome(HermeticCase):
    def test_both_create_data_and_say_what_was_missing(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        home = pathlib.Path(tmp.name) / "wren"
        home.mkdir()
        session = checkpoints.write_session_checkpoint(home, now=NOW).read_text()
        self.assertIn("# Session checkpoint - wren - ", session)
        for missing in ("No activity recorded.", "No STATUS.md.", "None recorded."):
            self.assertIn(missing, session)
        pre = checkpoints.write_pre_compact_checkpoint(home, now=NOW).read_text()
        for missing in ("Unknown.", "None recorded.", "No STATUS.md.", "No event stream."):
            self.assertIn(missing, pre)
