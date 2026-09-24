"""The in-process tool transport: one handler per registry command,
reply as the only chat.db writer, no subprocess anywhere."""
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from cousin_lib import mcp_server
from cousin_lib.runner import tools
from cousin_lib.runner.policy import Policy
from cousin_lib.runner.turn import Turn
from tests._hermetic import HermeticCase


# The shipped registry carries no tracker; docs/mcp.md shows how an
# install adds one. This is that table with every command the CLI has.
TRACKER_TOML = '''
[tools.tracker]
command = "cousin-tracker"
description = "The install's list of work in flight."

[tools.tracker.properties]
title = { type = "string" }
domain = { type = "string", optional = true }
tag = { type = "array", items = "string", optional = true }
id = { type = "integer" }
state = { type = "string", enum = ["open", "active", "blocked", "done", "dropped"] }
notes = { type = "string", optional = true }

[tools.tracker.commands.add]
argv = ["add", "{title}", "--json"]
options = { domain = "--domain", tag = "--tag" }

[tools.tracker.commands.update]
argv = ["update", "{id}"]
options = { title = "--title", domain = "--domain", state = "--state", notes = "--notes" }

[tools.tracker.commands.state]
argv = ["state", "{id}", "{state}"]

[tools.tracker.commands.list]
argv = ["list"]
options = { domain = "--domain", state = "--state" }

[tools.tracker.commands.show]
argv = ["show", "{id}"]

[tools.tracker.commands.delete]
argv = ["delete", "{id}"]
'''


def _install(case):
    """A framework root with one cousin (wren) and one peer (testa)."""
    tmp = tempfile.TemporaryDirectory(); case.addCleanup(tmp.cleanup)
    root = pathlib.Path(tmp.name); (root / "config").mkdir()
    for slug, port in (("wren", 8100), ("testa", 8101)):
        home = root / "cousins" / slug
        (home / "data").mkdir(parents=True); (home / "memory").mkdir(); (home / "chat").mkdir()
        (home / "cousin.toml").write_text(
            '[cousin]\nslug = "%s"\nname = "%s"\nchat_port = %d\n\n[operator]\nname = "Priya"\n'
            '\n[agent]\nrunner = "fake"\n' % (slug, slug.capitalize(), port))
    os.environ["FRAMEWORK_ROOT"] = str(root)
    os.environ["COUSIN_HOME"] = str(root / "cousins" / "wren")
    return root, root / "cousins" / "wren"


def _ctx(case, turn=None, policy=None):
    root, home = _install(case)
    # No policy.toml is written in _install: Policy.load(home) is then
    # the permissive default, same as the old stand-in.
    return tools.ToolContext(home=home, slug="wren", name="Wren", root=root,
                             turn=turn or Turn(),
                             policy=policy or Policy.load(home),
                             stream=None)


def _registry(root):
    # The temporary root carries no config/mcp-registry.toml[.example], so
    # default_registry_path finds nothing there; shipped_default_registry
    # falls back to the checkout's example, the registry every cousin gets.
    return mcp_server.parse_registry(mcp_server.shipped_default_registry(root), "shipped")


def _registry_with_tracker(root):
    text = mcp_server.shipped_default_registry(root) + TRACKER_TOML
    return mcp_server.parse_registry(text, "shipped + tracker")


class TestDefinitions(HermeticCase):
    def test_every_registry_command_has_a_handler(self):
        root, _ = _install(self)
        self.assertEqual(tools.missing_handlers(_registry(root)), [])
        self.assertEqual(tools.missing_handlers(_registry_with_tracker(root)), [])

    def test_definitions_are_the_registry_plus_reply_and_handoff(self):
        root, _ = _install(self)
        names = [d["name"] for d in tools.tool_definitions(_registry_with_tracker(root))]
        for n in ("memory", "send", "job", "schedule", "meeting", "tracker", "reply", "handoff"):
            self.assertIn(n, names)
        reply = next(d for d in tools.tool_definitions(_registry(root)) if d["name"] == "reply")
        self.assertEqual(reply["inputSchema"]["required"], ["text"])


