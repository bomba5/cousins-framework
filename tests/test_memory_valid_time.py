"""Claims carry valid time: every raw entry has
a `valid_from` (when it was written, or its own field) and a `valid_to`
(when an obsolete mark covering it was written, or its own field). raw
stays append-only: validity is derived, never written back. A topic-level
mark covers the topic's earlier entries; an entry-level mark (`--entry
<id>`) covers that entry alone. Hermetic: a temp home."""
import contextlib
import io
import json
import os
import pathlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from cousin_lib import distill, memory, raw_fold
from tests._hermetic import HermeticCase


def _home(case):
    tmp = tempfile.TemporaryDirectory(); case.addCleanup(tmp.cleanup)
    home = pathlib.Path(tmp.name) / "cousins" / "wren"
    (home / "data").mkdir(parents=True)
    (home / "cousin.toml").write_text('[cousin]\nslug = "wren"\nname = "Wren"\n')
    p = mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": "/nonexistent/framework-root"})
    p.start(); case.addCleanup(p.stop)
    return home


def _write(home, day, entries):
    """Entries with explicit stamps into memory/raw/<day>.jsonl."""
    rdir = memory.raw_dir(home)
    rdir.mkdir(parents=True, exist_ok=True)
    with open(rdir / ("%s.jsonl" % day), "a") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")


class TestValidTime(HermeticCase):
    def test_every_entry_is_valid_from_when_it_was_written_and_open_ended(self):
        home = _home(self)
        memory.remember(home, "spare keys", "Toki keeps the spare keys in the blue tin.")
        [row] = memory.validity(home)
        self.assertEqual(row["valid_from"], row["timestamp"])
        self.assertIsNone(row["valid_to"])
        self.assertEqual(len(row["id"]), 12)
        self.assertEqual([r["id"] for r in memory.live_entries(home)], [row["id"]])

    def test_obsolete_sets_valid_to_and_the_entry_leaves_the_live_set(self):
        home = _home(self)
        memory.remember(home, "spare keys", "Toki keeps the spare keys in the blue tin.")
        mark = memory.mark_obsolete(home, "spare keys", "the tin moved to the shed")
        [row] = [r for r in memory.validity(home) if r["topic"] == "spare keys"
                 and r.get("truth_level") != memory.OBSOLETE_LEVEL]
        self.assertEqual(row["valid_to"], mark["timestamp"])
        self.assertEqual(memory.live_entries(home), [])

    def test_a_later_entry_on_a_retired_topic_is_live(self):
        home = _home(self)
        _write(home, "2026-01-01", [
            {"timestamp": "2026-01-01T10:00:00+00:00", "topic": "keys", "content": "blue tin"}])
        memory.mark_obsolete(home, "keys", "moved")
        _write(home, "2099-01-01", [
            {"timestamp": "2099-01-01T10:00:00+00:00", "topic": "keys", "content": "the shed"}])
        self.assertEqual([r["content"] for r in memory.live_entries(
            home, at="2099-06-01T00:00:00+00:00")], ["the shed"])

    def test_an_entry_level_mark_retires_that_entry_alone(self):
        home = _home(self)
        _write(home, "2026-01-01", [
            {"timestamp": "2026-01-01T10:00:00+00:00", "topic": "keys", "content": "blue tin"},
            {"timestamp": "2026-01-01T11:00:00+00:00", "topic": "keys", "content": "the shed"}])
        first = memory.validity(home)[0]
        mark = memory.mark_obsolete(home, "keys", "the tin is gone", entry=first["id"])
        self.assertEqual(mark["entry"], first["id"])
        self.assertEqual([r["content"] for r in memory.live_entries(home)], ["the shed"])
        with self.assertRaisesRegex(memory.ObsoleteRefused, "no entry"):
            memory.mark_obsolete(home, "keys", "x", entry="000000000000")

    def test_explicit_fields_win(self):
        home = _home(self)
        _write(home, "2030-01-01", [
            {"timestamp": "2030-01-01T10:00:00+00:00", "topic": "lease", "content": "until june",
             "valid_from": "2030-01-01T00:00:00+00:00", "valid_to": "2030-06-30T00:00:00+00:00"}])
        [row] = memory.validity(home)
        self.assertEqual((row["valid_from"], row["valid_to"]),
                         ("2030-01-01T00:00:00+00:00", "2030-06-30T00:00:00+00:00"))
        self.assertEqual(memory.live_entries(home, at="2030-03-01T00:00:00+00:00")[0]["topic"], "lease")
        self.assertEqual(memory.live_entries(home, at="2030-07-01T00:00:00+00:00"), [])

    def test_the_id_survives_the_monthly_fold(self):
        home = _home(self)
        old = (datetime.now(timezone.utc) - timedelta(days=90))
        day = old.strftime("%Y-%m-%d")
        _write(home, day, [{"timestamp": old.isoformat(), "topic": "keys", "content": "blue tin"}])
        before = memory.validity(home)[0]["id"]
        raw_fold.fold_raw(home)
        self.assertFalse((memory.raw_dir(home) / ("%s.jsonl" % day)).exists())
        ids = [r["id"] for r in memory.validity(home)]
        self.assertEqual(ids, [before])                 # archived once, digests left out


