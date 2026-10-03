"""The review gate for bulk memory writes: more than N new authored raw
entries since the gate last looked are held, out of every memory view,
until a reviewer (a second model on the runner lane, the operator with
`cousin-memory review`) keeps or drops each. N comes from cousin.toml
`[memory] review_batch` (default 3). raw stays append-only: a hold and
a keep are raw records of their own (topic `framework:review-gate`),
and a drop is one line that is both its release and an entry-level
obsolete mark (valid time)."""
import contextlib
import io
import json
import os
import pathlib
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from cousin_lib import accounts, boot, distill, memory, raw_fold, review_gate
from tests._hermetic import HermeticCase


def _home(case, extra=""):
    tmp = tempfile.TemporaryDirectory(); case.addCleanup(tmp.cleanup)
    home = pathlib.Path(tmp.name) / "cousins" / "wren"
    (home / "data").mkdir(parents=True)
    (home / "cousin.toml").write_text('[cousin]\nslug = "wren"\nname = "Wren"\n' + extra)
    p = mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": "/nonexistent/framework-root"})
    p.start(); case.addCleanup(p.stop)
    review_gate.begin(home, now=time.time() - 1)
    return home


def _write(home, n, topic="ledger fact %d"):
    for i in range(n):
        memory.remember(home, topic % i, "The quokka ledger fact number %d." % i)


def _distilled(home):
    distill.distill(home)
    return "".join(p.read_text() for p in memory.distilled_dir(home).glob("*.md"))


def _keep_all(entries):
    return {e["id"]: "keep" for e in entries}


class TestConfig(HermeticCase):
    def test_n_defaults_to_3_and_comes_from_cousin_toml(self):
        self.assertEqual(review_gate.batch_limit(_home(self)), 3)
        self.assertEqual(review_gate.batch_limit(_home(self, '[memory]\nreview_batch = 5\n')), 5)


class TestGate(HermeticCase):
    def test_n_entries_pass_straight_through(self):
        home = _home(self)
        _write(home, 3)
        out = review_gate.gate(home, reviewer=_keep_all)
        self.assertEqual(out["held"], [])
        self.assertEqual(review_gate.pending(home), [])
        self.assertIn("ledger fact 2", _distilled(home))

    def test_n_plus_1_are_held_and_reach_distilled_only_once_reviewed(self):
        home = _home(self)
        _write(home, 4)
        seen = []

        def reviewer(entries):
            seen.extend(entries)
            self.assertNotIn("ledger fact", _distilled(home))    # while held: out of the views
            return {e["id"]: ("drop" if e["topic"] == "ledger fact 3" else "keep")
                    for e in entries}
        out = review_gate.gate(home, reviewer=reviewer)
        self.assertEqual((len(out["held"]), len(seen)), (4, 4))
        self.assertEqual(sorted(out["verdicts"].values()), ["drop", "keep", "keep", "keep"])
        self.assertEqual(review_gate.pending(home), [])
        text = _distilled(home)
        self.assertIn("ledger fact 0", text)
        self.assertNotIn("ledger fact 3", text)
        [dropped] = [r for r in memory.validity(home) if r["topic"] == "ledger fact 3"]
        self.assertIsNotNone(dropped["valid_to"])

    def test_a_reviewer_that_fails_leaves_them_held(self):
        home = _home(self)
        _write(home, 4)

        def broken(entries):
            raise RuntimeError("the reviewer is down")
        out = review_gate.gate(home, reviewer=broken)
        self.assertIn("RuntimeError: the reviewer is down", out["error"])
        self.assertEqual(len(review_gate.pending(home)), 4)
        self.assertNotIn("ledger fact", _distilled(home))

    def test_n_comes_from_configuration(self):
        home = _home(self, '[memory]\nreview_batch = 5\n')
        _write(home, 5)
        self.assertEqual(review_gate.gate(home, reviewer=_keep_all)["held"], [])

    def test_the_framework_log_and_mined_episodes_are_not_counted(self):
        home = _home(self)
        _write(home, 2)
        for i in range(3):
            memory.record_event(home, "framework", "framework:flip", "flipped %d" % i, "flip")
            memory._append_raw(home, {"topic": "episode:abcd1234", "content": "mined %d" % i,
                                      "truth_level": "L3_COUSIN_CONCLUSION",
                                      "source": "turn-extract"})
        self.assertEqual(review_gate.gate(home, reviewer=_keep_all)["held"], [])


