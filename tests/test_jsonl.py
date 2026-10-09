"""A torn line in a JSONL log (a writer killed mid-append) costs that line
only (#286, point 1): the next append starts a line of its own, so the
reader keeps every entry after it."""
import json
import os
import pathlib
import tempfile
from datetime import datetime
from unittest import mock

from cousin_lib import jsonl, memory
from tests._hermetic import HermeticCase


class TestAppendLine(HermeticCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.path = pathlib.Path(tmp.name) / "log.jsonl"

    def test_a_new_file_gets_one_line(self):
        jsonl.append_line(self.path, json.dumps({"a": 1}))
        self.assertEqual(self.path.read_text(), '{"a": 1}\n')

    def test_a_torn_last_line_is_closed_before_the_next(self):
        self.path.write_text('{"a": 1}\n{"b": ')
        jsonl.append_line(self.path, json.dumps({"c": 3}))
        lines = self.path.read_text().splitlines()
        self.assertEqual(lines, ['{"a": 1}', '{"b": ', '{"c": 3}'])

    def test_the_line_goes_out_in_one_write(self):
        writes = []
        real = os.write
        with mock.patch.object(os, "write", side_effect=lambda fd, b: writes.append(b) or real(fd, b)):
            jsonl.append_line(self.path, "x" * 20000)
        self.assertEqual(len(writes), 1)


    def test_a_short_write_raises_and_the_next_line_starts_clean(self):
        real = os.write
        with mock.patch.object(os, "write", side_effect=lambda fd, b: real(fd, b[:5])):
            with self.assertRaises(OSError):
                jsonl.append_line(self.path, json.dumps({"a": 1}))
        jsonl.append_line(self.path, json.dumps({"b": 2}))
        self.assertEqual(self.path.read_text().splitlines()[-1], '{"b": 2}')


class TestRawMemoryAfterATornLine(HermeticCase):
    def test_the_entry_after_a_torn_line_survives(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        home = pathlib.Path(tmp.name) / "cousins" / "wren"
        (home / "memory").mkdir(parents=True)
        (home / "cousin.toml").write_text('[cousin]\nslug = "wren"\n')
        memory.remember(home, "a", "before the crash")
        raw = home / "memory" / "raw" / (datetime.now().strftime("%Y-%m-%d") + ".jsonl")
        with open(raw, "a") as fh:      # a writer killed mid-line
            fh.write('{"timestamp": "2026-10-08T00:00:00", "topic": "torn", "content": "half')
        memory.remember(home, "b", "right after the crash")
        memory.remember(home, "c", "later")
        self.assertEqual([e["topic"] for e in memory._all_raw(home)], ["a", "b", "c"])


class TestNoBareAppends(HermeticCase):
    """A line log appends through jsonl.append_line, or a torn line comes
    back. What still opens a file in append mode is listed here with why."""

    ALLOWED = {
        # lock files: opened for their descriptor, nothing is written
        ("cousin_lib/artifacts.py", "lock"), ("cousin_lib/memory_search.py", "lock_path"),
        # a child process's stdout and stderr, handed to it whole
        ("cousin_lib/supervisor.py", "log_file"), ("cousin_lib/chat_hooks.py", "log_path"),
        ("cousin_lib/upgrade_switch.py", "log"),
        # an import's bulk copy of whole lines, with its own newline guard
        ("cousin_lib/memory_export.py", "path"),
        # job logs: free text a command or a transcript writes, read by people
        ("cousin_lib/jobs.py", "self.log_path"), ("cousin_lib/jobs.py", "path"),
        ("cousin_lib/jobs.py", "job[\"log_path\"]"), ("cousin_lib/runner/tools.py", "job[\"log_path\"]"),
        ("cousin_lib/activity.py", "path"), ("cousin_lib/activity.py", "log_path"),
        # archives: whole lines copied over in one call, closed lines only
        ("cousin_lib/reinforce.py", "archive"), ("cousin_lib/memory.py", "archive"),
        ("cousin_lib/capsule.py", "archive"),
        # Markdown, not a line log: the mirror and a transplant's merge guard their own newline
        ("cousin_lib/capsule.py", "path"), ("cousin_lib/lifecycle.py", "dst"),
        # a test harness's crash mark: one short line written just before a SIGKILL
        ("cousin_lib/crashpoint.py", "mark"),
    }

    def test_append_mode_opens_are_the_listed_ones(self):
        import re
        root = pathlib.Path(__file__).resolve().parents[1]
        found = set()
        for path in (root / "cousin_lib").rglob("*.py"):
            rel = str(path.relative_to(root))
            if rel == "cousin_lib/jsonl.py":
                continue
            for m in re.finditer(r"""open\(([^,()]+(?:\[[^\]]*\])?)\s*,\s*["']a[b+]?["']""",
                                 path.read_text(encoding="utf-8")):
                found.add((rel, m.group(1).strip()))
        self.assertEqual(sorted(found - self.ALLOWED), [],
                         "append through jsonl.append_line, or list it here with why")
