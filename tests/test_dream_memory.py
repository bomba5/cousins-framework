"""Dream memory (cousin_lib/dream_memory.py): the slice and its cursor,
the attempt token, the four operations and their refusals, the journal
and undo.

The refusals are the module. A pass that may only touch what its slice
showed it, and never what the operator or a tool said, is enforced here
and not in the prompt, so every one of them is asserted against the code
path the model would actually reach.
"""
import gzip
import json
import pathlib
import tempfile
import unittest

from cousin_lib import dream_memory, memory


class DreamCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = pathlib.Path(tmp.name) / "cousins" / "wren"
        self.home.mkdir(parents=True)
        memory.ensure_layout(self.home)
        self.pass_id = "20261002T201500-abc123"

    def raw(self, day, *rows, **kw):
        """Write raw lines with the stamps given, so ids and positions are
        the test's to know: (topic, content) or (topic, content, level)."""
        source = kw.get("source", "remember")
        lines = []
        for i, row in enumerate(rows):
            topic, content = row[0], row[1]
            level = row[2] if len(row) > 2 else "L3_COUSIN_CONCLUSION"
            lines.append(json.dumps({"timestamp": "%sT10:%02d:00+00:00" % (day, i),
                                     "topic": topic, "content": content,
                                     "truth_level": level, "source": source}))
        path = memory.raw_dir(self.home) / ("%s.jsonl" % day)
        with open(path, "a") as fh:
            fh.write("".join(l + "\n" for l in lines))
        return [memory.entry_id(json.loads(l)) for l in lines]

    def dream(self, *, chars=40000, pass_id=None):
        """What a pass does before it thinks: take a slice, take the
        token. Returns the slice."""
        piece = dream_memory.slice_for(self.home, chars=chars)
        if not piece.empty:
            dream_memory.begin(self.home, pass_id or self.pass_id)
        return piece

    def live(self, topic):
        return [r for r in memory.validity(self.home)
                if str(r.get("topic") or "") == topic and not r.get("valid_to")]

    def run_op(self, name, args, *, pass_id=None):
        """Call an operation the way the harness does: by its schema
        name, through the same ValueError path."""
        op = next(o for o in dream_memory.OPERATIONS if o["name"] == name)
        return op["fn"](self.home, pass_id or self.pass_id, args)


# ---- the slice and its cursor ------------------------------------------------

class TestSlice(DreamCase):
    def test_a_slice_is_the_next_lines_and_says_where_it_got_to(self):
        ids = self.raw("2026-10-01", ("kestrel", "the barn is blue"),
                       ("wren", "a claim"), ("kestrel", "and the barn is red"))
        piece = self.dream()
        self.assertFalse(piece.empty)
        self.assertEqual(piece.through, {"month": "2026-10-01", "lines": 3})
        for eid in ids:
            self.assertIn("(%s)" % eid, piece.text)
        self.assertEqual(piece.coverage["entries"], 3)
        self.assertEqual(piece.coverage["topics"], 2)
        self.assertEqual(piece.coverage["cut"], "end")
        self.assertIsNone(piece.coverage["from"])   # nothing dreamed yet

    def test_the_next_slice_starts_where_the_last_one_stopped(self):
        self.raw("2026-10-01", ("kestrel", "one"), ("kestrel", "two"))
        with open(memory.raw_dir(self.home) / "2026-10-02.jsonl", "a") as fh:
            fh.write(json.dumps({"timestamp": "2026-10-02T10:00:00+00:00",
                                 "topic": "kestrel", "content": "three",
                                 "truth_level": "L3_COUSIN_CONCLUSION",
                                 "source": "remember"}) + "\n")
        first = self.dream(chars=90)
        self.assertEqual(first.coverage["cut"], "budget")
        self.assertEqual(first.through["lines"], 1)
        dream_memory.commit(self.home, self.pass_id, first.through)
        second = dream_memory.slice_for(self.home, chars=40000)
        self.assertNotIn("(one)", second.text.split("\n")[0])
        self.assertEqual(second.through, {"month": "2026-10-02", "lines": 1})
        # the union of two bounded slices is the whole file, once each
        seen = [l for l in (first.text + "\n" + second.text).split("\n") if l]
        self.assertEqual(len(seen), 3)
        self.assertEqual(len(set(seen)), 3)

    def test_an_empty_home_is_empty_and_says_so(self):
        piece = dream_memory.slice_for(self.home, chars=40000)
        self.assertTrue(piece.empty)
        self.assertEqual(piece.through, None)
        self.assertEqual(piece.text, "")

    def test_nothing_new_after_a_commit_is_empty(self):
        self.raw("2026-10-01", ("kestrel", "one"))
        piece = self.dream()
        dream_memory.commit(self.home, self.pass_id, piece.through)
        again = dream_memory.slice_for(self.home, chars=40000)
        self.assertTrue(again.empty)

    def test_the_frameworks_own_log_is_consumed_but_never_shown(self):
        self.raw("2026-10-01", ("job:12", "a job finished"), ("kestrel", "a claim"))
        piece = dream_memory.slice_for(self.home, chars=40000)
        self.assertNotIn("a job finished", piece.text)
        self.assertEqual(piece.coverage["machine"], 1)
        self.assertEqual(piece.coverage["entries"], 1)
        self.assertEqual(piece.through["lines"], 2)     # consumed, not shown

    def test_a_month_digest_is_never_read_as_a_claim(self):
        self.raw("2026-10-01", ("kestrel", "a claim"))
        digest = memory.raw_dir(self.home) / "2026-09-digest.jsonl"
        digest.write_text(json.dumps({"timestamp": "2026-09-30T10:00:00+00:00",
                                      "topic": "kestrel", "content": "a summary",
                                      "truth_level": "L5_OBSOLETE",
                                      "source": "digest"}) + "\n")
        piece = dream_memory.slice_for(self.home, chars=40000)
        self.assertNotIn("a summary", piece.text)
        self.assertEqual(piece.through, {"month": "2026-10-01", "lines": 1})

    def test_a_claim_longer_than_the_budget_is_taken_alone_and_flagged(self):
        self.raw("2026-10-01", ("kestrel", "x" * 900))
        piece = self.dream(chars=200)
        self.assertEqual(piece.coverage["cut"], "over-budget")
        self.assertEqual(piece.coverage["entries"], 1)
        self.assertIn("(truncated)", piece.text)          # shown whole-ish
        self.assertEqual(piece.coverage["chars"], len(piece.text) + 1)
        dream_memory.commit(self.home, self.pass_id, piece.through)
        # the next slice starts past it, and a short claim fits again
        self.raw("2026-10-02", ("kestrel", "short"))
        second = dream_memory.slice_for(self.home, chars=200)
        self.assertEqual(second.coverage["cut"], "end")
        self.assertEqual(second.through, {"month": "2026-10-02", "lines": 1})

    def test_one_pass_at_a_time(self):
        self.raw("2026-10-01", ("kestrel", "one"))
        self.dream()
        with self.assertRaisesRegex(ValueError, "one pass at a time"):
            dream_memory.slice_for(self.home, chars=40000)
        with self.assertRaisesRegex(ValueError, "has been dreaming since"):
            dream_memory.begin(self.home, "20261002T210000-def456")


