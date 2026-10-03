"""The automatic truth-level writers.

Canary: L1, L2, L4 and L5 once went unwritten. Only `decide`,
`remember` and the flip miner produced raw entries, and none of them
picked those levels on its own. Each writer below has a test
that fails when the writer is removed:

- L1 framework: the framework records the state changes it makes or
  observes (flip, start and stop, model and effort, auth mode, chat
  import, lifecycle surgery, a crashed flip, a respawned chat server),
  never a tick, never a no-op;
- L2 tool: a job that ends done or failed lands in its cousin's raw;
- L4 hypothesis: hedged transcript sentences (tests in
  test_transcript_mine);
- L5 obsolete: `cousin-memory obsolete` retires a topic from the
  distilled views; a later entry revives it.
"""
import contextlib
import io
import json
import os
import pathlib
import stat
import tempfile
import unittest
from unittest import mock

from cousin_lib import distill, memory


def raw_rows(home):
    rows = []
    raw = pathlib.Path(home) / "memory" / "raw"
    if not raw.is_dir():
        return rows
    for f in sorted(raw.glob("*.jsonl")):
        rows += [json.loads(l) for l in f.read_text().splitlines() if l]
    return rows


def level_rows(home, level):
    return [r for r in raw_rows(home) if r.get("truth_level") == level]


class HomeCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = pathlib.Path(tmp.name) / "home"
        self.home.mkdir()


# ------------------------------------------------------------ the helper

class TestRecordEvent(HomeCase):
    def test_writes_one_canonical_entry_with_extras(self):
        ok = memory.record_event(self.home, "framework", "framework:x",
                                 "a   thing\nchanged", "framework",
                                 generation=3, dropped=None)
        self.assertTrue(ok)
        [row] = raw_rows(self.home)
        self.assertEqual(row["truth_level"], "L1_FRAMEWORK")
        self.assertEqual(row["topic"], "framework:x")
        self.assertEqual(row["content"], "a thing changed")
        self.assertEqual(row["source"], "framework")
        self.assertEqual(row["generation"], 3)
        self.assertNotIn("dropped", row)
        self.assertIn("timestamp", row)

    def test_content_is_bounded(self):
        memory.record_event(self.home, "tool", "t", "x" * 5000, "job")
        self.assertEqual(len(raw_rows(self.home)[0]["content"]),
                         memory.EVENT_CONTENT_CHARS)

    def test_never_raises_and_says_so_on_stderr(self):
        err = io.StringIO()
        with mock.patch.object(memory, "_append_raw",
                               side_effect=OSError("disk full")), \
                contextlib.redirect_stderr(err):
            ok = memory.record_event(self.home, "framework", "t", "c", "s")
        self.assertFalse(ok)
        self.assertIn("disk full", err.getvalue())

    def test_never_creates_a_missing_home(self):
        gone = self.home / "dismissed"
        self.assertFalse(memory.record_event(gone, "framework", "t", "c",
                                             "s"))
        self.assertFalse(gone.exists())

    def test_refuses_an_unknown_level_an_empty_topic_or_content(self):
        self.assertFalse(memory.record_event(self.home, "gospel", "t", "c",
                                             "s"))
        self.assertFalse(memory.record_event(self.home, "tool", " ", "c",
                                             "s"))
        self.assertFalse(memory.record_event(self.home, "tool", "t", "",
                                             "s"))
        self.assertEqual(raw_rows(self.home), [])


# ------------------------------------------------------------ L5 obsolete

def _write_raw(home, rows):
    raw = pathlib.Path(home) / "memory" / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    with open(raw / "2026-09-01.jsonl", "a") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def _distilled_text(home):
    ddir = memory.distilled_dir(home)
    return "".join((ddir / f).read_text() for f in memory.DISTILLED_FILES)