class TestTheCursor(HermeticCase):
    """The gate counts per home from a cursor on disk."""

    def test_writes_nobody_gated_are_held_by_the_next_gate(self):
        """A runner that died after the writes, or a turn that errored:
        the next gate, whoever runs it, finds them."""
        home = _home(self)
        _write(home, 4)                                         # ...and no gate ran
        self.assertEqual(len(review_gate.hold_new(home)), 4)

    def test_a_second_gate_holds_nothing_the_first_counted(self):
        home = _home(self)
        _write(home, 4)
        self.assertEqual(len(review_gate.hold_new(home)), 4)
        self.assertEqual(review_gate.hold_new(home), [])
        _write(home, 2, topic="pantry %d")
        self.assertEqual(review_gate.hold_new(home), [])        # 2 new, not 6

    def test_a_home_never_gated_opens_its_cursor_and_holds_nothing_old(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        home = pathlib.Path(tmp.name) / "cousins" / "wren"
        (home / "data").mkdir(parents=True)
        _write(home, 5)
        self.assertEqual(review_gate.hold_new(home), [])
        self.assertTrue(pathlib.Path(home, *review_gate.STATE).exists())

    def test_one_id_that_is_no_longer_held_does_not_stop_the_rest(self):
        home = _home(self)
        _write(home, 4)
        rows = review_gate.hold_new(home)
        review_gate.release(home, rows[0]["id"], "keep")         # another reviewer got there first
        done, errors = review_gate.settle(home, rows, _keep_all(rows))
        self.assertEqual((len(done), list(errors)), (3, [rows[0]["id"]]))
        self.assertEqual(review_gate.pending(home), [])

    def test_a_settle_reads_what_is_held_once_under_one_lock(self):
        # 20 verdicts once read the whole history 20 times (a console "mark
        # all" on a large home took seconds per id).
        home = _home(self)
        _write(home, 6)
        rows = review_gate.hold_new(home)
        real = review_gate.pending
        with mock.patch.object(review_gate, "pending", side_effect=real) as seen:
            done, errors = review_gate.settle(home, rows, _keep_all(rows))
        self.assertEqual((len(done), errors), (6, {}))
        self.assertEqual(seen.call_count, 1)
        self.assertEqual(review_gate.pending(home), [])

    def test_the_gate_reads_only_new_files_never_the_archives(self):
        """A gate after every result must not scan the history."""
        home = _home(self)
        _write(home, 4)
        with mock.patch.object(memory, "_all_raw", side_effect=AssertionError("read history")):
            self.assertEqual(len(review_gate.hold_new(home)), 4)


class TestBegin(HermeticCase):
    def test_what_was_written_just_before_the_cursor_opened_is_not_counted(self):
        """The clean stop's handoff memories, written seconds
        before the runner starts, are not the gate's."""
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        home = pathlib.Path(tmp.name) / "cousins" / "wren"
        (home / "data").mkdir(parents=True)
        _write(home, 5)                                         # the handoff
        review_gate.begin(home)
        self.assertEqual(review_gate.hold_new(home), [])
        _write(home, 4, topic="pantry %d")
        self.assertEqual(len(review_gate.hold_new(home)), 4)

    def test_a_reset_opens_the_cursor_again(self):
        """`cousin-migrate apply` resets it; what the cousin
        wrote on the tmux lane since is not held."""
        home = _home(self)
        review_gate.begin(home, now=time.time() - 10 * 86400, reset=True)   # a stale cursor
        _write(home, 5)
        review_gate.begin(home, reset=True)
        self.assertEqual(review_gate.hold_new(home), [])


class TestTheModelMayOnlyKeepAnOperatorFact(HermeticCase):
    """A drop is an entry-level mark with
    no undo, so the reviewing model may keep an operator-level entry but
    never drop one; that drop stays the operator's (`review --drop`)."""

    def test_a_model_drop_of_an_operator_fact_leaves_it_held(self):
        home = _home(self)
        memory.remember(home, "bins", "The bins go out on Thursday.", level="operator",
                        cite="chat 2026-09-24")
        _write(home, 3)
        rows = review_gate.hold_new(home)
        verdicts = {r["id"]: "drop" for r in rows}
        done, errors = review_gate.settle(home, rows, verdicts, model=True)
        pending = review_gate.pending(home)
        self.assertEqual([r["topic"] for r in pending], ["bins"])
        self.assertEqual(len(done), 3)
        self.assertIn("operator", errors[pending[0]["id"]])

    def test_a_model_may_keep_one_and_the_operator_may_drop_one(self):
        home = _home(self)
        memory.remember(home, "bins", "The bins go out on Thursday.", level="operator",
                        cite="chat 2026-09-24")
        memory.remember(home, "keys", "Toki keeps the keys.", level="operator",
                        cite="chat 2026-09-24")
        _write(home, 2)
        rows = review_gate.hold_new(home)
        by_topic = {r["topic"]: r["id"] for r in rows}
        done, _ = review_gate.settle(home, rows, {by_topic["bins"]: "keep"}, model=True)
        self.assertEqual(done, {by_topic["bins"]: "keep"})
        review_gate.release(home, by_topic["keys"], "drop", why="moved")   # the operator's drop
        self.assertNotIn("keys", [r["topic"] for r in review_gate.pending(home)])

    def test_the_gate_s_own_reviewer_is_a_model(self):
        home = _home(self)
        memory.remember(home, "bins", "The bins go out on Thursday.", level="operator",
                        cite="chat 2026-09-24")
        _write(home, 3)
        out = review_gate.gate(home, reviewer=lambda rows: {r["id"]: "drop" for r in rows})
        self.assertEqual([r["topic"] for r in review_gate.pending(home)], ["bins"])
        self.assertIn("operator", out["error"])


class TestTheLockIsTheMemoryWriteLock(HermeticCase):
    """The gate's read-decide-write takes memory_lock.write_lock
    (reentrant per thread), so it is atomic against every memory writer,
    not only another gate, and it opens no file of its own (a read-write
    0600 lock file would shut out a second uid)."""

    def test_a_hold_waits_for_a_memory_writer(self):
        """Under the batch (no hold is written), so only the gate's own lock
        can make it wait: it must be the memory write lock."""
        import threading
        from cousin_lib import memory_lock
        home = _home(self)
        _write(home, 2)
        inside, release, held = threading.Event(), threading.Event(), []

        def writer():
            with memory_lock.write_lock(home):
                inside.set()
                release.wait(10)
        t = threading.Thread(target=writer); t.start()
        self.assertTrue(inside.wait(5))
        g = threading.Thread(target=lambda: held.extend(review_gate.hold_new(home))); g.start()
        g.join(0.5)
        self.assertTrue(g.is_alive(), "the hold did not wait for the memory write lock")
        release.set(); t.join(5); g.join(5)
        self.assertFalse(g.is_alive())
        self.assertEqual(held, [])
        self.assertFalse(pathlib.Path(home, "data", ".review-gate.lock").exists())


class TestADropIsOneLine(HermeticCase):
    def test_the_release_and_the_mark_are_the_same_line(self):
        """No crash can leave a drop retired but still held."""
        home = _home(self)
        _write(home, 4)
        rows = review_gate.hold_new(home)
        review_gate.release(home, rows[0]["id"], "drop", why="a duplicate")
        lines = [json.loads(l) for p in memory.raw_dir(home).glob("*.jsonl")
                 for l in p.read_text().splitlines()]
        [line] = [l for l in lines if l.get("released") == rows[0]["id"]]
        self.assertEqual((line["truth_level"], line["entry"], line["verdict"]),
                         (memory.OBSOLETE_LEVEL, rows[0]["id"], "drop"))


class TestADropSaysWhy(HermeticCase):
    """The reviewer's reason goes into the drop's mark, so the entry's
    owner can tell a duplicate from chatter from a misread."""

    def _drop_line(self, home, entry_id):
        lines = [json.loads(l) for p in memory.raw_dir(home).glob("*.jsonl")
                 for l in p.read_text().splitlines()]
        [line] = [l for l in lines if l.get("released") == entry_id]
        return line

    def test_a_drop_s_mark_carries_the_reviewer_s_reason(self):
        home = _home(self)
        _write(home, 4)
        reply = {}

        def reviewer(rows):
            text = json.dumps({r["id"]: ({"verdict": "drop", "why": "repeats ledger fact 1"}
                                         if r["topic"] == "ledger fact 0" else "keep")
                               for r in rows})
            reply.update(review_gate.parse_verdicts(text, [r["id"] for r in rows]))
            return reply
        out = review_gate.gate(home, reviewer=reviewer)
        self.assertEqual(sorted(out["verdicts"].values()), ["drop", "keep", "keep", "keep"])
        [dropped] = [k for k, v in out["verdicts"].items() if v == "drop"]
        line = self._drop_line(home, dropped)
        self.assertEqual(line["why"], "the review gate's reviewer: repeats ledger fact 1")
        self.assertEqual(line["content"], "obsolete: the review gate's reviewer: repeats ledger fact 1")
        self.assertEqual(line["truth_level"], memory.OBSOLETE_LEVEL)

    def test_no_reason_or_a_garbage_one_keeps_today_s_text(self):
        home = _home(self)
        _write(home, 4)
        rows = review_gate.hold_new(home)
        verdicts = {rows[0]["id"]: "drop",
                    rows[1]["id"]: {"verdict": "drop"},
                    rows[2]["id"]: {"verdict": "drop", "why": {"nested": "x"}},
                    rows[3]["id"]: {"verdict": "drop", "why": " \n\t "}}
        done, errors = review_gate.settle(home, rows, verdicts,
                                          why="the review gate's reviewer", model=True)
        self.assertEqual((len(done), errors), (4, {}))
        for r in rows:
            self.assertEqual(self._drop_line(home, r["id"])["why"], "the review gate's reviewer")

    def test_a_long_reason_is_capped_in_the_mark(self):
        home = _home(self)
        _write(home, 4)
        rows = review_gate.hold_new(home)
        done, _ = review_gate.settle(home, rows[:1],
                                     {rows[0]["id"]: {"verdict": "drop", "why": "x\ny " * 500}},
                                     why="the review gate's reviewer", model=True)
        why = self._drop_line(home, rows[0]["id"])["why"]
        self.assertEqual(done, {rows[0]["id"]: "drop"})
        self.assertTrue(why.startswith("the review gate's reviewer: x y x y"))
        self.assertLessEqual(len(why), len("the review gate's reviewer: ") + review_gate.REASON_CHARS)
        self.assertNotIn("\n", why)

    def test_a_dict_with_no_valid_verdict_stays_held(self):
        home = _home(self)
        _write(home, 4)
        rows = review_gate.hold_new(home)
        done, errors = review_gate.settle(home, rows, {
            rows[0]["id"]: {"why": "a duplicate"}, rows[1]["id"]: {"verdict": "DROP"}})
        self.assertEqual((done, errors), ({}, {}))
        self.assertEqual(len(review_gate.pending(home)), 4)


def _old(minutes=0):
    return datetime.now(timezone.utc) - timedelta(days=60) + timedelta(minutes=minutes)


class TestTheFoldKeepsAHold(HermeticCase):
    """The monthly fold must not let a held entry, or the gate's
    records, into the views."""

    def test_held_entries_stay_out_after_the_fold(self):
        """Entries, their holds and a released record all 60 days old, so
        the fold takes every one of them into the archive."""
        home = _home(self)
        rdir = memory.raw_dir(home); rdir.mkdir(parents=True, exist_ok=True)
        claims = [{"timestamp": _old(i).isoformat(), "topic": "pantry %d" % i,
                   "content": "Shelf %d holds the quokka tins." % i,
                   "truth_level": "L3_COUSIN_CONCLUSION"} for i in range(4)]
        with open(rdir / ("%s.jsonl" % _old().strftime("%Y-%m-%d")), "a") as fh:
            for i, c in enumerate(claims):
                fh.write(json.dumps(c) + "\n")
                fh.write(json.dumps({"timestamp": _old(10 + i).isoformat(),
                                     "topic": review_gate.TOPIC, "content": "held",
                                     "truth_level": "L1_FRAMEWORK", "source": review_gate.SOURCE,
                                     "held": memory.entry_id(c)}) + "\n")
        self.assertNotIn("quokka tins", _distilled(home))
        raw_fold.fold_raw(home)
        self.assertEqual(list(memory.raw_dir(home).glob("????-??-??.jsonl")), [])
        text = _distilled(home)
        self.assertNotIn("quokka tins", text)
        self.assertNotIn("review-gate", text)
        self.assertEqual(len(review_gate.pending(home)), 4)
        digests = "".join(p.read_text() for p in memory.raw_dir(home).glob("*-digest.jsonl"))
        self.assertNotIn("quokka tins", digests)
        self.assertNotIn(review_gate.TOPIC, digests)
        for row in review_gate.pending(home):                # kept later: back from the archive
            review_gate.release(home, row["id"], "keep")
        self.assertIn("Shelf 3 holds the quokka tins", _distilled(home))


class TestTheBootPacket(HermeticCase):
    """What the gate holds or drops never reaches a new session."""

    def test_held_dropped_and_gate_lines_are_not_recent_raw_memory(self):
        home = _home(self)
        _write(home, 4)
        rows = review_gate.hold_new(home)
        text = boot._memories(home, 20000)
        self.assertNotIn("quokka ledger", text)
        self.assertNotIn("review-gate", text)
        review_gate.release(home, rows[0]["id"], "keep")
        review_gate.release(home, rows[1]["id"], "drop")
        text = boot._memories(home, 20000)
        self.assertIn("fact number 0", text)
        self.assertNotIn("fact number 1", text)
        self.assertNotIn("review-gate", text)
        self.assertNotIn("obsolete:", text)

    def test_the_packet_says_how_many_are_held(self):
        """Held entries are not silent at boot."""
        home = _home(self)
        _write(home, 4)
        review_gate.hold_new(home)
        self.assertIn("4 memory entries are held by the review gate", boot._memories(home, 20000))

    def test_a_held_entry_that_is_gone_is_not_counted(self):
        """The count is of held entries that exist; one
        removed from raw (the trash) leaves no ghost."""
        home = _home(self)
        _write(home, 4)
        rows = review_gate.hold_new(home)
        for path in memory.raw_dir(home).glob("????-??-??.jsonl"):
            lines = [l for l in path.read_text().splitlines()
                     if json.loads(l).get("topic") != rows[0]["topic"]]
            path.write_text("\n".join(lines) + "\n")
        self.assertIn("3 memory entries are held by the review gate", boot._memories(home, 20000))

    def test_the_runner_digest_leaves_them_out_too(self):
        from cousin_lib.runner import prompt
        home = _home(self)
        _write(home, 4)
        review_gate.hold_new(home)
        digest = prompt.state_digest(home, root=home.parent.parent, slug="wren")
        self.assertNotIn("quokka ledger", str(digest))


class TestCli(HermeticCase):
    def _main(self, home, *argv, ancestry=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(accounts, "_inside_cousin_ancestry", return_value=ancestry):
            rc = memory.memory_main(["--home", str(home), *argv])
        return rc, out.getvalue(), err.getvalue()

    def test_the_operator_reviews_what_the_gate_held(self):
        home = _home(self)
        _write(home, 4)
        review_gate.gate(home, reviewer=None)                   # no reviewer: held for a person
        rc, out, _ = self._main(home, "review")
        self.assertEqual(rc, 0)
        ids = [r["id"] for r in review_gate.pending(home)]
        self.assertTrue(all(i in out for i in ids))
        rc, out, err = self._main(home, "review", "--keep", ids[0], ids[1], ids[2])
        self.assertEqual(rc, 0, err)
        self.assertIn("by operator:", out)
        rc, out, err = self._main(home, "review", "--drop", ids[3], "--why", "a duplicate")
        self.assertEqual(rc, 0, err)
        self.assertEqual(review_gate.pending(home), [])
        rc, out, _ = self._main(home, "review")
        self.assertEqual(out.strip(), "nothing held for review")

    def test_the_cousin_cannot_release_its_own_writes(self):
        """A verdict from inside a cousin's process tree."""
        home = _home(self)
        _write(home, 4)
        review_gate.hold_new(home)
        ids = [r["id"] for r in review_gate.pending(home)]
        rc, out, err = self._main(home, "review", "--keep", *ids, ancestry=4242)
        self.assertEqual(rc, 2)
        self.assertIn("the operator's", err)
        self.assertEqual(len(review_gate.pending(home)), 4)
        rc, out, _ = self._main(home, "review", ancestry=4242)    # listing is fine
        self.assertEqual(rc, 0)

    def test_an_unknown_id_is_refused(self):
        home = _home(self)
        rc, _out, err = self._main(home, "review", "--keep", "000000000000")
        self.assertEqual(rc, 2)
        self.assertIn("not held", err)


if __name__ == "__main__":
    unittest.main()