class TestTheCursorIsAStructuralRefusal(DreamCase):
    """The cursor is {month, lines} and the module refuses anything else,
    because a wrong cursor is a silent re-dream that never ends."""

    def test_a_timestamp_or_an_entry_id_is_refused(self):
        for bad in ({"month": "2026-10-01T10:00:00", "lines": 3},
                    {"month": "2026-10-01", "lines": 3, "at": "10:00"},
                    {"month": "1f3c9ab204de", "lines": 1},
                    {"month": "2026-10-01", "lines": "3"},
                    {"month": "2026-10-01", "lines": -1},
                    {"month": "2026-10-01", "lines": 3.0},
                    {"month": "2026-10-01", "lines": True},
                    "2026-10-01", 3, None, {}):
            with self.assertRaises(ValueError):
                dream_memory.cursor(bad)

    def test_commit_refuses_a_cursor_behind_the_ledger(self):
        self.raw("2026-10-01", ("kestrel", "one"), ("kestrel", "two"))
        piece = self.dream()
        dream_memory.commit(self.home, self.pass_id, piece.through)
        self.raw("2026-10-02", ("kestrel", "three"))
        self.dream(pass_id="20261002T210000-def456")
        with self.assertRaisesRegex(ValueError, "behind the ledger"):
            dream_memory.commit(self.home, "20261002T210000-def456",
                                {"month": "2026-10-01", "lines": 1})

    def test_a_cursor_in_a_folded_day_resumes_in_its_archive(self):
        archive = memory.raw_dir(self.home) / "archive"
        archive.mkdir(parents=True)
        with gzip.open(archive / "2026-09.jsonl.gz", "wt") as fh:
            fh.write(json.dumps({"timestamp": "2026-09-20T10:00:00+00:00",
                                 "topic": "kestrel", "content": "archived claim",
                                 "truth_level": "L3_COUSIN_CONCLUSION",
                                 "source": "remember"}) + "\n")
        led = dream_memory._save(self.home, {
            "schema_version": dream_memory.SCHEMA_VERSION,
            "through": {"month": "2026-09-15", "lines": 7}})
        self.assertEqual(led["through"]["month"], "2026-09-15")
        piece = dream_memory.slice_for(self.home, chars=40000)
        # the day the cursor named is gone, so the slice starts at the
        # first file past it: its month's archive
        self.assertIn("archived claim", piece.text)
        self.assertEqual(piece.through, {"month": "2026-09", "lines": 1})
        self.assertEqual(piece.coverage["from"], {"month": "2026-09-15",
                                                  "lines": 7})


    def test_new_days_are_dreamed_after_a_walk_that_ended_in_an_archive(self):
        archive = memory.raw_dir(self.home) / "archive"
        archive.mkdir(parents=True)
        with gzip.open(archive / "2026-08.jsonl.gz", "wt") as fh:
            fh.write(json.dumps({"timestamp": "2026-08-20T10:00:00+00:00",
                                 "topic": "kestrel", "content": "archived claim",
                                 "truth_level": "L3_COUSIN_CONCLUSION",
                                 "source": "remember"}) + "\n")
        self.raw("2026-10-01", ("kestrel", "a day claim"))
        piece = self.dream()
        # oldest first: the archived month, then the day
        self.assertLess(piece.text.index("archived claim"), piece.text.index("a day claim"))
        self.assertEqual(piece.through, {"month": "2026-10-01", "lines": 1})
        dream_memory.commit(self.home, self.pass_id, piece.through)
        self.raw("2026-10-03", ("kestrel", "written after the pass"))
        again = dream_memory.slice_for(self.home, chars=40000)
        self.assertFalse(again.empty)
        self.assertIn("written after the pass", again.text)


