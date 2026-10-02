"""Restart with resume: stop and start keep the session; a lost resume is loud."""
import json
import os
import time
import unittest
from unittest import mock

try:
    import claude_agent_sdk  # noqa: F401
except ImportError:
    raise unittest.SkipTest("claude-agent-sdk not installed")

from cousin_lib import accounts, boot
from cousin_lib.delivery import Item
from cousin_lib.runner.sdk import SdkRunner
from tests._hermetic import HermeticCase
from tests.runner._home import temp_home
from tests.runner.test_sdk import ScriptedClient, assistant, init_msg, result


def _wait(pred, timeout=10.0):
    t = time.monotonic()
    while time.monotonic() - t < timeout:
        if pred():
            return True
        time.sleep(0.02)
    return False


class TestResume(HermeticCase):
    def setUp(self):
        super().setUp()
        self.home = temp_home(self)
        root = self.home.parent.parent
        (root / "config").mkdir(exist_ok=True)
        (root / "config" / "law.md").write_text("1. The law.\n")
        p = mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": "/nonexistent/framework-root"})
        p.start(); self.addCleanup(p.stop)
        self.options = []

    def runner(self, *, source="none", refuse_resume=False, resume_as=None, account=None):
        def factory(options):
            self.options.append(options)
            asked = options.resume or (options.extra_args or {}).get("resume")
            sid = (resume_as or "s-live") if asked else "s-live"
            client = ScriptedClient(options, [[init_msg(source=source, session=sid),
                                               assistant(text="ok"), result(session=sid)]
                                              for _ in range(4)])
            if refuse_resume and asked:
                async def boom(prompt=None):
                    raise RuntimeError("no such session")
                client.connect = boom
            return client
        r = SdkRunner(self.home, client_factory=factory, account=account)
        self.addCleanup(lambda: r.stop(timeout=5))
        return r

    def one_turn(self, r, body="hi"):
        rec = r.enqueue(Item("operator:priya", "chat", body, sender="Priya"))
        # the runner appends a turn's result, then closes its rows, runs its
        # post-turn work (the session file, usage, mining, the proposal), then goes
        # idle
        self.assertTrue(_wait(lambda: r.inbox.get(rec.inbox_id)["state"] == "done"
                              and r.state() == "idle"))

    def inits(self, r):
        return [e["payload"]["session_id"] for e in r.events() if e["kind"] == "session_init"]

    def bodies(self, r):
        return [(r.inbox.get(i) or {}).get("body") or "" for i in range(1, 30)]

    def session_file(self):
        return json.loads((self.home / "data" / "runner-session.json").read_text())

    def test_stop_and_start_keep_the_session_id(self):
        g0 = boot.read_generation(self.home)
        r1 = self.runner(); r1.start(); self.one_turn(r1); r1.stop(timeout=5)
        saved = json.loads((self.home / "data" / "runner-session.json").read_text())
        self.assertEqual(saved["session_id"], "s-live")
        r2 = self.runner(); r2.start(); self.one_turn(r2)
        self.assertEqual(self.inits(r2)[0], "s-live")          # the first init names the saved id
        self.assertEqual(r2.saved_session(), "s-live")
        kinds = [(e["kind"], e["payload"].get("subtype")) for e in r2.events()]
        self.assertIn(("system", "resumed"), kinds)
        self.assertNotIn(("system", "resume_failed"), kinds)
        self.assertEqual(boot.read_generation(self.home), g0)   # a deploy costs no generation

    def test_a_fresh_start_records_the_generation_start_and_a_resume_keeps_it(self):
        # the daily flip leaves a session younger than the day's flip
        # point alone: a first boot is a generation start, a resume is not
        before = time.time()
        r1 = self.runner(); r1.start(); self.one_turn(r1)
        self.assertGreaterEqual(boot.generation_started(self.home) or 0, before)
        r1.stop(timeout=5)
        boot.mark_generation_start(self.home, now=1000.0)
        r2 = self.runner(); r2.start(); self.one_turn(r2)
        self.assertEqual(self.inits(r2)[0], "s-live")              # resumed
        self.assertEqual(boot.generation_started(self.home), 1000.0)

    def test_the_key_lane_resumes_from_the_store(self):
        # the account's kind decides the resume path
        key = accounts.Account("metered", "anthropic-key", None, None, secret_value="k-test")
        r1 = self.runner(source="ANTHROPIC_API_KEY", account=key)
        r1.start(); self.one_turn(r1); r1.stop(timeout=5)
        self.assertEqual(json.loads((self.home / "data" / "runner-session.json").read_text())["lane"],
                         "key")
        r2 = self.runner(source="ANTHROPIC_API_KEY", account=key); r2.start()
        self.assertTrue(_wait(lambda: len(self.options) == 2))
        self.assertEqual(self.options[1].resume, "s-live")
        self.assertNotIn("resume", self.options[1].extra_args or {})

    def test_the_account_kind_decides_the_resume_path_whatever_the_init_says(self):
        # a key account whose init reports "none" still resumes store-backed;
        # the init's source is the CHECK that the account took effect
        key = accounts.Account("metered", "anthropic-key", None, None, secret_value="k-test")
        r1 = self.runner(account=key); r1.start(); self.one_turn(r1); r1.stop(timeout=5)
        self.assertEqual(self.session_file()["lane"], "login")     # the record, not the decider
        r2 = self.runner(account=key); r2.start()
        self.assertTrue(_wait(lambda: len(self.options) == 2))
        self.assertEqual(self.options[1].resume, "s-live")
        self.assertNotIn("resume", self.options[1].extra_args or {})

    def test_the_login_lane_resumes_through_the_clis_own_flag(self):
        r1 = self.runner(); r1.start(); self.one_turn(r1); r1.stop(timeout=5)
        r2 = self.runner(); r2.start()
        self.assertTrue(_wait(lambda: len(self.options) == 2))
        self.assertIsNone(self.options[1].resume)                   # no store materialization
        self.assertEqual(self.options[1].extra_args["resume"], "s-live")
        self.assertIsNotNone(self.options[1].session_store)         # the store still mirrors

    def test_a_restart_runs_no_start_hooks_and_no_digest(self):
        with open(self.home / "cousin.toml", "a") as fh:
            fh.write('\n[session]\nstart_hooks = ["echo start >> %s/hooks.log"]\n' % self.home)
        (self.home / "STATUS.md").write_text("## Open loops\n- x\n")
        r1 = self.runner(); r1.start(); self.one_turn(r1); r1.stop(timeout=5)
        r2 = self.runner(); r2.start(); self.one_turn(r2)
        self.assertEqual((self.home / "hooks.log").read_text().split(), ["start"])  # r1 only
        self.assertEqual(sum("STATE DIGEST" in b for b in self.bodies(r2)), 1)     # r1's only

    def test_a_connect_that_refuses_the_resume_starts_fresh_with_the_digest(self):
        (self.home / "data" / "runner-session.json").write_text(
            json.dumps({"session_id": "s-gone", "generation": 0, "updated": 0}))
        (self.home / "STATUS.md").write_text("## Open loops\n- carry this\n")
        r = self.runner(refuse_resume=True); r.start()
        self.assertTrue(_wait(lambda: any("carry this" in b for b in self.bodies(r))))
        kinds = [(e["kind"], e["payload"].get("subtype")) for e in r.events()]
        self.assertIn(("system", "resume_failed"), kinds)
        self.assertIsNone(r.fatal)

    def test_a_resume_that_comes_back_as_another_session_is_a_lost_resume(self):
        (self.home / "data" / "runner-session.json").write_text(
            json.dumps({"session_id": "s-live", "generation": 0, "updated": 0}))
        (self.home / "STATUS.md").write_text("## Open loops\n- carry this too\n")
        r = self.runner(resume_as="s-other"); r.start(); self.one_turn(r)
        failed = [e["payload"] for e in r.events()
                  if e["kind"] == "system" and e["payload"].get("subtype") == "resume_failed"]
        self.assertEqual((failed[0]["session_id"], failed[0]["got"]), ("s-live", "s-other"))
        self.assertTrue(_wait(lambda: any("carry this too" in b for b in self.bodies(r))))
        self.assertTrue(_wait(lambda: r.saved_session() == "s-other"))   # after the fresh start
        digest = next(i for i in range(1, 30)
                      if "carry this too" in ((r.inbox.get(i) or {}).get("body") or ""))
        self.assertTrue(_wait(lambda: r.inbox.get(digest)["outcome"] == "delivered"))
        system = [e["payload"] for e in r.events() if e["kind"] == "system"]
        at = [p.get("subtype") for p in system].index("resume_failed")
        fresh = [p for p in system if p.get("subtype") == "fresh"]
        self.assertEqual(fresh, [{"subtype": "fresh", "digest": True}])    # exactly one
        self.assertIn(fresh[0], system[at + 1:])                           # after the failure

    def test_a_lane_change_on_the_same_session_rewrites_the_file(self):
        # the file says key, the CLI's init says login: the lane on file is a
        # record, rewritten to what the init said; the account's KIND picks the
        # resume path (a login resumes through the CLI's flag), never the file
        (self.home / "data" / "runner-session.json").write_text(
            json.dumps({"session_id": "s-live", "lane": "key", "generation": 0, "updated": 0}))
        host = accounts.Account(accounts.HOST, "claude-login", None, None, implicit=True)
        r = self.runner(account=host); r.start(); self.one_turn(r)
        self.assertIsNone(self.options[0].resume)                   # the kind, not the file's "key"
        self.assertEqual(self.options[0].extra_args["resume"], "s-live")
        self.assertTrue(_wait(lambda: self.session_file()["lane"] == "login"))
        self.assertEqual(self.session_file()["session_id"], "s-live")
        r.stop(timeout=5)
        r2 = self.runner(account=host); r2.start()
        self.assertTrue(_wait(lambda: len(self.options) == 2))
        self.assertIsNone(self.options[1].resume)
        self.assertEqual(self.options[1].extra_args["resume"], "s-live")

    def test_a_session_file_that_is_not_an_object_is_a_fresh_start(self):
        (self.home / "data" / "runner-session.json").write_text("null")
        r = self.runner(); r.start(); self.one_turn(r)
        self.assertIsNone(r.fatal)
        self.assertIn(("system", "fresh"),
                      [(e["kind"], e["payload"].get("subtype")) for e in r.events()])
        self.assertEqual(self.session_file()["session_id"], "s-live")

    def test_a_digest_turn_that_raises_does_not_end_the_worker(self):
        (self.home / "STATUS.md").write_text("## Open loops\n- x\n")
        r = self.runner()
        real = r._turn

        async def turn(first):
            if first["source"] == "boot":
                raise RuntimeError("digest turn broke")
            return await real(first)
        r._turn = turn
        r.start()
        errors = lambda: [e["payload"].get("error", "") for e in r.events() if e["kind"] == "error"]
        self.assertTrue(_wait(lambda: any("digest turn broke" in m for m in errors())))
        self.assertTrue(r.worker_alive())
        self.one_turn(r)                                             # the next row runs
        self.assertIsNone(r.fatal)

    def hanging_runner(self, sid):
        """A runner whose CLI names `sid` in its init, then stays silent until
        interrupted: a turn in flight, as a kill would find it."""
        def factory(options):
            self.options.append(options)
            return ScriptedClient(options, [[init_msg(session=sid), "HANG", result(session=sid)]])
        r = SdkRunner(self.home, client_factory=factory)
        self.addCleanup(lambda: r.stop(timeout=5))
        return r

    def test_a_new_sessions_id_is_on_file_while_its_first_turn_runs(self):
        # a runner killed inside a brand-new session's first turn never
        # reaches the turn's end nor its teardown; what is on file at that
        # moment is what the next start resumes
        r = self.hanging_runner("s-first")
        r.start()
        r.enqueue(Item("operator:priya", "chat", "a long first job", sender="Priya"))
        self.assertTrue(_wait(lambda: "s-first" in self.inits(r)))
        self.assertTrue(_wait(lambda: (self.home / "data" / "runner-session.json").exists()
                              and self.session_file()["session_id"] == "s-first", 3.0),
                        "the id the init named is not on file while the first turn runs")
        self.assertEqual(r.state(), "running")               # still inside that turn
        self.assertNotIn("fresh", self.session_file())       # the tmux kind's mark, never ours
        self.assertEqual(self.session_file()["lane"], "login")

    def test_a_lost_resume_leaves_the_file_alone_until_the_fresh_start(self):
        # the init named another session than the resume asked for: the
        # fresh start with the digest runs at the boundary after this turn; a
        # kill before it must find the old id, so the next start takes the
        # lost-resume path again instead of resuming a session with no digest
        (self.home / "data" / "runner-session.json").write_text(
            json.dumps({"session_id": "s-old", "lane": "login", "generation": 0, "updated": 0}))
        r = self.hanging_runner("s-other")
        r.start()
        r.enqueue(Item("operator:priya", "chat", "a job", sender="Priya"))
        self.assertTrue(_wait(lambda: "s-other" in self.inits(r)))
        time.sleep(0.5)
        self.assertEqual(r.state(), "running")
        self.assertEqual(self.session_file()["session_id"], "s-old")

    def test_a_lost_resume_keeps_the_old_id_until_its_fresh_start_runs(self):
        # between the lost-resume turn's result and the
        # fresh start with the digest, a kill must still find the old id,
        # or the next start resumes the new session and no digest ever comes
        (self.home / "data" / "runner-session.json").write_text(
            json.dumps({"session_id": "s-old", "lane": "login", "generation": 0, "updated": 0}))
        (self.home / "STATUS.md").write_text("## Open loops\n- carry this\n")
        r = self.runner(resume_as="s-other")
        seen, real = [], r._start_fresh

        async def start_fresh(**kw):
            seen.append(self.session_file()["session_id"])   # what a kill here would find
            return await real(**kw)
        r._start_fresh = start_fresh
        r.start()
        self.one_turn(r)
        self.assertTrue(_wait(lambda: seen))
        self.assertEqual(seen, ["s-old"])
        self.assertTrue(_wait(lambda: r.saved_session() == "s-other"))   # once the digest is in

    def test_a_failing_session_write_is_retried_once_per_turn_not_per_message(self):
        # a full disk is one error at the init and one at
        # the result, not one per streamed message
        def factory(options):
            self.options.append(options)
            return ScriptedClient(options, [[init_msg(session="s-new")]
                                            + [assistant(text="t%d" % i) for i in range(6)]
                                            + [result(session="s-new")]])
        r = SdkRunner(self.home, client_factory=factory)
        self.addCleanup(lambda: r.stop(timeout=5))

        def full(session_id):
            raise OSError("No space left on device")
        r._save_session = full
        r.start()
        self.one_turn(r)
        errors = [e for e in r.events() if e["kind"] == "error"
                  and "runner-session.json" in e["payload"].get("error", "")]
        self.assertLessEqual(len(errors), 2)

    def test_a_brand_new_cousin_gets_no_digest(self):
        r = self.runner(); r.start(); self.one_turn(r)
        self.assertFalse(any("STATE DIGEST" in b for b in self.bodies(r)))


