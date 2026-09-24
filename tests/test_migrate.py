"""cousin-migrate (master plan phase 7 tasks 7-8): one cousin from the tmux
lane to the SDK runner, as explicit operator steps, each reversible.
`plan` writes nothing; `apply` runs only with --yes, records the prior
cousin.toml (bytes and mode) before its first step and stops at the first
failed one; `rollback` undoes exactly what ran; `check` measures the exit
criterion. Every live action (the clean stop, the supervisor, tmux, the
account check, the chat server) is injected here: this test touches no
live home, no tmux, no supervisor and no model."""
import base64
import contextlib
import importlib.util
import io
import json
import os
import pathlib
import sqlite3
import stat
import tempfile
import time
import tomllib
import unittest
from unittest import mock

from cousin_lib import migrate
from cousin_lib.delivery import Item
from cousin_lib.runner.inbox import Inbox
from tests._hermetic import HermeticCase

TOML = ('[cousin]\r\nslug = "wren"\r\nname = "Wren"\r\nrole = "archivist"\r\n\r\n'
        '# the operator\'s own comment stays\r\n[runtime]\r\nmodel = "opus"\r\n')


def _root(case):
    tmp = tempfile.TemporaryDirectory(); case.addCleanup(tmp.cleanup)
    root = pathlib.Path(tmp.name)
    home = root / "cousins" / "wren"
    (home / "data").mkdir(parents=True)
    (root / "config").mkdir()
    (root / "config" / "accounts.toml").write_text(
        '[accounts.team]\nkind = "claude-login"\nconfig_dir = "accounts/team"\n')
    (home / "cousin.toml").write_bytes(TOML.encode())
    os.chmod(home / "cousin.toml", 0o640)
    p = mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": str(root)})
    p.start(); case.addCleanup(p.stop)
    return root, home


class Live:
    """Every live action, recorded; each one's outcome is settable."""

    def __init__(self, *, logged_in=True, supervisor=True, close_ok=True, start_error=None,
                 running=True, tmux=True, stop_answer="stopped", runner_down_after=0):
        self.calls = []
        self.logged_in, self.supervisor, self.close_ok = logged_in, supervisor, close_ok
        self.start_error, self.running, self.tmux = start_error, running, tmux
        self.stop_answer, self.runner_down_after = stop_answer, runner_down_after
        self.runner_up = False

    def kw(self):
        return dict(auth_check=self.auth_check, validator=self.validator,
                    cli_version=lambda: "Claude Code 9.9.9 (bundled with claude-agent-sdk 0.0.1)", supervisor_up=lambda root: self.supervisor,
                    sdk_ok=lambda: True, tmux_alive=lambda home: self.tmux, close=self.close,
                    import_auto=self.import_auto, start=self.start, verify=self.verify,
                    stop=self.stop, runner_alive=self.runner_alive, reload=self.reload,
                    start_tmux=self.start_tmux, new_packet=self.new_packet, release=self.release,
                    sleep=lambda s: None, clock=self.clock)

    def clock(self):
        self._t = getattr(self, "_t", 0.0) + 1.0
        return self._t

    def auth_check(self, home, root, account):
        self.calls.append(("auth", account))
        return (0, "account=%s loggedIn=True" % account) if self.logged_in \
            else (4, "account=%s loggedIn=False -> run cousin-account login" % account)

    def validator(self, account, root, *, model=None, effort=None):
        self.calls.append(("validate", model, effort, account))
        return 0, "validate: ok (one model turn answered)"

    def close(self, slug, root):
        self.calls.append(("close", slug))
        if self.close_ok:
            self.tmux = False
        return {"ok": self.close_ok, "error": None if self.close_ok else
                "a flip or clean stop started 12s ago - refusing a concurrent one"}

    def import_auto(self, home, root):
        self.calls.append(("import",))
        return {"import": 2}

    def start(self, home, root):
        self.calls.append(("start", tomllib.loads((home / "cousin.toml").read_text())["agent"]))
        if self.start_error:
            raise RuntimeError(self.start_error)
        self.runner_up = True

    def verify(self, home, root):
        self.calls.append(("verify",))
        return {"ok": self.running, "detail": "runner:wren running" if self.running else "backoff"}

    def stop(self, home, root):
        self.calls.append(("stop",))
        self._down_in = self.runner_down_after
        if self.stop_answer == "stopped":
            self.runner_up = False
        return {"runner": self.stop_answer, "supervisor": "running"}

    def runner_alive(self, home):
        if getattr(self, "_down_in", None) is not None and self.runner_up:
            self._down_in -= 1
            if self._down_in < 0:
                self.runner_up = False
        return self.runner_up

    def start_tmux(self, home, root):
        self.calls.append(("start_tmux", (home / "cousin.toml").read_bytes()))
        self.tmux = True

    def reload(self, root):
        self.calls.append(("reload",))

    def release(self, home):
        self.calls.append(("release",))

    def new_packet(self, home):
        self.calls.append(("packet",))
        return 7


def _tree(path):
    return sorted(str(p.relative_to(path)) for p in path.rglob("*"))


def _names(live):
    return [c[0] for c in live.calls if c[0] not in ("auth", "validate")]


class TestPlan(HermeticCase):
    def test_a_plan_lists_checks_and_steps_and_writes_nothing(self):
        root, home = _root(self)
        before = _tree(root)
        live = Live()
        p = migrate.plan(home, root=root, validate=True, account="team", **live.kw())
        self.assertTrue(p["ready"], p)
        self.assertEqual(p["steps"], list(migrate.STEPS))
        self.assertEqual(_tree(root), before)
        self.assertEqual((home / "cousin.toml").read_bytes(), TOML.encode())
        self.assertEqual(_names(live), [])

    def test_a_plan_never_tells_the_operator_to_delete_the_tmux_lanes_keys(self):
        """The tmux lane stays supported (the operator, 2026-09-24): its keys are
        not deprecated, so the plan carries no 'warn 2.0.0' removal lines."""
        root, home = _root(self)
        p = migrate.plan(home, root=root, validate=True, account="team", **Live().kw())
        self.assertNotIn("warnings", p)
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            migrate._print_plan(p)
        self.assertNotIn("2.0.0", out.getvalue())

    def test_a_blocker_makes_the_plan_not_ready_and_apply_refuses(self):
        root, home = _root(self)
        for live, needle in ((Live(logged_in=False), "cousin-account login"),
                             (Live(supervisor=False), "cousin-supervisor"),
                             (Live(tmux=False), "it is stopped")):
            p = migrate.plan(home, root=root, validate=True, account="team", **live.kw())
            self.assertFalse(p["ready"])
            self.assertIn(needle, json.dumps(p["checks"]))
            with self.assertRaisesRegex(migrate.MigrateError, "not ready"):
                migrate.apply(home, root=root, validate=True, account="team", **live.kw())
            self.assertEqual((home / "cousin.toml").read_bytes(), TOML.encode())
            self.assertFalse((home / migrate.RECORD).exists())

    def test_an_unknown_account_is_a_blocker(self):
        root, home = _root(self)
        p = migrate.plan(home, root=root, validate=True, account="nobody", **Live().kw())
        self.assertFalse(p["ready"])
        self.assertIn("not in config/accounts.toml", json.dumps(p["checks"]))

    def test_a_runner_cousin_has_nothing_to_migrate(self):
        root, home = _root(self)
        (home / "cousin.toml").write_text(TOML + '\n[agent]\nrunner = "sdk"\n')
        p = migrate.plan(home, root=root, validate=True, **Live().kw())
        self.assertFalse(p["ready"])
        self.assertIn("already on the runner lane", json.dumps(p["checks"]))