class TestTheAttemptToken(DreamCase):
    def test_begin_without_a_slice_refuses(self):
        self.raw("2026-10-01", ("kestrel", "one"))
        with self.assertRaisesRegex(ValueError, "no slice was taken"):
            dream_memory.begin(self.home, self.pass_id)

    def test_commit_without_an_attempt_refuses(self):
        with self.assertRaisesRegex(ValueError, "no open attempt"):
            dream_memory.commit(self.home, self.pass_id,
                                {"month": "2026-10-01", "lines": 1})

    def test_abandon_leaves_the_cursor_where_it_was(self):
        self.raw("2026-10-01", ("kestrel", "one"))
        piece = self.dream()
        dream_memory.abandon(self.home, self.pass_id, "the CLI died")
        self.assertIsNone(dream_memory.committed(self.home))
        again = dream_memory.slice_for(self.home, chars=40000)
        self.assertEqual(again.through, piece.through)   # the same slice again

    def test_abandon_never_clears_another_pass(self):
        (eid,) = self.raw("2026-10-01", ("kestrel", "one"))
        self.dream(pass_id="20261002T210000-def456")
        out = dream_memory.abandon(self.home, "20261002T220000-ghi789", "not mine")
        self.assertEqual(out["abandoned"], False)
        # the pass that does hold the token still holds it
        self.assertEqual(self.run_op("retire", {"entry_id": eid, "why": "dup"},
                                     pass_id="20261002T210000-def456")["op"],
                         "retire")

    def test_a_stale_attempt_is_released_and_a_live_one_kept(self):
        (eid,) = self.raw("2026-10-01", ("kestrel", "one"))
        self.dream()
        self.run_op("retire", {"entry_id": eid, "why": "dup"})
        # the pass dies here: no commit, no abandon
        self.assertIsNone(dream_memory.release_stale(self.home, 3600))
        with self.assertRaisesRegex(ValueError, "one pass at a time"):
            dream_memory.slice_for(self.home, chars=40000)
        self.assertEqual(dream_memory.release_stale(self.home, -1), self.pass_id)
        self.assertIsNone(dream_memory.committed(self.home))   # the cursor stays
        dream_memory.slice_for(self.home, chars=40000)          # the next pass runs
        # what the dead pass changed is still on its journal
        self.assertEqual([r["op"] for r in dream_memory.journal(self.home, self.pass_id)],
                         ["retire"])

    def test_an_operation_after_a_commit_writes_nothing(self):
        self.raw("2026-10-01", ("kestrel", "one"))
        (eid,) = self.raw("2026-10-02", ("kestrel", "two"))
        piece = self.dream()
        dream_memory.commit(self.home, self.pass_id, piece.through)
        with self.assertRaisesRegex(ValueError, "no open attempt"):
            self.run_op("retire", {"entry_id": eid, "why": "after the fact"})
        self.assertEqual(len(self.live("kestrel")), 2)

    def test_a_pass_id_is_not_a_path(self):
        for bad in ("", "../../etc/passwd", "a/b", "x" * 100, None):
            with self.assertRaises(ValueError):
                dream_memory.begin(self.home, bad)


# ---- what an operation may do ------------------------------------------------

