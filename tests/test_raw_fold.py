"""Bounding memory/raw losslessly.

Daily raw files older than the hot window fold into monthly gzip
archives byte-identically and are replaced by a compact monthly digest
that list_raw and the distiller still read. Nothing is deleted.
"""
import gzip
import json
import pathlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from cousin_lib import memory, raw_fold


def _day(days_ago):
    return (datetime.now(timezone.utc)
            - timedelta(days=days_ago)).date().isoformat()


class RawFoldCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = pathlib.Path(tmp.name) / "cousins" / "testa"
        self.home.mkdir(parents=True)

    def _write_day(self, day, entries):
        """Append entries in the _append_raw shape; returns the exact
        lines written so the archive can be compared byte for byte."""
        memory.ensure_layout(self.home)
        lines = []
        with open(memory.raw_dir(self.home) / (day + ".jsonl"), "a") as fh:
            for entry in entries:
                entry.setdefault("timestamp", day + "T10:00:00+00:00")
                entry.setdefault("truth_level", "cousin-conclusion")
                line = json.dumps(entry)
                fh.write(line + "\n")
                lines.append(line)
        return lines

    def _digests(self):
        return [e for e in memory.list_raw(self.home, since_days=365)
                if e.get("source") == "digest"]


class TestFold(RawFoldCase):
    def test_moves_old_days_to_monthly_gzip_byte_identical(self):
        old_day = _day(45)
        lines = self._write_day(old_day, [{"topic": "a", "content": "one"},
                                          {"topic": "b", "content": "two"}])
        self._write_day(_day(2), [{"topic": "c", "content": "recent"}])
        report = raw_fold.fold_raw(self.home, keep_days=30)
        self.assertEqual(report["folded_days"], 1)
        self.assertEqual(report["folded_entries"], 2)
        self.assertEqual(report["months"], [old_day[:7]])
        raw = memory.raw_dir(self.home)
        self.assertFalse((raw / (old_day + ".jsonl")).exists())
        self.assertTrue((raw / (_day(2) + ".jsonl")).exists())
        archive = raw / "archive" / (old_day[:7] + ".jsonl.gz")
        self.assertTrue(archive.exists())
        with gzip.open(archive, "rt") as fh:
            self.assertEqual([l.rstrip("\n") for l in fh], lines)

    def test_leaves_a_monthly_digest_that_list_raw_reads(self):
        old_day = _day(45)
        self._write_day(old_day, [
            {"topic": "a", "content": "one"},
            {"topic": "a", "content": "one-newer",
             "timestamp": old_day + "T12:00:00+00:00"},
            {"topic": "b", "content": "two"},
        ])
        raw_fold.fold_raw(self.home, keep_days=30)
        by_topic = {e["topic"]: e for e in self._digests()}
        self.assertEqual(set(by_topic), {"a", "b"})
        self.assertEqual(by_topic["a"]["content"], "one-newer")
        self.assertEqual(by_topic["a"]["entries"], 2)
        self.assertTrue(by_topic["a"]["timestamp"].startswith(old_day))
        self.assertEqual(by_topic["a"]["truth_level"], "cousin-conclusion")

    def test_is_idempotent_and_appends_to_an_existing_month(self):
        d1, d2 = _day(45), _day(44)
        l1 = self._write_day(d1, [{"topic": "a", "content": "one"}])
        rep1 = raw_fold.fold_raw(self.home, keep_days=30)
        l2 = self._write_day(d2, [{"topic": "a", "content": "two"}])
        rep2 = raw_fold.fold_raw(self.home, keep_days=30)
        rep3 = raw_fold.fold_raw(self.home, keep_days=30)
        self.assertEqual(
            (rep1["folded_days"], rep2["folded_days"], rep3["folded_days"]),
            (1, 1, 0))
        if d1[:7] == d2[:7]:
            archive = (memory.raw_dir(self.home) / "archive"
                       / (d1[:7] + ".jsonl.gz"))
            with gzip.open(archive, "rt") as fh:
                self.assertEqual([l.rstrip("\n") for l in fh], l1 + l2)
            digests = self._digests()
            self.assertEqual(len(digests), 1)
            self.assertEqual(digests[0]["entries"], 2)
            self.assertEqual(digests[0]["content"], "two")

    def test_digest_ids_are_deterministic_across_runs(self):
        # hash() is salted per process; a digest id must not change
        # between runs or every index re-embeds every digest on every
        # boot.
        old_day = _day(45)
        self._write_day(old_day, [{"topic": "stable topic", "content": "x"}])
        raw_fold.fold_raw(self.home, keep_days=30)
        digest = self._digests()[0]
        self.assertEqual(
            digest["id"],
            "digest-%s-%s" % (old_day[:7], raw_fold.topic_key("stable topic")))
        self.assertEqual(raw_fold.topic_key("stable topic"),
                         raw_fold.topic_key("Stable Topic "))
        self.assertEqual(len(raw_fold.topic_key("stable topic")), 12)

    def test_the_distiller_reads_the_digest_with_its_history(self):
        old_day = _day(45)
        self._write_day(old_day, [{"topic": "a", "content": "one"},
                                  {"topic": "a", "content": "two",
                                   "timestamp": old_day + "T12:00:00+00:00"}])
        raw_fold.fold_raw(self.home, keep_days=30)
        from cousin_lib import distill
        distill.distill(self.home)
        text = (self.home / "memory" / "distilled"
                / "decisions.md").read_text()
        self.assertIn("two", text)
        self.assertIn("2 entries", text)


if __name__ == "__main__":
    unittest.main()
