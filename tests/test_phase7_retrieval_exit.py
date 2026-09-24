"""Phase 7 tasks 3-6: the exit criteria this slice can prove in the
default suite. The two that need the fleet (the largest cousin's real
replay, a week on the SDK lane) belong to master tasks 7-8."""
import asyncio
import os
import pathlib
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

from cousin_lib import memory, memory_import
from cousin_lib.runner import hooks
from cousin_lib.runner.state import StateMachine
from cousin_lib.runner.stream import EventStream
from tests._hermetic import HermeticCase

TOML = ('[cousin]\nslug = "wren"\nname = "Wren"\n'
        '[memory]\nproactive_recall = true\nrecall_keyword_only = true\n')


class _Yesterday(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime.now(tz) - timedelta(days=1)


class TestExit(HermeticCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        (self.root / "config").mkdir()
        self.home = self.root / "cousins" / "wren"
        for sub in ("data", "memory", "notes"):
            (self.home / sub).mkdir(parents=True)
        (self.home / "cousin.toml").write_text(TOML)
        p = mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": "/nonexistent/framework-root",
                                         "COUSIN_HOME": str(self.home)})
        p.start(); self.addCleanup(p.stop)

    def _recall(self, prompt):
        cbs = hooks.callbacks(self.home, slug="wren", root=self.root,
                              machine=StateMachine(on_change=lambda o, n, d: None),
                              stream=EventStream(self.home, "exit"))
        out = asyncio.run(cbs["UserPromptSubmit"](
            {"hook_event_name": "UserPromptSubmit", "session_id": "s",
             "transcript_path": "/dev/null", "cwd": str(self.home), "prompt": prompt}, None, {}))
        return out.get("hookSpecificOutput", {}).get("additionalContext", "")

    def test_a_raw_entry_written_yesterday_is_found_today_without_distill(self):
        """Exit criterion 2: written one day, found the next, never distilled."""
        with mock.patch.object(memory, "datetime", _Yesterday):
            memory.remember(self.home, "boiler service", "Mallory services the boiler every March.")
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        self.assertTrue((memory.raw_dir(self.home) / ("%s.jsonl" % yesterday)).exists())
        self.assertFalse(memory.distilled_dir(self.home).exists())      # distill never ran
        self.assertIn("boiler service (raw:", self._recall("when is the boiler serviced?"))
        self.assertEqual([e["topic"] for e in memory.recall_entries(self.home, "boiler",
                                                                   root=self.root)],
                         ["boiler service"])

    def test_an_imported_memory_reaches_the_runners_recall_once(self):
        """The import end to end, on the runner's own path: before, the
        harness file answers; after, its imported copy does, and only it."""
        auto = self.root / "harness" / "wren"
        auto.mkdir(parents=True)
        (auto / "feedback_ledgers.md").write_text(
            "---\nname: ledgers\ndescription: ledger routine\ntype: feedback\n---\n"
            "Priya closes the quokka ledgers on the first Monday.\n")
        (self.root / "config" / "harness.toml").write_text('auto_memory_dir = "%s"\n' % auto)
        before = self._recall("who closes the quokka ledgers?")
        self.assertIn("(harness:feedback_ledgers.md)", before)
        memory_import.apply(self.home, root=self.root)
        after = self._recall("who closes the quokka ledgers?")
        self.assertIn("(memory:imported/auto/feedback_ledgers.md)", after)
        self.assertNotIn("harness:feedback_ledgers.md", after)
        report = memory_import.verify(self.home, root=self.root)
        self.assertGreaterEqual(report["queries"], 1)          # something was compared
        self.assertEqual((report["kept"], report["lost"]), (report["queries"], []))


if __name__ == "__main__":
    unittest.main()
