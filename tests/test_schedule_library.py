"""schedule's library functions."""
import contextlib
import io
import os
import pathlib
import tempfile
import unittest
from datetime import datetime, timedelta

from cousin_lib import schedule
from tests._hermetic import HermeticCase

# The exact bytes `cousin-schedule list --all` printed before this file
# existed (commit 8aa4ec4, the pre-refactor `cousin_lib/schedule.py`),
# for the same three-row fixture the test below builds. Captured by
# running the old `_cmd_list` against that fixture and reading its
# stdout back; see task-0-2-report.md's fix note for how.
_OLD_LIST_ALL_OUTPUT = (
    "#3     pending    2030-01-01T12:00:00   short\n"
    "#2     pending    2030-01-01T11:00:00   check the build and make sure"
    " everything still compiles clea...\n"
    "#1     pending    2030-01-01T10:00:00   water the plant\n"
)


class TestScheduleLibrary(HermeticCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        root = pathlib.Path(tmp.name); (root / "config").mkdir()
        (root / "cousins" / "wren" / "data").mkdir(parents=True)
        (root / "cousins" / "wren" / "cousin.toml").write_text(
            '[cousin]\nslug = "wren"\nname = "Wren"\n')
        os.environ["FRAMEWORK_ROOT"] = str(root)
        os.environ["COUSIN_HOME"] = str(root / "cousins" / "wren")

    def test_add_list_cancel_round_trip(self):
        row = schedule.add("wren", "in 30m", "stretch")
        self.assertEqual(row["cousin"], "wren")
        entries = schedule.list_entries("wren")
        self.assertEqual([e["prompt"] for e in entries], ["stretch"])
        self.assertIn("stretch", schedule.format_entries(entries))
        self.assertTrue(schedule.cancel(row["id"], slug="wren"))
        self.assertEqual(schedule.list_entries("wren"), [])
        self.assertFalse(schedule.cancel(row["id"], slug="wren"))

    def test_add_refuses_the_past_and_empty(self):
        with self.assertRaises(ValueError):
            schedule.add("wren", "yesterday 09:00", "x")
        with self.assertRaises(ValueError):
            schedule.add("wren", "in 5m", "   ")

    def test_the_cli_add_still_works(self):
        rc = schedule.schedule_main(["add", "in 10m", "water the plant"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(schedule.list_entries("wren")), 1)

    def test_list_all_is_byte_identical_to_the_old_cli(self):
        # Fixed future ISO timestamps: deterministic regardless of when
        # this test runs (no "in Nm" wall-clock dependency).
        schedule.schedule_main(["add", "2030-01-01T10:00:00", "water the plant"])
        schedule.schedule_main(["add", "2030-01-01T11:00:00",
                                "check the build and make sure everything"
                                " still compiles cleanly on CI"])
        schedule.schedule_main(["add", "2030-01-01T12:00:00", "short"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = schedule.schedule_main(["list", "--all"])
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue(), _OLD_LIST_ALL_OUTPUT)


if __name__ == "__main__":
    unittest.main()