class TestDistillHonoursAnEntryLevelMark(HermeticCase):
    def test_the_topic_stays_and_the_retired_entry_is_not_its_line(self):
        home = _home(self)
        _write(home, "2026-01-01", [
            {"timestamp": "2026-01-01T10:00:00+00:00", "topic": "keys", "content": "blue tin",
             "truth_level": "L3_COUSIN_CONCLUSION"},
            {"timestamp": "2026-01-01T11:00:00+00:00", "topic": "keys", "content": "the shed",
             "truth_level": "L3_COUSIN_CONCLUSION"}])
        second = memory.validity(home)[1]
        memory.mark_obsolete(home, "keys", "the shed was wrong", entry=second["id"])
        distill.distill(home)
        text = "".join(p.read_text() for p in memory.distilled_dir(home).glob("*.md"))
        self.assertIn("blue tin", text)
        self.assertNotIn("the shed", text)


def _old(minutes):
    """A stamp 60 days back (a month the monthly fold takes), `minutes` apart."""
    return (datetime.now(timezone.utc) - timedelta(days=60) + timedelta(minutes=minutes))


def _write_old(home, entries):
    """(minutes, entry) pairs into the day file of 60 days ago."""
    day = _old(0).strftime("%Y-%m-%d")
    _write(home, day, [dict(e, timestamp=_old(m).isoformat()) for m, e in entries])


def _views(home):
    distill.distill(home)
    return "".join(p.read_text() for p in memory.distilled_dir(home).glob("*.md"))


class TestTheFoldKeepsEntryLevelMarks(HermeticCase):
    """The monthly fold keeps one digest line per topic with its
    newest line's text and level only, so the views must not trust a
    digest for a topic that has retired entries."""

    def test_a_mark_that_was_its_months_newest_line_does_not_retire_the_topic(self):
        home = _home(self)
        _write_old(home, [(0, {"topic": "keys", "content": "blue tin"}),
                          (1, {"topic": "keys", "content": "the shed"})])
        blue = memory.validity(home)[0]["id"]
        _write_old(home, [(2, {"topic": "keys", "content": "obsolete: gone",
                               "truth_level": "L5_OBSOLETE", "entry": blue})])
        self.assertIn("the shed", _views(home))
        raw_fold.fold_raw(home)
        text = _views(home)
        self.assertIn("the shed", text)
        self.assertNotIn("blue tin", text)

    def test_a_claim_retired_after_the_fold_leaves_the_topic_its_other_claim(self):
        home = _home(self)
        _write_old(home, [(0, {"topic": "keys", "content": "blue tin"}),
                          (1, {"topic": "keys", "content": "the shed"})])
        raw_fold.fold_raw(home)
        shed = memory.validity(home)[1]["id"]
        memory.mark_obsolete(home, "keys", "the shed was wrong", entry=shed)
        text = _views(home)
        self.assertIn("blue tin", text)
        self.assertNotIn("the shed", text)

    def test_the_digest_never_carries_a_retired_claim_or_a_mark(self):
        home = _home(self)
        _write_old(home, [(0, {"topic": "keys", "content": "blue tin"}),
                          (1, {"topic": "keys", "content": "the shed"})])
        shed = memory.validity(home)[1]["id"]
        memory.mark_obsolete(home, "keys", "the shed was wrong", entry=shed)
        raw_fold.fold_raw(home)
        digests = "".join(p.read_text() for p in memory.raw_dir(home).glob("*-digest.jsonl"))
        self.assertIn("blue tin", digests)
        self.assertNotIn("the shed", digests)


