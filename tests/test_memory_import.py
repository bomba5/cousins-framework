"""The one-time import of the agent CLI's own memory: provenance, dry-run
by default, idempotent, never two hits for one memory."""
import contextlib
import io
import json
import os
import pathlib
import re
import tempfile
import unittest
from unittest import mock

from cousin_lib import memory, memory_import, memory_search
from tests._hermetic import HermeticCase

LEDGERS = ("---\nname: ledger routine\ndescription: how Priya closes the ledgers\n"
           "type: feedback\n---\nPriya closes the quokka ledgers on the first Monday.\n")
KEYS = ("---\nname: spare keys\ndescription: where the spare keys live\nmetadata:\n"
        "  type: reference\n  originSessionId: 0000\n---\nToki keeps the spare keys in the blue tin.\n")


class ImportCase(HermeticCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        (self.root / "config").mkdir()
        self.auto = self.root / "harness" / "wren-memory"
        self.auto.mkdir(parents=True)
        (self.root / "config" / "harness.toml").write_text('auto_memory_dir = "%s"\n' % self.auto)
        self.home = self.root / "cousins" / "wren"
        for sub in ("data", "memory", "notes"):
            (self.home / sub).mkdir(parents=True)
        (self.home / "cousin.toml").write_text('[cousin]\nslug = "wren"\nname = "Wren"\n')
        (self.auto / "feedback_ledgers.md").write_text(LEDGERS)
        (self.auto / "reference_keys.md").write_text(KEYS)
        (self.auto / "MEMORY.md").write_text("- [Ledgers](feedback_ledgers.md)\n")
        (self.auto / "feedback_ledgers.md.pre-compact-20300101").write_text("old\n")
        (self.auto / "store.db").write_bytes(b"\x00\x01")
        p = mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": str(self.root),
                                         "COUSIN_HOME": str(self.home)})
        p.start(); self.addCleanup(p.stop)

    def _target(self, name):
        return self.home / "memory" / "imported" / "auto" / name

    def _actions(self, rows):
        return {r["name"]: r["action"] for r in rows}


