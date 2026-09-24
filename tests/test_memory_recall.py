"""One store: recall reads raw memory through the index search reads,
and data/decisions.jsonl is no longer a second copy it answers from."""
import contextlib
import io
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from cousin_lib import memory, memory_search
from tests._hermetic import HermeticCase

FACT = "The quarterly ledger reconciliation runs on the first Monday of the quarter."


def _home(case):
    tmp = tempfile.TemporaryDirectory(); case.addCleanup(tmp.cleanup)
    home = pathlib.Path(tmp.name) / "cousins" / "wren"
    (home / "data").mkdir(parents=True); (home / "memory").mkdir()
    (home / "cousin.toml").write_text('[cousin]\nslug = "wren"\nname = "Wren"\n')
    p = mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": "/nonexistent/framework-root"})
    p.start(); case.addCleanup(p.stop)
    return home


class TestOneStore(HermeticCase):
    def test_an_entry_written_by_remember_is_recallable(self):
        home = _home(self)
        memory.remember(home, "ledger reconciliation cadence", FACT)
        entries = memory.recall_entries(home, "reconciliation")
        self.assertEqual([e["content"] for e in entries], [FACT])

    def test_the_cli_recall_prints_a_remembered_fact(self):
        home = _home(self)
        memory.remember(home, "ledger reconciliation cadence", FACT)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = memory.memory_main(["--home", str(home), "recall", "reconciliation"])
        self.assertEqual(rc, 0)
        self.assertIn("ledger reconciliation cadence: " + FACT, out.getvalue())

    def test_the_runner_tool_recalls_a_remembered_fact(self):
        from cousin_lib.runner import tools
        from tests.runner.test_tools import _ctx
        ctx = _ctx(self)
        memory.remember(ctx.home, "ledger reconciliation cadence", FACT)
        text, err = tools.call(ctx, "memory", {"command": "recall", "keyword": "reconciliation"})
        self.assertFalse(err, text)
        self.assertIn(FACT, text)

    def test_no_keyword_lists_authored_entries_not_the_log(self):
        home = _home(self)
        memory.remember(home, "spare keys", "Toki keeps the spare keys.")
        memory.record_event(home, "framework", "framework:flip", "flipped to generation 2", "flip")
        memory.record_event(home, "tool", "job:nightly-build", "the build finished", "job")
        self.assertEqual([e["topic"] for e in memory.recall_entries(home)], ["spare keys"])

    def test_a_decision_still_prints_its_why(self):
        """guard: a decision recalls with its reasoning line before and after
        this task (the old recall read decisions.jsonl, the new one raw)."""
        home = _home(self)
        memory.decide(home, "ports", "exclude claimed ports", "they collide")
        text = memory.format_recall(memory.recall_entries(home, "ports"), "ports")
        self.assertIn("ports: exclude claimed ports\n  Why: they collide\n", text)

    def test_decide_still_appends_decisions_jsonl(self):
        """guard (R2): the compatibility log keeps being written for its
        other readers (the boot staleness warning, the shell hooks)."""
        home = _home(self)
        memory.decide(home, "ports", "exclude claimed ports", "they collide")
        row = json.loads((home / "data" / "decisions.jsonl").read_text().splitlines()[-1])
        self.assertEqual(row["decision"], "exclude claimed ports")


def _log(home, rows, name="decisions.jsonl"):
    with open(home / "data" / name, "a") as fh:
        for ts, topic, decision, why in rows:
            fh.write(json.dumps({"timestamp": ts, "topic": topic,
                                 "decision": decision, "reasoning": why}) + "\n")