class TestOnlyAMarkIsBookkeeping(HermeticCase):
    def test_a_claim_that_happens_to_carry_an_entry_field_is_distilled(self):
        """Only an L5 line with `entry` is an entry-level mark."""
        home = _home(self)
        _write(home, "2026-01-01", [
            {"timestamp": "2026-01-01T10:00:00+00:00", "topic": "ledger", "entry": "row 4",
             "content": "the ledger's row 4 is the pantry", "truth_level": "L3_COUSIN_CONCLUSION"}])
        self.assertIn("row 4 is the pantry", _views(home))


class TestCli(HermeticCase):
    def _main(self, home, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = memory.memory_main(["--home", str(home), *argv])
        return rc, out.getvalue(), err.getvalue()

    def test_history_lists_the_ids_and_obsolete_takes_one(self):
        home = _home(self)
        _write(home, "2026-01-01", [
            {"timestamp": "2026-01-01T10:00:00+00:00", "topic": "keys", "content": "blue tin"},
            {"timestamp": "2026-01-01T11:00:00+00:00", "topic": "keys", "content": "the shed"}])
        rc, out, _ = self._main(home, "history", "keys")
        self.assertEqual(rc, 0)
        first = memory.validity(home)[0]["id"]
        self.assertIn(first, out)
        self.assertIn("live", out)
        rc, out, err = self._main(home, "obsolete", "keys", "--why", "gone", "--entry", first)
        self.assertEqual(rc, 0, err)
        rc, out, _ = self._main(home, "history", "keys")
        self.assertRegex(out, r"%s .*valid to" % first)


if __name__ == "__main__":
    unittest.main()


class TestDeclaredScopeAndEnd(HermeticCase):
    """#246: a fact declares, when it is written, what it holds for
    (`scope`) and until when (`valid_until`, stored as `valid_to`). Past
    its end it leaves the views like a retired claim; recall labels it."""

    NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)

    def test_a_date_holds_through_that_day(self):
        self.assertEqual(memory.parse_valid_until("2026-10-31", now=self.NOW),
                         "2026-11-01T00:00:00+00:00")

    def test_a_time_with_or_without_a_zone(self):
        self.assertEqual(memory.parse_valid_until("2026-10-31T18:00Z", now=self.NOW),
                         "2026-10-31T18:00:00+00:00")
        self.assertEqual(memory.parse_valid_until("2026-10-31T18:00", now=self.NOW),
                         "2026-10-31T18:00:00+00:00")
        self.assertEqual(memory.parse_valid_until("2026-10-31T20:00+02:00", now=self.NOW),
                         "2026-10-31T18:00:00+00:00")

    def test_past_or_garbage_is_refused(self):
        for bad in ("2026-10-01", "yesterday", "", "31/10/2026"):
            with self.assertRaises(ValueError, msg=bad):
                memory.parse_valid_until(bad, now=self.NOW)

    def test_the_labels(self):
        later = {"valid_to": "2026-11-01T00:00:00+00:00", "scope": "board rev A"}
        self.assertEqual(memory.qualifiers(later, now=self.NOW), "scope: board rev A; through 2026-10-31")
        self.assertEqual(memory.qualifiers({"valid_to": "2026-10-31T18:00:00+00:00"}, now=self.NOW),
                         "until 2026-10-31 18:00 UTC")
        past = self.NOW + timedelta(days=60)
        self.assertEqual(memory.qualifiers(later, now=past), "scope: board rev A; expired after 2026-10-31")
        self.assertEqual(memory.qualifiers({}, now=self.NOW), "")

    def test_scope_is_one_line_and_bounded(self):
        self.assertEqual(memory.check_scope("  board\n rev A "), "board rev A")
        self.assertIsNone(memory.check_scope("   "))
        with self.assertRaises(ValueError):
            memory.check_scope("x" * (memory.SCOPE_CHARS + 1))

    def test_remember_stores_both_and_says_so(self):
        home = _home(self)
        end = (datetime.now(timezone.utc) + timedelta(days=3)).date().isoformat()
        line = memory.remember(home, "spi base", "SPI1 is at 0x40013000.",
                               scope="board rev A", valid_until=end)
        self.assertIn("scope: board rev A", line)
        self.assertIn("through %s" % end, line)
        [row] = memory.validity(home)
        self.assertEqual(row["scope"], "board rev A")
        self.assertTrue(row["valid_to"].endswith("+00:00"))
        self.assertIsNone(row["retired_by"])
        self.assertEqual([r["id"] for r in memory.live_entries(home)], [row["id"]])

    def test_a_bad_valid_until_writes_nothing(self):
        home = _home(self)
        with self.assertRaises(ValueError):
            memory.remember(home, "spi base", "x", valid_until="2001-01-01")
        self.assertEqual(memory.validity(home), [])

    def _expired_entry(self, home):
        past = datetime.now(timezone.utc) - timedelta(days=1)
        entry = {"timestamp": (past - timedelta(days=2)).isoformat(), "topic": "office wifi",
                 "content": "The guest wifi password is on the fridge this week.",
                 "truth_level": "L2_TOOL", "source": "remember",
                 "valid_to": past.isoformat(timespec="seconds"), "scope": "the main office"}
        _write(home, past.date().isoformat(), [entry])
        return memory.entry_id(entry)

    def test_past_its_end_it_leaves_the_live_set_and_the_views(self):
        home = _home(self)
        eid = self._expired_entry(home)
        self.assertEqual(memory.live_entries(home), [])
        self.assertIn(eid, memory.hidden_ids(memory._all_raw(home)))
        distill.distill(home)
        text = "".join(p.read_text() for p in (home / "memory" / "distilled").glob("*.md"))
        self.assertNotIn("guest wifi", text)

    def test_an_entry_with_an_end_is_never_read_from_a_digest(self):
        home = _home(self)
        eid = self._expired_entry(home)
        self.assertIn(eid, memory.digest_unsafe_ids(memory._all_raw(home)))

    def test_a_live_entry_with_an_end_shows_it_in_the_view(self):
        home = _home(self)
        end = (datetime.now(timezone.utc) + timedelta(days=3)).date().isoformat()
        memory.remember(home, "spi base", "SPI1 is at 0x40013000.", scope="board rev A",
                        valid_until=end, level="tool", cite="datasheet p.12")
        distill.distill(home)
        text = "".join(p.read_text() for p in (home / "memory" / "distilled").glob("*.md"))
        line = next(l for l in text.splitlines() if "0x40013000" in l)
        self.assertIn("scope: board rev A", line)
        self.assertIn("through %s" % end, line)

    def test_recall_names_an_expired_hit_as_expired(self):
        from cousin_lib import memory_search
        home = _home(self)
        self._expired_entry(home)
        [path] = list((home / "memory" / "raw").glob("*.jsonl"))
        hit = {"collection": "raw", "path": "%s#1" % path}
        name = memory_search._hit_name(hit)
        self.assertTrue(name.startswith("office wifi ["), name)
        self.assertIn("scope: the main office", name)
        self.assertIn("expired ", name)
        item = memory_search.recall_item(home, hit)
        self.assertTrue(item["expired"])
        self.assertEqual(item["scope"], "the main office")

    def test_the_cli_takes_scope_and_valid_until(self):
        home = _home(self)
        end = (datetime.now(timezone.utc) + timedelta(days=3)).date().isoformat()
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"COUSIN_HOME": str(home)}), \
                contextlib.redirect_stdout(out):
            rc = memory.memory_main(["remember", "spi base", "SPI1 is at 0x40013000.",
                                     "--scope", "board rev A", "--valid-until", end])
        self.assertEqual(rc, 0)
        self.assertIn("scope: board rev A", out.getvalue())
        [row] = memory.validity(home)
        self.assertEqual(row["scope"], "board rev A")