class TestApply(HermeticCase):
    def test_apply_runs_the_steps_in_order_and_records_the_exact_prior_file(self):
        root, home = _root(self)
        (home / "data" / "pending-boot.json").write_text('{"generation": 3}')
        live = Live()
        rec = migrate.apply(home, root=root, validate=True, account="team", **live.kw())
        self.assertEqual(rec["state"], "migrated")
        self.assertEqual(_names(live), ["close", "import", "start", "verify"])
        # [runtime] model is carried into [agent], where the runner reads it (#96)
        self.assertEqual(live.calls[-2][1], {"runner": "sdk", "model": "opus", "account": "team"})
        text = (home / "cousin.toml").read_bytes().decode()
        self.assertEqual(tomllib.loads(text)["runtime"]["model"], "opus")
        self.assertIn("# the operator's own comment stays\r\n", text)
        self.assertEqual(stat.S_IMODE((home / "cousin.toml").stat().st_mode), 0o640)
        saved = json.loads((home / migrate.RECORD).read_text())
        self.assertEqual(base64.b64decode(saved["prior_toml_b64"]), TOML.encode())
        self.assertEqual([s["step"] for s in saved["steps"]], list(migrate.STEPS))
        # the migration day's packet is archived: the runner boots on its digest (review I7)
        self.assertFalse((home / "data" / "pending-boot.json").exists())
        self.assertTrue((home / migrate.PRE_RUNNER_BOOT).exists())

    def test_a_tmux_session_that_came_back_stops_apply_before_the_lane_flips(self):
        """Review M7: a flip or a console start between close and toml."""
        root, home = _root(self)
        live = Live()
        orig = live.import_auto

        def import_and_restart(home_, root_):
            live.tmux = True                                  # something started it again
            return orig(home_, root_)
        live.import_auto = import_and_restart
        rec = migrate.apply(home, root=root, validate=True, account="team", **live.kw())
        self.assertEqual((rec["state"], rec["steps"][-1]["step"]), ("failed", "toml"))
        self.assertIn("up again", rec["steps"][-1]["detail"])
        self.assertEqual((home / "cousin.toml").read_bytes(), TOML.encode())

    def test_a_failed_clean_stop_stops_before_anything_changes(self):
        root, home = _root(self)
        live = Live(close_ok=False)
        rec = migrate.apply(home, root=root, validate=True, account="team", **live.kw())
        self.assertEqual(rec["state"], "failed")
        self.assertIn("refusing a concurrent one", rec["steps"][-1]["detail"])
        self.assertEqual((home / "cousin.toml").read_bytes(), TOML.encode())
        self.assertNotIn("start", _names(live))

    def test_a_second_apply_is_refused_until_a_rollback(self):
        root, home = _root(self)
        live = Live(start_error="no child")
        migrate.apply(home, root=root, validate=True, account="team", **live.kw())
        with self.assertRaisesRegex(migrate.MigrateError, "rollback"):
            migrate.apply(home, root=root, validate=True, account="team", **live.kw())


class TestVerify(HermeticCase):
    """Review I8: a runner that exits at once must not pass."""

    def _verify(self, states, alive=True, health=(True, "answers")):
        seq = iter(states)
        clock = iter(range(1000))
        return migrate.verify_runner(
            pathlib.Path("/x/cousins/wren"), "/x",
            snapshot=lambda root: {"children": {"runner:wren": {"state": next(seq, states[-1])}}},
            alive=lambda home: alive, health=lambda home: health, stable_s=3, timeout=20,
            sleep=lambda s: None, clock=lambda: float(next(clock)))

    def test_running_for_the_whole_window_passes(self):
        self.assertTrue(self._verify(["running"])["ok"])

    def test_running_for_a_moment_then_backoff_fails(self):
        out = self._verify(["running", "backoff"])
        self.assertFalse(out["ok"])
        self.assertIn("backoff", out["detail"])

    def test_a_runner_without_its_lock_or_a_silent_chat_server_fails(self):
        self.assertFalse(self._verify(["running"], alive=False)["ok"])
        out = self._verify(["running"], health=(False, "the chat server on :8091 does not answer"))
        self.assertFalse(out["ok"])
        self.assertIn(":8091", out["detail"])


def _inbox(home, rows):
    """(state, outcome, age_s) rows through the real inbox."""
    inbox = Inbox(home)
    for state, outcome, age in rows:
        rec = inbox.put(Item("operator:priya", "chat", "hello", sender="Priya"))
        with sqlite3.connect(home / "data" / "inbox.db") as db:
            db.execute("UPDATE inbox SET state = ?, outcome = ?, created_at = ? WHERE id = ?",
                       (state, outcome, time.time() - age, rec))


def _stream(home, events):
    (home / "data" / "stream").mkdir(parents=True, exist_ok=True)
    with open(home / "data" / "stream" / "sdk-00000001.jsonl", "w") as fh:
        for i, (kind, payload, age) in enumerate(events, 1):
            fh.write(json.dumps({"seq": i, "ts": time.time() - age, "kind": kind,
                                 "payload": payload}) + "\n")