class TestCallNeverSpawns(HermeticCase):
    def test_memory_and_schedule_calls_run_in_process(self):
        ctx = _ctx(self)
        with mock.patch.object(subprocess, "run") as run, mock.patch.object(subprocess, "Popen") as popen:
            text, err = tools.call(ctx, "memory", {"command": "decide", "topic": "t",
                                                   "decision": "d", "reasoning": "w"})
            self.assertFalse(err, text); self.assertIn("Decision logged", text)
            text, err = tools.call(ctx, "memory", {"command": "recall", "keyword": "t", "last": 5})
            self.assertFalse(err); self.assertIn("t: d", text)
            text, err = tools.call(ctx, "schedule", {"command": "add", "when": "in 30m", "prompt": "p"})
            self.assertFalse(err, text); self.assertIn("scheduled #", text)
            text, err = tools.call(ctx, "tracker", {"command": "add", "title": "x", "domain": "d"})
            self.assertFalse(err, text)
            text, err = tools.call(ctx, "job", {"command": "start", "kind": "other", "title": "j"})
            self.assertFalse(err, text)
        self.assertEqual(run.call_count + popen.call_count, 0)

    def test_an_unknown_command_and_a_bad_enum_are_tool_errors(self):
        ctx = _ctx(self)
        text, err = tools.call(ctx, "memory", {"command": "levitate"})
        self.assertTrue(err); self.assertIn("unknown command", text)
        text, err = tools.call(ctx, "memory", {"command": "remember", "topic": "t", "fact": "f",
                                               "level": "gossip"})
        self.assertTrue(err); self.assertIn("level", text)

    def test_a_raising_handler_is_a_tool_error_not_an_exception(self):
        ctx = _ctx(self)
        with mock.patch.dict(tools.HANDLERS["memory"], {"activity": lambda c, a: 1 / 0}):
            text, err = tools.call(ctx, "memory", {"command": "activity", "text": "x"})
        self.assertTrue(err); self.assertIn("ZeroDivisionError", text)


