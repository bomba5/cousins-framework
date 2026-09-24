"""Keyword memory search: FTS index, sanitize, staleness, collections
and the CLI surface.

The keyword leg (SQLite FTS5, BM25) is what every install has; the
semantic leg is tested in test_semantic_search. Real databases over
real files.
"""
import json
import os
import pathlib
import tempfile
import time
import unittest
from unittest import mock

from cousin_lib.config import MissingConfigError
from cousin_lib.memory_search import build_index, search
from tests._fakes import fake_embedder
from tests._hermetic import HermeticCase


class SearchCase(HermeticCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = pathlib.Path(tmp.name) / "cousins" / "wren"
        (self.home / "memory").mkdir(parents=True)
        (self.home / "notes").mkdir()
        (self.home / "memory" / "ports.md").write_text(
            "# Port allocation\n\nThe claimed set excludes every port"
            " in any cousin.toml.\n")
        (self.home / "notes" / "flip-plan.md").write_text(
            "# Flip plan\n\nThe generation boundary needs a verified"
            " identity write.\n")
        patcher = mock.patch.dict(os.environ,
                                  {"COUSIN_HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)


class TestContext(SearchCase):
    def test_no_context_refuses_loudly(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(MissingConfigError):
                search("anything")


class TestSearch(SearchCase):
    def test_finds_the_right_file_by_keyword(self):
        build_index()
        hits, _ = search("claimed set port")
        self.assertTrue(hits)
        self.assertIn("ports.md", hits[0]["path"])
        self.assertEqual(hits[0]["collection"], "memory")

    def test_notes_are_a_tagged_collection(self):
        build_index()
        hits, _ = search("verified identity write")
        self.assertIn("flip-plan.md", hits[0]["path"])
        self.assertEqual(hits[0]["collection"], "notes")

    def test_fts_syntax_characters_do_not_crash_the_query(self):
        # FTS5 treats -, /, ', ( as query syntax; a raw natural-language
        # query used to error and the keyword leg swallowed it silently.
        build_index()
        hits, _ = search("what's the claimed-set (port) rule?")
        self.assertTrue(hits)
        self.assertIn("ports.md", hits[0]["path"])

    def test_new_files_are_picked_up_without_explicit_reindex(self):
        build_index()
        time.sleep(0.05)
        (self.home / "memory" / "fresh.md").write_text(
            "# Fresh\n\nzanzibar considerations\n")
        hits, _ = search("zanzibar")
        self.assertTrue(hits)
        self.assertIn("fresh.md", hits[0]["path"])


class TestBonusReach(SearchCase):
    def test_a_bonus_beyond_top_never_displaces_a_path_ranked_within_top(self):
        # Ruling P1131-1: the #83 fusion-depth fix widened each leg's
        # candidate list past `top` so a path just past the old cut
        # could be fused at all, but _bonuses must not widen with it -
        # _fuse's contract is "nudged up, never carried past a better
        # match". Reproduces the reviewer's case through search():
        # keyword ranks 0..19 for widget*.md, a near-MAX_BONUS bonus
        # recorded on the rank-10 file (kw10.md), which the pre-#83
        # top=5 leg could never even fetch. kw10 must not displace
        # kw4 (rank 4, inside top=5).
        build_index()
        for i in range(20):
            (self.home / "memory" / ("kw%d.md" % i)).write_text(
                "# KW %d\n\n%s\n" % (i, " ".join(["widget"] * (20 - i))))
        from cousin_lib import reinforce
        target = str(self.home / "memory" / "kw10.md")
        (self.home / "memory" / ".recall-counts.json").write_text(json.dumps({
            target: {"count": 10 ** 9, "last": reinforce._iso(time.time())}
        }))
        self.assertGreater(reinforce.bonus(self.home, target), 0.14,
                           "fixture bonus must be near MAX_BONUS")
        hits, _ = search("widget", top=5)
        names = [pathlib.Path(h["path"]).name for h in hits]
        self.assertEqual(len(names), 5)
        self.assertNotIn("kw10.md", names,
                         "a bonus on a path beyond top must not reach"
                         " into the fused result (ruling P1131-1)")
        self.assertIn("kw4.md", names,
                      "the rank-4 path must not be displaced by a"
                      " deeper path's bonus")


class TestCliWiring(SearchCase):
    def _main(self, argv):
        import contextlib
        import io

        from cousin_lib.memory import memory_main
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = memory_main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_search_subcommand_returns_ranked_results(self):
        rc, out, _ = self._main(["search", "claimed set port"])
        self.assertEqual(rc, 0)
        self.assertIn("ports.md", out)

    def test_reindex_subcommand_rebuilds(self):
        rc, out, _ = self._main(["reindex"])
        self.assertEqual(rc, 0)
        self.assertIn("indexed", out.lower())

    def test_search_json_prints_a_list_of_hits(self):
        rc, out, _ = self._main(["search", "claimed set port", "--json"])
        self.assertEqual(rc, 0)
        hits = json.loads(out)
        self.assertIsInstance(hits, list)
        self.assertIn("ports.md", hits[0]["path"])
        self.assertEqual(set(hits[0]),
                         {"path", "collection", "score", "snippet", "chunk",
                          "similarity"})

    def test_search_json_with_no_hits_is_an_empty_list(self):
        rc, out, _ = self._main(["search", "zzz-nothing-here", "--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out), [])

    def test_search_collection_filters(self):
        rc, out, _ = self._main(["search", "verified identity write",
                                 "--collection", "notes", "--json"])
        self.assertEqual(rc, 0)
        hits = json.loads(out)
        self.assertTrue(hits)
        self.assertTrue(all(h["collection"] == "notes" for h in hits))
        rc, out, _ = self._main(["search", "verified identity write",
                                 "--collection", "memory", "--json"])
        self.assertEqual(json.loads(out), [])

    def test_reindex_reports_embedding_work_when_configured(self):
        root = self.home.parent.parent
        (root / "config").mkdir()
        with fake_embedder() as url, \
                mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": str(root)}):
            (root / "config" / "embedding.toml").write_text(
                'url = "%s"\nmodel = "m"\ntimeout_s = 2\n' % url)
            rc, out, err = self._main(["reindex"])
        self.assertEqual(rc, 0)
        self.assertIn("indexed 2 file(s)", out)
        self.assertIn("embedded 2 chunk(s)", out)
        self.assertEqual(err, "")


class TestCollections(SearchCase):
    def test_collection_filter_on_the_keyword_leg(self):
        hits, _ = search("verified identity write", collection="memory")
        self.assertEqual(hits, [])
        hits, _ = search("verified identity write", collection="notes")
        self.assertTrue(hits)
        self.assertTrue(all(h["collection"] == "notes" for h in hits))

    def _root_with_harness(self, template):
        root = self.home.parent.parent
        (root / "config").mkdir(exist_ok=True)
        (root / "config" / "harness.toml").write_text(
            'auto_memory_dir = "%s"\n' % template)
        return root

    def test_harness_collection_when_configured_and_present(self):
        root = self._root_with_harness(
            str(self.home.parent.parent / "harness") + "/{home_encoded}/memory")
        encoded = str(self.home).replace("/", "-")
        harness = root / "harness" / encoded / "memory"
        harness.mkdir(parents=True)
        (harness / "feedback_terse.md").write_text(
            "# Terse replies\n\nquokka preference: short statuses.\n")
        with mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": str(root)}):
            hits, _ = search("quokka")
            self.assertTrue(hits)
            self.assertEqual(hits[0]["collection"], "harness")
            self.assertIn("feedback_terse.md", hits[0]["path"])
            only, _ = search("quokka", collection="harness")
            self.assertEqual([h["path"] for h in only],
                             [hits[0]["path"]])

    def test_harness_collection_absent_when_directory_missing(self):
        root = self._root_with_harness(
            str(self.home.parent.parent / "nowhere") + "/{home_encoded}")
        with mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": str(root)}):
            hits, notice = search("claimed set port")
        self.assertIsNone(notice)
        self.assertTrue(all(h["collection"] != "harness" for h in hits))

    def test_no_root_means_own_files_only(self):
        # Without FRAMEWORK_ROOT there is no config/ to consult: the
        # searchable surface is memory/ and notes/, nothing else.
        with mock.patch.dict(os.environ, {}, clear=True):
            os.environ["COUSIN_HOME"] = str(self.home)
            hits, notice = search("claimed set port")
        self.assertIsNone(notice)
        self.assertEqual({h["collection"] for h in hits}, {"memory"})


if __name__ == "__main__":
    unittest.main()


class TestRefreshIfStale(SearchCase):
    """The unattended refresh cousin-loops runs for every cousin."""

    def test_builds_when_missing_then_reports_fresh(self):
        from cousin_lib import memory_search
        with mock.patch.object(memory_search, "_embedding_config",
                               return_value=None):
            report = memory_search.refresh_if_stale(self.home)
            self.assertEqual(report["files"], 2)
            self.assertIsNone(memory_search.refresh_if_stale(self.home))
            time.sleep(0.05)
            (self.home / "notes" / "new.md").write_text("# New\n")
            self.assertEqual(
                memory_search.refresh_if_stale(self.home)["files"], 3)


class TestRawEntriesAreIndexed(SearchCase):
    """The raw store is what `cousin-memory decide` and `remember`
    write, and it was never indexed: search covered `*.md` only, so an
    entry reached recall only through `distill`, which keeps one
    truncated line per topic and caps each file at 40 lines. Measured
    2026-09-21 on a real cousin: 904 entries over 789 topics reached
    recall as 139 lines, so 82% of topics could not be found at all.
    """

    def _raw(self, day, *entries):
        raw = self.home / "memory" / "raw"
        raw.mkdir(parents=True, exist_ok=True)
        (raw / ("%s.jsonl" % day)).write_text(
            "".join(json.dumps(e) + "\n" for e in entries))

    def test_an_entry_is_found_by_its_content(self):
        self._raw("2026-09-20", {
            "timestamp": "2026-09-20T10:00:00+02:00",
            "topic": "kestrel gateway",
            "content": "The kestrel gateway answers on the loopback only.",
            "truth_level": "L3_COUSIN_CONCLUSION", "source": "decision"})
        hits, _ = search("kestrel gateway", home=self.home, top=5)
        self.assertTrue(hits, "a raw entry must be findable")
        self.assertEqual(hits[0]["collection"], "raw")
        self.assertIn("kestrel", hits[0]["snippet"].lower())

    def test_each_entry_is_its_own_hit(self):
        self._raw("2026-09-20",
                  {"timestamp": "2026-09-20T10:00:00+02:00",
                   "topic": "plover one", "content": "plover flies north."},
                  {"timestamp": "2026-09-20T11:00:00+02:00",
                   "topic": "plover two", "content": "plover flies south."})
        hits, _ = search("plover", home=self.home, top=5)
        paths = {h["path"] for h in hits if h["collection"] == "raw"}
        self.assertEqual(len(paths), 2,
                         "two entries in one file are two hits, not one")

    def test_an_archived_entry_is_found_too(self):
        import gzip
        arch = self.home / "memory" / "raw" / "archive"
        arch.mkdir(parents=True)
        with gzip.open(arch / "2026-08.jsonl.gz", "wt") as fh:
            fh.write(json.dumps({
                "timestamp": "2026-08-03T09:00:00+02:00",
                "topic": "bittern pump",
                "content": "The bittern pump was replaced in August."}) + "\n")
        hits, _ = search("bittern pump", home=self.home, top=5)
        self.assertTrue(hits, "raw_fold's archives must stay findable")
        self.assertEqual(hits[0]["collection"], "raw")

    def test_a_new_entry_is_picked_up_without_an_explicit_reindex(self):
        self._raw("2026-09-20", {"timestamp": "2026-09-20T10:00:00+02:00",
                                 "topic": "first", "content": "osprey one"})
        search("osprey", home=self.home, top=3)
        time.sleep(0.01)
        self._raw("2026-09-21", {"timestamp": "2026-09-21T10:00:00+02:00",
                                 "topic": "second", "content": "osprey two"})
        hits, _ = search("osprey", home=self.home, top=5)
        self.assertEqual(len({h["path"] for h in hits
                              if h["collection"] == "raw"}), 2)

    def test_a_malformed_line_never_costs_the_index(self):
        raw = self.home / "memory" / "raw"
        raw.mkdir(parents=True, exist_ok=True)
        (raw / "2026-09-20.jsonl").write_text(
            "not json\n"
            + json.dumps({"timestamp": "2026-09-20T10:00:00+02:00",
                          "topic": "godwit", "content": "godwit survives"})
            + "\n")
        hits, _ = search("godwit", home=self.home, top=3)
        self.assertTrue(hits, "one bad line must not lose the good ones")

    def test_an_entry_kept_in_two_files_is_indexed_once(self):
        """raw_fold writes a month's entries to BOTH memory/raw/
        <YYYY-MM>-digest.jsonl and archive/<YYYY-MM>.jsonl.gz. Measured
        on a real cousin: 149 of 149 entries identical between the two.
        Indexed twice, they return as two hits and eat the result slots
        twice over."""
        import gzip
        # The twins share topic and content and differ in metadata,
        # which is what the real files look like: the digest carries
        # source "digest" plus id/entries/first_at/last_at/stability
        # /confidence that the archived original does not.
        original = {"timestamp": "2026-07-04T10:00:00+02:00",
                    "topic": "curlew", "content": "The curlew nested late.",
                    "source": "decision", "truth_level": "L3_COUSIN_CONCLUSION"}
        digest = dict(original, source="digest", entries=3, id="abc123",
                      first_at="2026-07-01T09:00:00+02:00",
                      last_at="2026-07-04T10:00:00+02:00",
                      stability="stable", confidence="medium")
        raw = self.home / "memory" / "raw"
        (raw / "archive").mkdir(parents=True, exist_ok=True)
        (raw / "2026-07-digest.jsonl").write_text(json.dumps(digest) + "\n")
        with gzip.open(raw / "archive" / "2026-07.jsonl.gz", "wt") as fh:
            fh.write(json.dumps(original) + "\n")
        hits, _ = search("curlew", home=self.home, top=5)
        raw_hits = [h for h in hits if h["collection"] == "raw"]
        self.assertEqual(len(raw_hits), 1,
                         "the same entry in two files is one memory")
        self.assertNotIn(".gz", raw_hits[0]["path"],
                         "keep the readable copy, not the archive")

    def test_the_collection_filter_reaches_raw(self):
        self._raw("2026-09-20", {"timestamp": "2026-09-20T10:00:00+02:00",
                                 "topic": "avocet", "content": "avocet here"})
        (self.home / "memory" / "avocet.md").write_text("# avocet\n\navocet\n")
        raw_only, _ = search("avocet", home=self.home, top=5, collection="raw")
        self.assertTrue(raw_only)
        self.assertEqual({h["collection"] for h in raw_only}, {"raw"})

    def test_a_truncated_archive_never_costs_the_other_entries(self):
        """Item 5 (final fix wave): a truncated gzip archive raises
        EOFError (a corrupt deflate stream, zlib.error) reading the bytes
        back, not OSError; _raw_entries and raw_entry must skip it like
        any other unreadable file, and try_backfill (a reader's own call
        into ensure_backfilled) must not let it through either."""
        import gzip
        self._raw("2026-09-20", {"timestamp": "2026-09-20T10:00:00+02:00",
                                 "topic": "sanderling", "content": "sanderling runs the tideline"})
        arch = self.home / "memory" / "raw" / "archive"
        arch.mkdir(parents=True)
        with gzip.open(arch / "2026-08.jsonl.gz", "wt") as fh:
            fh.write(json.dumps({"timestamp": "2026-08-03T09:00:00+02:00",
                                 "topic": "dunlin", "content": "dunlin was here"}) + "\n")
        good = (arch / "2026-08.jsonl.gz").read_bytes()
        (arch / "2026-08.jsonl.gz").write_bytes(good[:len(good) // 2])
        hits, _ = search("sanderling", home=self.home, top=5)
        self.assertTrue(hits, "a truncated archive must not cost the other entries")
        from cousin_lib import memory
        memory.try_backfill(self.home)          # must not raise
        entries = memory.recall_entries(self.home, "sanderling")
        self.assertTrue(entries, "recall must still answer over the truncated archive")


class TestCuratedFloor(SearchCase):
    """Indexing the raw store put entries in 36 of 45 top-three slots on
    a real corpus and pushed curated topic files from 13 to 2: BM25
    rewards a short document, so a 900-character entry outranks a 10 KB
    note that carries the same term. One slot is reserved so the thing a
    cousin wrote on purpose cannot be crowded out entirely. Operator's
    choice, 2026-09-21."""

    def _raw(self, *entries):
        raw = self.home / "memory" / "raw"
        raw.mkdir(parents=True, exist_ok=True)
        (raw / "2026-09-20.jsonl").write_text(
            "".join(json.dumps(e) + "\n" for e in entries))

    def _flood(self, term, count):
        # The real shape: entries are short and almost entirely the
        # term, so BM25's length normalisation puts them above a long
        # curated file that mentions it once.
        self._raw(*[{"timestamp": "2026-09-20T10:%02d:00+02:00" % i,
                     "topic": "%s %d" % (term, i),
                     "content": "%s %s %s occurrence %d."
                                % (term, term, term, i)}
                    for i in range(count)])

    def _curated(self, name, term):
        (self.home / "memory" / ("%s.md" % name)).write_text(
            "# %s\n\nThe %s protocol is the durable summary.\n\n"
            % (term.capitalize(), term)
            + "Unrelated background prose about other matters. " * 400)

    def test_a_curated_file_keeps_a_slot_when_entries_would_take_them_all(self):
        self._curated("teal", "teal")
        self._flood("teal", 10)
        hits, _ = search("teal", home=self.home, top=3)
        self.assertEqual(len(hits), 3)
        self.assertIn("memory", [h["collection"] for h in hits],
                      "a matching curated file must keep one slot")

    def test_the_best_hit_is_never_displaced(self):
        self._curated("teal", "teal")
        self._flood("teal", 10)
        hits, _ = search("teal", home=self.home, top=3)
        top_by_score = max(hits, key=lambda h: h["score"])
        self.assertEqual(hits[0]["path"], top_by_score["path"],
                         "the floor fills the last slot, never the first")

    def test_nothing_changes_when_a_curated_file_already_ranks(self):
        (self.home / "memory" / "widgeon.md").write_text(
            "# Widgeon\n\nwidgeon\n")
        self._raw({"timestamp": "2026-09-20T10:00:00+02:00",
                   "topic": "widgeon entry", "content": "widgeon once."})
        hits, _ = search("widgeon", home=self.home, top=3)
        self.assertEqual(len([h for h in hits
                              if h["collection"] == "memory"]), 1)

    def test_a_collection_filter_is_never_overridden(self):
        self._curated("gadwall", "gadwall")
        self._flood("gadwall", 5)
        hits, _ = search("gadwall", home=self.home, top=3,
                         collection="raw")
        self.assertTrue(hits)
        self.assertEqual({h["collection"] for h in hits}, {"raw"},
                         "an explicit collection filter wins over the floor")

    def test_a_single_result_is_left_alone(self):
        self._curated("smew", "smew")
        self._flood("smew", 5)
        hits, _ = search("smew", home=self.home, top=1)
        self.assertEqual(len(hits), 1)
