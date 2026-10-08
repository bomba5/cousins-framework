"""Job tracking: the store, the context manager, the CLI.

The store is module-owned SQLite - registering a job must work with no
service running anywhere. Real databases, real processes; the only
seams are environment roots.
"""
import json
import os
import pathlib
import tempfile
import time
import unittest
from unittest import mock

from cousin_lib.jobs import (
    finish_job,
    get_job,
    jobs_main,
    list_jobs,
    register_job,
    track_job,
)


class JobsCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        home = self.root / "cousins" / "wren"
        home.mkdir(parents=True)
        (home / "cousin.toml").write_text(
            '[cousin]\nslug = "wren"\n[chat]\nport = 8100\n'
        )
        patcher = mock.patch.dict(os.environ, {
            "FRAMEWORK_ROOT": str(self.root),
            "COUSIN_HOME": str(home),
        })
        patcher.start()
        self.addCleanup(patcher.stop)

    def _main(self, argv):
        import contextlib
        import io
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = jobs_main(argv)
        return rc, out.getvalue(), err.getvalue()


class TestStore(JobsCase):
    def test_register_finish_roundtrip(self):
        job_id = register_job(kind="subagent", title="map the tree")
        job = get_job(job_id)
        self.assertEqual(job["status"], "running")
        self.assertEqual(job["spawned_by"], "wren")
        self.assertEqual(job["kind"], "subagent")
        finish_job(job_id, status="done", summary="mapped")
        job = get_job(job_id)
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["result_summary"], "mapped")
        self.assertIsNotNone(job["finished_at"])

    def test_list_filters_by_owner_and_active(self):
        a = register_job(kind="shell", title="mine")
        register_job(kind="shell", title="theirs", spawned_by="toki")
        finish_job(a, status="done")
        self.assertEqual(
            {j["title"] for j in list_jobs(spawned_by="wren")}, {"mine"}
        )
        self.assertEqual(
            [j["title"] for j in list_jobs(active_only=True)], ["theirs"]
        )


class TestTrackJob(JobsCase):
    def test_clean_exit_marks_done(self):
        with track_job("subagent", "quick check") as jt:
            jt.summary = "all fine"
        self.assertEqual(get_job(jt.job_id)["status"], "done")
        self.assertEqual(get_job(jt.job_id)["result_summary"], "all fine")

    def test_exception_marks_failed_and_reraises(self):
        with self.assertRaises(ValueError):
            with track_job("subagent", "doomed") as jt:
                raise ValueError("boom")
        job = get_job(jt.job_id)
        self.assertEqual(job["status"], "failed")
        self.assertIn("boom", job["result_summary"])

    def test_clean_sys_exit_zero_is_success_not_failure(self):
        # Earned the hard way: a wrapper's sys.exit(0) unwinds as
        # SystemExit and used to leave the row stuck at 'failed' (and
        # before that, 'running' forever) despite a successful run.
        with self.assertRaises(SystemExit):
            with track_job("shell", "exits cleanly") as jt:
                raise SystemExit(0)
        self.assertEqual(get_job(jt.job_id)["status"], "done")

    def test_nonzero_sys_exit_is_a_failure(self):
        with self.assertRaises(SystemExit):
            with track_job("shell", "exits angrily") as jt:
                raise SystemExit(3)
        self.assertEqual(get_job(jt.job_id)["status"], "failed")