class TestReply(HermeticCase):
    def _rows(self, home):
        # A refused reply never opens the store: no chat.db is no rows.
        if not (home / "data" / "chat.db").exists():
            return []
        conn = sqlite3.connect(home / "data" / "chat.db")
        try:
            return conn.execute("SELECT chat_user, user, message, type, reply_to_user"
                                " FROM messages ORDER BY id").fetchall()
        finally:
            conn.close()

    def test_reply_with_one_live_thread_writes_the_row_for_that_thread(self):
        turn = Turn(); turn.begin({"id": 1, "thread_id": "operator:priya", "sender": "Priya"})
        ctx = _ctx(self, turn)
        out = tools.reply(ctx, "hello")
        self.assertIn("replied to priya", out)
        rows = self._rows(ctx.home)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0][0], rows[0][1], rows[0][2], rows[0][3]), ("priya", "Wren", "hello", "wren"))

    def test_reply_without_thread_is_refused_when_two_threads_are_live(self):
        turn = Turn(); turn.begin({"id": 1, "thread_id": "operator:priya", "sender": "Priya"})
        turn.add({"id": 2, "thread_id": "person:sam", "sender": "Sam"})
        ctx = _ctx(self, turn)
        text, err = tools.call(ctx, "reply", {"text": "hi"})
        self.assertTrue(err); self.assertIn("operator:priya", text); self.assertIn("person:sam", text)
        self.assertEqual(self._rows(ctx.home), [])
        text, err = tools.call(ctx, "reply", {"text": "hi", "thread": "person:sam"})
        self.assertFalse(err, text)
        self.assertEqual(self._rows(ctx.home)[0][0], "sam")

    def test_a_folded_peer_is_no_reply_candidate(self):
        """#118: operator:priya with peer:testa folded in. The peer thread
        is refused by name; unnamed, the reply goes to the operator."""
        turn = Turn(); turn.begin({"id": 1, "thread_id": "operator:priya", "sender": "Priya"})
        turn.add({"id": 2, "thread_id": "peer:testa", "sender": "Testa"})
        ctx = _ctx(self, turn)
        text, err = tools.call(ctx, "reply", {"text": "hi", "thread": "peer:testa"})
        self.assertTrue(err); self.assertIn("send", text)
        self.assertEqual(self._rows(ctx.home), [])
        text, err = tools.call(ctx, "reply", {"text": "hi"})
        self.assertFalse(err, text)
        self.assertEqual([(r[0], r[2]) for r in self._rows(ctx.home)], [("priya", "hi")])

    def test_a_peer_turn_with_another_peer_folded_still_refuses_an_unnamed_reply(self):
        turn = Turn(); turn.begin({"id": 1, "thread_id": "peer:testa", "sender": "Testa"})
        turn.add({"id": 2, "thread_id": "peer:sam", "sender": "Sam"})
        text, err = tools.call(_ctx(self, turn), "reply", {"text": "hi"})
        self.assertTrue(err); self.assertIn("send", text)

    def test_reply_outside_a_turn_is_refused(self):
        ctx = _ctx(self)
        text, err = tools.call(ctx, "reply", {"text": "hi"})
        self.assertTrue(err); self.assertIn("no turn is live", text)

    def test_reply_to_a_peer_thread_is_refused_with_the_send_hint(self):
        turn = Turn(); turn.begin({"id": 1, "thread_id": "peer:testa", "sender": "Testa"})
        text, err = tools.call(_ctx(self, turn), "reply", {"text": "hi"})
        self.assertTrue(err); self.assertIn("send", text)

    def test_reply_crosses_the_outbound_filter(self):
        turn = Turn(); turn.begin({"id": 1, "thread_id": "operator:priya", "sender": "Priya"})
        ctx = _ctx(self, turn)
        (ctx.root / "config" / "outbound-filter.json").write_text(json.dumps(
            {"protected": ["mallory"], "terms": []}))
        text, err = tools.call(ctx, "reply", {"text": "ask mallory"})
        self.assertTrue(err); self.assertEqual(self._rows(ctx.home), [])

    def test_send_to_an_operator_is_a_reply_and_to_a_peer_is_a_post(self):
        turn = Turn(); turn.begin({"id": 1, "thread_id": "operator:priya", "sender": "Priya"})
        ctx = _ctx(self, turn)
        text, err = tools.call(ctx, "send", {"to": "Priya", "text": "via send"})
        self.assertFalse(err, text); self.assertEqual(self._rows(ctx.home)[0][2], "via send")
        with mock.patch("cousin_lib.chat.send_message", return_value={"ok": True}) as sm:
            text, err = tools.call(ctx, "send", {"to": "testa", "text": "peer"})
        self.assertFalse(err, text); self.assertEqual(sm.call_args.args[2], "testa")
        text, err = tools.call(ctx, "send", {"to": "nobody", "text": "x"})
        self.assertTrue(err); self.assertIn("testa", text); self.assertIn("Priya", text)


class TestNamedThread(HermeticCase):
    """An explicitly named operator: or person: thread is always accepted;
    the live-thread rule binds only the implicit default (ruling P12)."""

    _rows = TestReply._rows

    def test_a_schedule_turn_reaches_the_operator_by_name(self):
        turn = Turn(); turn.begin({"id": 1, "thread_id": "schedule", "sender": ""})
        ctx = _ctx(self, turn)
        out = tools.reply(ctx, "the timer fired", thread="operator:priya")
        self.assertIn("replied to priya", out)
        text, err = tools.call(ctx, "send", {"to": "Priya", "text": "via send"})
        self.assertFalse(err, text)
        rows = self._rows(ctx.home)
        self.assertEqual([(r[0], r[2]) for r in rows],
                         [("priya", "the timer fired"), ("priya", "via send")])

    def test_a_peer_turn_reaches_a_person_by_name(self):
        turn = Turn(); turn.begin({"id": 1, "thread_id": "peer:testa", "sender": "Testa"})
        ctx = _ctx(self, turn)
        text, err = tools.call(ctx, "reply", {"text": "hi sam", "thread": "person:sam"})
        self.assertFalse(err, text)
        self.assertEqual(self._rows(ctx.home)[0][0], "sam")

    def test_a_named_peer_thread_stays_refused(self):
        turn = Turn(); turn.begin({"id": 1, "thread_id": "operator:priya", "sender": "Priya"})
        ctx = _ctx(self, turn)
        text, err = tools.call(ctx, "reply", {"text": "hi", "thread": "peer:testa"})
        self.assertTrue(err); self.assertIn("send", text)
        self.assertEqual(self._rows(ctx.home), [])

    @unittest.skipIf(os.geteuid() == 0, "root ignores directory modes")
    def test_an_unopenable_store_leaves_no_staged_attachment(self):
        turn = Turn(); turn.begin({"id": 1, "thread_id": "operator:priya", "sender": "Priya"})
        ctx = _ctx(self, turn)
        picture = ctx.root / "render.png"; picture.write_bytes(b"\x89PNG\r\n")
        data = ctx.home / "data"
        data.chmod(0o500)
        self.addCleanup(data.chmod, 0o700)
        text, err = tools.call(ctx, "reply", {"text": "look", "image": str(picture)})
        self.assertTrue(err, text)
        images = ctx.home / "chat" / "images"
        self.assertEqual(sorted(images.iterdir()) if images.exists() else [], [])