class TestRollback(HermeticCase):
    def test_a_failed_start_is_rolled_back_to_the_exact_file_on_a_fresh_packet(self):
        root, home = _root(self)
        live = Live(start_error="no child")
        rec = migrate.apply(home, root=root, validate=True, account="team", **live.kw())
        self.assertEqual((rec["state"], rec["steps"][-1]["step"]), ("failed", "start"))
        back = migrate.rollback(home, root=root, **live.kw())
        self.assertEqual(back["state"], "rolled_back")
        self.assertEqual((home / "cousin.toml").read_bytes(), TOML.encode())
        self.assertEqual(stat.S_IMODE((home / "cousin.toml").stat().st_mode), 0o640)
        self.assertEqual(_names(live)[-5:], ["stop", "reload", "packet", "start_tmux", "release"])
        self.assertEqual(live.calls[-2][1], TOML.encode())    # tmux starts on the restored file

    def test_a_stop_that_answers_stopping_is_waited_out_before_anything_is_restored(self):
        """Review I6(a): a stop that returns before the runner is down."""
        root, home = _root(self)
        live = Live(stop_answer="stopping", runner_down_after=3)
        migrate.apply(home, root=root, validate=True, account="team", **live.kw())
        self.assertTrue(live.runner_up)
        migrate.rollback(home, root=root, **live.kw())
        self.assertFalse(live.runner_up)
        self.assertEqual((home / "cousin.toml").read_bytes(), TOML.encode())

    def test_a_runner_that_never_lets_go_is_refused_and_nothing_restored(self):
        root, home = _root(self)
        live = Live(stop_answer="stopping", runner_down_after=10 ** 6)
        migrate.apply(home, root=root, validate=True, account="team", **live.kw())
        with self.assertRaisesRegex(migrate.MigrateError, "still holds its lock"):
            migrate.rollback(home, root=root, **live.kw())
        self.assertIn(b'runner = "sdk"', (home / "cousin.toml").read_bytes())
        self.assertNotIn("start_tmux", _names(live))

    def test_a_second_rollback_is_refused(self):
        """Review I6(b): it would stop the live tmux session with no handoff."""
        root, home = _root(self)
        live = Live(start_error="no child")
        migrate.apply(home, root=root, validate=True, account="team", **live.kw())
        migrate.rollback(home, root=root, **live.kw())
        calls = len(live.calls)
        with self.assertRaisesRegex(migrate.MigrateError, "already rolled back"):
            migrate.rollback(home, root=root, **live.kw())
        self.assertEqual(len(live.calls), calls)

    def test_after_a_failed_close_rollback_touches_no_process(self):
        """Review I6(c): the refused close left a flip running; nothing to undo."""
        root, home = _root(self)
        live = Live(close_ok=False)
        migrate.apply(home, root=root, validate=True, account="team", **live.kw())
        calls = len(live.calls)
        back = migrate.rollback(home, root=root, **live.kw())
        self.assertEqual(back["state"], "rolled_back")
        self.assertEqual([c[0] for c in live.calls[calls:]], ["release"])    # idempotent, no process

    def test_a_tmux_session_that_came_back_is_not_started_twice(self):
        """Review round 2 m2: after the M7 refusal the session is up."""
        root, home = _root(self)
        live = Live()
        orig = live.import_auto

        def import_and_restart(home_, root_):
            live.tmux = True
            return orig(home_, root_)
        live.import_auto = import_and_restart
        migrate.apply(home, root=root, validate=True, account="team", **live.kw())
        back = migrate.rollback(home, root=root, **live.kw())
        self.assertEqual(back["state"], "rolled_back")
        self.assertNotIn("start_tmux", _names(live))

    def test_a_failing_step_is_recorded_and_reported_not_a_traceback(self):
        root, home = _root(self)
        live = Live(start_error="no child")
        migrate.apply(home, root=root, validate=True, account="team", **live.kw())

        def broken(home_, root_):
            raise RuntimeError("tmux new-session: duplicate session")
        live.start_tmux = broken
        with self.assertRaisesRegex(migrate.MigrateError, "start_tmux failed"):
            migrate.rollback(home, root=root, **live.kw())
        rec = json.loads((home / migrate.RECORD).read_text())
        self.assertEqual(rec["rollback_attempts"][-1]["failed"], "start_tmux")

    def test_a_retried_rollback_never_stops_the_tmux_session_it_restored(self):
        """Execution review I1: a rollback that failed after the file was
        back on tmux is retried; stopping again would take the tmux lane
        and kill the live session with no handoff."""
        root, home = _root(self)
        live = Live(start_error="no child")
        migrate.apply(home, root=root, validate=True, account="team", **live.kw())
        good_start = live.start_tmux

        def broken(home_, root_):
            live.calls.append(("start_tmux_failed",))
            live.tmux = False
            raise RuntimeError("tmux new-session failed")
        live.start_tmux = broken
        with self.assertRaises(migrate.MigrateError):
            migrate.rollback(home, root=root, **live.kw())
        self.assertEqual((home / "cousin.toml").read_bytes(), TOML.encode())
        calls = len(live.calls)
        live.start_tmux = good_start
        back = migrate.rollback(home, root=root, **live.kw())
        self.assertEqual(back["state"], "rolled_back")
        self.assertEqual([c[0] for c in live.calls[calls:]], ["start_tmux", "release"])

    def test_the_file_is_restored_even_when_the_record_missed_the_toml_step(self):
        """Review round 2 m2: a Ctrl-C between the write and the record."""
        root, home = _root(self)
        live = Live(start_error="no child")
        migrate.apply(home, root=root, validate=True, account="team", **live.kw())
        rec = json.loads((home / migrate.RECORD).read_text())
        rec["steps"] = [s for s in rec["steps"] if s["step"] not in ("toml", "start")]
        (home / migrate.RECORD).write_text(json.dumps(rec))
        self.assertIn(b'runner = "sdk"', (home / "cousin.toml").read_bytes())
        migrate.rollback(home, root=root, **live.kw())
        self.assertEqual((home / "cousin.toml").read_bytes(), TOML.encode())

    def test_a_rollback_releases_the_runner_hold(self):
        root, home = _root(self)
        live = Live()
        migrate.apply(home, root=root, validate=True, account="team", **live.kw())
        migrate.rollback(home, root=root, **live.kw())
        self.assertEqual(_names(live)[-1], "release")


class TestReMigration(HermeticCase):
    def test_what_the_cousin_wrote_on_tmux_between_migrations_is_not_held(self):
        """Review round 2 N1: apply, rollback, writes on tmux, apply again:
        the gate's cursor opens afresh, and the start sweep holds none."""
        from cousin_lib import memory, review_gate
        root, home = _root(self)
        live = Live()
        migrate.apply(home, root=root, validate=True, account="team", **live.kw())
        migrate.rollback(home, root=root, **live.kw())
        with open(pathlib.Path(home, *review_gate.STATE), "w") as fh:     # days pass on tmux
            json.dump({"cursor": time.time() - 5 * 86400, "seen": []}, fh)
        for i in range(5):
            memory.remember(home, "pantry %d" % i, "Shelf %d holds the quokka tins." % i)
        live.tmux = True
        rec = migrate.apply(home, root=root, validate=True, account="team", **live.kw())
        self.assertEqual(rec["state"], "migrated")
        review_gate.begin(home)                                 # the runner's start sweep
        self.assertEqual(review_gate.hold_new(home), [])

    def test_rollback_refuses_while_rows_wait_or_the_inbox_is_unreadable(self):
        root, home = _root(self)
        live = Live()
        migrate.apply(home, root=root, validate=True, account="team", **live.kw())
        _inbox(home, [("queued", None, 5)])
        with self.assertRaisesRegex(migrate.MigrateError, "1 inbox row"):
            migrate.rollback(home, root=root, **live.kw())
        with mock.patch.object(migrate, "_inbox_rows", return_value=None):
            with self.assertRaisesRegex(migrate.MigrateError, "cannot be read"):
                migrate.rollback(home, root=root, **live.kw())
        self.assertEqual(migrate.rollback(home, root=root, force=True, **live.kw())["state"],
                         "rolled_back")