class TestCli(JobsCase):
    def test_start_register_only_prints_the_id(self):
        rc, out, _ = self._main(["start", "subagent", "map the tree"])
        self.assertEqual(rc, 0)
        job_id = int(out.strip())
        self.assertEqual(get_job(job_id)["status"], "running")

    def test_options_after_title_without_separator_still_parse(self):
        # argparse.REMAINDER hoovers --desc into the command line; the
        # re-parse keeps register-only mode working without a `--`.
        rc, out, _ = self._main(
            ["start", "shell", "titled", "--desc", "described"]
        )
        self.assertEqual(rc, 0)
        job = get_job(int(out.strip()))
        self.assertEqual(job["description"], "described")
        self.assertIsNone(job["pid"])

    def test_done_fail_cancel_close_the_row(self):
        _, out, _ = self._main(["start", "shell", "a"])
        job_id = out.strip()
        self.assertEqual(self._main(["done", job_id, "finished"])[0], 0)
        self.assertEqual(get_job(int(job_id))["status"], "done")
        _, out, _ = self._main(["start", "shell", "b"])
        self.assertEqual(self._main(["fail", out.strip(),
                                     "went wrong", "--exit", "3"])[0], 0)
        job = get_job(int(out.strip()))
        self.assertEqual((job["status"], job["exit_code"]), ("failed", 3))

    def test_a_cousin_cannot_close_another_cousins_job(self):
        job_id = register_job(kind="other", title="theirs", spawned_by="toki")
        for argv in (["done", str(job_id)], ["fail", str(job_id)],
                     ["cancel", str(job_id)]):
            rc, _, err = self._main(argv)
            self.assertEqual(rc, 3, argv)
            self.assertIn("job #%d refused: it was started by toki, not by"
                          " wren" % job_id, err)
        self.assertEqual(get_job(job_id)["status"], "running")

    def test_a_shell_with_no_cousin_home_closes_any_job(self):
        job_id = register_job(kind="other", title="theirs", spawned_by="toki")
        with mock.patch.dict(os.environ):
            del os.environ["COUSIN_HOME"]
            rc, _, err = self._main(["done", str(job_id)])
        self.assertEqual(rc, 0, err)
        self.assertEqual(get_job(job_id)["status"], "done")

    def test_done_on_missing_job_exits_one(self):
        rc, _, err = self._main(["done", "999"])
        self.assertEqual(rc, 1)

    def test_list_mine_scopes_to_the_calling_cousin(self):
        self._main(["start", "shell", "mine"])
        register_job(kind="shell", title="theirs", spawned_by="toki")
        rc, out, _ = self._main(["list", "--mine"])
        self.assertIn("mine", out)
        self.assertNotIn("theirs", out)