class TestRetire(DreamCase):
    """Each test writes everything it needs into raw first, then takes
    its own slice: a claim the slice never showed is refused before any
    other rule is even reached."""

    def setUp(self):
        super().setUp()
        self.ids = self.raw("2026-10-01", ("kestrel", "the barn is blue"),
                            ("kestrel", "the barn is blue too"))

    def test_it_writes_one_mark_and_journals_the_change(self):
        self.dream()
        record = self.run_op("retire", {"entry_id": self.ids[0],
                                        "why": "the second says it in full"})
        self.assertEqual(record["op"], "retire")
        self.assertEqual(record["entry_ids"], [self.ids[0]])
        self.assertEqual(record["topic"], "kestrel")
        self.assertTrue(record["mark_id"])
        mark = [e for e in memory._all_raw(self.home)
                if memory.is_entry_mark(e)][0]
        self.assertEqual(mark["source"], "dream")
        self.assertEqual(mark["by"], "dream:%s" % self.pass_id)
        self.assertEqual(mark["entry"], self.ids[0])
        self.assertEqual([r["id"] for r in self.live("kestrel")], [self.ids[1]])
        (jnl,) = dream_memory.journal(self.home, self.pass_id)
        self.assertEqual(jnl["mark_id"], record["mark_id"])

    def test_it_refuses_an_operator_a_framework_or_a_tool_claim(self):
        said = []
        for level in ("L0_OPERATOR", "L1_FRAMEWORK", "L2_TOOL"):
            said += self.raw("2026-10-01", ("kestrel", "said at %s" % level,
                                            level))
        self.dream()
        for eid in said:
            with self.assertRaisesRegex(ValueError, "L0-L2") as caught:
                self.run_op("retire", {"entry_id": eid, "why": "no"})
            self.assertIn("never retires it", str(caught.exception))
        self.assertEqual(len(self.live("kestrel")), 5)
        self.assertEqual(dream_memory.journal(self.home, self.pass_id), [])

    def test_it_refuses_a_claim_the_slice_never_showed(self):
        self.dream()
        # written after the slice was taken, into a file the slice did not
        # reach: in raw, but not in the pass's page
        (eid,) = self.raw("2026-10-09", ("kestrel", "written later"))
        with self.assertRaisesRegex(ValueError, "not in this pass's slice"):
            self.run_op("retire", {"entry_id": eid, "why": "no"})

    def test_it_refuses_a_claim_stamped_after_the_pass_began(self):
        future = {"timestamp": "2099-01-01T00:00:00+00:00", "topic": "kestrel",
                  "content": "from the future",
                  "truth_level": "L3_COUSIN_CONCLUSION", "source": "remember"}
        with open(memory.raw_dir(self.home) / "2026-10-01.jsonl", "a") as fh:
            fh.write(json.dumps(future) + "\n")
        self.dream()
        with self.assertRaisesRegex(ValueError, "after this pass started"):
            self.run_op("retire", {"entry_id": memory.entry_id(future),
                                   "why": "no"})

    def test_it_refuses_an_unknown_an_already_retired_and_an_empty_why(self):
        self.dream()
        with self.assertRaisesRegex(ValueError, "no claim"):
            self.run_op("retire", {"entry_id": "ffffffffffff", "why": "no"})
        self.run_op("retire", {"entry_id": self.ids[0], "why": "once"})
        with self.assertRaisesRegex(ValueError, "already retired"):
            self.run_op("retire", {"entry_id": self.ids[0], "why": "twice"})
        with self.assertRaisesRegex(ValueError, "why is required"):
            self.run_op("retire", {"entry_id": self.ids[1], "why": "  "})
        with self.assertRaisesRegex(ValueError, "entry_id is required"):
            self.run_op("retire", {"why": "no id"})
        # the one call that was allowed is the only thing on the journal
        self.assertEqual(len(self.live("kestrel")), 1)
        self.assertEqual([r["op"] for r in
                          dream_memory.journal(self.home, self.pass_id)],
                         ["retire"])


