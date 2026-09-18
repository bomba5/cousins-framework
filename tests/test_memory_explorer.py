"""The memory explorer's read model: the layers a cousin's memory is
built from, with counts and update times, raw entries as fields
filterable by truth level, topic and date, decisions as records, and
the cheap insights (levels, recall, obsolete and stale counts)."""
import gzip
import json
import os
import pathlib
import tempfile
import time
import unittest

from cousin_lib import memory_explorer as mx
from cousin_lib import memory_trash


def _w(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


class ExplorerCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.home = self.root / "cousins" / "wren"
        self.home.mkdir(parents=True)
        raw = self.home / "memory" / "raw"
        rows = [
            {"topic": "kestrel api", "content": "port is 9",
             "truth_level": "L0_OPERATOR",
             "timestamp": "2026-09-01T10:00:00+00:00", "source": "chat"},
            {"topic": "kestrel api", "content": "port is 10",
             "truth_level": "operator-stated",
             "created_at": "2026-09-02T10:00:00+00:00", "source": "chat",
             "confidence": "high"},
            {"topic": "old idea", "content": "use the blue box",
             "truth_level": "L5_OBSOLETE",
             "timestamp": "2026-09-02T12:00:00+00:00", "source": "decision"},
            {"topic": "guess", "content": "maybe the cable",
             "truth_level": "L4_COUSIN_HYPOTHESIS",
             "timestamp": "2026-09-03T12:00:00+00:00"},
            {"topic": "plain", "content": "no level"},
        ]
        _w(raw / "2026-09-01.jsonl", json.dumps(rows[0]) + "\n")
        _w(raw / "2026-09-02.jsonl", "".join(json.dumps(r) + "\n"
                                             for r in rows[1:3]) + "garbage\n")
        _w(raw / "2026-09-03.jsonl", "".join(json.dumps(r) + "\n"
                                             for r in rows[3:]))
        _w(raw / "2026-07-digest.jsonl", json.dumps(
            {"topic": "folded", "content": "digest body", "entries": 4,
             "source": "digest", "truth_level": "cousin-conclusion",
             "timestamp": "2026-07-20T00:00:00+00:00",
             "first_at": "2026-07-01", "last_at": "2026-07-20"}) + "\n")
        (raw / "archive").mkdir()
        with gzip.open(raw / "archive" / "2026-07.jsonl.gz", "wt") as fh:
            fh.write(json.dumps({"topic": "folded", "content": "a",
                                 "timestamp": "2026-07-01T00:00:00+00:00"})
                     + "\n")
        _w(self.home / "memory" / "distilled" / "glossary.md",
           "# Glossary\n\n_(empty - awaiting distillation)_\n")
        _w(self.home / "memory" / "distilled" / "decisions.md",
           "# Decisions\n\n" + "- line\n" * 300)
        _w(self.home / "memory" / "fact-one.md", "# one\n")
        _w(self.home / "memory" / "cold.md", "# cold\n")
        _w(self.home / "notes" / "sub" / "n.md", "# n\n")
        _w(self.home / "MEMORY.md", "# index\n- [one](memory/fact-one.md)\n"
           "- [cold](cold.md) relative to memory/\n- [web](https://x.test/a)\n")
        _w(self.home / "STATUS.md", "# status\n")
        _w(self.home / "data" / "handoff.md", "# handoff\n")
        _w(self.home / "data" / "decisions.jsonl", "".join(
            json.dumps(d) + "\n" for d in [
                {"timestamp": "2026-09-01T09:00:00+02:00", "topic": "wire",
                 "decision": "use the red wire", "reasoning": "it is live"},
                {"timestamp": "2026-09-02T09:00:00+02:00", "topic": "box",
                 "decision": "blue box", "reasoning": "cheap"},
            ]))
        # the decide bridge's raw mirror of the first decision
        with open(raw / "2026-09-03.jsonl", "a") as fh:
            fh.write(json.dumps({"topic": "wire",
                                 "content": "use the red wire - why: it is live",
                                 "source": "decision",
                                 "truth_level": "cousin-conclusion",
                                 "timestamp": "2026-09-03T13:00:00+00:00"})
                     + "\n")
        _w(self.home / "memory" / ".recall-counts.json", json.dumps({
            str(self.home / "memory" / "fact-one.md"): {"count": 7,
                                                        "last": "2026-09-03"},
            "notes:sub/n.md": {"count": 2, "last": "2026-09-01"},
        }))
        _w(self.home / "memory" / ".recall-log.jsonl",
           json.dumps({"ts": "2026-09-03T10:00:00+00:00", "query": "q",
                       "paths": []}) + "\n")
        _w(self.home / "legacy" / "old.tar.gz", "x")


class Levels(unittest.TestCase):
    def test_aliases_and_defaults_normalize_to_the_taxonomy(self):
        self.assertEqual(mx.normalize_level("operator-stated"), "L0_OPERATOR")
        self.assertEqual(mx.normalize_level("cousin-conclusion"),
                         "L3_COUSIN_CONCLUSION")
        self.assertEqual(mx.normalize_level(None), "L3_COUSIN_CONCLUSION")
        self.assertEqual(mx.normalize_level("L5_OBSOLETE"), "L5_OBSOLETE")
        self.assertEqual(mx.normalize_level("l4_cousin_hypothesis"),
                         "L4_COUSIN_HYPOTHESIS")
        self.assertEqual(mx.normalize_level("vibes"), "other")


class Overview(ExplorerCase):
    def test_layers_with_counts_and_update_times(self):
        ov = mx.overview(self.home)
        layers = {l["id"]: l for l in ov["layers"]}
        for lid in ("active", "index", "raw", "digest", "archive",
                    "distilled", "decisions", "memory", "notes", "harness",
                    "search", "recall", "trash", "legacy"):
            self.assertIn(lid, layers, lid)
        self.assertEqual(layers["raw"]["count"], 5 + 1)
        self.assertEqual(layers["raw"]["files"], 3)
        self.assertEqual(layers["digest"]["count"], 1)
        self.assertEqual(layers["archive"]["files"], 1)
        self.assertEqual(layers["distilled"]["count"], 2)
        self.assertEqual(layers["distilled"]["stubs"], 1)
        self.assertEqual(layers["decisions"]["count"], 2)
        self.assertEqual(layers["memory"]["count"], 2)
        self.assertEqual(layers["notes"]["count"], 1)
        self.assertEqual(layers["active"]["count"], 2)
        self.assertEqual(layers["index"]["count"], 1)
        self.assertFalse(layers["harness"]["configured"])
        self.assertEqual(layers["legacy"]["count"], 1)
        self.assertIsInstance(layers["raw"]["updated"], float)

    def test_insights(self):
        ins = mx.overview(self.home)["insights"]
        self.assertEqual(ins["levels"]["L0_OPERATOR"], 2)
        self.assertEqual(ins["levels"]["L5_OBSOLETE"], 1)
        self.assertEqual(ins["levels"]["L4_COUSIN_HYPOTHESIS"], 1)
        self.assertEqual(ins["levels"]["L3_COUSIN_CONCLUSION"], 3)
        self.assertEqual(ins["obsolete"], 1)
        self.assertEqual(ins["hypotheses"], 1)
        self.assertEqual(ins["unparsable_lines"], 1)
        self.assertEqual(ins["undated"], 1)
        self.assertEqual(ins["multi_entry_topics"], 1)
        top = ins["most_recalled"]
        self.assertEqual(top[0]["path"], "memory/fact-one.md")
        self.assertEqual(top[0]["count"], 7)
        self.assertEqual(top[1]["path"], "notes/sub/n.md")
        self.assertEqual(ins["cold_files"], ["memory/cold.md"])
        self.assertEqual(ins["recall_events"], 1)
        self.assertEqual(ins["dangling_index_links"], [])
        (self.home / "memory" / "fact-one.md").unlink()
        self.assertEqual(mx.overview(self.home)["insights"]
                         ["dangling_index_links"], ["memory/fact-one.md"])
        self.assertGreaterEqual(ins["pending_fold_files"], 0)

    def test_an_empty_home_has_every_layer_at_zero(self):
        empty = self.root / "cousins" / "testa"
        empty.mkdir()
        ov = mx.overview(empty)
        self.assertTrue(all(l.get("count", 0) == 0 for l in ov["layers"]
                            if l["id"] != "search"))
        self.assertFalse((empty / "memory").exists(),
                         "the explorer must not create layout")


class RawEntries(ExplorerCase):
    def test_fields_not_json_and_newest_first(self):
        got = mx.raw_entries(self.home)
        # live = the daily files plus the monthly digests: what the
        # distiller reads
        self.assertEqual(got["total"], 7)
        first = got["entries"][0]
        self.assertEqual(first["topic"], "wire")
        self.assertEqual(first["source"], "decision")
        self.assertEqual(first["level"], "L3_COUSIN_CONCLUSION")
        self.assertEqual(first["ref"]["path"], "memory/raw/2026-09-03.jsonl")
        self.assertEqual(first["ref"]["line_no"], 3)
        second = [e for e in got["entries"] if e["content"] == "port is 10"][0]
        self.assertEqual(second["truth_level"], "operator-stated")
        self.assertEqual(second["level"], "L0_OPERATOR")
        self.assertEqual(second["extra"], {"confidence": "high"})
        self.assertEqual(second["timestamp"], "2026-09-02T10:00:00+00:00")

    def test_filters(self):
        def topics(**kw):
            return sorted(e["topic"] for e in
                          mx.raw_entries(self.home, **kw)["entries"])
        self.assertEqual(topics(levels=["L0_OPERATOR"]),
                         ["kestrel api", "kestrel api"])
        self.assertEqual(topics(levels=["L5_OBSOLETE", "L4_COUSIN_HYPOTHESIS"]),
                         ["guess", "old idea"])
        self.assertEqual(topics(topic="KESTREL"), ["kestrel api"] * 2)
        self.assertEqual(topics(q="cable"), ["guess"])
        self.assertEqual(topics(since="2026-09-02", until="2026-09-02"),
                         ["kestrel api", "old idea"])
        self.assertEqual(topics(tier="digest"), ["folded"])
        self.assertEqual(topics(tier="archive"), ["folded"])

    def test_digest_entries_carry_their_span_and_archive_has_no_ref(self):
        dig = mx.raw_entries(self.home, tier="digest")["entries"][0]
        self.assertEqual(dig["tier"], "digest")
        self.assertEqual(dig["entries"], 4)
        self.assertEqual(dig["first_at"], "2026-07-01")
        arc = mx.raw_entries(self.home, tier="archive")["entries"][0]
        self.assertIsNone(arc["ref"])

    def test_paging_and_facets(self):
        got = mx.raw_entries(self.home, limit=2, offset=1)
        self.assertEqual(len(got["entries"]), 2)
        self.assertEqual(got["total"], 7)
        self.assertEqual(got["facets"]["levels"]["L0_OPERATOR"], 2)

    def test_a_ref_deletes_exactly_that_entry(self):
        entry = [e for e in mx.raw_entries(self.home)["entries"]
                 if e["topic"] == "guess"][0]
        ref = entry["ref"]
        memory_trash.trash_lines(
            self.home, [(ref["path"], ref["line_no"], ref["sha"])])
        topics = [e["topic"] for e in mx.raw_entries(self.home)["entries"]]
        self.assertNotIn("guess", topics)
        self.assertIn("plain", topics)


class Decisions(ExplorerCase):
    def test_records_newest_first_with_their_raw_mirror(self):
        got = mx.decisions(self.home)
        self.assertEqual(got["total"], 2)
        first, second = got["entries"]
        self.assertEqual(first["topic"], "box")
        self.assertEqual(second["decision"], "use the red wire")
        self.assertEqual(second["reasoning"], "it is live")
        self.assertEqual(second["ref"]["line_no"], 1)
        self.assertEqual(len(second["mirrors"]), 1)
        self.assertEqual(second["mirrors"][0]["path"],
                         "memory/raw/2026-09-03.jsonl")
        self.assertEqual(first["mirrors"], [])

    def test_query_filter(self):
        got = mx.decisions(self.home, q="LIVE")
        self.assertEqual([d["topic"] for d in got["entries"]], ["wire"])


class LayerFiles(ExplorerCase):
    def test_memory_files_exclude_generated_and_index_artifacts(self):
        (self.home / "memory" / "fts_index.db").write_text("x")
        batch = memory_trash.trash_file(self.home, "memory/cold.md")
        self.assertTrue(batch)
        paths = [f["path"] for f in mx.layer_files(self.home, "memory")]
        self.assertEqual(paths, ["memory/fact-one.md"])

    def test_files_carry_recall_count_and_deletable(self):
        rows = {f["path"]: f for f in mx.layer_files(self.home, "memory")}
        self.assertEqual(rows["memory/fact-one.md"]["recalls"], 7)
        self.assertTrue(rows["memory/fact-one.md"]["deletable"])
        rows = {f["path"]: f for f in mx.layer_files(self.home, "distilled")}
        self.assertTrue(rows["memory/distilled/glossary.md"]["stub"])
        self.assertFalse(rows["memory/distilled/glossary.md"]["deletable"])
        rows = {f["path"]: f for f in mx.layer_files(self.home, "active")}
        self.assertEqual(sorted(rows), ["STATUS.md", "data/handoff.md"])
        self.assertFalse(rows["STATUS.md"]["deletable"])
        rows = mx.layer_files(self.home, "legacy")
        self.assertTrue(rows[0]["deletable"])
        self.assertTrue(rows[0]["legacy"])

    def test_the_harness_layer_reads_the_configured_directory(self):
        harness = self.root / "harness-mem"
        _w(harness / "feedback_x.md", "# x\n")
        _w(self.root / "config" / "harness.toml",
           'auto_memory_dir = "%s"\n' % harness)
        ov = mx.overview(self.home, root=self.root)
        layer = [l for l in ov["layers"] if l["id"] == "harness"][0]
        self.assertTrue(layer["configured"])
        self.assertEqual(layer["count"], 1)
        rows = mx.layer_files(self.home, "harness", root=self.root)
        self.assertEqual(rows[0]["path"], "feedback_x.md")
        self.assertFalse(rows[0]["deletable"])

    def test_unknown_layer_is_refused(self):
        with self.assertRaises(ValueError):
            mx.layer_files(self.home, "nope")


if __name__ == "__main__":
    unittest.main()