class TestBackfill(HermeticCase):
    """Decisions logged before the raw store existed live only in
    data/decisions.jsonl. Recall reads raw, so they are backfilled once."""

    def test_a_decision_only_in_the_log_is_recallable(self):
        home = _home(self)
        _log(home, [("2026-05-17T10:00:00+02:00", "orphan", "kept only in the log", "a copy")])
        [entry] = memory.recall_entries(home, "orphan")
        self.assertEqual((entry["content"], entry["timestamp"], entry["source"]),
                         ("kept only in the log - why: a copy", "2026-05-17T10:00:00+02:00",
                          "decision"))
        self.assertTrue((memory.raw_dir(home) / "2026-05-17.jsonl").exists())   # its own day

    def test_rotated_archives_are_backfilled_too(self):
        home = _home(self)
        _log(home, [("2026-04-02T09:00:00+02:00", "ancient", "kept in the archive", "rotated")],
             name="decisions-archive-20260501.jsonl")
        self.assertEqual([e["topic"] for e in memory.recall_entries(home, "ancient")], ["ancient"])

    def test_the_backfill_is_idempotent_and_skips_twins(self):
        home = _home(self)
        memory.decide(home, "ports", "exclude claimed ports", "they collide")    # log AND raw
        _log(home, [("2026-05-17T10:00:00+02:00", "orphan", "kept only in the log", "a copy")])
        self.assertEqual(memory.backfill_decisions(home, dry_run=True), 1)
        self.assertEqual(memory.backfill_decisions(home), 1)
        self.assertEqual(memory.backfill_decisions(home), 0)
        memory.recall_entries(home, "ports")
        raw = [e for e in memory.list_raw(home, since_days=36500) if e["topic"] in ("ports", "orphan")]
        self.assertEqual(sorted(e["topic"] for e in raw), ["orphan", "ports"])

    def test_the_first_recall_backfills_once(self):
        home = _home(self)
        _log(home, [("2026-05-17T10:00:00+02:00", "orphan", "kept only in the log", "a copy")])
        memory.recall_entries(home)
        self.assertIn("backfilled 1 decision", (home / "data" / ".decisions-backfilled").read_text())
        with mock.patch.object(memory, "backfill_decisions") as again:
            memory.recall_entries(home, "orphan")
        again.assert_not_called()


class TestBackfillReach(HermeticCase):
    def test_a_log_only_decision_is_found_by_search_too(self):
        """The proactive recall and the memory tool's search never call
        recall_entries: the backfill must not wait for one."""
        home = _home(self)
        _log(home, [("2026-05-17T10:00:00+02:00", "orphan", "kept only in the log", "a copy")])
        hits, _notice = memory_search.search("orphan", home=home)
        self.assertEqual([memory_search.raw_entry(h["path"])["topic"] for h in hits], ["orphan"])

    def test_the_mark_lives_in_data_not_memory(self):
        """A transplant copies memory/ (lifecycle._copy_memory_over): a mark
        there would tell the recipient its own log was already backfilled."""
        home = _home(self)
        _log(home, [("2026-05-17T10:00:00+02:00", "orphan", "kept only in the log", "a copy")])
        memory.recall_entries(home, "orphan")
        self.assertTrue((home / "data" / ".decisions-backfilled").exists())
        self.assertEqual(list((home / "memory").glob(".decisions-backfilled*")), [])

    def test_a_home_without_a_log_gets_no_mark(self):
        home = _home(self)
        memory.recall_entries(home, "anything")
        self.assertFalse((home / "data" / ".decisions-backfilled").exists())

    def test_a_naive_local_stamp_is_backfilled_as_written(self):
        """guard: most log-only decisions carry a naive local stamp (no
        offset); it is kept verbatim and filed under its own day."""
        home = _home(self)
        _log(home, [("2026-05-17T23:30:00", "naive", "stamped without an offset", "old decide")])
        [entry] = memory.recall_entries(home, "naive")
        self.assertEqual(entry["timestamp"], "2026-05-17T23:30:00")
        self.assertTrue((memory.raw_dir(home) / "2026-05-17.jsonl").exists())

    def test_consolidate_counts_a_decision_once(self):
        """decide writes the log AND raw; consolidate counted both, so two
        decisions on one topic read as four entries and a candidate."""
        home = _home(self)
        memory.decide(home, "ports", "exclude claimed ports", "they collide")
        memory.decide(home, "ports", "exclude claimed ports again", "they still collide")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            memory.memory_main(["--home", str(home), "consolidate"])
        self.assertIn("no promotion candidates", out.getvalue())