class TestMerge(DreamCase):
    def setUp(self):
        super().setUp()
        self.ids = self.raw("2026-10-01", ("kestrel", "the barn is blue"),
                            ("kestrel", "the barn is blue, painted"),
                            ("kestrel", "the barn is red"))
        # the slice shows a claim on another topic too, so the topic rule
        # is exercised on a claim the pass was actually shown
        self.other = self.raw("2026-10-01", ("wren", "an unrelated claim"))
        self.dream()

    def test_keep_leaves_one_live_claim(self):
        record = self.run_op("merge", {"topic": "kestrel", "keep": self.ids[1],
                                       "retire": [self.ids[0], self.ids[2]],
                                       "why": "one says it in full"})
        self.assertEqual(record["op"], "merge")
        self.assertEqual(sorted(record["entry_ids"]),
                         sorted([self.ids[0], self.ids[2]]))
        self.assertEqual(record["kept"], [self.ids[1]])
        self.assertIsNone(record["created"])
        self.assertEqual([r["id"] for r in self.live("kestrel")], [self.ids[1]])

    def test_fact_writes_the_consolidated_claim_and_leaves_one_live(self):
        record = self.run_op("merge", {"topic": "kestrel",
                                       "fact": "the barn is blue",
                                       "retire": self.ids,
                                       "why": "the three claims are one fact"})
        live = self.live("kestrel")
        self.assertEqual(len(live), 1)
        self.assertEqual(live[0]["id"], record["created"])
        self.assertEqual(live[0]["source"], "dream")
        self.assertEqual(live[0]["truth_level"], "L3_COUSIN_CONCLUSION")
        self.assertEqual(memory.entry_id(live[0]), record["created"])
        self.assertEqual(len(record["marks"]), 3)

    def test_exactly_one_of_keep_and_fact(self):
        with self.assertRaisesRegex(ValueError, "exactly one of keep"):
            self.run_op("merge", {"topic": "kestrel", "keep": self.ids[1],
                                  "fact": "x", "retire": [self.ids[0]],
                                  "why": "both"})
        with self.assertRaisesRegex(ValueError, "exactly one of keep"):
            self.run_op("merge", {"topic": "kestrel", "retire": [self.ids[0]],
                                  "why": "neither"})
        with self.assertRaisesRegex(ValueError, "retire must be a list"):
            self.run_op("merge", {"topic": "kestrel", "keep": self.ids[1],
                                  "retire": [], "why": "empty"})

    def test_keep_and_retire_must_be_disjoint(self):
        with self.assertRaisesRegex(ValueError, "disjoint"):
            self.run_op("merge", {"topic": "kestrel", "keep": self.ids[1],
                                  "retire": [self.ids[0], self.ids[1]],
                                  "why": "both"})

    def test_one_topic_per_operation(self):
        with self.assertRaisesRegex(ValueError, "is on topic 'wren'"):
            self.run_op("merge", {"topic": "kestrel", "keep": self.ids[1],
                                  "retire": [self.other[0]],
                                  "why": "wrong topic"})

    def test_a_pass_never_writes_an_operator_level_claim(self):
        for level in ("operator", "L0_OPERATOR", "framework", "tool"):
            with self.assertRaisesRegex(ValueError, "L3"):
                self.run_op("merge", {"topic": "kestrel", "fact": "x",
                                      "level": level, "retire": [self.ids[0]],
                                      "why": "the operator did not say this"})
        record = self.run_op("merge", {"topic": "kestrel", "fact": "maybe red",
                                       "level": "hypothesis",
                                       "retire": [self.ids[2]], "why": "a guess"})
        written = [r for r in self.live("kestrel") if r["id"] == record["created"]]
        self.assertEqual(written[0]["truth_level"], "L4_COUSIN_HYPOTHESIS")
        self.assertEqual(written[0]["content"], "maybe red")


