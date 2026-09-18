"""Jobs routes: a projection of jobs.db with the reaper as accepted
maintenance, log tails, field updates with SIGTERM on cancel, and
delete limited to console-minted logs."""
import os
import signal
import subprocess
import sys
import unittest

from cousin_lib import jobs
from tests.console._harness import ConsoleCase


class TestJobs(ConsoleCase):
    def test_list_filters_and_running_first(self):
        a = jobs.register_job(kind="shell", title="a", spawned_by="wren")
        b = jobs.register_job(kind="subagent", title="b", spawned_by="toki")
        jobs.finish_job(a, status="done")
        c = jobs.register_job(kind="shell", title="c", spawned_by="wren")
        self.serve()
        _, body = self.get("/api/jobs")
        self.assertEqual([j["title"] for j in body["jobs"]], ["c", "b", "a"])
        self.assertIn("finished_at", body["jobs"][2])
        _, body = self.get("/api/jobs?kind=shell&spawned_by=wren")
        self.assertEqual([j["title"] for j in body["jobs"]], ["c", "a"])
        _, body = self.get("/api/jobs?active_only=1")
        self.assertEqual([j["title"] for j in body["jobs"]], ["c", "b"])
        _, body = self.get("/api/jobs?status=done&since_hours=1")
        self.assertEqual([j["title"] for j in body["jobs"]], ["a"])
        self.assertEqual(self.get("/api/jobs?limit=x")[0], 400)

    def test_list_runs_the_reaper_at_most_once_per_window(self):
        old = jobs.register_job(kind="shell", title="old", spawned_by="w")
        conn = jobs._db()
        conn.execute("UPDATE jobs SET started_at='2000-01-01T00:00:00+00:00'"
                     " WHERE id=?", (old,))
        conn.commit()
        conn.close()
        self.serve()
        _, body = self.get("/api/jobs")
        self.assertEqual(body["jobs"][0]["status"], "failed")
        again = jobs.register_job(kind="shell", title="old2", spawned_by="w")
        conn = jobs._db()
        conn.execute("UPDATE jobs SET started_at='2000-01-01T00:00:00+00:00'"
                     " WHERE id=?", (again,))
        conn.commit()
        conn.close()
        _, body = self.get("/api/jobs")
        self.assertEqual(jobs.get_job(again)["status"], "running")

    def test_show_and_log(self):
        jid = jobs.register_job(kind="shell", title="a", spawned_by="w")
        self.serve()
        status, body = self.get("/api/jobs/%d" % jid)
        self.assertEqual((status, body["job"]["title"]), (200, "a"))
        self.assertEqual(self.get("/api/jobs/999")[0], 404)
        _, body = self.get("/api/jobs/%d/log" % jid)
        self.assertEqual(body["log"], "")
        log = self.root / "job.log"
        jobs.set_log_path(jid, str(log))
        _, body = self.get("/api/jobs/%d/log" % jid)
        self.assertIn("not", body["log"].lower())
        log.write_text("".join("line %d\n" % i for i in range(100)))
        _, body = self.get("/api/jobs/%d/log?lines=3" % jid)
        self.assertEqual(body["log"], "line 97\nline 98\nline 99\n")
        self.assertEqual(body["size"], log.stat().st_size)
        self.assertEqual(body["next"], body["size"])
        _, body = self.get("/api/jobs/%d/log?from=%d" % (jid, body["size"] - 8))
        self.assertEqual(body["log"], "line 99\n")
        self.assertEqual(body["next"], log.stat().st_size)

    def test_a_follower_reads_a_growing_log_once_by_next(self):
        jid = jobs.register_job(kind="shell", title="a", spawned_by="w")
        log = self.root / "grow.log"
        log.write_text("")
        jobs.set_log_path(jid, str(log))
        self.serve()
        _, body = self.get("/api/jobs/%d/log" % jid)
        seen, offset = body["log"], body["next"]
        for i in range(3):
            with open(log, "a") as fh:
                fh.write("tick %d\n" % i)
            _, body = self.get("/api/jobs/%d/log?from=%d" % (jid, offset))
            seen += body["log"]
            offset = body["next"]
        self.assertEqual(seen, "tick 0\ntick 1\ntick 2\n")
        # a chunk larger than one read ends on a line boundary, and the
        # next read continues exactly there
        with open(log, "a") as fh:
            fh.write("".join("%05d %s\n" % (i, "x" * 90)
                             for i in range(1500)))
        text = ""
        while True:
            _, body = self.get("/api/jobs/%d/log?from=%d" % (jid, offset))
            if not body["log"]:
                break
            text += body["log"]
            offset = body["next"]
        self.assertEqual(text.count("\n"), 1500)
        self.assertTrue(text.endswith("01499 " + "x" * 90 + "\n"))

    def test_a_job_without_a_log_says_so(self):
        jid = jobs.register_job(kind="build", title="by hand", spawned_by="w")
        self.serve()
        _, body = self.get("/api/jobs/%d/log" % jid)
        self.assertEqual((body["log"], body["log_path"]), ("", None))
        self.assertFalse(body["has_log"])

    def test_update_and_cancel_sends_sigterm(self):
        proc = subprocess.Popen([sys.executable, "-c",
                                 "import time; time.sleep(30)"])
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        jid = jobs.register_job(kind="shell", title="a", spawned_by="w")
        conn = jobs._db()
        conn.execute("UPDATE jobs SET pid=? WHERE id=?", (proc.pid, jid))
        conn.commit()
        conn.close()
        self.serve()
        status, body = self.post("/api/jobs/%d" % jid, {"title": "b"})
        self.assertEqual((status, body["job"]["title"]), (200, "b"))
        self.assertEqual(self.post("/api/jobs/%d" % jid, {})[0], 400)
        self.assertEqual(self.post("/api/jobs/%d" % jid,
                                   {"status": "odd"})[0], 400)
        status, body = self.post("/api/jobs/%d" % jid,
                                 {"status": "cancelled"})
        self.assertEqual(body["job"]["status"], "cancelled")
        self.assertIsNotNone(body["job"]["finished_at"])
        proc.wait(timeout=5)
        self.assertEqual(proc.returncode, -signal.SIGTERM)
        self.assertEqual(self.post("/api/jobs/999", {"title": "x"})[0], 404)

    def test_delete_removes_only_minted_logs(self):
        a = jobs.register_job(kind="shell", title="a", spawned_by="w")
        minted = jobs._default_log_path(a)
        minted.write_text("x")
        jobs.set_log_path(a, str(minted))
        b = jobs.register_job(kind="shell", title="b", spawned_by="w")
        outside = self.root / "keep.log"
        outside.write_text("x")
        jobs.set_log_path(b, str(outside))
        self.serve()
        self.assertEqual(self.delete("/api/jobs/%d" % a)[1], {"ok": True})
        self.assertFalse(minted.exists())
        self.assertEqual(self.delete("/api/jobs/%d" % b)[1], {"ok": True})
        self.assertTrue(outside.exists())
        self.assertEqual(self.delete("/api/jobs/%d" % b)[0], 404)
        self.assertEqual(self.post("/api/jobs", {"title": "x"})[0], 405)


if __name__ == "__main__":
    unittest.main()
