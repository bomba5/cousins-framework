"""Memory written under a live session by someone else (runner/memory_watch.py,
hooks.on_prompt): told once at the next prompt, the session's own writes never."""
import asyncio
import json
import pathlib
import tempfile

from cousin_lib import memory
from cousin_lib.runner import hooks, memory_watch
from cousin_lib.runner.state import StateMachine
from cousin_lib.runner.stream import EventStream
from tests._hermetic import HermeticCase


class WatchCase(HermeticCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        (self.root / "config").mkdir()
        self.home = self.root / "cousins" / "wren"
        for sub in ("data", "memory"):
            (self.home / sub).mkdir(parents=True)
        (self.home / "cousin.toml").write_text('[cousin]\nslug = "wren"\n')
        memory.remember(self.home, "kestrel", "nests on the roof")      # before the session

    def raw_line(self, entry):
        day = self.home / "memory" / "raw" / "2026-10-02.jsonl"
        day.parent.mkdir(parents=True, exist_ok=True)
        with open(day, "a") as f:
            f.write(json.dumps(entry) + "\n")


class TestWatch(WatchCase):
    def test_only_foreign_writes_are_told_and_each_once(self):
        watch = memory_watch.MemoryWatch(self.home)
        memory.remember(self.home, "kestrel", "the session's own claim")
        memory.remember(self.home, "kestrel", "nests in the barn",
                        cite="console user ana, 2026-10-02T21:00:00Z")
        self.raw_line({"topic": "kestrel", "content": "merged", "truth_level": "L3_COUSIN_CONCLUSION",
                       "source": "dream", "cite": "dream:p1", "timestamp": "2026-10-02T21:01:00"})
        self.raw_line({"topic": "kestrel", "content": "x", "truth_level": "L5_OBSOLETE",
                       "source": "review_gate", "why": "dropped at review",
                       "timestamp": "2026-10-02T21:02:00"})
        found = watch.check()
        self.assertEqual([e.get("content") for e in found], ["nests in the barn", "merged", "x"])
        text = memory_watch.note(found)
        self.assertIn(memory_watch.NOTE_HEAD, text)
        self.assertIn("nests in the barn (by the console, id ", text)
        self.assertIn("(by dreaming, id ", text)
        self.assertIn("retired: dropped at review (by the review gate", text)
        self.assertNotIn("the session's own claim", text)
        self.assertEqual(watch.check(), [])                     # said once

    def test_a_half_written_line_waits_and_a_folded_file_is_rebaselined(self):
        watch = memory_watch.MemoryWatch(self.home)
        day = self.home / "memory" / "raw" / "2026-10-02.jsonl"
        entry = {"topic": "t", "content": "c", "cite": "console user ana", "truth_level": "L3"}
        with open(day, "a") as f:
            f.write(json.dumps(entry)[:10])                     # a writer mid-line
        self.assertEqual(watch.check(), [])
        with open(day, "a") as f:
            f.write(json.dumps(entry)[10:] + "\n")
        self.assertEqual([e["content"] for e in watch.check()], ["c"])
        day.write_text("")                                     # raw_fold folded it away
        self.assertEqual(watch.check(), [])

    def test_the_note_is_bounded_and_counts_what_it_left_out(self):
        entries = [{"topic": "t%d" % i, "content": "x" * 400, "cite": "console user ana",
                    "truth_level": "L3_COUSIN_CONCLUSION"} for i in range(30)]
        text = memory_watch.note(entries)
        self.assertLessEqual(len(text), memory_watch.MAX_CHARS + 80)
        self.assertIn("older entries left out", text)
        self.assertIn("t29", text)                              # the newest kept


class TestThroughTheHooks(WatchCase):
    def test_the_next_prompt_carries_it(self):
        cbs = hooks.callbacks(self.home, slug="wren", root=self.root,
                              machine=StateMachine(on_change=lambda *a: None),
                              stream=EventStream(self.home, "mw"), recall=lambda body: (None, 0),
                              memory_watch=memory_watch.MemoryWatch(self.home))
        memory.remember(self.home, "kestrel", "nests in the barn", cite="console user ana")
        out = asyncio.run(cbs["UserPromptSubmit"](
            {"hook_event_name": "UserPromptSubmit", "prompt": "go on"}, None, {}))
        self.assertIn("nests in the barn", out["hookSpecificOutput"]["additionalContext"])
        again = asyncio.run(cbs["UserPromptSubmit"](
            {"hook_event_name": "UserPromptSubmit", "prompt": "and?"}, None, {}))
        self.assertEqual(again, {})