class TestDerivedFrom(DreamCase):
    """What a pass writes says what it was built from, so `why` can walk
    back from a dream-written claim to the material. Written through
    memory.remember_entry, so it is the same stored field every other
    claim uses; and the level rule is asymmetric on purpose - retirement
    stays L3/L4-only, derivation is open at any level, because a pass
    concluding something out of what the operator said is the most
    legitimate thing it can do.
    """
    def setUp(self):
        super().setUp()
        self.ids = self.raw("2026-10-01", ("kestrel", "the barn is blue"),
                            ("kestrel", "the barn is blue, painted"))
        self.operator = self.raw("2026-10-01",
                                 ("wren", "the operator said the barn is blue",
                                  "L0_OPERATOR"))
        self.dream()

    def raw_entry(self, eid):
        for entry in memory._all_raw(self.home):
            if memory.entry_id(entry) == eid:
                return entry
        return None

    def test_a_merge_names_the_claims_it_consolidated(self):
        record = self.run_op("merge", {"topic": "kestrel",
                                       "fact": "the barn is blue",
                                       "retire": self.ids, "why": "one fact"})
        entry = self.raw_entry(record["created"])
        self.assertEqual(sorted(entry["derived_from"]), sorted(self.ids))
        self.assertEqual(record["derived_from"], self.ids)
        # one storage format: `why` reads it without knowing a pass wrote it
        walked = memory.why(self.home, record["created"])
        self.assertEqual(sorted(w["id"] for w in walked["derived_from"]),
                         sorted(self.ids))

    def test_why_walks_back_from_a_merged_claim_to_what_it_retired(self):
        record = self.run_op("merge", {"topic": "kestrel",
                                       "fact": "the barn is blue",
                                       "retire": self.ids, "why": "one fact"})
        out = memory.why(self.home, record["created"])
        self.assertEqual(out["used_by"], [])
        for source in out["derived_from"]:
            self.assertFalse(source.get("missing"), source)
            self.assertIn(source["content"],
                          ("the barn is blue", "the barn is blue, painted"))

    def test_a_keep_merges_without_writing_and_so_without_deriving(self):
        record = self.run_op("merge", {"topic": "kestrel", "keep": self.ids[1],
                                       "retire": [self.ids[0]],
                                       "why": "one says it in full"})
        self.assertIsNone(record["created"])
        self.assertEqual(record["derived_from"], [])
        # the kept claim is left exactly as it was: a pass does not rewrite
        # the provenance of a claim it did not write
        self.assertNotIn("derived_from", self.raw_entry(self.ids[1]))

    def test_remember_records_the_sources_it_was_given(self):
        record = self.run_op("remember", {"topic": "kestrel",
                                           "fact": "the barn is blue, painted"
                                                   " twice over",
                                           "derived_from": self.ids})
        entry = self.raw_entry(record["created"])
        self.assertEqual(sorted(entry["derived_from"]), sorted(self.ids))
        self.assertEqual(record["derived_from"], self.ids)

    def test_a_claim_may_be_derived_from_what_the_operator_said(self):
        record = self.run_op("remember", {"topic": "wren",
                                           "fact": "so the operator has the"
                                                   " barn colour right",
                                           "derived_from": self.operator})
        entry = self.raw_entry(record["created"])
        self.assertEqual(entry["derived_from"], self.operator)
        source = self.raw_entry(self.operator[0])
        self.assertEqual(source["truth_level"], "L0_OPERATOR")
        # nothing is inherited along the hop: an L0 source does not make
        # the claim an operator statement
        self.assertEqual(entry["truth_level"], "L3_COUSIN_CONCLUSION")
        self.assertEqual(entry["source"], "dream")

    def test_deriving_from_an_operator_claim_is_not_retiring_it(self):
        record = self.run_op("remember", {"topic": "wren",
                                           "fact": "so the operator has the"
                                                   " barn colour right",
                                           "derived_from": self.operator})
        # the source is still live: naming it is not a mark on it
        live = {r["id"] for r in self.live("wren")}
        self.assertIn(self.operator[0], live)
        self.assertIn(record["created"], live)
        source_row = next(r for r in memory.validity(self.home)
                          if r["id"] == self.operator[0])
        self.assertIsNone(source_row.get("retired_by"))
        self.assertIsNone(source_row.get("valid_to"))

    def test_a_source_the_slice_never_showed_is_refused(self):
        stranger = self.raw("2026-10-02", ("kestrel", "written after the slice"))
        with self.assertRaisesRegex(ValueError, "not in this pass's slice"):
            self.run_op("remember", {"topic": "kestrel", "fact": "new",
                                     "derived_from": stranger})

    def test_a_source_written_after_the_pass_began_is_refused(self):
        later = self.raw("2026-10-02", ("kestrel", "written after the pass began"))
        with self.assertRaisesRegex(ValueError, "not in this pass's slice"):
            self.run_op("remember", {"topic": "kestrel", "fact": "new",
                                     "derived_from": later})

    def test_an_already_retired_source_is_refused(self):
        self.run_op("retire", {"entry_id": self.ids[0], "why": "a duplicate"})
        with self.assertRaisesRegex(ValueError, "already retired"):
            self.run_op("remember", {"topic": "kestrel", "fact": "new",
                                     "derived_from": [self.ids[0]]})

    def test_derived_from_must_hold_claim_ids(self):
        # an empty list is no derivation at all, the same reading
        # memory.check_derived gives it; a blank is a mistake worth naming
        record = self.run_op("remember", {"topic": "kestrel",
                                           "fact": "a barn nobody mentioned",
                                           "derived_from": []})
        self.assertNotIn("derived_from", self.raw_entry(record["created"]))
        with self.assertRaisesRegex(ValueError, "must hold claim ids"):
            self.run_op("remember", {"topic": "kestrel", "fact": "new",
                                     "derived_from": [""]})

    def test_remember_without_sources_stands_alone(self):
        record = self.run_op("remember", {"topic": "kestrel",
                                           "fact": "a barn nobody mentioned"})
        entry = self.raw_entry(record["created"])
        self.assertNotIn("derived_from", entry)
        self.assertEqual(record["derived_from"], [])

    def test_a_refused_write_leaves_nothing_behind(self):
        with self.assertRaisesRegex(ValueError, "no claim '000000000000'"):
            self.run_op("remember", {"topic": "kestrel", "fact": "new",
                                     "derived_from": ["0" * 12]})
        self.assertEqual([r for r in memory._all_raw(self.home)
                          if r.get("source") == "dream"], [])

    def test_undo_takes_the_derivation_with_the_entry(self):
        changes = [self.run_op("merge", {"topic": "kestrel",
                                         "fact": "the barn is blue",
                                         "retire": self.ids, "why": "one fact"})]
        created = changes[0]["created"]
        self.assertIn(created, [memory.entry_id(e)
                                for e in memory._all_raw(self.home)])
        dream_memory.undo(self.home, self.pass_id, changes)
        # no orphan: the entry is gone from raw, so nothing points at it
        with self.assertRaises(KeyError):
            memory.why(self.home, created)
        live_ids = {memory.entry_id(e) for e in memory._all_raw(self.home)}
        dangling = [d for e in memory._all_raw(self.home)
                    for d in (e.get("derived_from") or [])
                    if d not in live_ids and not self.raw_entry(d)]
        self.assertEqual(dangling, [])
        # the sources came back live with it
        self.assertEqual(sorted(r["id"] for r in self.live("kestrel")),
                         sorted(self.ids))