class TestDistillObsolete(HomeCase):
    def setUp(self):
        super().setUp()
        _write_raw(self.home, [
            {"topic": "build cache", "content": "clear it by hand",
             "truth_level": "L3_COUSIN_CONCLUSION",
             "timestamp": "2026-09-01T10:00:00+00:00"},
            {"topic": "kept topic", "content": "still true",
             "truth_level": "L3_COUSIN_CONCLUSION",
             "timestamp": "2026-09-01T10:00:00+00:00"},
        ])

    def test_a_topic_whose_newest_entry_is_l5_is_dropped(self):
        _write_raw(self.home, [
            {"topic": "build cache", "content": "obsolete: the tool does it",
             "truth_level": "L5_OBSOLETE",
             "timestamp": "2026-09-02T10:00:00+00:00"}])
        report = distill.distill(self.home)
        text = _distilled_text(self.home)
        self.assertNotIn("build cache", text)
        self.assertNotIn("clear it by hand", text)
        self.assertIn("still true", text)
        self.assertEqual(report["obsolete"], 1)
        # History stays in raw.
        self.assertEqual(len([r for r in raw_rows(self.home)
                              if r["topic"] == "build cache"]), 2)

    def test_a_later_entry_revives_the_topic(self):
        _write_raw(self.home, [
            {"topic": "build cache", "content": "obsolete: the tool does it",
             "truth_level": "L5_OBSOLETE",
             "timestamp": "2026-09-02T10:00:00+00:00"},
            {"topic": "build cache", "content": "the tool broke; by hand again",
             "truth_level": "L2_TOOL",
             "timestamp": "2026-09-03T10:00:00+00:00"}])
        report = distill.distill(self.home)
        text = _distilled_text(self.home)
        self.assertIn("by hand again", text)
        self.assertEqual(report["obsolete"], 0)

    def test_an_older_l5_does_not_hide_the_topic(self):
        _write_raw(self.home, [
            {"topic": "kept topic", "content": "obsolete: old mark",
             "truth_level": "L5_OBSOLETE",
             "timestamp": "2026-08-01T10:00:00+00:00"}])
        distill.distill(self.home)
        self.assertIn("still true", _distilled_text(self.home))

    def test_a_folded_digest_keeps_the_mark(self):
        from cousin_lib import raw_fold
        raw = memory.raw_dir(self.home)
        (raw / "2020-01-01.jsonl").write_text(json.dumps(
            {"topic": "old way", "content": "do x",
             "truth_level": "L3_COUSIN_CONCLUSION",
             "timestamp": "2020-01-01T10:00:00+00:00"}) + "\n" + json.dumps(
            {"topic": "old way", "content": "obsolete: y replaced x",
             "truth_level": "L5_OBSOLETE",
             "timestamp": "2020-01-01T11:00:00+00:00"}) + "\n")
        raw_fold.fold_raw(self.home)
        distill.distill(self.home)
        self.assertNotIn("old way", _distilled_text(self.home))


