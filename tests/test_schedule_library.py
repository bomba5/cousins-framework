"""schedule's library functions."""
import os
import pathlib
import tempfile
import unittest
from datetime import datetime, timedelta

from cousin_lib import schedule
from tests._hermetic import HermeticCase


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


if __name__ == "__main__":
    unittest.main()