class TestCheck(HermeticCase):
    def test_check_counts_lost_messages_unrecorded_calls_and_failed_recorders(self):
        root, home = _root(self)
        _inbox(home, [("done", "delivered", 7200), ("done", "failed", 7200),
                      ("queued", None, 7200), ("queued", None, 5)])
        _stream(home, [("tool", {"id": "tu-1", "name": "Bash"}, 7200),
                       ("tool_result", {"tool_use_id": "tu-1"}, 7200),
                       ("tool", {"id": "tu-2", "name": "Bash"}, 7200),
                       ("tool", {"id": "tu-3", "name": "Bash"}, 5),
                       ("hook", {"event": "PostToolUse", "error": "OSError: jobs.db locked"}, 60)])
        c = migrate.check(home, since=0.0, health=lambda h: (True, "answers"))
        self.assertEqual(c["inbox"], {"done": 2, "failed": 1, "open": 2, "stale": 1})
        self.assertEqual((c["tool_calls"], c["unrecorded"]), (3, ["tu-2"]))
        self.assertEqual(c["hook_errors"], ["PostToolUse: OSError: jobs.db locked"])
        self.assertFalse(c["ok"])

    def test_since_leaves_out_what_came_before_it(self):
        root, home = _root(self)
        _inbox(home, [("queued", None, 7200)])
        _stream(home, [("tool", {"id": "tu-1"}, 7200)])
        self.assertTrue(migrate.check(home, since=time.time() - 3600)["ok"])

    def test_an_unreadable_inbox_or_a_silent_chat_server_is_not_ok(self):
        root, home = _root(self)
        with mock.patch.object(migrate, "_inbox_rows", return_value=None):
            c = migrate.check(home, since=0.0)
        self.assertEqual((c["inbox_readable"], c["ok"]), (False, False))
        c = migrate.check(home, since=0.0, health=lambda h: (False, "refused"))
        self.assertEqual((c["chat_ok"], c["ok"]), (False, False))

    def test_a_clean_week_is_ok(self):
        root, home = _root(self)
        _inbox(home, [("done", "delivered", 7200)])
        _stream(home, [("tool", {"id": "tu-1"}, 7200), ("tool_result", {"tool_use_id": "tu-1"}, 7200)])
        self.assertTrue(migrate.check(home, since=0.0, health=lambda h: (True, "answers"))["ok"])


# ------------------------------------------------------------ #96: [runtime] carried
KEY = "sk-ant-fixture-quokka-0123456789"
CARRY_TOML = ('[cousin]\nslug = "wren"\nname = "Wren"\n\n'
              '[runtime]\nmodel = "opus"\neffort = "high"\nauth = "api_key"\n')


def _key_cousin(case, *, key_line="ANTHROPIC_API_KEY=%s\n" % KEY, toml=CARRY_TOML):
    """wren on the tmux lane with [runtime] auth = "api_key" and its key
    in <home>/.secrets/api-key.env (0600 in a 0700 directory), as
    agent_auth.write_key leaves it; key_line None writes no key file."""
    root, home = _root(case)
    (home / "cousin.toml").write_text(toml)
    if key_line is not None:
        (home / ".secrets").mkdir(mode=0o700)
        os.chmod(home / ".secrets", 0o700)
        (home / ".secrets" / "api-key.env").write_text(key_line)
        os.chmod(home / ".secrets" / "api-key.env", 0o600)
    return root, home


def _files_holding(root, needle):
    out = []
    for path in root.rglob("*"):
        if path.is_file() and needle.encode() in path.read_bytes():
            out.append(str(path.relative_to(root)))
    return sorted(out)