if __name__ == "__main__":
    unittest.main()


class TestRestartNote(HermeticCase):
    """A runner restart interrupts the turn in flight, and the agent CLI
    records that as the user's stop ("[Request interrupted by user]", "stop
    and wait for the user"), so a resumed cousin read it as the operator's
    and parked. A stop that interrupted a live turn leaves a mark; the next
    runner that resumes the session puts one runner line first: the runner
    restarted, continue where you were."""

    def setUp(self):
        super().setUp()
        self.home = temp_home(self)
        root = self.home.parent.parent
        (root / "config").mkdir(exist_ok=True)
        (root / "config" / "law.md").write_text("1. The law.\n")
        p = mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": "/nonexistent/framework-root"})
        p.start(); self.addCleanup(p.stop)
        self.clients = []

    def runner(self, first_turn):
        def factory(options):
            turns = [first_turn] + [[init_msg(session="s-live"), assistant(text="ok"),
                                     result(session="s-live")] for _ in range(4)]
            client = ScriptedClient(options, turns)
            self.clients.append(client)
            return client
        r = SdkRunner(self.home, client_factory=factory)
        self.addCleanup(lambda: r.stop(timeout=5))
        return r

    def mark(self):
        return self.home / "data" / "runner-restart.json"

    def rows(self, r):
        return [row for row in (r.inbox.get(i) for i in range(1, 30)) if row]

    @staticmethod
    def is_note(row):
        body = row.get("body") or ""
        return any(m in body for m in ("the runner restarted", "a requested stop cut your last turn",
                                       "a requested restart cut your last turn"))

    def test_a_stop_mid_turn_puts_the_restart_line_first_in_the_resumed_session(self):
        # an established session (an earlier turn recorded its id): the case
        # a restart interrupts. A session cut in its very first turn is on
        # file from its init, so it resumes the same way.
        (self.home / "data" / "runner-session.json").write_text(
            json.dumps({"session_id": "s-live", "lane": "login"}))
        r1 = self.runner([init_msg(session="s-live"), "HANG", result(session="s-live")])
        r1.start()
        r1.enqueue(Item("operator:priya", "chat", "start the long job", sender="Priya"))
        self.assertTrue(_wait(lambda: r1.state() == "running"))
        r1.stop(timeout=5)
        self.assertTrue(self.mark().exists(), "a stop that interrupted a turn leaves a mark")
        r2 = self.runner([init_msg(session="s-live"), assistant(text="continuing"),
                          result(session="s-live")])
        r2.start()
        self.assertTrue(_wait(lambda: any(self.is_note(row) and row["state"] == "done"
                                          for row in self.rows(r2))), self.rows(r2))
        note = next(row for row in self.rows(r2) if self.is_note(row))
        self.assertEqual(note["source"], "boot")
        self.assertEqual((note["thread_id"], note["sender"]), ("system", "runner"))
        self.assertIn("the runner restarted", note["body"])
        self.assertIn("continue where you were", note["body"])
        self.assertIn("not the operator", note["body"])
        self.assertIn("continue where you were", json.dumps(self.clients[-1].queries[0]))
        self.assertFalse(self.mark().exists(), "the mark is taken once")

    def test_a_requested_stop_mid_turn_is_never_called_a_restart(self):
        """An operator's stop (console, cousin-supervisor
        stop) writes run/held before it signals. The resumed session must be
        told a requested stop cut its turn, never "not the operator"."""
        from cousin_lib import supervisor
        (self.home / "data" / "runner-session.json").write_text(
            json.dumps({"session_id": "s-live", "lane": "login"}))
        r1 = self.runner([init_msg(session="s-live"), "HANG", result(session="s-live")])
        r1.start()
        r1.enqueue(Item("operator:priya", "chat", "start the long job", sender="Priya"))
        self.assertTrue(_wait(lambda: r1.state() == "running"))
        held = supervisor.held_path(self.home)
        held.parent.mkdir(parents=True, exist_ok=True)
        held.write_text("2026-09-24T19:40:00+00:00 priya from the console")
        r1.stop(timeout=5)
        held.unlink()
        r2 = self.runner([init_msg(session="s-live"), assistant(text="waiting"),
                          result(session="s-live")])
        r2.start()
        self.assertTrue(_wait(lambda: any(self.is_note(row) and row["state"] == "done"
                                          for row in self.rows(r2))), self.rows(r2))
        note = next(row for row in self.rows(r2) if self.is_note(row))
        self.assertNotIn("not the operator", note["body"])
        self.assertNotIn("continue where you were", note["body"])
        self.assertIn("a requested stop cut your last turn", note["body"])
        self.assertIn("priya from the console", note["body"])

    def test_a_requested_restart_mid_turn_is_told_to_continue(self):
        """The console's restart holds the runner with its own
        name ("console restart") before it signals; the resumed session is
        told a requested restart cut its turn and to continue it, never
        that a requested stop did."""
        from cousin_lib import supervisor
        (self.home / "data" / "runner-session.json").write_text(
            json.dumps({"session_id": "s-live", "lane": "login"}))
        r1 = self.runner([init_msg(session="s-live"), "HANG", result(session="s-live")])
        r1.start()
        r1.enqueue(Item("operator:priya", "chat", "start the long job", sender="Priya"))
        self.assertTrue(_wait(lambda: r1.state() == "running"))
        supervisor.hold(self.home, "console restart")
        r1.stop(timeout=5)
        supervisor.release(self.home)
        r2 = self.runner([init_msg(session="s-live"), assistant(text="continuing"),
                          result(session="s-live")])
        r2.start()
        self.assertTrue(_wait(lambda: any(self.is_note(row) and row["state"] == "done"
                                          for row in self.rows(r2))), self.rows(r2))
        note = next(row for row in self.rows(r2) if self.is_note(row))
        self.assertIn("a requested restart cut your last turn", note["body"])
        self.assertIn("continue", note["body"])
        self.assertNotIn("a requested stop", note["body"])
        self.assertNotIn("not the operator", note["body"])

    def test_a_clean_stop_leaves_no_mark_and_no_line(self):
        r1 = self.runner([init_msg(session="s-live"), assistant(text="ok"),
                          result(session="s-live")])
        r1.start()
        rec = r1.enqueue(Item("operator:priya", "chat", "hi", sender="Priya"))
        self.assertTrue(_wait(lambda: r1.inbox.get(rec.inbox_id)["state"] == "done"
                              and r1.state() == "idle"))
        r1.stop(timeout=5)
        self.assertFalse(self.mark().exists())
        r2 = self.runner([init_msg(session="s-live"), assistant(text="ok"),
                          result(session="s-live")])
        r2.start()
        self.assertTrue(_wait(lambda: r2.state() == "idle"))
        time.sleep(0.3)
        self.assertEqual([row for row in self.rows(r2) if self.is_note(row)], [])

    def test_a_fresh_start_discards_the_mark(self):
        """No session to resume: nothing in a new session was interrupted
        (the digest carries the state), so the mark goes and no line is put."""
        self.mark().write_text(json.dumps({"at": "2026-09-24T12:57:00+00:00",
                                           "why": "a stop interrupted a turn"}))
        r = self.runner([init_msg(session="s-live"), assistant(text="ok"),
                         result(session="s-live")])
        r.start()
        self.assertTrue(_wait(lambda: not self.mark().exists()))
        time.sleep(0.3)
        self.assertEqual([row for row in self.rows(r) if self.is_note(row)], [])