class TestRegistryGatedDispatch(HermeticCase):
    """What production runs: build_tool_server always sets ctx.registry."""

    def _gated(self, turn=None):
        ctx = _ctx(self, turn)
        ctx.registry = _registry_with_tracker(ctx.root)
        return ctx

    def test_a_disabled_tool_is_refused(self):
        ctx = self._gated()
        ctx.registry["tools"]["tracker"]["enabled"] = False
        text, err = tools.call(ctx, "tracker", {"command": "list"})
        self.assertTrue(err); self.assertIn("unknown tool", text)

    def test_an_unknown_command_lists_the_registrys_commands(self):
        ctx = self._gated()
        del ctx.registry["tools"]["memory"]["commands"]["obsolete"]
        text, err = tools.call(ctx, "memory", {"command": "obsolete", "topic": "t", "why": "w"})
        self.assertTrue(err); self.assertIn("unknown command", text)
        self.assertIn("activity, decide, recall, remember, search", text)

    def test_check_enums_rejects_a_bad_value_on_a_command_and_on_send(self):
        ctx = self._gated()
        text, err = tools.call(ctx, "job", {"command": "start", "kind": "shell", "title": "x"})
        self.assertTrue(err); self.assertIn("ToolError", text); self.assertIn("not one of", text)
        ctx.registry["tools"]["send"]["properties"]["to"]["enum"] = ["testa", "Priya"]
        text, err = tools.call(ctx, "send", {"to": "nobody", "text": "x"})
        self.assertTrue(err); self.assertIn("ToolError", text); self.assertIn("not one of", text)

    def test_a_call_appends_a_tool_call_event(self):
        from cousin_lib.runner.stream import EventStream
        ctx = self._gated()
        ctx.stream = EventStream(ctx.home, "sess-tools")
        text, err = tools.call(ctx, "memory", {"command": "activity", "text": "x"})
        self.assertFalse(err, text)
        event = list(ctx.stream.tail())[-1]
        self.assertEqual(event["kind"], "tool_call")
        self.assertEqual({k: event["payload"][k] for k in ("tool", "command", "is_error")},
                         {"tool": "memory", "command": "activity", "is_error": False})
        self.assertIsInstance(event["payload"]["ms"], int)

    def test_the_sdk_handler_runs_the_call_in_process(self):
        try:
            import claude_agent_sdk
        except ImportError:
            self.skipTest("claude-agent-sdk not installed")
        import asyncio
        ctx = _ctx(self)
        with mock.patch("claude_agent_sdk.create_sdk_mcp_server",
                        wraps=claude_agent_sdk.create_sdk_mcp_server) as create:
            tools.build_tool_server(ctx, _registry(ctx.root))
        memory = next(t for t in create.call_args.kwargs["tools"] if t.name == "memory")
        with mock.patch.object(subprocess, "run") as run, mock.patch.object(subprocess, "Popen") as popen:
            result = asyncio.run(memory.handler({"command": "activity", "text": "x"}))
        self.assertEqual(run.call_count + popen.call_count, 0)
        self.assertEqual(set(result), {"content", "is_error"})
        self.assertIs(result["is_error"], False)
        self.assertEqual(len(result["content"]), 1)
        self.assertEqual(result["content"][0]["type"], "text")
        self.assertIn("Activity saved: x", result["content"][0]["text"])


