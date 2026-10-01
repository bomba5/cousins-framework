"""memory's library functions: what the CLI and the tools both call."""
import contextlib
import io
import json
import pathlib
import tempfile
import unittest

from cousin_lib import memory
from tests._hermetic import HermeticCase


def _home(case):
    tmp = tempfile.TemporaryDirectory(); case.addCleanup(tmp.cleanup)
    home = pathlib.Path(tmp.name) / "cousins" / "wren"
    (home / "data").mkdir(parents=True); (home / "memory").mkdir()
    (home / "cousin.toml").write_text('[cousin]\nslug = "wren"\nname = "Wren"\n')
    return home


# The exact bytes `cousin-memory recall alpha` printed before this file
# existed (commit 8aa4ec4, the pre-refactor `cousin_lib/memory.py`), for
# the fixed three-decision fixture below (fixed timestamps: written
# straight into decisions.jsonl, not produced by `decide`, so the
# expected text has nothing wall-clock-dependent in it). Captured by
# running the old `_cmd_recall` against that fixture and reading its
# stdout back; see task-0-2-report.md's fix note for how.
_OLD_RECALL_ALPHA_OUTPUT = (
    "[2030-01-01T10:00] alpha thing: one\n"
    "  Why: because alpha\n"
    "\n"
    "[2030-01-01T11:00] alpha other: two\n"
    "  Why: because also\n"
    "\n"
)

# What `cousin-memory decide "  padded topic  " " padded decision " " padded
# why "` returned before this file existed (commit 8aa4ec4, the
# pre-refactor `cousin_lib/memory.py`, non---stdin path): no stripping,
# so the padding survives into the printed line, decisions.jsonl and the
# raw bridge. Captured the same way as the recall fixture above (the old
# module run against the same input, stdout and the written files read
# back).
_OLD_DECIDE_PADDED_LINE = "Decision logged: [  padded topic  ]  padded decision "


class TestDecide(HermeticCase):
    def test_decide_writes_decisions_and_raw_and_returns_the_line(self):
        home = _home(self)
        out = memory.decide(home, "pick a name", "Toki", "short")
        self.assertTrue(out.startswith("Decision logged: [pick a name] Toki"), out)
        rows = [json.loads(l) for l in (home / "data" / "decisions.jsonl").read_text().splitlines()]
        self.assertEqual(rows[-1]["topic"], "pick a name")
        raw = list((home / "memory" / "raw").glob("*.jsonl"))
        self.assertTrue(raw and "pick a name" in raw[0].read_text())

    def test_decide_refuses_a_missing_part(self):
        with self.assertRaises(ValueError):
            memory.decide(_home(self), "t", "", "why")

    def test_decide_does_not_strip_padding_like_the_old_cli(self):
        home = _home(self)
        out = memory.decide(home, "  padded topic  ", " padded decision ", " padded why ")
        self.assertEqual(out, _OLD_DECIDE_PADDED_LINE)
        rows = [json.loads(l) for l in (home / "data" / "decisions.jsonl").read_text().splitlines()]
        self.assertEqual(rows[-1]["topic"], "  padded topic  ")
        self.assertEqual(rows[-1]["decision"], " padded decision ")
        self.assertEqual(rows[-1]["reasoning"], " padded why ")

    def test_operator_level_needs_a_cite(self):
        with self.assertRaises(ValueError):
            memory.decide(_home(self), "t", "d", "w", level="operator")


class TestRemember(HermeticCase):
    def test_remember_returns_the_line_and_writes_raw(self):
        home = _home(self)
        out = memory.remember(home, "office", "Priya sits by the window", level="operator",
                              cite="chat 12")
        self.assertIn("Remembered [office] (L0_OPERATOR)", out)
        raw = list((home / "memory" / "raw").glob("*.jsonl"))
        self.assertIn("Priya sits by the window", raw[0].read_text())

    def test_remember_refuses_empty(self):
        with self.assertRaises(ValueError):
            memory.remember(_home(self), "", "x")


class TestRecallAndActivity(HermeticCase):
    def test_recall_filters_and_formats_like_the_cli(self):
        home = _home(self)
        memory.decide(home, "alpha", "one", "because")
        memory.decide(home, "beta", "two", "since")
        entries = memory.recall_entries(home, "beta")
        self.assertEqual([e["topic"] for e in entries], ["beta"])
        text = memory.format_recall(entries, "beta")
        self.assertIn("beta: two", text); self.assertIn("Why: since", text)
        self.assertIn("No memories found matching 'zzz'", memory.format_recall([], "zzz"))

    def test_note_activity_writes_the_file(self):
        home = _home(self)
        out = memory.note_activity(home, "writing tests")
        self.assertEqual(out, "Activity saved: writing tests")
        self.assertIn("writing tests", (home / "data" / "last-activity.txt").read_text())


class TestCliStillWorks(HermeticCase):
    def test_the_cli_decide_calls_the_library(self):
        home = _home(self)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = memory.memory_main(["--home", str(home), "decide", "t", "d", "w"])
        self.assertEqual(rc, 0)
        self.assertIn("Decision logged", out.getvalue())
        self.assertTrue((home / "data" / "decisions.jsonl").exists())

    def test_recall_is_byte_identical_to_the_old_cli(self):
        home = _home(self)
        rows = [
            {"timestamp": "2030-01-01T10:00:00+01:00", "topic": "alpha thing",
             "decision": "one", "reasoning": "because alpha"},
            {"timestamp": "2030-01-01T11:00:00+01:00", "topic": "alpha other",
             "decision": "two", "reasoning": "because also"},
            {"timestamp": "2030-01-01T12:00:00+01:00", "topic": "beta",
             "decision": "three", "reasoning": "unrelated"},
        ]
        # Recall reads raw memory: the same three
        # decisions as `decide` stores them there. The printed bytes are
        # unchanged, which is what this test pins.
        (home / "memory" / "raw").mkdir(parents=True, exist_ok=True)
        with open(home / "memory" / "raw" / "2030-01-01.jsonl", "w") as fh:
            for row in rows:
                fh.write(json.dumps({
                    "timestamp": row["timestamp"], "topic": row["topic"],
                    "content": "%s - why: %s" % (row["decision"], row["reasoning"]),
                    "truth_level": "L3_COUSIN_CONCLUSION", "source": "decision"}) + "\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = memory.memory_main(["--home", str(home), "recall", "alpha"])
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue(), _OLD_RECALL_ALPHA_OUTPUT)


if __name__ == "__main__":
    unittest.main()
