"""Cycle counters: breadcrumbs plus the boot mtime signal.

The read that scoped this module found exactly one consumer of
cycle.json beyond the cousin's own prose - boot's staleness check
reads its mtime - and two fields (overlay, milestone) with zero
readers anywhere. Counters ship; dead fields do not.
"""
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from cousin_lib.cycle import cycle_main


class CycleCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = pathlib.Path(tmp.name) / "cousins" / "wren"
        (self.home / "data").mkdir(parents=True)
        patcher = mock.patch.dict(os.environ,
                                  {"COUSIN_HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _main(self, argv):
        import contextlib
        import io
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cycle_main(argv)
        return rc, out.getvalue(), err.getvalue()

    def _state(self):
        return json.loads(
            (self.home / "data" / "cycle.json").read_text())


class TestCycle(CycleCase):
    def test_inc_start_end_roundtrip(self):
        self.assertEqual(self._main(["inc", "--start"])[0], 0)
        state = self._state()
        self.assertEqual(state["cycle_id"], 1)
        self.assertIsNotNone(state["started_at"])
        self._main(["inc", "--action", "shipped the report"])
        state = self._state()
        self.assertEqual(state["event_count"], 1)
        self.assertEqual(state["last_action"], "shipped the report")
        self._main(["inc", "--end"])
        self.assertIsNotNone(self._state()["ended_at"])

    def test_state_prints_json(self):
        self._main(["inc", "--start"])
        rc, out, _ = self._main(["state", "--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["cycle_id"], 1)

    def test_reset_archives_never_deletes(self):
        self._main(["inc", "--start"])
        self._main(["inc", "--action", "worth keeping"])
        self._main(["reset"])
        self.assertEqual(self._state()["cycle_id"], 0)
        archive = json.loads(
            (self.home / "data" / "cycle-archive.json").read_text())
        self.assertEqual(archive[-1]["last_action"], "worth keeping")

    def test_no_context_refuses(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            rc, _, err = self._main(["state"])
        self.assertEqual(rc, 2)
        self.assertIn("COUSIN_HOME", err)

    def test_history_is_bounded(self):
        self._main(["inc", "--start"])
        for i in range(60):
            self._main(["inc", "--action", "step %d" % i])
        self.assertLessEqual(len(self._state()["history"]), 50)


if __name__ == "__main__":
    unittest.main()
