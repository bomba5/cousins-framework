"""The jobs additions the console contract names: kind and since_hours
filters with running-first ordering, field updates with the terminal
timestamp rule, row deletion, and the reaper/rotation maintenance any
reader may run."""
import os
import pathlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from cousin_lib import jobs


class JobsCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        patcher = mock.patch.dict(os.environ,
                                  {"FRAMEWORK_ROOT": str(self.root)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _backdate(self, job_id, hours):
        conn = jobs._db()
        try:
            when = (datetime.now(timezone.utc) - timedelta(hours=hours)
                    ).isoformat(timespec="seconds")
            conn.execute("UPDATE jobs SET started_at=? WHERE id=?",
                         (when, job_id))
            conn.commit()
        finally:
            conn.close()


class TestFilters(JobsCase):
    def test_kind_and_since_hours_and_running_first(self):
        a = jobs.register_job(kind="shell", title="a", spawned_by="wren")
        b = jobs.register_job(kind="subagent", title="b", spawned_by="wren")
        c = jobs.register_job(kind="shell", title="c", spawned_by="wren")
        jobs.finish_job(c, status="done")
        self._backdate(a, 50)
        self.assertEqual([j["title"] for j in jobs.list_jobs(kind="shell")],
                         ["c", "a"])
        self.assertEqual([j["title"] for j in jobs.list_jobs(since_hours=24)],
                         ["c", "b"])
        self.assertEqual(
            [j["title"] for j in jobs.list_jobs(running_first=True)],
            ["b", "a", "c"])


class TestUpdateAndDelete(JobsCase):
    def test_update_sets_fields_and_finished_at_on_a_terminal_status(self):
        jid = jobs.register_job(kind="shell", title="a", spawned_by="wren")
        row = jobs.update_job(jid, title="renamed")
        self.assertEqual(row["title"], "renamed")
        self.assertIsNone(row["finished_at"])
        row = jobs.update_job(jid, status="failed", exit_code=3,
                              result_summary="boom")
        self.assertEqual((row["status"], row["exit_code"],
                          row["result_summary"]), ("failed", 3, "boom"))
        self.assertIsNotNone(row["finished_at"])
        self.assertIsNone(jobs.update_job(999, title="x"))
        with self.assertRaises(ValueError):
            jobs.update_job(jid, status="weird")
        with self.assertRaises(ValueError):
            jobs.update_job(jid)

    def test_delete_removes_the_row(self):
        jid = jobs.register_job(kind="shell", title="a", spawned_by="wren")
        self.assertTrue(jobs.delete_job(jid))
        self.assertFalse(jobs.delete_job(jid))
        self.assertIsNone(jobs.get_job(jid))


class TestMaintenance(JobsCase):
    def test_reap_marks_old_running_rows_failed_with_a_note(self):
        old = jobs.register_job(kind="shell", title="old", spawned_by="w")
        fresh = jobs.register_job(kind="shell", title="new", spawned_by="w")
        self._backdate(old, 30)
        self.assertEqual(jobs.reap_stale(max_age_hours=24), 1)
        self.assertEqual(jobs.get_job(old)["status"], "failed")
        self.assertIn("auto-reap", jobs.get_job(old)["result_summary"])
        self.assertEqual(jobs.get_job(fresh)["status"], "running")

    def test_rotate_drops_the_oldest_finished_rows_and_their_minted_logs(self):
        ids = [jobs.register_job(kind="shell", title=str(i), spawned_by="w")
               for i in range(5)]
        for jid in ids[:3]:
            log = jobs._default_log_path(jid)
            log.write_text("x")
            jobs.set_log_path(jid, str(log))
            jobs.finish_job(jid, status="done")
        outside = self.root / "elsewhere.log"
        outside.write_text("keep")
        jobs.set_log_path(ids[3], str(outside))
        jobs.finish_job(ids[3], status="done")
        removed = jobs.rotate(cap=2)
        self.assertEqual(removed, 3)
        remaining = {j["id"] for j in jobs.list_jobs()}
        self.assertEqual(remaining, {ids[3], ids[4]})
        self.assertFalse(jobs._default_log_path(ids[0]).exists())
        self.assertTrue(outside.exists())


if __name__ == "__main__":
    unittest.main()
