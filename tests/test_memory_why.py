"""Derivation (memory.derived_from, recorded one hop) and its walk
(memory.why: the whole chain, or `depth` hops), and proactive
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

    def _chain(self):
        """raw -> fact -> plan -> decision: a -> b -> c -> d (each built
        from the one before)."""
        memory.remember(self.home, "measure", "the NAS snapshot runs at 02:00", level="tool",
                        cite="crontab")
        (a,) = self.ids("measure")
        memory.remember(self.home, "fact", "verify after 02:30", derived_from=[a])
        (b,) = self.ids("fact")
        memory.remember(self.home, "plan", "check at 02:45 nightly", derived_from=[b])
        (c,) = self.ids("plan")
        memory.remember(self.home, "decision", "a loop checks at 02:45", derived_from=[c])
        (d,) = self.ids("decision")
        return a, b, c, d

    def test_why_walks_the_whole_chain_back(self):
        a, b, c, d = self._chain()
        out = memory.why(self.home, d)
        [hop1] = out["derived_from"]
        [hop2] = hop1["built_from"]
        [hop3] = hop2["built_from"]
        self.assertEqual([hop1["id"], hop2["id"], hop3["id"]], [c, b, a])
        self.assertNotIn("built_from", hop3)            # the chain's root
        self.assertEqual(hop3["truth_level"], "L2_TOOL")  # nothing inherited
        text = memory.format_why(out)
        self.assertIn("the NAS snapshot runs at 02:00", text)
        self.assertIn("      " + a, text)                # three levels in

    def test_why_walks_forward_too(self):
        a, b, c, d = self._chain()
        out = memory.why(self.home, a)
        [hop1] = out["used_by"]
        self.assertEqual(hop1["id"], b)
        self.assertEqual(hop1["built_on_by"][0]["built_on_by"][0]["id"], d)

    def test_depth_cuts_the_chain_and_says_there_is_more(self):
        a, b, c, d = self._chain()
        out = memory.why(self.home, d, depth=1)
        [hop1] = out["derived_from"]
        self.assertNotIn("built_from", hop1)
        self.assertTrue(hop1["more"])
        self.assertIn("(and further)", memory.format_why(out))

    def test_a_cycle_stops_the_walk(self):
        a, b, c, d = self._chain()
        # an entry that names itself (by a later copy's id) cannot loop: hand-made cycle
        raw = next((self.home / "memory" / "raw").glob("*.jsonl"))
        rows = [json.loads(l) for l in raw.read_text().splitlines()]
        out = memory.why(self.home, d)
        self.assertEqual(out["depth"], memory.WHY_MAX_DEPTH)
        with mock.patch.object(memory, "_all_raw", return_value=[
                dict(rows[0], derived_from=[d])] + rows[1:]):
            looped = memory.why(self.home, d)
        tail = looped["derived_from"][0]["built_from"][0]["built_from"][0]["built_from"][0]
        self.assertTrue(tail["cycle"])

    def test_the_cli_takes_depth(self):
        a, b, c, d = self._chain()
        out = io.StringIO()
        with mock.patch.dict("os.environ", {"COUSIN_HOME": str(self.home)}), \
                contextlib.redirect_stdout(out):
            rc = memory.memory_main(["why", d, "--depth", "1", "--json"])
        self.assertEqual(rc, 0)
        body = json.loads(out.getvalue())
        self.assertEqual(body["depth"], 1)
        self.assertTrue(body["derived_from"][0]["more"])

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


class TestRawRefs(HomeCase):
    """derived_from takes the refs recall and search show, and what a
    decision's reasoning or a cite names is linked without the flag."""

    def ref(self, topic):
        """`raw:<file>#<line>` of the one entry on `topic`, and its id."""
        for path in sorted((self.home / "memory" / "raw").glob("*.jsonl")):
            for n, line in enumerate(path.read_text().splitlines(), 1):
                entry = json.loads(line)
                if entry.get("topic") == topic:
                    return "raw:%s#%d" % (path.name, n), memory.entry_id(entry), path, n
        raise AssertionError(topic)

    def test_a_raw_ref_is_stored_as_the_id_it_names(self):
        memory.remember(self.home, "kestrel", "seen on the roof at dawn")
        ref, eid, path, n = self.ref("kestrel")
        memory.remember(self.home, "nest", "nests on the roof",
                        derived_from=[ref, "%s#%d" % (path, n), "memory/raw/%s#%d" % (path.name, n),
                                      "cousins/wren/memory/raw/%s#%d" % (path.name, n)])
        _, nest, _, _ = self.ref("nest")
        self.assertEqual([e["id"] for e in memory.why(self.home, nest)["derived_from"]], [eid])

    def test_a_ref_to_nothing_or_to_another_home_is_refused(self):
        memory.remember(self.home, "kestrel", "seen")
        ref, _, path, n = self.ref("kestrel")
        other = self.root / "cousins" / "finch" / "memory" / "raw" / path.name
        for bad in ([ref.replace("#%d" % n, "#99")], ["raw:../../cousin.toml#1"],
                    [str(other) + "#%d" % n],
                    ["cousins/finch/memory/raw/%s#%d" % (path.name, n)]):
            with self.assertRaisesRegex(ValueError, "raw ref"):
                memory.remember(self.home, "t", "f", derived_from=bad)

    def test_what_the_reasoning_or_the_cite_names_is_linked(self):
        memory.remember(self.home, "kestrel", "seen at dawn")
        memory.remember(self.home, "owl", "seen at dusk")
        k_ref, k, _, _ = self.ref("kestrel")
        _, o, _, _ = self.ref("owl")
        # a ref, a known id, an unknown 12-hex token (a commit sha) and a dead ref
        memory.decide(self.home, "roof", "keep the roof clear",
                      "the kestrel (%s) and %s; built at c1099544cc13; raw:2020-01-01.jsonl#3"
                      % (k_ref, o), derived_from=[k])
        _, roof, _, _ = self.ref("roof")
        self.assertEqual([e["id"] for e in memory.why(self.home, roof)["derived_from"]], [k, o])
        memory.remember(self.home, "barn", "no nest in the barn", cite="checked against " + k_ref)
        _, barn, _, _ = self.ref("barn")
        self.assertEqual([e["id"] for e in memory.why(self.home, barn)["derived_from"]], [k])
        # nothing named, nothing linked
        memory.remember(self.home, "plain", "a fact with no source")
        _, plain, _, _ = self.ref("plain")
        self.assertEqual(memory.why(self.home, plain)["derived_from"], [])

    def test_the_memory_tool_takes_a_raw_ref(self):
        from cousin_lib.runner import tools
        memory.remember(self.home, "kestrel", "seen")
        ref, eid, _, _ = self.ref("kestrel")
        ctx = mock.Mock(home=self.home)
        tools.HANDLERS["memory"]["remember"](ctx, {"topic": "nest", "fact": "on the roof",
                                                  "derived_from": [ref]})
        _, nest, _, _ = self.ref("nest")
        self.assertEqual([e["id"] for e in memory.why(self.home, nest)["derived_from"]], [eid])


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
        # a raw line carries the id derived_from stores, beside its ref
        eid = memory.entry_id(json.loads((raw / "2026-10-06.jsonl").read_text()))
        self.assertTrue(entries[1].endswith("(raw:2026-10-06.jsonl#1, id %s)" % eid), entries[1])
        self.assertTrue(entries[0].endswith("(memory:a.md)"), entries[0])
        self.assertEqual([(i["collection"], i["rel"], i["similarity"]) for i in items],
                         [("memory", "memory/a.md", 0.9),
                          ("raw", "memory/raw/2026-10-06.jsonl#1", 0.8)])
        self.assertEqual((items[1]["name"], items[1]["level"]), ("kestrel nest", "L2_TOOL"))
        self.assertNotIn("level", items[0])

    def test_a_hit_outside_the_resolved_home_is_still_home_relative(self):
        """A home reached through a symlink resolves elsewhere: the rel the
        pane links to is still home-relative, never collection-relative."""
        import os
        real = self.root / "real-wren"
        (real / "memory" / "imported" / "auto").mkdir(parents=True)
        note = real / "memory" / "imported" / "auto" / "x.md"
        note.write_text("# X\n")
        link = self.root / "link-wren"
        os.symlink(real, link)
        from cousin_lib.memory_search import _home_rel
        self.assertEqual(_home_rel(link, link / "memory" / "imported" / "auto" / "x.md",
                                   "memory", "imported/auto/x.md"), "memory/imported/auto/x.md")
        self.assertEqual(_home_rel(pathlib.Path("/elsewhere"), pathlib.Path("/other/x.md"),
                                   "memory", "imported/auto/x.md"), "memory/imported/auto/x.md")

    def test_returned_and_excluded_hits_are_both_recorded(self):
        hits = [{"path": str(self.home / "memory" / "a.md"), "collection": "memory",
                 "similarity": 0.9},
                {"path": str(self.home / "memory" / "b.md"), "collection": "memory",
                 "similarity": 0.2}]
        with mock.patch.object(memory_search, "recall_thresholds",
                               return_value=({"min_chars": 1, "top": 3, "min_score": 0.5}, True)), \
                mock.patch.object(memory_search, "search", return_value=(hits, None)), \
                mock.patch.object(memory_search, "_hit_name", side_effect=lambda h, entry=None: h["path"][-4:]):
            kept = memory_search.recall_entries(self.home, "where is the kestrel",
                                                config=self.config())
        self.assertEqual(len(kept), 1)
        (rec,) = memory_search.receipts(self.home)
        self.assertEqual([r["path"] for r in rec["returned"]], ["a.md"])
        self.assertEqual([r["path"] for r in rec["excluded"]], ["b.md"])
        self.assertIn("below [recall] min_score", rec["excluded"][0]["reason"])
        self.assertEqual(rec["query"], "where is the kestrel")


class TestTheToolPassesDepth(HomeCase):
    """#247 review: the in-process memory tool forwards `depth`."""

    def test_depth_reaches_why(self):
        from types import SimpleNamespace
        from cousin_lib.runner import tools
        memory.remember(self.home, "a", "one")
        (a,) = self.ids("a")
        memory.remember(self.home, "b", "two", derived_from=[a])
        (b,) = self.ids("b")
        memory.remember(self.home, "c", "three", derived_from=[b])
        (c,) = self.ids("c")
        out = json.loads(tools._m_why(SimpleNamespace(home=self.home), {"id": c, "depth": 1,
                                                                        "json": True}))
        self.assertEqual(out["depth"], 1)
        self.assertTrue(out["derived_from"][0]["more"])
