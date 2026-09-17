"""CLI surface for the durable-layer producer: `cousin-memory distill`,
`consolidate` (which now promotes by running distill), and
`compact --target raw` (the lossless fold)."""
import contextlib
import io
import json
import os
import pathlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from cousin_lib import memory
from cousin_lib.memory import memory_main


class CliCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = pathlib.Path(tmp.name) / "cousins" / "testa"
        (self.home / "data").mkdir(parents=True)
        patcher = mock.patch.dict(os.environ,
                                  {"COUSIN_HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _seed(self, *, days_ago, topic, content):
        memory.ensure_layout(self.home)
        day = (datetime.now(timezone.utc)
               - timedelta(days=days_ago)).date().isoformat()
        entry = {"timestamp": day + "T10:00:00+00:00", "topic": topic,
                 "content": content, "truth_level": "cousin-conclusion",
                 "source": "decision"}
        with open(memory.raw_dir(self.home) / (day + ".jsonl"), "a") as fh:
            fh.write(json.dumps(entry) + "\n")

    def _main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = memory_main(argv)
        return rc, out.getvalue(), err.getvalue()

    def _distilled(self, name):
        return (self.home / "memory" / "distilled" / name).read_text()


class TestDistillCommand(CliCase):
    def test_writes_files_and_prints_the_report(self):
        self._seed(days_ago=1, topic="retention", content="thirty days")
        rc, out, _ = self._main(["distill"])
        self.assertEqual(rc, 0)
        self.assertIn("decisions.md: 1", out)
        self.assertIn("thirty days", self._distilled("decisions.md"))

    def test_max_lines_bounds_the_files(self):
        for i in range(5):
            self._seed(days_ago=i + 1, topic="t%d" % i, content="c%d" % i)
        rc, out, _ = self._main(["distill", "--max-lines", "2"])
        self.assertEqual(rc, 0)
        self.assertIn("decisions.md: 2", out)


class TestConsolidatePromotes(CliCase):
    def test_runs_distill_and_reports_lines(self):
        for i in range(3):
            self._seed(days_ago=i + 1, topic="rate limit wall",
                       content="take %d" % i)
        rc, out, _ = self._main(["consolidate"])
        self.assertEqual(rc, 0)
        self.assertIn("rate limit wall", out)
        self.assertIn("decisions.md: 1", out)
        text = self._distilled("decisions.md")
        self.assertIn("take 0", text)
        self.assertIn("3 entries", text)


class TestCompactRaw(CliCase):
    def test_target_raw_folds_old_days(self):
        self._seed(days_ago=45, topic="old", content="cold")
        self._seed(days_ago=1, topic="new", content="hot")
        rc, out, _ = self._main(["compact", "--target", "raw"])
        self.assertEqual(rc, 0)
        self.assertIn('"folded_days": 1', out)
        archives = list((self.home / "memory" / "raw" / "archive")
                        .glob("*.jsonl.gz"))
        self.assertEqual(len(archives), 1)
        self.assertTrue(any(p.name.endswith("-digest.jsonl")
                            for p in (self.home / "memory" / "raw").iterdir()))

    def test_target_raw_honours_hot_days(self):
        self._seed(days_ago=10, topic="old", content="cold")
        rc, out, _ = self._main(["compact", "--target", "raw",
                                 "--hot-days", "5"])
        self.assertEqual(rc, 0)
        self.assertIn('"folded_days": 1', out)

    def test_target_raw_dry_run_folds_nothing_and_says_why(self):
        self._seed(days_ago=45, topic="old", content="cold")
        rc, out, _ = self._main(["compact", "--target", "raw", "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertIn("lossless", out)
        self.assertFalse((self.home / "memory" / "raw" / "archive").exists())

    def test_default_target_is_still_the_index(self):
        # No MEMORY.md: the index compactor reports that, as before.
        rc, out, _ = self._main(["compact"])
        self.assertEqual(rc, 1)
        self.assertIn("no MEMORY.md", out)


if __name__ == "__main__":
    unittest.main()