class TestToolLedgerOnRestart(TestRestartNote):
    """A turn cut mid-way had already run tools. The next session is told
    which, so the message delivered again does not repeat a push or a send;
    a fresh session (nothing of that turn in its transcript) is told too."""

    def ledger(self):
        return self.home / "data" / "turn-tools.jsonl"

    def ledger_text(self):
        return self.ledger().read_text() if self.ledger().exists() else ""

    def test_a_resumed_session_is_told_what_the_cut_turn_ran(self):
        from claude_agent_sdk import AssistantMessage, ToolResultBlock, ToolUseBlock, UserMessage
        (self.home / "data" / "runner-session.json").write_text(
            json.dumps({"session_id": "s-live", "lane": "login"}))
        pushed = AssistantMessage(content=[ToolUseBlock(
            id="tu-push", name="Bash", input={"command": "git push origin feat/x"})],
            model="claude-test")
        done = UserMessage(content=[ToolResultBlock(tool_use_id="tu-push", content="ok")])
        sending = AssistantMessage(content=[
            ToolUseBlock(id="tu-read", name="Read", input={"file_path": "/x"}),
            ToolUseBlock(id="tu-send", name="mcp__cousin__send",
                         input={"to": "sage", "text": "branch is up"})], model="claude-test")
        r1 = self.runner([init_msg(session="s-live"), pushed, done, sending, "HANG",
                          result(session="s-live")])
        r1.start()
        r1.enqueue(Item("operator:priya", "chat", "push and tell sage", sender="Priya"))
        self.assertTrue(_wait(lambda: self.ledger().exists()
                              and "tu-send" in self.ledger().read_text()))
        r1.stop(timeout=5)
        r2 = self.runner([init_msg(session="s-live"), assistant(text="ok"),
                          result(session="s-live")])
        r2.start()
        self.assertTrue(_wait(lambda: any(self.is_note(row) for row in self.rows(r2))),
                        self.rows(r2))
        body = next(row for row in self.rows(r2) if self.is_note(row))["body"]
        self.assertIn("`git push origin feat/x` (finished)", body)
        self.assertIn("mcp__cousin__send", body)
        self.assertIn("STARTED, NO RESULT", body)
        self.assertNotIn("Read", body)                    # read-only: no line
        # a stop closes the cut turn's row as delivered: it is not coming
        # again, so the line says so and quotes the message
        self.assertEqual(r2.inbox.get(1)["state"], "done")
        self.assertEqual([row["id"] for row in self.rows(r2) if row["id"] != 1
                          and not self.is_note(row)], [])
        self.assertIn("is not delivered again", body)
        self.assertIn('"push and tell sage"', body)
        # taken once: the next turn's begin starts the file afresh
        self.assertTrue(_wait(lambda: "tu-send" not in self.ledger_text()), "taken once")

    def test_a_fresh_session_gets_the_line_when_the_cut_turn_ran_tools(self):
        from cousin_lib.runner import tool_ledger
        self.mark().write_text(json.dumps({"at": "2026-10-02T11:00:00+00:00",
                                           "why": "the last runner died with a row claimed"}))
        tool_ledger.begin(self.home, [{"id": 99, "body": "ship it"}])
        tool_ledger.started(self.home, "tu-1", "Bash", {"command": "git push origin main"})
        tool_ledger.finished(self.home, "tu-1")
        r = self.runner([init_msg(session="s-new"), assistant(text="ok"),
                         result(session="s-new")])
        r.start()
        self.assertTrue(_wait(lambda: any("before this session began" in (row["body"] or "")
                                          for row in self.rows(r))), self.rows(r))
        body = next(row["body"] for row in self.rows(r)
                    if "before this session began" in (row["body"] or ""))
        self.assertIn("`git push origin main` (finished)", body)
        # no `requeued` on the mark: a stop's, so the message is quoted
        self.assertIn('The message was: "ship it"', body)
        self.assertTrue(_wait(lambda: not self.mark().exists()
                              and '"tu-1"' not in self.ledger_text()))

    def test_after_a_death_the_line_says_the_message_comes_again(self):
        from cousin_lib.runner import tool_ledger
        self.mark().write_text(json.dumps({"at": "2026-10-02T11:00:00+00:00", "requeued": True,
                                           "why": "the last runner died with a row claimed"}))
        r = self.runner([init_msg(session="s-new"), assistant(text="ok"),
                         result(session="s-new")])
        cut = r.inbox.put(Item("operator:priya", "chat", "ship it", sender="Priya"))
        tool_ledger.begin(self.home, [{"id": cut, "body": "ship it"}])
        tool_ledger.started(self.home, "tu-1", "Bash", {"command": "git push origin main"})
        r.start()
        self.assertTrue(_wait(lambda: any("before this session began" in (row["body"] or "")
                                          for row in self.rows(r))), self.rows(r))
        body = next(row["body"] for row in self.rows(r)
                    if "before this session began" in (row["body"] or ""))
        self.assertIn("delivered again after this line", body)
        self.assertIn("STARTED, NO RESULT", body)

    def test_a_ledger_from_an_older_finished_turn_is_not_listed(self):
        # a death between a claim and the ledger's begin: the ledger still
        # holds the previous turn, whose rows are all done
        from cousin_lib.runner import tool_ledger
        self.mark().write_text(json.dumps({"at": "2026-10-02T11:00:00+00:00", "requeued": True,
                                           "why": "the last runner died with a row claimed"}))
        r = self.runner([init_msg(session="s-new"), assistant(text="ok"),
                         result(session="s-new")])
        old = r.inbox.put(Item("operator:priya", "chat", "older", sender="Priya"))
        r.inbox.claim_id(old); r.inbox.done(old, "delivered")
        tool_ledger.begin(self.home, [{"id": old, "body": "older"}])
        tool_ledger.started(self.home, "tu-1", "Bash", {"command": "git push origin main"})
        tool_ledger.finished(self.home, "tu-1")
        r.start()
        self.assertTrue(_wait(lambda: not self.mark().exists()))
        time.sleep(0.3)
        self.assertFalse(any("before this session began" in (row["body"] or "")
                             for row in self.rows(r)), self.rows(r))