class TestCarryRuntime(HermeticCase):
    """#96: the runner reads only [agent]; the tmux lane's [runtime]
    model, effort and auth must be carried or the cousin silently runs
    the CLI's default model and bills the host login."""

    def test_the_plan_lists_the_carried_keys_and_writes_nothing(self):
        root, home = _key_cousin(self)
        before = _tree(root)
        p = migrate.plan(home, root=root, validate=True, **Live().kw())
        self.assertTrue(p["ready"], p)
        self.assertEqual(p["carry"]["values"],
                         {"model": "opus", "effort": "high", "account": "wren-key"})
        self.assertEqual(p["account"], "wren-key")
        text = json.dumps(p)
        self.assertIn("[runtime] model", text)
        self.assertIn("[agent] effort", text)
        self.assertIn("wren-key", text)
        self.assertNotIn(KEY, text)
        self.assertEqual(_tree(root), before)

    def test_an_existing_agent_key_wins_and_is_reported(self):
        root, home = _root(self)
        (home / "cousin.toml").write_text(
            '[cousin]\nslug = "wren"\n\n[runtime]\nmodel = "opus"\neffort = "low"\n\n'
            '[agent]\nmodel = "sonnet"\n')
        p = migrate.plan(home, root=root, validate=True, account="team", **Live().kw())
        self.assertTrue(p["ready"], p)
        self.assertEqual(p["carry"]["values"], {"effort": "low"})
        kept = [r for r in p["carry"]["rows"] if r["key"] == "model"][0]
        self.assertEqual(kept["action"], "kept")
        self.assertIn("sonnet", kept["detail"])
        self.assertIn("opus", kept["detail"])

    def test_the_harness_default_is_carried_when_the_agent_command_renders_it(self):
        root, home = _root(self)
        (home / "cousin.toml").write_text('[cousin]\nslug = "wren"\n')
        (root / "config" / "agent-cmd").write_text("claude --model {model} --effort {effort}\n")
        (root / "config" / "harness.toml").write_text(
            '[agent]\ndefault_model = "haiku"\ndefault_effort = "medium"\n')
        p = migrate.plan(home, root=root, validate=True, **Live().kw())
        self.assertEqual(p["carry"]["values"], {"model": "haiku", "effort": "medium"})
        self.assertIn("default_model", json.dumps(p["carry"]))

    def test_apply_writes_model_effort_and_a_key_account_from_the_key_file(self):
        root, home = _key_cousin(self)
        (root / "config" / "accounts.toml").write_text(
            '# the operator\'s accounts\n[accounts.team]\nkind = "claude-login"\n')
        live = Live()
        p = migrate.plan(home, root=root, validate=True, **live.kw())
        rec = migrate.apply(home, root=root, validate=True, **live.kw())
        self.assertEqual(rec["state"], "migrated", rec)
        agent = tomllib.loads((home / "cousin.toml").read_text())["agent"]
        self.assertEqual(agent, {"runner": "sdk", "model": "opus", "effort": "high",
                                 "account": "wren-key"})
        from cousin_lib import accounts
        acct = accounts.load(root)["wren-key"]
        self.assertEqual(acct.kind, "anthropic-key")
        self.assertIn("team", accounts.load(root))                 # nothing else lost
        self.assertIn("# the operator's accounts", (root / "config" / "accounts.toml").read_text())
        secret = root / ".secrets" / "accounts" / "wren-key"
        self.assertEqual(acct.secret_file, secret)
        self.assertEqual(secret.read_text(), KEY + "\n")
        self.assertEqual(stat.S_IMODE(secret.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(secret.parent.stat().st_mode), 0o700)
        # the runner resolves it to the key, never the host login
        cousin_acct = accounts.for_cousin(home, root)
        self.assertEqual(accounts.account_env(cousin_acct, root)["ANTHROPIC_API_KEY"], KEY)
        # the key lives in exactly its two files: never in a record, a plan or a toml
        self.assertEqual(_files_holding(root, KEY),
                         [".secrets/accounts/wren-key", "cousins/wren/.secrets/api-key.env"])
        self.assertNotIn(KEY, json.dumps(p) + json.dumps(rec))
        self.assertEqual(rec["account_created"]["name"], "wren-key")
        self.assertIn("the auth check", json.dumps([c for c in p["checks"]
                                                    if c["check"] == "account"]))

    def test_an_account_already_made_by_hand_with_the_same_key_is_reused(self):
        root, home = _key_cousin(self)
        (root / "config" / "accounts.toml").write_text(
            '[accounts.wren-key]\nkind = "anthropic-key"\n')
        (root / ".secrets" / "accounts").mkdir(parents=True, mode=0o700)
        os.chmod(root / ".secrets", 0o700); os.chmod(root / ".secrets" / "accounts", 0o700)
        (root / ".secrets" / "accounts" / "wren-key").write_text(KEY + "\n")
        os.chmod(root / ".secrets" / "accounts" / "wren-key", 0o600)
        before = (root / "config" / "accounts.toml").read_bytes()
        live = Live()
        rec = migrate.apply(home, root=root, validate=True, **live.kw())
        self.assertEqual(rec["state"], "migrated", rec)
        self.assertEqual((root / "config" / "accounts.toml").read_bytes(), before)
        self.assertIn(("auth", "wren-key"), live.calls)          # an existing account is checked
        self.assertNotIn("account_created", rec)

    def test_an_account_of_that_name_with_another_key_is_a_blocker(self):
        root, home = _key_cousin(self)
        (root / "config" / "accounts.toml").write_text(
            '[accounts.wren-key]\nkind = "anthropic-key"\n')
        (root / ".secrets" / "accounts").mkdir(parents=True, mode=0o700)
        os.chmod(root / ".secrets", 0o700)
        (root / ".secrets" / "accounts" / "wren-key").write_text("sk-ant-someone-else\n")
        os.chmod(root / ".secrets" / "accounts" / "wren-key", 0o600)
        p = migrate.plan(home, root=root, validate=True, **Live().kw())
        self.assertFalse(p["ready"])
        self.assertIn("different key", json.dumps(p["checks"]))

    def test_a_missing_or_malformed_key_file_makes_the_plan_not_ready(self):
        for key_line, needle in ((None, "does not exist"),
                                 ("OTHER_VAR=%s\n" % KEY, "names a variable other than"),
                                 ("ANTHROPIC_API_KEY=\n", "empty")):
            with self.subTest(key_line=key_line):
                root, home = _key_cousin(self, key_line=key_line)
                before = _tree(root)
                p = migrate.plan(home, root=root, validate=True, **Live().kw())
                self.assertFalse(p["ready"])
                row = [c for c in p["checks"] if c["check"] == "carry"][0]
                self.assertFalse(row["ok"])
                self.assertIn(needle, row["detail"])
                self.assertIn("host login", row["detail"])
                self.assertNotIn(KEY, json.dumps(p))
                with self.assertRaisesRegex(migrate.MigrateError, "not ready"):
                    migrate.apply(home, root=root, validate=True, **Live().kw())
                self.assertEqual(_tree(root), before)

    def test_an_explicit_account_wins_over_the_key_and_is_reported(self):
        root, home = _key_cousin(self)
        p = migrate.plan(home, root=root, validate=True, account="team", **Live().kw())
        self.assertTrue(p["ready"], p)
        self.assertEqual(p["account"], "team")
        self.assertNotIn("account", p["carry"]["values"])
        row = [r for r in p["carry"]["rows"] if r["key"] == "account"][0]
        self.assertEqual(row["action"], "kept")
        self.assertIn("--account team", row["detail"])

    def test_rollback_restores_the_bytes_and_removes_the_account_nothing_else_uses(self):
        root, home = _key_cousin(self)
        live = Live()
        migrate.apply(home, root=root, validate=True, **live.kw())
        back = migrate.rollback(home, root=root, **live.kw())
        self.assertEqual(back["state"], "rolled_back")
        self.assertEqual((home / "cousin.toml").read_text(), CARRY_TOML)
        from cousin_lib import accounts
        self.assertNotIn("wren-key", accounts.load(root))
        self.assertFalse((root / ".secrets" / "accounts" / "wren-key").exists())
        self.assertIn("removed", json.dumps(back["rollback_steps"]))
        self.assertEqual(_files_holding(root, KEY), ["cousins/wren/.secrets/api-key.env"])

    def test_rollback_keeps_the_account_another_cousin_uses(self):
        root, home = _key_cousin(self)
        live = Live()
        migrate.apply(home, root=root, validate=True, **live.kw())
        other = root / "cousins" / "moss"
        other.mkdir()
        (other / "cousin.toml").write_text('[cousin]\nslug = "moss"\n\n[agent]\n'
                                           'runner = "sdk"\naccount = "wren-key"\n')
        back = migrate.rollback(home, root=root, **live.kw())
        from cousin_lib import accounts
        self.assertIn("wren-key", accounts.load(root))
        self.assertTrue((root / ".secrets" / "accounts" / "wren-key").exists())
        self.assertIn("moss", json.dumps(back["rollback_steps"]))

    def test_the_cli_plan_prints_what_it_carries_and_never_the_key(self):
        root, home = _key_cousin(self)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(migrate, "_live", return_value=Live().kw()), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = migrate.migrate_main(["plan", "wren", "--validate"])
            rc2 = migrate.migrate_main(["apply", "wren", "--validate", "--yes"])
        self.assertEqual((rc, rc2), (0, 0), err.getvalue())
        text = out.getvalue()
        self.assertIn("[runtime] model 'opus' -> [agent] model", text)
        self.assertIn("[runtime] effort 'high' -> [agent] effort", text)
        self.assertIn("wren-key", text)
        self.assertNotIn(KEY, text + err.getvalue())


class TestCheckConfig(HermeticCase):
    """#96: check says loudly when the runner would run another model,
    effort or billing than the cousin's [runtime]."""

    def _migrated(self, case_toml):
        root, home = _root(self)
        (home / "cousin.toml").write_text(case_toml)
        return root, home

    def test_a_runner_on_the_cli_default_model_and_the_host_login_is_a_mismatch(self):
        root, home = self._migrated(CARRY_TOML + '\n[agent]\nrunner = "sdk"\n')
        c = migrate.check(home, root=root, since=0.0)
        self.assertFalse(c["ok"])
        text = " ".join(c["mismatches"])
        self.assertIn("model", text)
        self.assertIn("opus", text)
        self.assertIn("effort", text)
        self.assertIn("host", text)
        self.assertIn("api_key", text)

    def test_a_carried_config_matches(self):
        root, home = _key_cousin(self)
        live = Live()
        migrate.apply(home, root=root, validate=True, **live.kw())
        c = migrate.check(home, root=root, since=0.0)
        self.assertEqual(c["mismatches"], [])
        self.assertTrue(c["ok"], c)
        self.assertEqual(c["config"]["runner"],
                         {"model": "opus", "effort": "high", "account": "wren-key",
                          "kind": "anthropic-key"})

    def test_a_key_account_under_a_login_runtime_is_a_mismatch(self):
        root, home = _root(self)
        (root / "config" / "accounts.toml").write_text(
            '[accounts.paid]\nkind = "anthropic-key"\n')
        (home / "cousin.toml").write_text('[cousin]\nslug = "wren"\n\n[agent]\n'
                                          'runner = "sdk"\naccount = "paid"\n')
        c = migrate.check(home, root=root, since=0.0)
        self.assertFalse(c["ok"])
        self.assertIn("paid", " ".join(c["mismatches"]))

    def test_the_cli_says_mismatch_loudly(self):
        root, home = self._migrated(CARRY_TOML + '\n[agent]\nrunner = "sdk"\n')
        out = io.StringIO()
        with mock.patch.object(migrate, "chat_health", return_value=(True, "answers")), \
                contextlib.redirect_stdout(out):
            rc = migrate.migrate_main(["check", "wren", "--since", "2000-01-01T00:00:00+00:00"])
        self.assertEqual(rc, 1)
        self.assertIn("MISMATCH", out.getvalue())


TOO_NEW = ("API Error: 400 Claude Code 2.1.277 does not support this model; version 2.1.280"
           " or newer is required")


def _too_new_validator(seen):
    """The real sdk.validate_account on a scripted client that answers as
    the live incident did: an invalid_request 400 in the turn, with a
    result that is not flagged is_error."""
    from claude_agent_sdk import AssistantMessage, TextBlock
    from cousin_lib.runner import sdk
    from tests.runner.test_sdk import ScriptedClient, init_msg, result

    def validator(account, root, *, model=None, effort=None):
        turn = [init_msg(), AssistantMessage(content=[TextBlock(text=TOO_NEW)], model="<synthetic>",
                                             error="invalid_request"), result()]
        return sdk.validate_account(account, root, model=model, effort=effort, timeout=5,
                                    client_factory=lambda o: seen.append(o) or ScriptedClient(o, [turn]))
    return validator


class TestValidateTheModel(HermeticCase):
    """#96, live: a carried model the SDK's bundled CLI is too old for
    failed every turn. A model the runner's CLI can't run is never written."""

    def test_a_carried_model_without_validate_is_not_ready_and_never_written(self):
        root, home = _key_cousin(self)
        before = _tree(root)
        live = Live()
        p = migrate.plan(home, root=root, **live.kw())
        self.assertFalse(p["ready"])
        row = [c for c in p["checks"] if c["check"] == "validate"][0]
        self.assertFalse(row["ok"])
        self.assertIn(migrate.NEVER_UNRUN, row["detail"])
        self.assertIn("--validate", row["detail"])
        with self.assertRaisesRegex(migrate.MigrateError, "never written"):
            migrate.apply(home, root=root, **live.kw())
        self.assertEqual(_tree(root), before)
        self.assertNotIn("validate", [c[0] for c in live.calls])

    def test_nothing_carried_needs_no_validate(self):
        root, home = _root(self)
        (home / "cousin.toml").write_text('[cousin]\nslug = "wren"\n')
        p = migrate.plan(home, root=root, account="team", **Live().kw())
        self.assertTrue(p["ready"], p)

    @unittest.skipUnless(importlib.util.find_spec("claude_agent_sdk"), "needs the sdk extra")
    def test_a_model_the_cli_cannot_run_fails_validate_with_the_apis_words(self):
        root, home = _key_cousin(self)
        seen = []
        live = Live()
        live.validator = _too_new_validator(seen)
        p = migrate.plan(home, root=root, validate=True, **live.kw())
        self.assertFalse(p["ready"])
        row = [c for c in p["checks"] if c["check"] == "validate"][0]
        self.assertFalse(row["ok"])
        self.assertIn("version 2.1.280 or newer is required", row["detail"])
        self.assertIn(migrate.NEVER_UNRUN, row["detail"])
        # the turn ran on what the runner would run: the model, the effort, the key
        self.assertEqual((seen[0].model, seen[0].effort), ("opus", "high"))
        self.assertEqual(seen[0].env["ANTHROPIC_API_KEY"], KEY)
        self.assertNotIn(KEY, json.dumps(p))
        with self.assertRaisesRegex(migrate.MigrateError, "2.1.280"):
            migrate.apply(home, root=root, validate=True, **live.kw())
        self.assertEqual((home / "cousin.toml").read_text(), CARRY_TOML)
        self.assertFalse((home / migrate.RECORD).exists())
        self.assertFalse((root / ".secrets").exists())
        self.assertNotIn("close", [c[0] for c in live.calls])

    def test_a_passing_validate_runs_the_model_effort_and_account_then_writes_them(self):
        root, home = _key_cousin(self)
        live = Live()
        rec = migrate.apply(home, root=root, validate=True, **live.kw())
        self.assertEqual(rec["state"], "migrated", rec)
        (_v, model, effort, acct), = [c for c in live.calls if c[0] == "validate"]
        self.assertEqual((model, effort, acct.name, acct.kind), ("opus", "high", "wren-key",
                                                                 "anthropic-key"))
        self.assertIn("one model turn answered", rec["validated"])
        self.assertIn("9.9.9", rec["cli"])
        self.assertNotIn(KEY, json.dumps(rec))

    def test_check_validate_runs_the_runners_config_and_fails_loudly(self):
        root, home = _key_cousin(self)
        live = Live()
        migrate.apply(home, root=root, validate=True, **live.kw())
        calls = []

        def failing(account, root_, *, model=None, effort=None):
            calls.append((model, effort, account.name))
            return 4, "validate: " + TOO_NEW
        c = migrate.check(home, root=root, since=0.0, validate=True, validator=failing,
                          cli_version=lambda: "Claude Code 2.1.277 (bundled)")
        self.assertEqual(calls, [("opus", "high", "wren-key")])
        self.assertFalse(c["ok"])
        self.assertFalse(c["validate_ok"])
        self.assertIn("2.1.280", c["validate"])
        self.assertEqual(c["cli"], "Claude Code 2.1.277 (bundled)")

    def test_plan_and_check_print_the_runners_cli(self):
        root, home = _key_cousin(self)
        out = io.StringIO()
        with mock.patch.object(migrate, "_live", return_value=Live().kw()), \
                mock.patch.object(migrate, "chat_health", return_value=(True, "answers")), \
                mock.patch.object(migrate, "runner_cli", return_value="Claude Code 2.1.277 (b)"), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            migrate.migrate_main(["plan", "wren"])
            migrate.migrate_main(["check", "wren"])
        text = out.getvalue()
        self.assertIn("the runner's CLI: Claude Code 9.9.9", text)      # plan: the injected one
        self.assertIn(migrate.NEVER_UNRUN, text)
        self.assertIn("runner CLI: Claude Code 2.1.277 (b)", text)      # check

    @unittest.skipUnless(importlib.util.find_spec("claude_agent_sdk"), "needs the sdk extra")
    def test_the_runner_cli_is_the_sdks_bundled_version(self):
        from claude_agent_sdk._cli_version import __cli_version__
        self.assertIn("Claude Code %s" % __cli_version__, migrate.runner_cli())


def _secret_at(root, key):
    d = root / ".secrets" / "accounts"
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(root / ".secrets", 0o700); os.chmod(d, 0o700)
    (d / "wren-key").write_text(key + "\n")
    os.chmod(d / "wren-key", 0o600)
    return d / "wren-key"


class TestAccountLifecycle(HermeticCase):
    """#96 review: a key account is made, and unmade, exactly: a partial
    make is rolled back, a secret the migration did not write is neither
    overwritten nor removed, and config/accounts.toml comes back byte for
    byte."""

    def test_a_table_write_that_fails_after_the_secret_is_rolled_back_whole(self):
        root, home = _key_cousin(self)
        before = (root / "config" / "accounts.toml").read_bytes()
        live = Live()
        with mock.patch.object(migrate, "_append_account", side_effect=OSError("disk full")):
            rec = migrate.apply(home, root=root, validate=True, **live.kw())
        self.assertEqual((rec["state"], rec["steps"][-1]["step"]), ("failed", "toml"))
        made = json.loads((home / migrate.RECORD).read_text())["account_created"]
        self.assertEqual((made["secret"], made["table"]["state"]), ("written", "planned"))
        secret = root / ".secrets" / "accounts" / "wren-key"
        self.assertTrue(secret.exists())
        back = migrate.rollback(home, root=root, **live.kw())
        self.assertEqual(back["state"], "rolled_back")
        self.assertFalse(secret.exists())
        self.assertEqual((root / "config" / "accounts.toml").read_bytes(), before)
        self.assertNotIn(KEY, json.dumps(back))
        # a retried apply makes the secret afresh: it is ours again, not "reused"
        live.tmux = True
        rec = migrate.apply(home, root=root, validate=True, **live.kw())
        self.assertEqual(rec["state"], "migrated", rec)
        self.assertEqual(rec["account_created"]["secret"], "written")
        self.assertEqual(secret.read_text(), KEY + "\n")

    def test_a_secret_already_there_with_the_same_key_is_neither_rewritten_nor_removed(self):
        root, home = _key_cousin(self)
        secret = _secret_at(root, KEY)
        inode = secret.stat().st_ino
        live = Live()
        p = migrate.plan(home, root=root, validate=True, **live.kw())
        self.assertEqual(p["carry"]["create"]["write_secret"], False)
        self.assertIn("kept, not ours", json.dumps(p["carry"]["rows"]))
        rec = migrate.apply(home, root=root, validate=True, **live.kw())
        self.assertEqual(rec["state"], "migrated", rec)
        self.assertIsNone(rec["account_created"]["secret"])
        self.assertEqual(secret.stat().st_ino, inode)                # never rewritten
        migrate.rollback(home, root=root, **live.kw())
        self.assertEqual(secret.read_text(), KEY + "\n")            # never removed
        from cousin_lib import accounts
        self.assertNotIn("wren-key", accounts.load(root))             # the table was ours

    def test_a_secret_already_there_with_another_key_is_a_blocker(self):
        root, home = _key_cousin(self)
        secret = _secret_at(root, "sk-ant-someone-else")
        live = Live()
        p = migrate.plan(home, root=root, validate=True, **live.kw())
        self.assertFalse(p["ready"])
        self.assertIn("different key", json.dumps(p["checks"]))
        with self.assertRaisesRegex(migrate.MigrateError, "different key"):
            migrate.apply(home, root=root, validate=True, **live.kw())
        self.assertEqual(secret.read_text(), "sk-ant-someone-else\n")

    def test_rollback_keeps_a_secret_another_account_resolves_to(self):
        root, home = _key_cousin(self)
        live = Live()
        migrate.apply(home, root=root, validate=True, **live.kw())
        with open(root / "config" / "accounts.toml", "a") as fh:
            fh.write('\n[accounts.shared]\nkind = "anthropic-key"\n'
                     'secret_file = ".secrets/accounts/wren-key"\n')
        back = migrate.rollback(home, root=root, **live.kw())
        from cousin_lib import accounts
        known = accounts.load(root)
        self.assertNotIn("wren-key", known)
        self.assertIn("shared", known)
        self.assertTrue((root / ".secrets" / "accounts" / "wren-key").exists())
        self.assertIn("shared use it", json.dumps(back["rollback_steps"]))

    def test_accounts_toml_comes_back_byte_for_byte(self):
        for before in (b'[accounts.team]\nkind = "claude-login"',          # no final newline
                       b'# ops\n[accounts.team]\nkind = "claude-login"\n\n\n',
                       None):                                               # no file at all
            with self.subTest(before=before):
                root, home = _key_cousin(self)
                path = root / "config" / "accounts.toml"
                if before is None:
                    path.unlink()
                else:
                    path.write_bytes(before)
                live = Live()
                rec = migrate.apply(home, root=root, validate=True, **live.kw())
                self.assertEqual(rec["state"], "migrated", rec)
                migrate.rollback(home, root=root, **live.kw())
                if before is None:
                    self.assertFalse(path.exists())
                else:
                    self.assertEqual(path.read_bytes(), before)

    def test_an_effort_carried_alone_is_validated_too(self):
        root, home = _root(self)
        (home / "cousin.toml").write_text('[cousin]\nslug = "wren"\n\n[runtime]\neffort = "low"\n')
        live = Live()
        p = migrate.plan(home, root=root, account="team", **live.kw())
        self.assertFalse(p["ready"])
        self.assertIn("effort 'low' would be written unvalidated",
                      json.dumps(p["checks"]))
        p = migrate.plan(home, root=root, account="team", validate=True, **live.kw())
        self.assertTrue(p["ready"], p)
        (_v, model, effort, acct), = [c for c in live.calls if c[0] == "validate"]
        self.assertEqual((model, effort, acct.name), (None, "low", "team"))

    def test_the_plan_says_validate_makes_the_accounts_config_dir(self):
        root, home = _key_cousin(self)
        for validate in (False, True):
            p = migrate.plan(home, root=root, validate=validate, **Live().kw())
            row = [c for c in p["checks"] if c["check"] == "validate"][0]
            self.assertIn("makes data/accounts/wren-key", row["detail"])


class TestFreshPacket(HermeticCase):
    def test_the_rollback_packet_is_the_cousins_state_now(self):
        """Review I7: tmux must not boot on the migration day's packet."""
        root, home = _root(self)
        gen = migrate.fresh_packet(home)
        pending = json.loads((home / "data" / "pending-boot.json").read_text())
        self.assertEqual(pending["generation"], gen)
        self.assertTrue(pathlib.Path(pending["packet"]).exists())


class TestCli(HermeticCase):
    def _main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = migrate.migrate_main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_apply_needs_yes(self):
        root, home = _root(self)
        with mock.patch.object(migrate, "_live", return_value=Live().kw()):
            rc, out, err = self._main("apply", "wren", "--account", "team")
            self.assertEqual(rc, 2)
            self.assertIn("--yes", err)
            self.assertEqual((home / "cousin.toml").read_bytes(), TOML.encode())
            rc, out, err = self._main("plan", "wren", "--account", "team", "--validate")
            self.assertEqual(rc, 0, err)
            self.assertIn("ready", out)
            rc, out, err = self._main("apply", "wren", "--account", "team", "--validate", "--yes")
            self.assertEqual(rc, 0, err)
            self.assertIn("migrated", out)


SID = "5e55a0de-0000-4000-8000-00000000abcd"
OLD_SID = "0ld5e55a-0000-4000-8000-000000000001"


def _transcripts(case, root, home, *, write_last=True, write_before=True):
    """config/harness.toml pointing at a scratch transcripts dir, the
    tmux lane's last session id in [runtime], and its transcript files.
    The dir is what the framework's resolver names for this home."""
    base = root / "transcripts"
    (root / "config" / "harness.toml").write_text(
        'transcripts_dir = "%s/{home_encoded}"\n' % base)
    (home / "cousin.toml").write_bytes(TOML.encode() + ('session_id = "%s"\r\n' % SID).encode())
    tdir = base / str(home).replace("/", "-")
    tdir.mkdir(parents=True)
    if write_before:
        (tdir / ("%s.jsonl" % OLD_SID)).write_text('{"type": "user"}\n')
        os.utime(tdir / ("%s.jsonl" % OLD_SID), (1000, 1000))
    if write_last:
        (tdir / ("%s.jsonl" % SID)).write_text('{"type": "assistant"}\n')
    return tdir


class TestHandover(HermeticCase):
    """#103: the working conversation does not carry across the move; the
    tmux lane's transcript path is recorded for the runner's first session."""

    def test_apply_records_the_transcripts_the_framework_resolves(self):
        root, home = _root(self)
        tdir = _transcripts(self, root, home)
        rec = migrate.apply(home, root=root, validate=True, account="team", **Live().kw())
        self.assertEqual(rec["state"], "migrated", rec)
        saved = json.loads((home / "data" / "previous-transcript.json").read_text())
        self.assertEqual([t["path"] for t in saved["transcripts"]],
                         [str(tdir / ("%s.jsonl" % SID)), str(tdir / ("%s.jsonl" % OLD_SID))])
        self.assertEqual(saved["session_id"], SID)
        self.assertIsNone(saved["missing"])
        self.assertTrue(saved["ended_at"])
        self.assertIn("handover", [s["step"] for s in rec["steps"]])
        # recorded after the close, before the runner's first start
        order = [s["step"] for s in rec["steps"]]
        self.assertLess(order.index("close"), order.index("handover"))
        self.assertLess(order.index("handover"), order.index("start"))

    def test_the_record_is_written_before_the_runner_starts(self):
        root, home = _root(self)
        _transcripts(self, root, home)
        live = Live()
        seen = []
        orig = live.start

        def start(home_, root_):
            seen.append((home_ / "data" / "previous-transcript.json").exists())
            return orig(home_, root_)
        live.start = start
        migrate.apply(home, root=root, validate=True, account="team", **live.kw())
        self.assertEqual(seen, [True])

    def test_a_missing_transcript_is_recorded_and_does_not_fail_the_migration(self):
        root, home = _root(self)
        _transcripts(self, root, home, write_last=False, write_before=False)
        rec = migrate.apply(home, root=root, validate=True, account="team", **Live().kw())
        self.assertEqual(rec["state"], "migrated", rec)
        saved = json.loads((home / "data" / "previous-transcript.json").read_text())
        self.assertEqual(saved["transcripts"], [])
        self.assertIn("not on disk", saved["missing"])

    def test_no_harness_seam_is_recorded_as_missing(self):
        root, home = _root(self)
        rec = migrate.apply(home, root=root, validate=True, account="team", **Live().kw())
        self.assertEqual(rec["state"], "migrated", rec)
        saved = json.loads((home / "data" / "previous-transcript.json").read_text())
        self.assertEqual(saved["transcripts"], [])
        self.assertIn("transcripts_dir", saved["missing"])

    def test_a_resolver_that_raises_is_recorded_not_fatal(self):
        root, home = _root(self)
        _transcripts(self, root, home)
        from cousin_lib import transcript_mine
        with mock.patch.object(transcript_mine, "transcripts_dir",
                               side_effect=OSError("the disk went away")):
            rec = migrate.apply(home, root=root, validate=True, account="team", **Live().kw())
        self.assertEqual(rec["state"], "migrated", rec)
        saved = json.loads((home / "data" / "previous-transcript.json").read_text())
        self.assertIn("the disk went away", saved["missing"])

    def test_rollback_removes_the_record(self):
        root, home = _root(self)
        _transcripts(self, root, home)
        live = Live(start_error="no child")
        migrate.apply(home, root=root, validate=True, account="team", **live.kw())
        self.assertTrue((home / "data" / "previous-transcript.json").exists())
        back = migrate.rollback(home, root=root, **live.kw())
        self.assertEqual(back["state"], "rolled_back")
        self.assertFalse((home / "data" / "previous-transcript.json").exists())
        self.assertIn("handover", [s["step"] for s in back["rollback_steps"]])

    def test_rollback_after_the_runner_consumed_it_removes_that_too(self):
        root, home = _root(self)
        _transcripts(self, root, home)
        live = Live()
        migrate.apply(home, root=root, validate=True, account="team", **live.kw())
        os.replace(home / "data" / "previous-transcript.json",
                   home / "data" / "previous-transcript.json.consumed")
        migrate.rollback(home, root=root, **live.kw())
        self.assertFalse((home / "data" / "previous-transcript.json.consumed").exists())

    def test_plan_prints_the_cost_line_and_apply_prints_the_paths(self):
        root, home = _root(self)
        tdir = _transcripts(self, root, home)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(migrate, "_live", return_value=Live().kw()), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = migrate.migrate_main(["plan", "wren", "--account", "team", "--validate"])
            plan_text = out.getvalue()
            rc2 = migrate.migrate_main(["apply", "wren", "--account", "team", "--validate",
                                        "--yes"])
        self.assertEqual((rc, rc2), (0, 0), err.getvalue())
        self.assertIn("The working conversation does not carry: the new session starts from"
                      " the state digest, the handoff and memory, and is handed the previous"
                      " transcript path", plan_text)
        applied = out.getvalue()[len(plan_text):]
        self.assertIn(str(tdir / ("%s.jsonl" % SID)), applied)
        self.assertIn(str(tdir / ("%s.jsonl" % OLD_SID)), applied)


class TestHandoffFreshness(HermeticCase):
    """#103: the runner starts from the handoff; apply says so when the
    close did not leave one written during it."""

    def test_a_handoff_written_during_the_close_is_fresh(self):
        root, home = _root(self)
        handoff = home / "data" / "handoff.md"
        handoff.write_text("# old\n")
        os.utime(handoff, (1000, 1000))
        live = Live()
        orig = live.close

        def close(slug, root_):
            handoff.write_text("# H\nnew\n")
            return orig(slug, root_)
        live.close = close
        rec = migrate.apply(home, root=root, validate=True, account="team", **live.kw())
        self.assertEqual(rec.get("warnings") or [], [])
        close_step = next(s for s in rec["steps"] if s["step"] == "close")
        self.assertIn("handoff written during the close", close_step["detail"])

    def test_a_stale_handoff_is_warned_with_its_age(self):
        root, home = _root(self)
        handoff = home / "data" / "handoff.md"
        handoff.write_text("# old\n")
        os.utime(handoff, (time.time() - 7200, time.time() - 7200))
        rec = migrate.apply(home, root=root, validate=True, account="team", **Live().kw())
        self.assertEqual(rec["state"], "migrated")          # a warning, never a blocker
        self.assertEqual(len(rec["warnings"]), 1)
        self.assertIn("not written during the close", rec["warnings"][0])
        self.assertIn("2.0h old", rec["warnings"][0])

    def test_no_handoff_at_all_is_warned(self):
        root, home = _root(self)
        rec = migrate.apply(home, root=root, validate=True, account="team", **Live().kw())
        self.assertIn("no data/handoff.md", rec["warnings"][0])

    def test_apply_prints_the_warning(self):
        root, home = _root(self)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(migrate, "_live", return_value=Live().kw()), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = migrate.migrate_main(["apply", "wren", "--account", "team", "--validate",
                                       "--yes"])
        self.assertEqual(rc, 0, err.getvalue())
        self.assertIn("warn", out.getvalue())
        self.assertIn("handoff", out.getvalue())


if __name__ == "__main__":
    unittest.main()
