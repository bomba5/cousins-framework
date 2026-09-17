"""STATUS.md to data/state.json: the machine-readable open loops.

A STATUS.md accumulates one `## Open loops (...)` heading per
generation, newest first, and never deletes the old ones, so "the open
loops" is the FIRST section of each kind, not the union. The source
learned this the hard way: its parser walked the whole file and the
boot fuel was months stale. Second half of the same lesson: bullets
are plain `- **text**` far more often than checkboxes, and a parser
that only counts checkboxes reports an empty section as "no loops".
"""
import contextlib
import io
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from cousin_lib import sync_state
from cousin_lib.sync_state import parse_status, sync_state_main

REAL_SHAPE = (
    "# Status\n"
    "_Testa, gen 12, closed today_\n"
    "\n"
    "## SESSION CLOSE gen 12\n"
    "- narrative bullet that is not an open loop\n"
    "\n"
    "## Open loops (current as of gen 12 close)\n"
    "- **current thing one**\n"
    "- **current thing two**\n"
    "\n"
    "## Parked / waiting\n"
    "- parked thing\n"
    "\n"
    "## Recently closed\n"
    "- [x] closed thing\n"
    "\n"
    "## Open loops (current as of gen 11 close)\n"
    "- **stale thing from yesterday**\n"
    "\n"
    "## Open loops (current as of gen 2 close)\n"
    "- [ ] ancient thing\n"
)


def _texts(items):
    return [i["text"] for i in items]


class TestParseStatus(unittest.TestCase):
    def test_only_the_newest_section_of_each_kind_is_read(self):
        st = parse_status(REAL_SHAPE)
        self.assertEqual(_texts(st["open_loops"]),
                         ["**current thing one**", "**current thing two**"])
        self.assertEqual(_texts(st["parked"]), ["parked thing"])
        self.assertEqual(_texts(st["recently_closed"]), ["closed thing"])

    def test_stale_generations_do_not_leak_in(self):
        blob = " ".join(_texts(parse_status(REAL_SHAPE)["open_loops"]))
        self.assertNotIn("ancient thing", blob)
        self.assertNotIn("stale thing", blob)

    def test_narrative_before_the_first_section_is_ignored(self):
        blob = " ".join(_texts(parse_status(REAL_SHAPE)["open_loops"]))
        self.assertNotIn("narrative", blob)

    def test_any_other_heading_ends_the_section(self):
        st = parse_status("## Open loops\n- real loop\n## Notes\n- not a loop\n")
        self.assertEqual(_texts(st["open_loops"]), ["real loop"])

    def test_bullets_of_any_style_count(self):
        st = parse_status(
            "## Open loops\n"
            "- plain bullet\n"
            "* star bullet\n"
            "+ plus bullet\n"
            "- **bold bullet**\n")
        self.assertEqual(len(st["open_loops"]), 4)
        self.assertTrue(all(not i["done"] and not i["partial"]
                            for i in st["open_loops"]))

    def test_checkbox_semantics(self):
        st = parse_status(
            "## Open loops\n"
            "- [ ] thing one\n"
            "- [x] thing two done\n"
            "- [~] thing three partial\n")
        by = {i["text"]: i for i in st["open_loops"]}
        self.assertFalse(by["thing one"]["done"])
        self.assertFalse(by["thing one"]["partial"])
        self.assertTrue(by["thing two done"]["done"])
        self.assertTrue(by["thing three partial"]["partial"])

    def test_heading_match_is_case_insensitive_prefix(self):
        st = parse_status("## OPEN LOOPS - week 3\n- a\n## recently closed\n- b\n")
        self.assertEqual(_texts(st["open_loops"]), ["a"])
        self.assertEqual(_texts(st["recently_closed"]), ["b"])

    def test_non_bullet_lines_inside_a_section_are_skipped(self):
        st = parse_status("## Open loops\nsome prose\n- a loop\n  - a sub-point\n")
        self.assertEqual(_texts(st["open_loops"]), ["a loop"])

    def test_long_text_is_bounded(self):
        st = parse_status("## Open loops\n- " + "x" * 500 + "\n")
        self.assertLessEqual(len(st["open_loops"][0]["text"]), 200)

    def test_empty_status_gives_empty_lists(self):
        st = parse_status("")
        self.assertEqual(st, {"open_loops": [], "parked": [],
                              "recently_closed": []})


class SyncCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = pathlib.Path(tmp.name) / "cousins" / "testa"
        self.home.mkdir(parents=True)
        patcher = mock.patch.dict(os.environ,
                                  {"COUSIN_HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = sync_state_main(argv)
        return rc, out.getvalue(), err.getvalue()

    def _state(self):
        return json.loads((self.home / "data" / "state.json").read_text())


class TestWriteState(SyncCase):
    def test_writes_state_json_with_generated_at(self):
        (self.home / "STATUS.md").write_text(REAL_SHAPE)
        state = sync_state.write_state(self.home)
        on_disk = self._state()
        self.assertEqual(state, on_disk)
        self.assertEqual(_texts(on_disk["open_loops"]),
                         ["**current thing one**", "**current thing two**"])
        self.assertIn("generated_at", on_disk)
        self.assertRegex(on_disk["generated_at"], r"^\d{4}-\d{2}-\d{2}T")

    def test_missing_status_writes_empty_state(self):
        state = sync_state.write_state(self.home)
        self.assertEqual(state["open_loops"], [])
        self.assertEqual(self._state()["parked"], [])

    def test_cli_prints_counts(self):
        (self.home / "STATUS.md").write_text(REAL_SHAPE)
        rc, out, _ = self._main([])
        self.assertEqual(rc, 0)
        self.assertIn("2 open", out)
        self.assertIn("1 parked", out)
        self.assertIn("1 closed", out)
        self.assertTrue((self.home / "data" / "state.json").is_file())

    def test_cli_home_flag(self):
        other = self.home.parent / "other"
        other.mkdir()
        (other / "STATUS.md").write_text("## Open loops\n- x\n")
        rc, _, _ = self._main(["--home", str(other)])
        self.assertEqual(rc, 0)
        self.assertTrue((other / "data" / "state.json").is_file())
        self.assertFalse((self.home / "data" / "state.json").exists())

    def test_no_context_refuses(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            rc, _, err = self._main([])
        self.assertEqual(rc, 2)
        self.assertIn("COUSIN_HOME", err)


if __name__ == "__main__":
    unittest.main()