class TestALiveFactWithAnEndIsLiveEverywhere(HermeticCase):
    """A declared end still ahead keeps the claim live for the checks
    that used to read any `valid_to` as retired."""

    def test_is_live(self):
        future = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
        past = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        self.assertTrue(memory.is_live({"valid_to": future}))
        self.assertFalse(memory.is_live({"valid_to": past}))
        self.assertFalse(memory.is_live({"valid_to": future, "retired_by": "abc"}))
        self.assertTrue(memory.is_live({}))

    def test_dreaming_counts_it_live(self):
        from cousin_lib import dream_memory
        home = _home(self)
        end = (datetime.now(timezone.utc) + timedelta(days=3)).date().isoformat()
        memory.remember(home, "spi base", "SPI1 is at 0x40013000.", valid_until=end)
        self.assertEqual(len(dream_memory._live_topic(home, "spi base")), 1)


class TestAMarkBeforeTheDeclaredEndRetiresIt(HermeticCase):
    """#246 review: a claim with a future end that a mark retires is not
    live, not in tensions, and its valid_to is the mark's time."""

    def test_the_mark_wins_when_it_comes_first(self):
        home = _home(self)
        end = (datetime.now(timezone.utc) + timedelta(days=60)).date().isoformat()
        memory.remember(home, "lease", "The lease runs to the end of the year.", valid_until=end)
        memory.remember(home, "lease", "The lease was cancelled.")
        first = [r for r in memory.validity(home) if r["content"].startswith("The lease runs")][0]
        mark = memory.mark_obsolete(home, "lease", "cancelled", entry=first["id"])
        row = [r for r in memory.validity(home) if r["id"] == first["id"]][0]
        self.assertEqual(row["valid_to"], mark["timestamp"])
        self.assertFalse(memory.is_live(row))
        self.assertNotIn(first["id"], [r["id"] for r in memory.live_entries(home)])
        self.assertEqual(memory.tensions(home), [])

    def test_history_says_live_through_and_scope(self):
        home = _home(self)
        end = (datetime.now(timezone.utc) + timedelta(days=3)).date().isoformat()
        memory.remember(home, "spi base", "SPI1 is at 0x40013000.", scope="board rev A",
                        valid_until=end)
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"COUSIN_HOME": str(home)}), contextlib.redirect_stdout(out):
            memory.memory_main(["history", "spi base"])
        self.assertIn("live, through %s; scope: board rev A" % end, out.getvalue())

    def test_recall_prints_the_labels(self):
        home = _home(self)
        end = (datetime.now(timezone.utc) + timedelta(days=3)).date().isoformat()
        memory.remember(home, "spi base", "SPI1 is at 0x40013000.", scope="board rev A",
                        valid_until=end)
        text = memory.format_recall(memory.list_raw(home))
        self.assertIn("scope: board rev A; through %s" % end, text)