class TestHandoffAndMeeting(HermeticCase):
    def test_meeting_say_out_of_turn_is_refused(self):
        ctx = _ctx(self)
        from cousin_lib import meetings
        m = meetings.open_meeting("pick a name", ["wren", "testa"], created_by="Priya",
                                  is_alive=lambda s: True, deliver=lambda s, t: True)
        # It is the user's floor: a cousin speaking now is out of turn.
        text, err = tools.call(ctx, "meeting", {"command": "say", "id": m["id"], "text": "me first"})
        self.assertTrue(err); self.assertIn("turn", text.lower())


class TestServerBuild(HermeticCase):
    def test_build_tool_server_refuses_a_registry_command_without_a_handler(self):
        ctx = _ctx(self)
        reg = _registry(ctx.root)
        with mock.patch.dict(tools.HANDLERS["memory"]):
            del tools.HANDLERS["memory"]["recall"]
            from cousin_lib.runner.base import RunnerError
            with self.assertRaises(RunnerError) as cm:
                tools.build_tool_server(ctx, reg)
        self.assertIn("memory.recall", str(cm.exception))

    def test_build_tool_server_names_the_server_cousin(self):
        try:
            import claude_agent_sdk  # noqa: F401
        except ImportError:
            self.skipTest("claude-agent-sdk not installed")
        ctx = _ctx(self)
        cfg = tools.build_tool_server(ctx, _registry(ctx.root))
        self.assertEqual(cfg["type"], "sdk"); self.assertEqual(cfg["name"], "cousin")


