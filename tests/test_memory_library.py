"""memory's library functions: what the CLI and the tools both call."""
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
        self.assertIn("No decisions found matching 'zzz'", memory.format_recall([], "zzz"))

    def test_note_activity_writes_the_file(self):
        home = _home(self)
        out = memory.note_activity(home, "writing tests")
        self.assertEqual(out, "Activity saved: writing tests")
        self.assertIn("writing tests", (home / "data" / "last-activity.txt").read_text())


class TestCliStillWorks(HermeticCase):
    def test_the_cli_decide_calls_the_library(self):
        home = _home(self)
        rc = memory.memory_main(["--home", str(home), "decide", "t", "d", "w"])
        self.assertEqual(rc, 0)
        self.assertTrue((home / "data" / "decisions.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