class TestImport(ImportCase):
    def test_the_plan_writes_nothing(self):
        rows = memory_import.plan(self.home, root=self.root)
        self.assertEqual(self._actions(rows), {
            "feedback_ledgers.md": "import", "reference_keys.md": "import",
            "MEMORY.md": "import", "feedback_ledgers.md.pre-compact-20300101": "ignore",
            "store.db": "ignore"})
        self.assertFalse((self.home / "memory" / "imported").exists())

    def test_apply_copies_with_provenance_and_keeps_the_frontmatter(self):
        memory_import.apply(self.home, root=self.root)
        text = self._target("reference_keys.md").read_text()
        self.assertTrue(text.startswith("---\nname: spare keys\n"))
        self.assertIn("metadata:\n  type: reference\n", text)          # nested type kept as is
        self.assertIn("imported_from: reference_keys.md\n", text)
        self.assertIn("imported_sha256: ", text)
        self.assertTrue(text.endswith("---\nToki keeps the spare keys in the blue tin.\n"))
        self.assertIn("imported_from: MEMORY.md\n", self._target("MEMORY.md").read_text())
        self.assertFalse(self._target("store.db").exists())

    def test_a_second_apply_changes_nothing(self):
        memory_import.apply(self.home, root=self.root)
        before = self._target("feedback_ledgers.md").read_text()
        rows = memory_import.apply(self.home, root=self.root)
        self.assertEqual({r["action"] for r in rows}, {"skip", "ignore"})
        self.assertEqual(self._target("feedback_ledgers.md").read_text(), before)

    def test_a_changed_source_is_updated(self):
        memory_import.apply(self.home, root=self.root)
        (self.auto / "feedback_ledgers.md").write_text(LEDGERS.replace("first Monday", "last Friday"))
        rows = memory_import.apply(self.home, root=self.root)
        self.assertEqual(self._actions(rows)["feedback_ledgers.md"], "update")
        self.assertIn("last Friday", self._target("feedback_ledgers.md").read_text())

    def test_an_edited_copy_is_never_overwritten(self):
        memory_import.apply(self.home, root=self.root)
        self._target("feedback_ledgers.md").write_text("Wren's own correction.\n")
        (self.auto / "feedback_ledgers.md").write_text(LEDGERS.replace("first Monday", "last Friday"))
        rows = memory_import.apply(self.home, root=self.root)
        self.assertEqual(self._actions(rows)["feedback_ledgers.md"], "conflict")
        self.assertEqual(self._target("feedback_ledgers.md").read_text(), "Wren's own correction.\n")

    def test_the_clis_index_is_imported_so_something_still_lists_them(self):
        memory_import.apply(self.home, root=self.root)
        text = self._target("MEMORY.md").read_text()
        self.assertIn("- [Ledgers](feedback_ledgers.md)", text)
        self.assertTrue(self._target("feedback_ledgers.md").exists())   # the link still resolves

    def test_a_removed_copy_is_dropped_and_never_imported_again(self):
        memory_import.apply(self.home, root=self.root)
        self._target("feedback_ledgers.md").unlink()
        (self.auto / "feedback_ledgers.md").write_text(LEDGERS + "Sam adds a line.\n")
        rows = memory_import.apply(self.home, root=self.root)
        self.assertEqual(self._actions(rows)["feedback_ledgers.md"], "dropped")
        self.assertFalse(self._target("feedback_ledgers.md").exists())

    def test_frontmatter_closing_at_the_end_of_the_file_is_one_block(self):
        (self.auto / "eof.md").write_text("---\nname: eof\ntype: user\n---")
        memory_import.apply(self.home, root=self.root)
        text = self._target("eof.md").read_text()
        self.assertEqual(text.count("---"), 2, text)
        self.assertIn("type: user\nimported_from: eof.md\n", text)

    def test_a_crlf_file_keeps_one_frontmatter_block(self):
        """guard: read_text's universal newlines already turn CRLF into LF
        before render sees it (passes before and after this round)."""
        (self.auto / "crlf.md").write_bytes(b"---\r\nname: crlf\r\n---\r\nSam uses CRLF.\r\n")
        memory_import.apply(self.home, root=self.root)
        text = self._target("crlf.md").read_text()
        self.assertEqual(text.count("---"), 2, text)
        self.assertTrue(text.endswith("---\nSam uses CRLF.\n"), text)

    def test_the_copy_inherits_its_originals_usage_history(self):
        """Usage bonuses are keyed by absolute path, and R9 hides the
        original once its copy is current: without a carry the import
        silently resets every imported memory's bonus to zero."""
        from cousin_lib import reinforce
        memory_search.search("quokka ledgers Monday", home=self.home, root=self.root)
        before = reinforce.load_counts(self.home)[str(self.auto / "feedback_ledgers.md")]
        memory_import.apply(self.home, root=self.root)
        after = reinforce.load_counts(self.home)
        self.assertEqual(after[str(self._target("feedback_ledgers.md"))]["count"], before["count"])
        self.assertGreater(reinforce.bonus(self.home, str(self._target("feedback_ledgers.md"))), 0.0)

    def test_no_harness_declared_is_an_empty_plan(self):
        (self.root / "config" / "harness.toml").unlink()
        self.assertEqual(memory_import.plan(self.home, root=self.root), [])


class TestOneHitPerMemory(ImportCase):
    def _paths(self, query):
        hits, _notice = memory_search.search(query, top=5, home=self.home, root=self.root)
        return [h["path"] for h in hits]

    def test_an_imported_file_is_found_once(self):
        self.assertIn(str(self.auto / "feedback_ledgers.md"), self._paths("quokka ledgers Monday"))
        memory_import.apply(self.home, root=self.root)
        paths = self._paths("quokka ledgers Monday")
        self.assertIn(str(self._target("feedback_ledgers.md")), paths)
        self.assertNotIn(str(self.auto / "feedback_ledgers.md"), paths)

    def test_a_source_changed_after_the_import_stays_searchable(self):
        memory_import.apply(self.home, root=self.root)
        (self.auto / "feedback_ledgers.md").write_text(LEDGERS + "Sam audits the wombat ledger too.\n")
        self.assertIn(str(self.auto / "feedback_ledgers.md"), self._paths("wombat ledger"))


