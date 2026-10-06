"""One-hop derivation (memory.derived_from, memory.why) and proactive
recall's read receipt (memory_search: what a recall returned, what it
left out, and why)."""
import contextlib
import io
import json
import pathlib
import tempfile
from unittest import mock

from cousin_lib import memory, memory_search
from tests._hermetic import HermeticCase


class HomeCase(HermeticCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.home = self.root / "cousins" / "wren"
        for sub in ("data", "memory"):
            (self.home / sub).mkdir(parents=True)
        (self.home / "cousin.toml").write_text('[cousin]\nslug = "wren"\n')

    def ids(self, topic):
        return [row["id"] for row in memory.validity(self.home) if row["topic"] == topic]


class TestDerivedFrom(HomeCase):
    def test_an_entry_names_what_it_was_built_from_and_why_walks_one_hop(self):
        memory.remember(self.home, "kestrel", "seen on the roof at dawn")
        memory.remember(self.home, "kestrel", "seen on the roof at dusk")
        a, b = self.ids("kestrel")
        memory.remember(self.home, "kestrel-nest", "nests on the roof",
                        derived_from=[a, b, a])                      # deduplicated
        (c,) = self.ids("kestrel-nest")
        memory.decide(self.home, "roof", "keep the roof clear", "a nest is there",
                      derived_from=[c])
        out = memory.why(self.home, c)
        self.assertEqual([e["id"] for e in out["derived_from"]], [a, b])
        self.assertEqual([e["topic"] for e in out["used_by"]], ["roof"])
        self.assertIsNone(out["retired_by"])
        text = memory.format_why(out)
        self.assertIn("built from:", text)
        self.assertIn("seen on the roof at dawn", text)
        # nothing is inherited along the hop: each keeps its own level
        self.assertEqual(out["entry"]["truth_level"], "L3_COUSIN_CONCLUSION")

    def test_a_missing_source_and_a_retired_entry_are_said(self):
        memory.remember(self.home, "kestrel", "nests in the barn", derived_from=["0123456789ab"])
        (a,) = self.ids("kestrel")
        memory.mark_obsolete(self.home, "kestrel", "the roof, not the barn", entry=a)
        out = memory.why(self.home, a)
        self.assertEqual(out["derived_from"], [{"id": "0123456789ab", "missing": True}])
        self.assertIsNotNone(out["retired_by"])
        with self.assertRaises(KeyError):
            memory.why(self.home, "ffffffffffff")

    def test_a_bad_id_is_refused(self):
        for bad in (["roof"], ["0123456789AB"], 7):
            with self.assertRaisesRegex(ValueError, "derived_from"):
                memory.remember(self.home, "t", "f", derived_from=bad)

    def test_the_cli_takes_repeated_flags_and_why(self):
        memory.remember(self.home, "a", "one")
        (a,) = self.ids("a")
        with mock.patch.dict("os.environ", {"COUSIN_HOME": str(self.home)}):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = memory.memory_main(["remember", "b", "two", "--derived-from", a])
            self.assertEqual(rc, 0)
            (b,) = self.ids("b")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = memory.memory_main(["why", b, "--json"])
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(out.getvalue())["derived_from"][0]["id"], a)

    def test_the_memory_tool_has_why(self):
        from cousin_lib.runner import tools
        memory.remember(self.home, "a", "one")
        (a,) = self.ids("a")
        ctx = mock.Mock(home=self.home)
        self.assertIn("built from: nothing recorded", tools.HANDLERS["memory"]["why"](ctx, {"id": a}))
        with self.assertRaisesRegex(ValueError, "no raw entry"):
            tools.HANDLERS["memory"]["why"](ctx, {"id": "ffffffffffff"})


class TestReceipt(HomeCase):
    def config(self, keyword_only=True):
        return mock.Mock(proactive_recall=True, recall_keyword_only=keyword_only)

    def test_a_gated_recall_says_why_it_returned_nothing(self):
        with mock.patch.object(memory_search, "recall_thresholds",
                               return_value=({"min_chars": 12, "top": 3, "min_score": 0.5}, False)):
            memory_search.recall_entries(self.home, "hi there friend",
                                         config=self.config(keyword_only=False))
            memory_search.recall_entries(self.home, "hi", config=self.config())
        newest, older = memory_search.receipts(self.home)
        self.assertIn("min_chars", newest["gate"])
        self.assertIn("keyword-only", older["gate"])

    def test_recall_hits_gives_the_reader_each_kept_hit(self):
        raw = self.home / "memory" / "raw"; raw.mkdir(parents=True, exist_ok=True)
        (raw / "2026-10-06.jsonl").write_text(json.dumps(
            {"timestamp": "2026-10-06T10:00:00+00:00", "topic": "kestrel nest",
             "content": "the kestrel nests in the barn", "truth_level": "L2_TOOL"}) + "\n")
        hits = [{"path": str(self.home / "memory" / "a.md"), "collection": "memory",
                 "similarity": 0.9},
                {"path": str(raw / "2026-10-06.jsonl") + "#1", "collection": "raw",
                 "similarity": 0.8},
                {"path": str(self.home / "memory" / "b.md"), "collection": "memory",
                 "similarity": 0.2}]
        with mock.patch.object(memory_search, "recall_thresholds",
                               return_value=({"min_chars": 1, "top": 3, "min_score": 0.5}, True)), \
                mock.patch.object(memory_search, "search", return_value=(hits, None)):
            entries, items = memory_search.recall_hits(self.home, "where is the kestrel",
                                                       config=self.config())
        self.assertEqual(len(entries), 2)
        self.assertEqual([(i["collection"], i["rel"], i["similarity"]) for i in items],
                         [("memory", "memory/a.md", 0.9),
                          ("raw", "memory/raw/2026-10-06.jsonl#1", 0.8)])
        self.assertEqual((items[1]["name"], items[1]["level"]), ("kestrel nest", "L2_TOOL"))
        self.assertNotIn("level", items[0])

    def test_returned_and_excluded_hits_are_both_recorded(self):
        hits = [{"path": str(self.home / "memory" / "a.md"), "collection": "memory",
                 "similarity": 0.9},
                {"path": str(self.home / "memory" / "b.md"), "collection": "memory",
                 "similarity": 0.2}]
        with mock.patch.object(memory_search, "recall_thresholds",
                               return_value=({"min_chars": 1, "top": 3, "min_score": 0.5}, True)), \
                mock.patch.object(memory_search, "search", return_value=(hits, None)), \
                mock.patch.object(memory_search, "_hit_name", side_effect=lambda h: h["path"][-4:]):
            kept = memory_search.recall_entries(self.home, "where is the kestrel",
                                                config=self.config())
        self.assertEqual(len(kept), 1)
        (rec,) = memory_search.receipts(self.home)
        self.assertEqual([r["path"] for r in rec["returned"]], ["a.md"])
        self.assertEqual([r["path"] for r in rec["excluded"]], ["b.md"])
        self.assertIn("below [recall] min_score", rec["excluded"][0]["reason"])
        self.assertEqual(rec["query"], "where is the kestrel")
