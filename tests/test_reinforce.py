"""Usage-weighted recall: a memory strengthens by being used.

Every search records which files it surfaced (memory/.recall-log.jsonl
and memory/.recall-counts.json) and later searches lift those files by
a BOUNDED, DECAYING bonus: asymptotic in the count so use can nudge but
never drown relevance, halved every 14 days of disuse so a file that
was hot last quarter stops riding on it.

Ported from the source framework's reinforcement tests (bounded and
monotonic boost, counts collapse duplicates, empty hits never log,
failures never raise, search wiring is fail-open) and re-expressed
over this repository's home-based API, keyed by absolute path; the
decay and the record-at-every-return behaviours are new here. The
search-level tests use the loopback fake embedder, so the whole path
from query to log line is exercised.
"""
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from cousin_lib import memory_search, reinforce
from cousin_lib.memory_search import search
from tests._fakes import fake_embedder

DAY = 86400.0
VECTOR_A = [1.0, 0.0, 0.0]
VECTOR_B = [0.0, 1.0, 0.0]


def _sunrise_vectors(text):
    return VECTOR_A if ("sunrise" in text or "dawn" in text) else VECTOR_B


class ReinforceCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.home = self.root / "cousins" / "wren"
        (self.home / "memory").mkdir(parents=True)
        self.a = str(self.home / "memory" / "a.md")
        self.b = str(self.home / "memory" / "b.md")

    def _counts_path(self):
        return self.home / "memory" / ".recall-counts.json"

    def _log_path(self):
        return self.home / "memory" / ".recall-log.jsonl"


class TestBonus(ReinforceCase):
    def test_bonus_zero_for_never_recalled(self):
        self.assertEqual(reinforce.bonus(self.home, self.a), 0.0)
        # A counts file that knows other files is still zero for this one.
        reinforce.record(self.home, [self.b])
        self.assertEqual(reinforce.bonus(self.home, self.a), 0.0)

    def test_bonus_grows_with_recalls_and_is_capped(self):
        seen = [reinforce.bonus(self.home, self.a)]
        for _ in range(40):
            reinforce.record(self.home, [self.a])
            seen.append(reinforce.bonus(self.home, self.a))
        self.assertEqual(seen[0], 0.0)
        self.assertGreater(seen[1], 0.0)
        # Strictly monotonic in use, and never at or past the cap.
        for prev, nxt in zip(seen, seen[1:]):
            self.assertGreater(nxt, prev)
        self.assertLess(seen[-1], reinforce.MAX_BONUS)
        self.assertEqual(reinforce.MAX_BONUS, 0.15)
        # A heavily-used memory nudges, never drowns relevance.
        self.assertLessEqual(
            reinforce._boost(10 ** 6), reinforce.MAX_BONUS + 1e-9)

    def test_bonus_decays_with_age(self):
        t0 = 1_800_000_000.0
        with mock.patch.object(reinforce.time, "time", lambda: t0):
            for _ in range(5):
                reinforce.record(self.home, [self.a])
        fresh = reinforce.bonus(self.home, self.a, now=t0)
        half = reinforce.bonus(self.home, self.a, now=t0 + 14 * DAY)
        quarter = reinforce.bonus(self.home, self.a, now=t0 + 28 * DAY)
        old = reinforce.bonus(self.home, self.a, now=t0 + 365 * DAY)
        self.assertGreater(fresh, 0.0)
        self.assertAlmostEqual(half, fresh / 2, places=9)
        self.assertAlmostEqual(quarter, fresh / 4, places=9)
        self.assertLess(old, fresh / 1000)
        self.assertGreaterEqual(old, 0.0)
        # A recall after the decay restores it: use is what counts.
        with mock.patch.object(reinforce.time, "time",
                               lambda: t0 + 28 * DAY):
            reinforce.record(self.home, [self.a])
        self.assertGreater(reinforce.bonus(self.home, self.a,
                                           now=t0 + 28 * DAY), quarter)

    def test_corrupt_counts_file_is_reset_not_fatal(self):
        self._counts_path().write_text("{not json")
        self.assertEqual(reinforce.bonus(self.home, self.a), 0.0)
        reinforce.record(self.home, [self.a])
        counts = json.loads(self._counts_path().read_text())
        self.assertEqual(counts[self.a]["count"], 1)
        # A well-formed file with the wrong shape is corrupt too.
        self._counts_path().write_text(json.dumps(["a", "list"]))
        self.assertEqual(reinforce.bonus(self.home, self.a), 0.0)
        self._counts_path().write_text(json.dumps({self.a: "junk"}))
        self.assertEqual(reinforce.bonus(self.home, self.a), 0.0)
        reinforce.record(self.home, [self.a])
        self.assertEqual(reinforce.load_counts(self.home)[self.a]["count"],
                         1)