class TestCli(ImportCase):
    def _main(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = memory.memory_main(["--home", str(self.home), "import-auto", *args])
        return rc, out.getvalue()

    def test_the_cli_is_a_dry_run_by_default(self):
        rc, out = self._main()
        self.assertEqual(rc, 0)
        self.assertIn("dry run", out)
        self.assertTrue(re.search(r"^  import +feedback_ledgers\.md  \(new\)$", out, re.M), out)
        self.assertFalse((self.home / "memory" / "imported").exists())

    def test_apply_writes_and_json_reports(self):
        rc, out = self._main("--apply", "--json")
        self.assertEqual(rc, 0)
        rows = json.loads(out)
        self.assertEqual(sorted(r["name"] for r in rows if r["action"] == "import"),
                         ["MEMORY.md", "feedback_ledgers.md", "reference_keys.md"])
        self.assertTrue(self._target("feedback_ledgers.md").exists())


class TestManifestIsTheRecord(ImportCase):
    """Review fix round 1: the manifest is what keeps an edited copy from
    being overwritten and a removed one from coming back (R8). A manifest
    that will not parse must stop an import, not reset it; and a copy that
    exists with no manifest row is an import only when it is exactly what
    the import would write."""

    def _manifest(self):
        return self._target(memory_import.MANIFEST)

    def test_a_corrupt_manifest_refuses_and_writes_nothing(self):
        memory_import.apply(self.home, root=self.root)
        self._target("feedback_ledgers.md").write_text("Wren's own correction.\n")
        self._target("reference_keys.md").unlink()                       # dropped
        self._manifest().write_text("{not json")
        with self.assertRaises(memory_import.ManifestError):
            memory_import.apply(self.home, root=self.root)
        with self.assertRaises(memory_import.ManifestError):
            memory_import.plan(self.home, root=self.root)
        self.assertEqual(self._target("feedback_ledgers.md").read_text(), "Wren's own correction.\n")
        self.assertFalse(self._target("reference_keys.md").exists())
        self.assertEqual(self._manifest().read_text(), "{not json")

    def test_the_cli_says_so_and_exits_2(self):
        memory_import.apply(self.home, root=self.root)
        self._manifest().write_text("{not json")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = memory.memory_main(["--home", str(self.home), "import-auto", "--apply"])
        self.assertEqual(rc, 2)
        self.assertIn(memory_import.MANIFEST, err.getvalue())
        self.assertEqual(out.getvalue(), "")

    def _forget(self, name):
        manifest = json.loads(self._manifest().read_text())
        del manifest[name]
        self._manifest().write_text(json.dumps(manifest))

    def test_a_copy_without_a_row_that_is_exactly_the_import_is_imported(self):
        """A run that died after writing the copy and before the manifest:
        the next run converges."""
        memory_import.apply(self.home, root=self.root)
        self._forget("feedback_ledgers.md")
        rows = memory_import.plan(self.home, root=self.root)
        self.assertEqual(self._actions(rows)["feedback_ledgers.md"], "import")

    def test_a_copy_without_a_row_that_was_edited_is_a_conflict(self):
        memory_import.apply(self.home, root=self.root)
        self._forget("feedback_ledgers.md")
        self._target("feedback_ledgers.md").write_text("Wren's own correction.\n")
        rows = memory_import.apply(self.home, root=self.root)
        self.assertEqual(self._actions(rows)["feedback_ledgers.md"], "conflict")
        self.assertEqual(self._target("feedback_ledgers.md").read_text(), "Wren's own correction.\n")


class TestLoggedQueriesDedupe(ImportCase):
    """Item 3 (final fix wave): _logged_queries did not dedupe query text,
    so a repeated query filled the replay sample and double-counted kept
    or lost in the exit measurement. Keep the newest event per query
    text, skip later (older, since the log is read newest-first) repeats."""

    def test_a_repeated_query_gives_one_row_the_newest(self):
        from cousin_lib import reinforce
        reinforce.record(self.home, [str(self.auto / "feedback_ledgers.md")],
                         query="quokka ledgers Monday")
        reinforce.record(self.home, [str(self.auto / "reference_keys.md")],
                         query="quokka ledgers Monday")
        reinforce.record(self.home, [str(self.auto / "feedback_ledgers.md"),
                                     str(self.auto / "reference_keys.md")],
                         query="quokka ledgers Monday")
        reinforce.record(self.home, [str(self.auto / "feedback_ledgers.md")],
                         query="spare keys blue tin")
        rows = memory_import._logged_queries(self.home, self.auto, sample=50)
        matches = [r for r in rows if r[0] == "quokka ledgers Monday"]
        self.assertEqual(len(matches), 1, rows)
        self.assertEqual(matches[0][1], ["feedback_ledgers.md", "reference_keys.md"])
        self.assertEqual(matches[0][2], 2)
        self.assertEqual(sorted(r[0] for r in rows),
                         ["quokka ledgers Monday", "spare keys blue tin"])


class TestReplay(ImportCase):
    """The recall regression test over the cousin's REAL queries, before
    against after: the queries come from memory/.recall-log.jsonl
    (reinforce.record, every search); --apply replays them as a baseline
    just before it writes, --verify replays them again. The searches below
    are real searches in this home, logged the same way."""

    def _search(self, query):
        return memory_search.search(query, top=3, home=self.home, root=self.root)

    def _cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = memory.memory_main(["--home", str(self.home), "import-auto", *args])
        return rc, out.getvalue()

    def test_the_logged_queries_keep_their_memories_after_the_import(self):
        self._search("quokka ledgers Monday")
        self._search("spare keys blue tin")
        memory_import.apply(self.home, root=self.root)
        base = json.loads(self._target(memory_import.BASELINE).read_text())
        self.assertEqual(sorted(q["query"] for q in base["queries"]),
                         ["quokka ledgers Monday", "spare keys blue tin"])
        report = memory_import.verify(self.home, root=self.root)
        self.assertEqual((report["queries"], report["kept"], report["lost"]), (2, 2, []))

    def test_a_lost_memory_is_reported(self):
        """The dedupe (R9) hides a harness file whose source is unchanged;
        a copy that no longer holds the memory must show as a loss. (The
        query avoids the file's own name: the keyword index matches paths.)"""
        self._search("Priya quokka Monday")
        memory_import.apply(self.home, root=self.root)
        self._target("feedback_ledgers.md").write_text("Wren rewrote this page.\n")
        report = memory_import.verify(self.home, root=self.root)
        self.assertEqual(report["lost"], [{"query": "Priya quokka Monday",
                                           "missing": ["feedback_ledgers.md"]}])

    def test_a_memory_the_query_already_missed_before_the_import_is_not_a_loss(self):
        """The log's own result is history: a harness file the query no
        longer surfaced before the import cannot be lost by it."""
        self._search("Priya quokka Monday")
        (self.auto / "feedback_ledgers.md").write_text("Toki took over the accounts.\n")
        memory_import.apply(self.home, root=self.root)
        self.assertEqual(memory_import.verify(self.home, root=self.root)["lost"], [])

    def test_a_dropped_copy_is_not_a_loss(self):
        self._search("quokka ledgers Monday")
        memory_import.apply(self.home, root=self.root)
        self._target("feedback_ledgers.md").unlink()
        self.assertEqual(memory_import.verify(self.home, root=self.root)["lost"], [])

    def test_the_replay_does_not_reinforce_what_it_measures(self):
        """guard: search(record=False) arrives in Task 2; this pins that the
        baseline and the replay both use it."""
        self._search("quokka ledgers Monday")
        log = self.home / "memory" / ".recall-log.jsonl"
        before = log.read_text()
        memory_import.apply(self.home, root=self.root)
        memory_import.verify(self.home, root=self.root)
        self.assertEqual(log.read_text(), before)

    def test_nothing_to_compare_exits_2(self):
        (self.home / "memory" / "chores.md").write_text("# Chores\nSam waters the ferns.\n")
        self._search("ferns Sam")         # no word any harness file holds (the keyword leg ORs words)
        rc, out = self._cli("--verify")
        self.assertEqual(rc, 2)
        self.assertIn("no baseline", out)
        rc, out = self._cli("--apply", "--verify")
        self.assertEqual(rc, 2)
        self.assertIn("replayed 0 queries", out)

    def test_the_cli_replays_and_exits_1_on_a_loss(self):
        self._search("Priya quokka Monday")
        rc, out = self._cli("--apply", "--verify")
        self.assertEqual(rc, 0, out)
        self.assertIn("replayed 1 logged query: 1 kept, 0 lost", out)
        self._target("feedback_ledgers.md").write_text("Wren rewrote this page.\n")
        rc, _out = self._cli("--verify")
        self.assertEqual(rc, 1)


class TestReplayOverACorruptManifest(ImportCase):
    """Controller ruling on Task 6 (Task 5's strict manifest kept): a
    --verify over a manifest it cannot read must not pretend to compare,
    because without it a dropped copy looks like a loss."""

    def test_verify_refuses_and_the_cli_exits_2(self):
        memory_search.search("Priya quokka Monday", top=3, home=self.home, root=self.root)
        memory_import.apply(self.home, root=self.root)
        self._target(memory_import.MANIFEST).write_text("{not json")
        with self.assertRaises(memory_import.ManifestError):
            memory_import.verify(self.home, root=self.root)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = memory.memory_main(["--home", str(self.home), "import-auto", "--verify"])
        self.assertEqual(rc, 2)
        self.assertIn(memory_import.MANIFEST, err.getvalue())
        self.assertNotIn("replayed", out.getvalue())


class TestReplayIndex(ImportCase):
    """R19: every search a replay runs sees a fully current index. The
    embedder is a stub, and the foreground budget is cut to 1 so a single
    search cannot catch up by itself."""

    def setUp(self):
        super().setUp()
        (self.root / "config" / "embedding.toml").write_text(
            'url = "http://embed.invalid"\nmodel = "stub"\n')
        stub = lambda text, config: [float(len(text) % 7 + 1), float(text.count("e") + 1), 1.0]
        for target, value in (("_embed", stub), ("FOREGROUND_BUDGET", 1)):
            p = mock.patch.object(memory_search, target, value); p.start(); self.addCleanup(p.stop)

    def _coverage(self):
        config = memory_search._embedding_config(self.root)
        chunks = memory_search._chunks(self.home, config, self.root)
        index = memory_search._load_index(self.home) or {}
        current = sum(1 for key, (_c, _p, text) in chunks.items()
                      if (index.get(key) or {}).get("vector")
                      and index[key].get("text_hash") == memory_search._text_hash(text))
        return current, len(chunks)

    def test_every_replay_search_sees_a_fully_current_index(self):
        memory_search.search("quokka ledgers Monday", top=3, home=self.home, root=self.root)
        seen, real = [], memory_search.search

        def spy(query, **kw):
            seen.append(self._coverage())
            return real(query, **kw)
        with mock.patch.object(memory_search, "search", spy):
            memory_import.apply(self.home, root=self.root)          # the baseline's searches
            memory_import.verify(self.home, root=self.root)         # the replay's searches
        self.assertGreaterEqual(len(seen), 2)
        self.assertEqual([c for c, _t in seen], [t for _c, t in seen])


class TestReplayIndexOverAnUnbackfilledDecisionLog(ImportCase):
    """Item 1 (final fix wave): a home whose data/decisions.jsonl still
    holds orphans (no mark) when the baseline runs. _index_current must
    backfill them BEFORE it brings the index current, or the baseline's
    own first search (memory_search.search -> memory.try_backfill,
    memory_search.py:919) appends them into raw right after the index
    was declared current, re-staling it; the semantic leg then only
    catches up FOREGROUND_BUDGET chunks per search, so the baseline
    ranks against a partly built index while a later --verify (run once
    the daemon or later searches have caught it up) ranks against a
    complete one."""

    def setUp(self):
        super().setUp()
        (self.root / "config" / "embedding.toml").write_text(
            'url = "http://embed.invalid"\nmodel = "stub"\n')
        stub = lambda text, config: [float(len(text) % 7 + 1), float(text.count("e") + 1), 1.0]
        for target, value in (("_embed", stub), ("FOREGROUND_BUDGET", 1)):
            p = mock.patch.object(memory_search, target, value); p.start(); self.addCleanup(p.stop)
        with open(self.home / "data" / "decisions.jsonl", "a") as fh:
            for i in range(3):
                fh.write(json.dumps({
                    "timestamp": "2026-05-1%dT10:00:00+02:00" % i,
                    "topic": "orphan-%d" % i,
                    "decision": "kept only in the log %d" % i,
                    "reasoning": "a copy %d" % i}) + "\n")
        from cousin_lib import reinforce
        # A hand-written log entry: a real search here would itself run
        # try_backfill and defeat the setup (the mark would already exist
        # before take_baseline ever runs).
        reinforce.record(self.home, [str(self.auto / "feedback_ledgers.md")],
                         query="quokka ledgers Monday")

    def test_the_baseline_never_re_stales_the_index_it_just_built(self):
        reports = []
        real = memory_search.ensure_index

        def spy(*a, **kw):
            report = real(*a, **kw)
            reports.append(report)
            return report
        with mock.patch.object(memory_search, "ensure_index", spy):
            memory_import.apply(self.home, root=self.root)
        self.assertTrue((self.home / "data" / ".decisions-backfilled").exists())
        self.assertTrue(reports)
        self.assertFalse(any(r["incomplete"] for r in reports), reports)


if __name__ == "__main__":
    unittest.main()
