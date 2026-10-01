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
                         ["--desc", "--home-log", "--json", "--log"])
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