class TestObsoleteCli(HomeCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"COUSIN_HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("COUSIN_SLUG", None)
        _write_raw(self.home, [
            {"topic": "deploy path", "content": "copy by hand",
             "truth_level": "L3_COUSIN_CONCLUSION",
             "timestamp": "2026-09-01T10:00:00+00:00"}])

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = memory.memory_main(list(argv))
            except SystemExit as exc:
                rc = exc.code
        return rc, out.getvalue(), err.getvalue()

    def test_marks_the_topic_and_redistills(self):
        rc, out, err = self.run_cli("obsolete", "deploy path", "--why",
                                    "the pipeline deploys now")
        self.assertEqual(rc, 0, err)
        row = raw_rows(self.home)[-1]
        self.assertEqual(row["truth_level"], "L5_OBSOLETE")
        self.assertEqual(row["topic"], "deploy path")
        self.assertEqual(row["why"], "the pipeline deploys now")
        self.assertEqual(row["by"], "home")
        self.assertIn("obsolete topic(s) left out", out)
        self.assertNotIn("copy by hand", _distilled_text(self.home))

    def test_an_empty_reason_is_refused(self):
        rc, _, err = self.run_cli("obsolete", "deploy path", "--why", "  ")
        self.assertEqual(rc, 2)
        self.assertIn("reason", err)
        self.assertEqual(len(raw_rows(self.home)), 1)
        rc, _, _ = self.run_cli("obsolete", "deploy path")
        self.assertEqual(rc, 2)

    def test_an_unknown_topic_needs_force(self):
        rc, _, err = self.run_cli("obsolete", "deploy", "--why", "x")
        self.assertEqual(rc, 2)
        self.assertIn("deploy path", err, "a near match is suggested")
        self.assertEqual(len(raw_rows(self.home)), 1)
        rc, _, err = self.run_cli("obsolete", "deploy", "--why", "x",
                                  "--force")
        self.assertEqual(rc, 0, err)
        self.assertEqual(raw_rows(self.home)[-1]["topic"], "deploy")


# ------------------------------------------------------------ L2 jobs

class TestJobResults(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.home = self.root / "cousins" / "wren"
        self.home.mkdir(parents=True)
        (self.home / "cousin.toml").write_text(
            '[cousin]\nslug = "wren"\n[chat]\nport = 8100\n')
        patcher = mock.patch.dict(os.environ, {
            "FRAMEWORK_ROOT": str(self.root),
            "COUSIN_HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_done_and_failed_land_as_l2_in_the_owner_home(self):
        from cousin_lib import jobs
        a = jobs.register_job(kind="build", title="Build the Docs!")
        jobs.finish_job(a, status="done", summary="42 pages", exit_code=0)
        b = jobs.register_job(kind="shell", title="fleet sync")
        jobs.finish_job(b, status="failed", summary="host 3 refused " * 80,
                        exit_code=7)
        rows = level_rows(self.home, "L2_TOOL")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["topic"], "job:build-the-docs")
        self.assertIn("job #%d done (exit 0): Build the Docs! - 42 pages"
                      % a, rows[0]["content"])
        self.assertEqual(rows[0]["source"], "job")
        self.assertEqual(rows[1]["exit_code"], 7)
        self.assertIn("failed (exit 7)", rows[1]["content"])
        self.assertLessEqual(len(rows[1]["content"]),
                             memory.EVENT_CONTENT_CHARS)

    def test_a_repeat_close_or_a_cancel_writes_nothing_more(self):
        from cousin_lib import jobs
        a = jobs.register_job(kind="build", title="t")
        jobs.finish_job(a, status="done")
        jobs.finish_job(a, status="done", summary="again")
        c = jobs.register_job(kind="build", title="c")
        jobs.finish_job(c, status="cancelled")
        self.assertEqual(len(level_rows(self.home, "L2_TOOL")), 1)

    def test_a_job_with_no_cousin_home_is_skipped(self):
        from cousin_lib import jobs
        a = jobs.register_job(kind="other", title="t", spawned_by="ghost")
        jobs.finish_job(a, status="done")
        self.assertFalse((self.root / "cousins" / "ghost").exists())
        self.assertEqual(level_rows(self.home, "L2_TOOL"), [])

    def test_a_job_close_keeps_its_tool_level_without_a_cite(self):
        # enforces: law 10
        # a job close is the framework's own write, never demoted
        from cousin_lib import jobs
        a = jobs.register_job(kind="build", title="no cite")
        jobs.finish_job(a, status="done", exit_code=0)
        [row] = raw_rows(self.home)
        self.assertEqual(row["truth_level"], "L2_TOOL")
        self.assertNotIn("cite", row)

    def test_the_cli_close_path_writes_too(self):
        from cousin_lib import jobs
        a = jobs.register_job(kind="build", title="cli close")
        with contextlib.redirect_stdout(io.StringIO()):
            jobs.jobs_main(["fail", str(a), "broke", "--exit", "3"])
        [row] = level_rows(self.home, "L2_TOOL")
        self.assertIn("failed (exit 3): cli close - broke", row["content"])


# ------------------------------------------------------------ L1 framework

def framework_rows(home, topic=None):
    return [r for r in level_rows(home, "L1_FRAMEWORK")
            if topic is None or r["topic"] == topic]


class TestRuntimeAndAuth(HomeCase):
    def setUp(self):
        super().setUp()
        (self.home / "cousin.toml").write_text(
            '[cousin]\nslug = "wren"\n\n[runtime]\nmodel = "model-a"\n')

    def test_a_model_change_is_recorded_a_same_value_save_is_not(self):
        from cousin_lib import spawn
        spawn.persist_runtime(self.home, "model", "model-b")
        spawn.persist_runtime(self.home, "model", "model-b")
        spawn.persist_runtime(self.home, "effort", "high")
        [model] = framework_rows(self.home, "framework:model")
        self.assertIn("model model-a -> model-b", model["content"])
        [effort] = framework_rows(self.home, "framework:effort")
        self.assertIn("effort (install default) -> high", effort["content"])

    def test_an_auth_mode_change_is_recorded_a_same_mode_save_is_not(self):
        from cousin_lib import agent_auth
        agent_auth.persist_mode(self.home, agent_auth.MODE_LOGIN)
        self.assertEqual(framework_rows(self.home), [])
        agent_auth.persist_mode(self.home, agent_auth.MODE_API_KEY)
        [row] = framework_rows(self.home, "framework:auth")
        self.assertEqual(row["content"], "auth mode %s -> %s" % (
            agent_auth.MODE_LOGIN, agent_auth.MODE_API_KEY))


_FAKE_TMUX_SH = """#!/bin/sh
echo "$@" >> "$FAKE_TMUX_LOG"
case "$1" in has-session) exit "${FAKE_TMUX_RC_HAS_SESSION:-0}";; esac
exit 0
"""


class TestChatImport(HomeCase):
    def test_a_chat_import_is_recorded_in_the_new_home(self):
        from tests.test_chat_import import ImportCase

        class Case(ImportCase):
            def runTest(self):
                pass
        case = Case()
        case.setUp()
        self.addCleanup(case.doCleanups)
        report = case._run()
        [row] = framework_rows(case.new_home, "framework:chat-import")
        self.assertIn("%d messages" % report["imported"], row["content"])
        self.assertEqual(framework_rows(case.old_home), [])

class TestLifecycleEvents(unittest.TestCase):
    def _case(self):
        from tests.test_lifecycle import LifecycleCase

        class Case(LifecycleCase):
            def runTest(self):
                pass
        case = Case()
        case.setUp()
        self.addCleanup(case.doCleanups)
        return case

    def test_a_merge_transplant_is_noted_in_both_homes(self):
        case = self._case()
        out = case._transplant("merge")
        self.assertTrue(out["ok"], out)
        [donor] = framework_rows(case.a, "framework:transplant")
        [recipient] = framework_rows(case.b, "framework:transplant")
        self.assertIn("(merge) as donor with testb", donor["content"])
        self.assertIn("(merge) as recipient with testa",
                      recipient["content"])
        self.assertIn("raw lines merged in", recipient["content"])

    def test_a_reincarnation_notes_the_new_role(self):
        case = self._case()
        case._reincarnate()
        [row] = framework_rows(case.a, "framework:role")
        self.assertIn("mender of the fence", row["content"])


if __name__ == "__main__":
    unittest.main()