class TestSettle(DreamCase):
    def setUp(self):
        super().setUp()
        self.ids = self.raw("2026-10-01", ("kestrel", "the barn is blue"),
                            ("kestrel", "the barn is red"))
        self.dream()

    def test_evidence_must_be_a_quote_from_the_claim_being_settled(self):
        record = self.run_op("settle", {
            "topic": "kestrel", "entry_ids": [self.ids[1]],
            "evidence": "the barn is red", "why": "the paint record says blue"})
        self.assertEqual(record["op"], "settle")
        self.assertEqual(record["evidence"], "the barn is red")
        self.assertEqual([r["id"] for r in self.live("kestrel")], [self.ids[0]])

    def test_no_evidence_is_no_settlement(self):
        for evidence in ("", "   ", None):
            with self.assertRaisesRegex(ValueError, "needs evidence"):
                self.run_op("settle", {"topic": "kestrel",
                                       "entry_ids": [self.ids[1]],
                                       "evidence": evidence, "why": "x"})
        with self.assertRaisesRegex(ValueError, "not in the content"):
            self.run_op("settle", {"topic": "kestrel",
                                   "entry_ids": [self.ids[1]],
                                   "evidence": "a quote from nowhere",
                                   "why": "x"})
        self.assertEqual(len(self.live("kestrel")), 2)

    def test_settling_every_claim_is_a_topic_obsolete_not_a_settle(self):
        with self.assertRaisesRegex(ValueError, "leave the topic with none"):
            self.run_op("settle", {"topic": "kestrel", "entry_ids": self.ids,
                                   "evidence": "the barn is blue", "why": "x"})


class TestRemember(DreamCase):
    def setUp(self):
        super().setUp()
        self.ids = self.raw("2026-10-01", ("kestrel", "the barn is blue"))
        self.dream()

    def test_it_says_whether_it_created_the_topic_or_appended_to_it(self):
        first = self.run_op("remember", {"topic": "wren", "fact": "a new topic"})
        self.assertEqual(first["mode"], "create")
        second = self.run_op("remember", {"topic": "wren", "fact": "and another"})
        self.assertEqual(second["mode"], "append")
        self.assertEqual([r["content"] for r in self.live("wren")],
                         ["a new topic", "and another"])
        self.assertEqual(second["created"], self.live("wren")[1]["id"])

    def test_a_duplicate_is_refused_not_appended(self):
        with self.assertRaisesRegex(ValueError, "already says exactly that"):
            self.run_op("remember", {"topic": "kestrel",
                                     "fact": "the  barn   is blue"})
        self.assertEqual(len(self.live("kestrel")), 1)

    def test_it_writes_at_its_own_level_with_its_own_source(self):
        record = self.run_op("remember", {"topic": "wren", "fact": "a fact",
                                          "level": "hypothesis",
                                          "cite": "the slice"})
        row = [r for r in self.live("wren") if r["id"] == record["created"]][0]
        self.assertEqual((row["source"], row["truth_level"]), ("dream", "L4_COUSIN_HYPOTHESIS"))
        self.assertEqual(row["cite"], "the slice")

    def test_a_topic_is_required(self):
        with self.assertRaisesRegex(ValueError, "topic is required"):
            self.run_op("remember", {"fact": "no topic"})
        with self.assertRaisesRegex(ValueError, "fact is required"):
            self.run_op("remember", {"topic": "wren", "fact": "  "})


# ---- undo --------------------------------------------------------------------

class TestUndo(DreamCase):
    def setUp(self):
        super().setUp()
        self.ids = self.raw("2026-10-01", ("kestrel", "the barn is blue"),
                            ("kestrel", "the barn is red"))

    def test_undo_brings_a_claim_back_and_keeps_the_cursor(self):
        piece = self.dream()
        changes = [self.run_op("retire", {"entry_id": self.ids[1],
                                          "why": "the paint record"})]
        dream_memory.commit(self.home, self.pass_id, piece.through)
        self.assertEqual(len(self.live("kestrel")), 1)
        out = dream_memory.undo(self.home, self.pass_id, changes)
        self.assertEqual(out[0]["reversed"], "retire")
        self.assertEqual(out[0]["removed"], [changes[0]["mark_id"]])
        self.assertEqual([r["id"] for r in self.live("kestrel")], self.ids)
        # the material stays dreamed: the cursor is not rewound
        self.assertEqual(dream_memory.committed(self.home), piece.through)
        self.assertTrue(dream_memory.slice_for(self.home, chars=40000).empty)

    def test_undo_of_a_merge_takes_the_new_claim_and_the_marks(self):
        self.dream()
        changes = [self.run_op("merge", {"topic": "kestrel",
                                         "fact": "the barn is blue",
                                         "retire": self.ids, "why": "one fact"})]
        self.assertEqual(len(self.live("kestrel")), 1)
        dream_memory.undo(self.home, self.pass_id, changes)
        self.assertEqual(sorted(r["id"] for r in self.live("kestrel")),
                         sorted(self.ids))

    def test_undo_is_reversible_and_auditable(self):
        self.dream()
        changes = [self.run_op("retire", {"entry_id": self.ids[0], "why": "dup"})]
        (out,) = dream_memory.undo(self.home, self.pass_id, changes)
        self.assertEqual(len(self.live("kestrel")), 2)
        # a second undo of the same changes has nothing left to do
        with self.assertRaisesRegex(ValueError, "nothing to undo"):
            dream_memory.undo(self.home, self.pass_id, changes)
        from cousin_lib import memory_trash
        # and the reversal is itself reversible: restoring the batch
        # brings the mark back, and the claim is retired again
        self.assertTrue(memory_trash.restore(self.home, out["trash"]))
        self.assertEqual(len(self.live("kestrel")), 1)
        # and the reversal itself is on the journal
        kinds = [r.get("op") for r in dream_memory.journal(self.home, self.pass_id)]
        self.assertIn("undone", kinds)

    def test_nothing_to_undo_is_a_refusal_not_an_empty_list(self):
        self.dream()
        with self.assertRaisesRegex(ValueError, "changed nothing to undo"):
            dream_memory.undo(self.home, self.pass_id, [])

    def test_a_lost_pass_is_read_from_the_journal(self):
        self.dream()
        self.run_op("retire", {"entry_id": self.ids[0], "why": "dup"})
        # the harness's pass log never got an end line: the journal is the
        # only record of what the dead pass changed
        out = dream_memory.undo(self.home, self.pass_id,
                                dream_memory.journal(self.home, self.pass_id))
        self.assertEqual(len(self.live("kestrel")), 2)