class TestBackgroundCommand(JobsCase):
    def _wait_status(self, job_id, wanted, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = get_job(job_id)
            if job["status"] in wanted:
                return job
            time.sleep(0.05)
        self.fail("job never reached %s (last: %s)"
                  % (wanted, job["status"]))

    def test_forked_command_logs_output_and_writes_rc_back(self):
        rc, out, _ = self._main([
            "start", "shell", "echoer", "--",
            "sh", "-c", "echo hello-from-job",
        ])
        self.assertEqual(rc, 0)
        job_id = int(out.strip())
        job = self._wait_status(job_id, ("done",))
        self.assertEqual(job["exit_code"], 0)
        self.assertIn("hello-from-job",
                      pathlib.Path(job["log_path"]).read_text())

    def test_named_artifacts_are_recorded_on_exit_zero(self):
        """`--artifact` names what the command builds: on exit 0 each one
        is an artifact row of the job (measured then), relative paths
        from where the start ran; a named file that is not there fails
        the job with the reason, though the command exited 0."""
        from cousin_lib import artifacts
        here = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, here)
        _, out, _ = self._main([
            "start", "shell", "build", "--artifact", "out/fw.bin",
            "--artifact-commit", "abc1234", "--",
            "sh", "-c", "mkdir -p out && printf firmware > out/fw.bin",
        ])
        job_id = int(out.strip())
        job = self._wait_status(job_id, ("done", "failed"))
        self.assertEqual(job["status"], "done", job.get("result_summary"))
        [row] = artifacts.list_rows(job_id=job_id)
        self.assertEqual(row["path"], str(self.root / "out" / "fw.bin"))
        self.assertEqual(row["size"], len("firmware"))
        self.assertEqual(row["git_commit"], "abc1234")
        self.assertIn("artifact #%d recorded" % row["id"], pathlib.Path(job["log_path"]).read_text())
        _, out, _ = self._main([
            "start", "shell", "forgot", "--artifact", "out/missing.bin", "--", "true"])
        job = self._wait_status(int(out.strip()), ("done", "failed"))
        self.assertEqual((job["status"], job["exit_code"]), ("failed", 0))
        self.assertIn("missing.bin", job["result_summary"])
        self.assertEqual(artifacts.list_rows(job_id=job["id"]), [])

    def test_a_failing_build_records_nothing(self):
        from cousin_lib import artifacts
        _, out, _ = self._main([
            "start", "shell", "broken", "--artifact", str(self.root / "x.bin"), "--",
            "sh", "-c", "printf x > %s; exit 3" % (self.root / "x.bin")])
        job = self._wait_status(int(out.strip()), ("failed",))
        self.assertEqual(job["exit_code"], 3)
        self.assertEqual(artifacts.list_rows(job_id=job["id"]), [])

    def test_an_artifact_needs_a_command(self):
        rc, _, err = self._main(["start", "shell", "nothing", "--artifact", "x.bin"])
        self.assertEqual(rc, 2)
        self.assertIn("needs a command", err)
        rc, _, err = self._main(["start", "shell", "--artifact-commit", "abc", "--", "t", "true"])
        self.assertEqual(rc, 2)
        self.assertIn("give --artifact too", err)

    def test_a_relative_log_is_recorded_absolute(self):
        # The job tool's `run` passes its `log` relative to the home, the
        # working directory there; the console reads the row from anywhere.
        here = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, here)
        _, out, _ = self._main([
            "start", "shell", "logged", "--log", "mine.log", "--",
            "sh", "-c", "echo into-mine",
        ])
        job = self._wait_status(int(out.strip()), ("done",))
        self.assertEqual(job["log_path"], str(self.root / "mine.log"))
        self.assertIn("into-mine", (self.root / "mine.log").read_text())

    def test_a_home_log_is_confined_to_the_home(self):
        home = pathlib.Path(os.environ["COUSIN_HOME"])
        for bad in (str(self.root / "escape.log"), "../escape.log",
                    "~/escape.log", ".secrets/x.log"):
            rc, _, err = self._main([
                "start", "shell", "--home-log", bad, "--", "t",
                "sh", "-c", "echo no",
            ])
            self.assertEqual(rc, 2, bad)
            self.assertIn("--home-log", err)
        self.assertEqual(list_jobs(), [])
        _, out, _ = self._main([
            "start", "shell", "--home-log", "data/ok.log", "--", "t",
            "sh", "-c", "echo into-home",
        ])
        job = self._wait_status(int(out.strip()), ("done",))
        self.assertEqual(job["log_path"],
                         os.path.realpath(home / "data" / "ok.log"))
        self.assertIn("into-home", pathlib.Path(job["log_path"]).read_text())

    def test_an_empty_program_is_refused_before_any_row(self):
        """An empty argv[0] registered a job that died 127 (the runner
        lane already refuses it)."""
        for program in ("", " ", "\t"):
            with self.subTest(program=program):
                rc, _, err = self._main(["start", "shell", "--", "t", program, "x"])
                self.assertEqual(rc, 2)
                self.assertIn("program", err)
        self.assertEqual(list_jobs(), [])

    def test_a_program_with_surrounding_whitespace_is_refused_like_a_blank_one(self):
        """A program with surrounding whitespace (" echo", "echo ") is no
        program on PATH either; it registered a job that died 127."""
        for program in (" echo", "echo ", "\techo", "echo\n"):
            with self.subTest(program=program):
                rc, _, err = self._main(["start", "shell", "--", "t", program, "x"])
                self.assertEqual(rc, 2)
                self.assertIn("program", err)
        self.assertEqual(list_jobs(), [])

    def test_one_option_set_serves_every_start_parser(self):
        """The separated shape's check and the title-first re-parse
        read the start options from the same builder as the start parser,
        so an option added there cannot drift from them."""
        import argparse
        from cousin_lib import jobs
        def options(build):
            p = argparse.ArgumentParser(add_help=False)
            build(p)
            return sorted(o for a in p._actions for o in a.option_strings)
        self.assertEqual(options(jobs._start_options),
                         ["--artifact", "--artifact-commit", "--desc", "--home-log",
                          "--json", "--log", "--notify"])
        # the separated shape counts an option given before `--` the same way
        self.assertTrue(jobs._title_after_separator(
            ["start", "shell", "--home-log", "logs/x.log", "--", "t"]))
        self.assertFalse(jobs._title_after_separator(
            ["start", "shell", "t", "--home-log", "logs/x.log", "--", "cmd"]))

    def test_after_the_separator_nothing_is_read_as_an_option(self):
        # `start shell [options] -- TITLE CMD`: the title and the command
        # come after `--`, and neither is ever re-parsed as cousin-job's
        # own options, so --log (or a prefix of it) cannot set the log.
        escape = self.root / "escape.log"
        for first in ("--log=%s" % escape, "--lo=%s" % escape, "--desc=x",
                      "--json", "-x"):
            rc, _, err = self._main(["start", "shell", "--json", "--", "t",
                                     first, "x"])
            self.assertEqual(rc, 2, first)
            self.assertIn("must not start with", err)
        self.assertEqual(list_jobs(), [])
        self.assertFalse(escape.exists())
        _, out, _ = self._main(["start", "shell", "--json", "--",
                                "--log=%s" % escape, "sh", "-c",
                                "echo $0", "--log=%s" % escape])
        job = self._wait_status(json.loads(out)["job_id"], ("done",))
        self.assertEqual(job["title"], "--log=%s" % escape)
        self.assertNotEqual(job["log_path"], str(escape))
        self.assertIn("--log=%s" % escape, pathlib.Path(job["log_path"]).read_text())
        self.assertFalse(escape.exists())

    def test_the_legacy_title_first_shape_still_takes_its_options(self):
        legacy = self.root / "legacy.log"
        _, out, _ = self._main(["start", "shell", "legacy", "--log",
                                str(legacy), "--desc", "old shape", "--",
                                "sh", "-c", "echo legacy-shape"])
        job = self._wait_status(int(out.strip()), ("done",))
        self.assertEqual((job["title"], job["description"], job["log_path"]),
                         ("legacy", "old shape", str(legacy)))
        self.assertEqual(job["command"], "sh -c echo legacy-shape")
        self.assertIn("legacy-shape", legacy.read_text())
        _, out, _ = self._main(["start", "subagent", "mapping", "--desc", "d",
                                "--json"])
        job = get_job(json.loads(out)["job_id"])
        self.assertEqual((job["title"], job["description"]), ("mapping", "d"))

    def test_failing_command_marks_failed_with_its_exit_code(self):
        _, out, _ = self._main([
            "start", "shell", "fails", "--", "sh", "-c", "exit 7",
        ])
        job = self._wait_status(int(out.strip()), ("failed",))
        self.assertEqual(job["exit_code"], 7)

    def test_cancel_kills_the_running_process(self):
        _, out, _ = self._main([
            "start", "shell", "sleeper", "--", "sleep", "30",
        ])
        job_id = int(out.strip())
        job = get_job(job_id)
        self.assertIsNotNone(job["pid"])
        rc, _, _ = self._main(["cancel", str(job_id)])
        self.assertEqual(rc, 0)
        self.assertEqual(get_job(job_id)["status"], "cancelled")
        # SIGTERM delivered: the pid must be gone (or a zombie being
        # reaped by init, which kill(pid, 0) treats as gone once reaped).
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                os.kill(job["pid"], 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            self.fail("process survived cancel")


class TestProcessGroup(TestBackgroundCommand):
    """A job's command runs in its own process group; closing the job
    ends the whole group, and a finished job whose group still runs is
    shown as a leak."""

    def _alive(self, pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        try:
            with open("/proc/%d/stat" % pid) as fh:
                return fh.read().split(")")[-1].split()[0] != "Z"
        except OSError:
            return False

    def _gone(self, pid, timeout=6):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self._alive(pid):
                return True
            time.sleep(0.05)
        return False

    def _pid_from(self, path, timeout=5):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                return int(path.read_text().strip())
            except (OSError, ValueError):
                time.sleep(0.05)
        self.fail("no pid written to %s" % path)

    def test_cancel_ends_a_grandchild_too(self):
        pidfile = self.root / "bg.pid"
        _, out, _ = self._main([
            "start", "shell", "tail-like", "--", "sh", "-c",
            "sleep 300 & echo $! > %s; wait" % pidfile])
        job_id = int(out.strip())
        grandchild = self._pid_from(pidfile)
        self.assertEqual(get_job(job_id)["pgid"], get_job(job_id)["pid"])
        rc, text, _ = self._main(["cancel", str(job_id)])
        self.assertEqual(rc, 0)
        self.assertIn("stopped", text)
        self.assertTrue(self._gone(grandchild), "grandchild survived cancel")
        self.assertEqual(get_job(job_id)["status"], "cancelled")

    def test_a_finished_job_with_a_live_child_is_a_leak_until_reaped(self):
        pidfile = self.root / "leak.pid"
        _, out, _ = self._main([
            "start", "shell", "leaker", "--", "sh", "-c",
            "sleep 300 > /dev/null 2>&1 & echo $! > %s; exit 0" % pidfile])
        job_id = int(out.strip())
        orphan = self._pid_from(pidfile)
        self._wait_status(job_id, ("done",))
        _, listing, _ = self._main(["list"])
        self.assertIn("LEAK", listing)
        _, shown, _ = self._main(["show", str(job_id)])
        self.assertIn("LEAK", shown)
        self._main(["cancel", str(job_id)])
        self.assertTrue(self._gone(orphan), "leaked child survived cancel")
        _, listing, _ = self._main(["list"])
        self.assertNotIn("LEAK", listing)

    def test_other_process_groups_are_never_touched(self):
        from cousin_lib import jobs
        self.assertEqual(jobs.group_members(os.getpgrp(), since=time.time() + 60), [])
        self.assertEqual(jobs.group_members(None), [])


class TestTailCommand(JobsCase):
    def test_tail_prints_the_last_lines_of_the_log(self):
        _, out, _ = self._main([
            "start", "shell", "counter", "--",
            "sh", "-c", "seq 1 100",
        ])
        job_id = int(out.strip())
        deadline = time.time() + 10
        while (get_job(job_id)["status"] == "running"
               and time.time() < deadline):
            time.sleep(0.05)
        rc, out, _ = self._main(["tail", str(job_id), "--lines", "3"])
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip().splitlines(), ["98", "99", "100"])

    def test_a_hand_registered_job_gets_a_log_with_its_outcome(self):
        # No command, no --log: the row still gets a readable log, its
        # header at start and its outcome at done.
        _, out, _ = self._main(["start", "subagent", "review the diff",
                                "--desc", "check the flip changes"])
        job_id = int(out.strip())
        path = get_job(job_id)["log_path"]
        self.assertTrue(path)
        self._main(["done", str(job_id), "two nits"])
        text = pathlib.Path(path).read_text()
        self.assertIn("# subagent: review the diff", text)
        self.assertIn("check the flip changes", text)
        self.assertIn("## done", text)
        self.assertIn("two nits", text)
        rc, out, _ = self._main(["tail", str(job_id)])
        self.assertEqual(rc, 0)
        self.assertIn("two nits", out)


if __name__ == "__main__":
    unittest.main()


class TrackedJobsLog(JobsCase):
    """An in-process tracked job (media, for one) gets a log: header,
    what the caller logged, and the outcome."""

    def test_done_and_failed_both_land_in_the_log(self):
        from cousin_lib.jobs import track_job
        with track_job("media", "image: a robot", "a robot") as job:
            job.log("request: image via http://x (model m)")
            job.summary = "/tmp/out.png"
        text = pathlib.Path(get_job(job.job_id)["log_path"]).read_text()
        self.assertIn("# media: image: a robot", text)
        self.assertIn("request: image via http://x", text)
        self.assertIn("done: /tmp/out.png", text)
        with self.assertRaises(RuntimeError):
            with track_job("media", "image: b", "b") as job:
                raise RuntimeError("HTTP Error 429: quota cap")
        text = pathlib.Path(get_job(job.job_id)["log_path"]).read_text()
        self.assertIn("FAILED: RuntimeError: HTTP Error 429: quota cap", text)


class TestLostJobs(JobsCase):
    """#244: a running row whose recorded process is gone turns 'lost' on
    the next read, not 24 hours later."""

    def _row_with(self, **fields):
        from cousin_lib import jobs
        job_id = register_job(kind="shell", title="dies unclosed")
        conn = jobs._db()
        try:
            conn.execute("UPDATE jobs SET %s WHERE id=?"
                         % ", ".join("%s=?" % k for k in fields),
                         list(fields.values()) + [job_id])
            conn.commit()
        finally:
            conn.close()
        return job_id

    def _dead_pid(self):
        import subprocess
        proc = subprocess.Popen(["true"])
        proc.wait()
        return proc.pid

    def test_a_group_led_job_with_no_live_member_is_lost(self):
        from cousin_lib import jobs
        pid = self._dead_pid()
        job_id = self._row_with(pid=pid, pgid=pid)
        self.assertEqual(jobs.reap_lost(), [job_id])
        job = get_job(job_id)
        self.assertEqual(job["status"], "lost")
        self.assertIn("lost: its process is gone", job["result_summary"])
        self.assertIsNotNone(job["finished_at"])

    def test_a_pid_only_row_whose_pid_is_gone_is_lost(self):
        from cousin_lib import jobs
        job_id = self._row_with(pid=self._dead_pid())
        self.assertEqual(jobs.reap_lost(), [job_id])

    def _sleeper(self):
        import subprocess
        proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        return proc

    def test_a_live_group_keeps_its_job_running(self):
        # Started after the row, as a job's runner always is.
        from cousin_lib import jobs
        job_id = register_job(kind="shell", title="still running")
        proc = self._sleeper()
        jobs.record_spawn(job_id, proc.pid)
        self.assertEqual(jobs.reap_lost(), [])
        self.assertEqual(get_job(job_id)["status"], "running")

    def test_the_runner_identity_survives_a_clock_step(self):
        # A wall-clock jump back makes started_at look newer than the
        # process; the stored start ticks still say it is the same one.
        from cousin_lib import jobs
        job_id = register_job(kind="shell", title="clock stepped back")
        proc = self._sleeper()
        jobs.record_spawn(job_id, proc.pid)
        conn = jobs._db()
        try:
            conn.execute("UPDATE jobs SET started_at=? WHERE id=?",
                         ("2099-01-01T00:00:00+00:00", job_id))
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(jobs.reap_lost(), [])

    def test_a_reused_pid_with_other_start_ticks_is_not_the_job(self):
        from cousin_lib import jobs
        job_id = register_job(kind="shell", title="pid reused")
        proc = self._sleeper()
        jobs.record_spawn(job_id, proc.pid)
        conn = jobs._db()
        try:
            conn.execute("UPDATE jobs SET start_ticks=start_ticks-1000, pgid=-1 WHERE id=?", (job_id,))
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(jobs.reap_lost(), [job_id])

    def test_a_row_from_an_earlier_boot_is_lost(self):
        from cousin_lib import jobs
        job_id = register_job(kind="shell", title="before the reboot")
        proc = self._sleeper()
        jobs.record_spawn(job_id, proc.pid)
        conn = jobs._db()
        try:
            conn.execute("UPDATE jobs SET boot_id='an-earlier-boot', pgid=-1 WHERE id=?", (job_id,))
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(jobs.reap_lost(), [job_id])

    def test_a_runner_that_finishes_after_a_wrong_lost_still_closes_it(self):
        from cousin_lib import jobs
        job_id = self._row_with(pid=self._dead_pid())
        jobs.reap_lost()
        finish_job(job_id, status="done", exit_code=0)
        self.assertEqual((get_job(job_id)["status"], get_job(job_id)["exit_code"]), ("done", 0))

    def test_the_quiet_reap_never_raises(self):
        from cousin_lib import jobs
        with mock.patch.object(jobs, "reap_lost", side_effect=RuntimeError("locked")):
            self.assertEqual(jobs.reap_lost_quietly(), [])

    def test_a_row_with_no_pid_is_never_lost(self):
        from cousin_lib import jobs
        job_id = register_job(kind="subagent", title="no process to check")
        self.assertEqual(jobs.reap_lost(), [])
        self.assertEqual(get_job(job_id)["status"], "running")

    def test_a_reused_pid_older_than_the_job_does_not_keep_it_running(self):
        # pid 1 is alive but started long before the job: not its process.
        from cousin_lib import jobs
        job_id = self._row_with(pid=1)
        self.assertEqual(jobs.reap_lost(), [job_id])

    def test_a_finished_row_is_left_alone(self):
        from cousin_lib import jobs
        job_id = self._row_with(pid=self._dead_pid())
        finish_job(job_id, status="done")
        self.assertEqual(jobs.reap_lost(), [])
        self.assertEqual(get_job(job_id)["status"], "done")

    def test_no_proc_marks_nothing(self):
        from cousin_lib import jobs
        self._row_with(pid=self._dead_pid())
        with mock.patch("cousin_lib.jobs.os.path.isdir", return_value=False):
            self.assertEqual(jobs.reap_lost(), [])

    def test_list_marks_it_lost_and_shows_the_status(self):
        job_id = self._row_with(pid=self._dead_pid())
        rc, out, _ = self._main(["list"])
        self.assertEqual(rc, 0)
        line = next(l for l in out.splitlines() if l.strip().startswith(str(job_id)))
        self.assertIn("lost", line)

    def test_lost_is_a_status_a_row_can_be_filtered_and_set_to(self):
        from cousin_lib import jobs
        self.assertIn("lost", jobs.STATUSES)
        job_id = self._row_with(pid=self._dead_pid())
        jobs.reap_lost()
        self.assertEqual([j["id"] for j in list_jobs(status="lost")], [job_id])


class TestAZombieRunsNothing(JobsCase):
    """#244 re-review: a runner that exited but was not yet reaped (a
    zombie) still has /proc/<pid>/stat; it runs nothing, so its job is
    lost. Fails on the code before ad0ae6d (the zombie kept the job
    running)."""

    def test_an_unreaped_runner_makes_its_job_lost(self):
        import subprocess
        from cousin_lib import jobs
        job_id = register_job(kind="shell", title="exits, never reaped")
        proc = subprocess.Popen(["sleep", "0.3"], start_new_session=True)
        self.addCleanup(proc.wait)
        jobs.record_spawn(job_id, proc.pid)
        self.assertIsNotNone(get_job(job_id)["start_ticks"])
        deadline = time.time() + 5
        while time.time() < deadline:
            with open("/proc/%d/stat" % proc.pid) as fh:
                stat = fh.read()
            if stat[stat.rfind(")") + 2:].split()[0] == "Z":
                break
            time.sleep(0.05)
        else:
            self.fail("the child never became a zombie")
        self.assertEqual(jobs.reap_lost(), [job_id])


class TestCloseNotice(JobsCase):
    """#282: a job started with notify puts one row in its owner's inbox
    when it ends, so the cousin waiting on it does not poll."""

    def setUp(self):
        super().setUp()
        from cousin_lib import delivery
        self.sent = []
        for name, fake in (("deliver", lambda home, item, wait=True: self.sent.append(
                                (pathlib.Path(home).name, item)) or "ok"),
                           ("accepted", lambda result, home: True),
                           ("backend_for", lambda home: object())):
            p = mock.patch.object(delivery, name, side_effect=fake)
            p.start()
            self.addCleanup(p.stop)

    def test_a_notify_job_tells_its_owner_once_when_it_ends(self):
        job_id = register_job(kind="shell", title="build the image", notify=True,
                              log_path="/tmp/build.log")
        finish_job(job_id, status="failed", summary="disk full", exit_code=2)
        finish_job(job_id, status="failed", summary="again")      # a closed row: no second notice
        self.assertEqual(len(self.sent), 1)
        slug, item = self.sent[0]
        self.assertEqual(slug, "wren")
        self.assertEqual((item.source, item.thread_id), ("job", "system"))
        self.assertIn("job #%d failed (exit 2): build the image - disk full" % job_id, item.body)
        self.assertIn("log: /tmp/build.log", item.body)

    def test_a_job_without_notify_tells_nobody(self):
        job_id = register_job(kind="shell", title="quiet")
        finish_job(job_id, status="done")
        self.assertEqual(self.sent, [])

    def test_a_lost_notify_job_tells_its_owner(self):
        import subprocess
        from cousin_lib import jobs
        job_id = register_job(kind="shell", title="dies unclosed", notify=True)
        proc = subprocess.Popen(["true"]); proc.wait()
        conn = jobs._db()
        conn.execute("UPDATE jobs SET pid=? WHERE id=?", (proc.pid, job_id))
        conn.commit(); conn.close()
        self.assertEqual(jobs.reap_lost(), [job_id])
        self.assertEqual(len(self.sent), 1)
        self.assertIn("job #%d lost" % job_id, self.sent[0][1].body)

    def test_an_owner_with_no_home_is_skipped(self):
        job_id = register_job(kind="shell", title="orphan", spawned_by="nobody", notify=True)
        finish_job(job_id, status="done")
        self.assertEqual(self.sent, [])

    def test_the_cli_flag_sets_notify(self):
        rc, out, _ = self._main(["start", "shell", "--json", "--notify", "--", "flagged"])
        self.assertEqual(rc, 0, out)
        self.assertEqual(get_job(json.loads(out)["job_id"])["notify"], 1)