class TestRecord(ReinforceCase):
    def test_record_counts_collapse_duplicates(self):
        reinforce.record(self.home, [self.a, self.a, self.b],
                         query="query one")
        reinforce.record(self.home, [self.a], query="query two")
        counts = reinforce.load_counts(self.home)
        self.assertEqual(counts[self.a]["count"], 2)
        self.assertEqual(counts[self.b]["count"], 1)
        # Keyed by absolute path, not by basename or collection key.
        self.assertTrue(all(os.path.isabs(k) for k in counts))
        lines = [json.loads(l) for l in
                 self._log_path().read_text().splitlines()]
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["query"], "query one")
        self.assertEqual(sorted(lines[0]["paths"]), sorted([self.a, self.b]))
        self.assertIn("ts", lines[0])

    def test_empty_recall_never_logs(self):
        reinforce.record(self.home, [])
        self.assertFalse(self._log_path().exists())
        self.assertFalse(self._counts_path().exists())

    def test_top_used_orders_by_count_then_path(self):
        reinforce.record(self.home, [self.a, self.b])
        reinforce.record(self.home, [self.b])
        top = reinforce.top_used(self.home, n=5)
        self.assertEqual([t[0] for t in top], [self.b, self.a])
        self.assertEqual(top[0][1], 2)

    def test_log_rotates_past_threshold(self):
        with mock.patch.object(reinforce, "LOG_ROTATE_BYTES", 200):
            for i in range(20):
                reinforce.record(self.home, [self.a], query="q%d" % i)
        archive = self.home / "memory" / ".recall-log-archive.jsonl"
        self.assertTrue(archive.exists())
        self.assertLess(self._log_path().stat().st_size, 400)
        # Nothing is lost: archive plus live log hold every event.
        total = (len(archive.read_text().splitlines())
                 + len(self._log_path().read_text().splitlines()))
        self.assertEqual(total, 20)
        self.assertEqual(reinforce.load_counts(self.home)[self.a]["count"],
                         20)

    def test_record_never_raises_on_unwritable_home(self):
        blocker = self.root / "file-not-dir"
        blocker.write_text("x")
        reinforce.record(blocker / "cousins" / "nope", [self.a])
        self.assertEqual(reinforce.bonus(blocker / "cousins" / "nope",
                                         self.a), 0.0)


class SearchCase(ReinforceCase):
    def setUp(self):
        super().setUp()
        (self.home / "memory" / "a.md").write_text(
            "# A\n\nThe sunrise over the ridge was violet.\n")
        (self.home / "memory" / "b.md").write_text(
            "# B\n\nAnother sunrise, this one over the harbour.\n")
        (self.home / "memory" / "ports.md").write_text(
            "# Ports\n\nThe claimed set excludes every port.\n")
        patcher = mock.patch.dict(os.environ, {
            "COUSIN_HOME": str(self.home),
            "FRAMEWORK_ROOT": str(self.root),
        })
        patcher.start()
        self.addCleanup(patcher.stop)

    def _configure_embedding(self, url):
        (self.root / "config").mkdir(exist_ok=True)
        (self.root / "config" / "embedding.toml").write_text(
            'url = "%s"\nmodel = "test-embed"\ntimeout_s = 2\n' % url)

    def _serve_fake(self, vector_for=_sunrise_vectors):
        ctx = fake_embedder(vector_for=vector_for)
        url = ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        return url


class TestSearchRecords(SearchCase):
    def test_search_records_surfaced_paths(self):
        self._configure_embedding(self._serve_fake())
        hits, notice = search("dawn")
        self.assertIsNone(notice)
        self.assertTrue(hits)
        surfaced = sorted(h["path"] for h in hits)
        counts = reinforce.load_counts(self.home)
        self.assertEqual(sorted(counts), surfaced)
        self.assertTrue(all(counts[p]["count"] == 1 for p in surfaced))
        line = json.loads(self._log_path().read_text().splitlines()[-1])
        self.assertEqual(sorted(line["paths"]), surfaced)
        self.assertEqual(line["query"], "dawn")

    def test_keyword_only_search_records_too(self):
        # No embedding config: the keyword-only return still records.
        hits, _ = search("claimed set port")
        self.assertIn("ports.md", hits[0]["path"])
        counts = reinforce.load_counts(self.home)
        self.assertEqual(list(counts), [hits[0]["path"]])

    def test_degraded_search_records_too(self):
        self._configure_embedding("http://127.0.0.1:9/nothing-here")
        hits, notice = search("claimed set port")
        self.assertIsNotNone(notice)
        self.assertIn(hits[0]["path"], reinforce.load_counts(self.home))

    def test_empty_result_records_nothing(self):
        hits, _ = search("zzzz-nothing-matches")
        self.assertEqual(hits, [])
        self.assertFalse(self._log_path().exists())

    def test_bonus_lifts_a_recalled_file(self):
        # a.md and b.md tie on every leg for "sunrise"; the tie breaks
        # by path order, so a.md leads. Recalling b.md a few times
        # must lift it past a.md - the bonus changes a ranking, it is
        # not decoration.
        self._configure_embedding(self._serve_fake())
        hits, _ = search("sunrise", top=2)
        self.assertEqual([pathlib.Path(h["path"]).name for h in hits],
                         ["a.md", "b.md"])
        base = hits[0]["score"]
        for _ in range(3):
            reinforce.record(self.home, [self.b])
        hits, _ = search("sunrise", top=2)
        self.assertEqual(pathlib.Path(hits[0]["path"]).name, "b.md")
        self.assertGreater(hits[0]["score"], base)
        self.assertLessEqual(hits[0]["score"],
                             base * (1 + reinforce.MAX_BONUS) + 1e-12)

    def test_search_survives_broken_reinforcement(self):
        # Reinforcement is fail-open: a broken bonus or record path can
        # never take search down with it.
        self._configure_embedding(self._serve_fake())
        with mock.patch.object(reinforce, "bonus",
                               side_effect=RuntimeError("boom")), \
                mock.patch.object(reinforce, "record",
                                  side_effect=RuntimeError("boom")):
            hits, notice = search("dawn")
        self.assertTrue(hits)
        self.assertIsNone(notice)
        self.assertIs(memory_search.reinforce, reinforce)


if __name__ == "__main__":
    unittest.main()