# ---- the prompt --------------------------------------------------------------

class TestPrompt(DreamCase):
    def test_it_carries_the_doctrine_the_tensions_and_the_slice(self):
        ids = self.raw("2026-10-01", ("kestrel", "the barn is blue"),
                       ("kestrel", "the barn is red"))
        piece = dream_memory.slice_for(self.home, chars=40000)
        text = dream_memory.prompt(piece)
        self.assertIn("Prefer nothing", text)
        self.assertIn("Prefer modify over create", text)
        self.assertIn("L0, L1 or L2", text)
        self.assertIn("reported, never resolved by", text)
        self.assertIn("Open disagreements", text)
        self.assertIn("kestrel", text)
        for eid in ids:
            self.assertIn(eid, text)
        self.assertIn("Your slice:", text)

    def test_a_clean_slice_still_gets_the_doctrine(self):
        text = dream_memory.prompt(dream_memory.slice_for(self.home, chars=40000))
        self.assertIn("Prefer nothing", text)
        self.assertIn("(empty)", text)
        self.assertNotIn("Open disagreements", text)


# ---- the surface -------------------------------------------------------------

class TestTheSurface(DreamCase):
    def test_every_operation_is_named_described_and_schedulable(self):
        names = [op["name"] for op in dream_memory.OPERATIONS]
        self.assertEqual(sorted(names), ["merge", "remember", "retire", "settle"])
        for op in dream_memory.OPERATIONS:
            self.assertTrue(op["description"].strip())
            self.assertEqual(op["inputSchema"]["type"], "object")
            for field in op["inputSchema"]["required"]:
                self.assertIn(field, op["inputSchema"]["properties"])

    def test_the_ledger_is_where_a_transplant_would_carry_it(self):
        self.raw("2026-10-01", ("kestrel", "one"))
        self.dream()
        self.assertTrue(dream_memory.ledger_path(self.home)
                        .as_posix().endswith("memory/.dream-ledger.json"))
        self.assertTrue(dream_memory.journal_path(self.home, self.pass_id)
                        .as_posix().endswith("data/dreams/journal/%s.jsonl"
                                             % self.pass_id))
        # the journal is out of data/dreams/ itself, which the harness
        # globs for pass records
        self.assertEqual(dream_memory.journal_path(self.home,
                                                   self.pass_id).parent.name,
                         "journal")

    def test_it_writes_only_memory_and_the_pass_log(self):
        self.raw("2026-10-01", ("kestrel", "one"))
        piece = self.dream()
        self.run_op("remember", {"topic": "kestrel", "fact": "a second claim"})
        written = sorted(p.relative_to(self.home).as_posix()
                         for p in self.home.rglob("*")
                         if p.is_file() and not p.parts[:2] == ("memory", "raw"))
        for path in written:
            if path == "data/.memory-write.lock":
                continue          # the home's memory lock, not this module's
            self.assertTrue(path.startswith("memory/")
                            or path.startswith("data/dreams/"), path)
        self.assertIn("memory/distilled/decisions.md", written)

    def test_an_unreadable_ledger_dreams_from_the_start_rather_than_guessing(self):
        self.raw("2026-10-01", ("kestrel", "one"))
        self.dream()
        dream_memory.commit(self.home, self.pass_id,
                            {"month": "2026-10-01", "lines": 1})
        dream_memory.ledger_path(self.home).write_text("{not json")
        piece = dream_memory.slice_for(self.home, chars=40000)
        self.assertFalse(piece.empty)         # back to the start, not a guess
        self.assertEqual(piece.through["lines"], 1)


if __name__ == "__main__":
    unittest.main()