class TestRecallFacts(HermeticCase):
    def test_a_digested_decision_still_prints_its_why(self):
        """raw_fold folds entries older than its window into a monthly
        digest (source "digest") that wins the index's dedupe."""
        home = _home(self)
        (memory.raw_dir(home)).mkdir(parents=True, exist_ok=True)
        (memory.raw_dir(home) / "2026-06-digest.jsonl").write_text(json.dumps({
            "timestamp": "2026-06-03T10:00:00+00:00", "topic": "ports",
            "content": "exclude claimed ports - why: they collide", "source": "digest",
            "truth_level": "L3_COUSIN_CONCLUSION", "entries": 2}) + "\n")
        text = memory.format_recall(memory.recall_entries(home, "ports"), "ports")
        self.assertIn("ports: exclude claimed ports\n  Why: they collide\n", text)

    def test_recall_leaves_no_trace_in_the_recall_log(self):
        home = _home(self)
        memory.remember(home, "ledger reconciliation cadence", FACT)
        memory.recall_entries(home, "reconciliation")
        self.assertFalse((home / "memory" / ".recall-log.jsonl").exists())

    def _hit(self, home, similarity):
        key = str(next(memory.raw_dir(home).glob("*.jsonl"))) + "#1"
        return {"path": key, "collection": "raw", "score": 0.1, "snippet": "", "chunk": 0,
                "similarity": similarity}

    def test_a_semantic_hit_below_the_floor_is_not_recalled(self):
        """With an embedding service the semantic leg returns the nearest
        entries whatever they are: `recall xyzzy` must not print them."""
        home = _home(self)
        memory.remember(home, "spare keys", "Toki keeps the spare keys.")
        with mock.patch.object(memory_search, "search", return_value=([self._hit(home, 0.12)], None)), \
                mock.patch.object(memory_search, "_keyword_search", return_value=[]):
            self.assertEqual(memory.recall_entries(home, "xyzzy"), [])

    def test_a_semantic_hit_above_the_floor_is_recalled(self):
        home = _home(self)
        memory.remember(home, "spare keys", "Toki keeps the spare keys.")
        with mock.patch.object(memory_search, "search", return_value=([self._hit(home, 0.91)], None)), \
                mock.patch.object(memory_search, "_keyword_search", return_value=[]):
            self.assertEqual([e["topic"] for e in memory.recall_entries(home, "where are the keys")],
                             ["spare keys"])


class TestRecallToolRoot(HermeticCase):
    def test_the_recall_tool_never_reads_the_environments_install(self):
        """Review Focus 1, the memory tool's leg: recall reads the runner's
        root. The environment names another install whose embedding
        service must never be called."""
        from cousin_lib.runner import tools
        from tests.runner.test_tools import _ctx
        ctx = _ctx(self)
        memory.remember(ctx.home, "ledger reconciliation cadence", FACT)
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        other = pathlib.Path(tmp.name)
        (other / "config").mkdir()
        (other / "config" / "embedding.toml").write_text(
            'url = "http://127.0.0.1:9/embed"\nmodel = "m"\n')
        p = mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": str(other)})
        p.start(); self.addCleanup(p.stop)
        called = []
        with mock.patch.object(memory_search, "_embed",
                               side_effect=lambda text, config: called.append(config["url"])):
            text, err = tools.call(ctx, "memory", {"command": "recall", "keyword": "reconciliation"})
        self.assertFalse(err, text)
        self.assertIn(FACT, text)
        self.assertEqual(called, [])


if __name__ == "__main__":
    unittest.main()