class TestJobRun(HermeticCase):
    """`job run`: the tool form of `cousin-job start shell TITLE -- CMD`. A
    real short command in a scratch root: the row runs, then closes with
    the command's own exit code, and the log holds its output."""

    def _ctx(self):
        ctx = _ctx(self)
        ctx.registry = _registry(ctx.root)
        return ctx

    def _run(self, ctx, **args):
        text, err = tools.call(ctx, "job", dict({"command": "run"}, **args))
        self.assertFalse(err, text)
        return json.loads(text)

    def _wait(self, job_id, timeout=20):
        """The closed row, once the job's detached runner has also exited:
        it goes on writing (the row, then a raw memory entry) after the
        status flips, and the scratch root is removed at cleanup."""
        from cousin_lib import jobs
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = jobs.get_job(job_id)
            if job["status"] != "running" and not jobs.live_members(job):
                return jobs.get_job(job_id)
            time.sleep(0.05)
        self.fail("job #%d still running after %ss" % (job_id, timeout))

    def test_a_failing_command_runs_then_closes_failed_with_its_exit_code(self):
        from cousin_lib import jobs
        ctx = self._ctx()
        # The command holds until the test says go (a file in its working
        # directory, the home), so "running" is observed, not raced.
        code = ("import os, time\nprint('hello from run', flush=True)\n"
                "deadline = time.time() + 20\n"
                "while not os.path.exists('go') and time.time() < deadline:\n"
                "    time.sleep(0.02)\nraise SystemExit(3)")
        out = self._run(ctx, title="exit three", argv=[sys.executable, "-c", code])
        job = jobs.get_job(out["job_id"])
        self.assertEqual((job["status"], job["kind"], job["spawned_by"]), ("running", "shell", "wren"))
        self.assertEqual(job["log_path"], out["log_path"])
        self.assertTrue(job["pgid"])
        (ctx.home / "go").write_text("")
        job = self._wait(out["job_id"])
        self.assertEqual((job["status"], job["exit_code"]), ("failed", 3))
        self.assertIn("hello from run", pathlib.Path(out["log_path"]).read_text())
        self.assertTrue(jobs.is_minted_log(out["log_path"]))

    def test_a_succeeding_command_closes_done(self):
        ctx = self._ctx()
        out = self._run(ctx, title="ok", desc="a check", argv=[sys.executable, "-c", "print('fine')"])
        job = self._wait(out["job_id"])
        self.assertEqual((job["status"], job["exit_code"], job["description"]), ("done", 0, "a check"))

    def test_run_returns_before_the_command_finishes(self):
        ctx = self._ctx()
        t0 = time.monotonic()
        out = self._run(ctx, title="sleeper", argv=[sys.executable, "-c", "import time; time.sleep(30)"])
        self.assertLess(time.monotonic() - t0, 10)
        text, err = tools.call(ctx, "job", {"command": "fail", "id": out["job_id"], "summary": "test over"})
        self.assertFalse(err, text)
        self.assertEqual(self._wait(out["job_id"])["status"], "failed")

    def test_the_command_runs_from_the_home_and_argv_is_never_a_shell(self):
        ctx = self._ctx()
        code = "import os, sys; print('cwd=' + os.getcwd()); print('arg=' + sys.argv[1])"
        out = self._run(ctx, title="where", argv=[sys.executable, "-c", code, "$(echo no) `x` ;"])
        self._wait(out["job_id"])
        log = pathlib.Path(out["log_path"]).read_text()
        self.assertIn("cwd=%s" % os.path.realpath(ctx.home), log)
        self.assertIn("arg=$(echo no) `x` ;", log)

    def test_a_relative_log_lands_under_the_home(self):
        ctx = self._ctx()
        out = self._run(ctx, title="logged", log="data/my-run.log",
                        argv=[sys.executable, "-c", "print('into my log')"])
        self.assertEqual(out["log_path"], os.path.realpath(ctx.home / "data" / "my-run.log"))
        self._wait(out["job_id"])
        self.assertIn("into my log", (ctx.home / "data" / "my-run.log").read_text())

    def test_a_log_outside_the_home_or_in_secrets_is_refused_and_no_row_is_made(self):
        from cousin_lib import jobs
        ctx = self._ctx()
        outside = ctx.root / "escape.log"
        for log in (str(outside), "../escape.log", "data/../../escape.log",
                    "~/escape.log", ".secrets/x.log", "data/.secrets/x.log"):
            text, err = tools.call(ctx, "job", {"command": "run", "title": "t", "log": log,
                                                "argv": [sys.executable, "-c", "print(1)"]})
            self.assertTrue(err, log)
            self.assertIn("log", text, log)
        self.assertEqual(jobs.list_jobs(), [])
        self.assertFalse(outside.exists())
        self.assertFalse((ctx.root / "cousins" / "escape.log").exists())

    def test_a_title_that_looks_like_an_option_is_only_a_title(self):
        ctx = self._ctx()
        for title in ("--", "-x", "--json"):
            out = self._run(ctx, title=title, argv=[sys.executable, "-c", "print('titled')"])
            job = self._wait(out["job_id"])
            self.assertEqual((job["title"], job["status"]), (title, "done"), title)
            self.assertEqual(job["command"], "%s -c print('titled')" % sys.executable)

    def test_a_launcher_answer_without_a_job_id_is_an_error_naming_it(self):
        ctx = self._ctx()
        for stdout in ("[3]", "not json", '{"log_path": "x"}', "7"):
            done = subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")
            with mock.patch.object(subprocess, "run", return_value=done):
                text, err = tools.call(ctx, "job", {"command": "run", "title": "t",
                                                    "argv": ["true"]})
            self.assertTrue(err, stdout)
            self.assertIn(stdout, text)

    def test_the_launch_handshake_is_bounded_tightly(self):
        self.assertLessEqual(tools._LAUNCH_TIMEOUT, 15)

    def test_empty_or_invalid_argv_and_a_missing_title_are_refused(self):
        from cousin_lib import jobs
        ctx = self._ctx()
        for args in ({"title": "t", "argv": []},
                     {"title": "t"},
                     {"title": "t", "argv": "echo hi"},
                     {"title": "t", "argv": ["echo", 3]},
                     {"title": "t", "argv": ["", "x"]},
                     {"title": "t", "argv": ["--desc", "x"]},
                     {"title": "t", "argv": ["--log=/tmp/escape.log", "x"]},
                     {"title": "t", "argv": ["--lo=/tmp/escape.log", "x"]},
                     {"title": "t", "argv": ["--desc=x", "x"]},
                     {"title": "t", "argv": ["--json", "x"]},
                     {"argv": ["echo", "hi"]}):
            text, err = tools.call(ctx, "job", dict({"command": "run"}, **args))
            self.assertTrue(err, args)
            self.assertRegex(text, "argv|title", args)
        self.assertEqual(jobs.list_jobs(), [])


if __name__ == "__main__":
    unittest.main()
