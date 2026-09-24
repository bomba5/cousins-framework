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
        return dict(auth_check=self.auth_check, supervisor_up=lambda root: self.supervisor,
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
    return [c[0] for c in live.calls if c[0] != "auth"]


class TestPlan(HermeticCase):
    def test_a_plan_lists_checks_and_steps_and_writes_nothing(self):
        root, home = _root(self)
        before = _tree(root)
        live = Live()
        p = migrate.plan(home, root=root, account="team", **live.kw())
        self.assertTrue(p["ready"], p)
        self.assertEqual(p["steps"], list(migrate.STEPS))
        self.assertEqual(_tree(root), before)
        self.assertEqual((home / "cousin.toml").read_bytes(), TOML.encode())
        self.assertEqual(_names(live), [])

    def test_a_blocker_makes_the_plan_not_ready_and_apply_refuses(self):
        root, home = _root(self)
        for live, needle in ((Live(logged_in=False), "cousin-account login"),
                             (Live(supervisor=False), "cousin-supervisor"),
                             (Live(tmux=False), "it is stopped")):
            p = migrate.plan(home, root=root, account="team", **live.kw())
            self.assertFalse(p["ready"])
            self.assertIn(needle, json.dumps(p["checks"]))
            with self.assertRaisesRegex(migrate.MigrateError, "not ready"):
                migrate.apply(home, root=root, account="team", **live.kw())
            self.assertEqual((home / "cousin.toml").read_bytes(), TOML.encode())
            self.assertFalse((home / migrate.RECORD).exists())

    def test_an_unknown_account_is_a_blocker(self):
        root, home = _root(self)
        p = migrate.plan(home, root=root, account="nobody", **Live().kw())
        self.assertFalse(p["ready"])
        self.assertIn("not in config/accounts.toml", json.dumps(p["checks"]))

    def test_a_runner_cousin_has_nothing_to_migrate(self):
        root, home = _root(self)
        (home / "cousin.toml").write_text(TOML + '\n[agent]\nrunner = "sdk"\n')
        p = migrate.plan(home, root=root, **Live().kw())
        self.assertFalse(p["ready"])
        self.assertIn("already on the runner lane", json.dumps(p["checks"]))


class TestApply(HermeticCase):
    def test_apply_runs_the_steps_in_order_and_records_the_exact_prior_file(self):
        root, home = _root(self)
        (home / "data" / "pending-boot.json").write_text('{"generation": 3}')
        live = Live()
        rec = migrate.apply(home, root=root, account="team", **live.kw())
        self.assertEqual(rec["state"], "migrated")
        self.assertEqual(_names(live), ["close", "import", "start", "verify"])
        self.assertEqual(live.calls[-2][1], {"runner": "sdk", "account": "team"})
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
        rec = migrate.apply(home, root=root, account="team", **live.kw())
        self.assertEqual((rec["state"], rec["steps"][-1]["step"]), ("failed", "toml"))
        self.assertIn("up again", rec["steps"][-1]["detail"])
        self.assertEqual((home / "cousin.toml").read_bytes(), TOML.encode())

    def test_a_failed_clean_stop_stops_before_anything_changes(self):
        root, home = _root(self)
        live = Live(close_ok=False)
        rec = migrate.apply(home, root=root, account="team", **live.kw())
        self.assertEqual(rec["state"], "failed")
        self.assertIn("refusing a concurrent one", rec["steps"][-1]["detail"])
        self.assertEqual((home / "cousin.toml").read_bytes(), TOML.encode())
        self.assertNotIn("start", _names(live))

    def test_a_second_apply_is_refused_until_a_rollback(self):
        root, home = _root(self)
        live = Live(start_error="no child")
        migrate.apply(home, root=root, account="team", **live.kw())
        with self.assertRaisesRegex(migrate.MigrateError, "rollback"):
            migrate.apply(home, root=root, account="team", **live.kw())


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
        rec = migrate.apply(home, root=root, account="team", **live.kw())
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
        migrate.apply(home, root=root, account="team", **live.kw())
        self.assertTrue(live.runner_up)
        migrate.rollback(home, root=root, **live.kw())
        self.assertFalse(live.runner_up)
        self.assertEqual((home / "cousin.toml").read_bytes(), TOML.encode())

    def test_a_runner_that_never_lets_go_is_refused_and_nothing_restored(self):
        root, home = _root(self)
        live = Live(stop_answer="stopping", runner_down_after=10 ** 6)
        migrate.apply(home, root=root, account="team", **live.kw())
        with self.assertRaisesRegex(migrate.MigrateError, "still holds its lock"):
            migrate.rollback(home, root=root, **live.kw())
        self.assertIn(b'runner = "sdk"', (home / "cousin.toml").read_bytes())
        self.assertNotIn("start_tmux", _names(live))

    def test_a_second_rollback_is_refused(self):
        """Review I6(b): it would stop the live tmux session with no handoff."""
        root, home = _root(self)
        live = Live(start_error="no child")
        migrate.apply(home, root=root, account="team", **live.kw())
        migrate.rollback(home, root=root, **live.kw())
        calls = len(live.calls)
        with self.assertRaisesRegex(migrate.MigrateError, "already rolled back"):
            migrate.rollback(home, root=root, **live.kw())
        self.assertEqual(len(live.calls), calls)

    def test_after_a_failed_close_rollback_touches_no_process(self):
        """Review I6(c): the refused close left a flip running; nothing to undo."""
        root, home = _root(self)
        live = Live(close_ok=False)
        migrate.apply(home, root=root, account="team", **live.kw())
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
        migrate.apply(home, root=root, account="team", **live.kw())
        back = migrate.rollback(home, root=root, **live.kw())
        self.assertEqual(back["state"], "rolled_back")
        self.assertNotIn("start_tmux", _names(live))

    def test_a_failing_step_is_recorded_and_reported_not_a_traceback(self):
        root, home = _root(self)
        live = Live(start_error="no child")
        migrate.apply(home, root=root, account="team", **live.kw())

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
        migrate.apply(home, root=root, account="team", **live.kw())
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
        migrate.apply(home, root=root, account="team", **live.kw())
        rec = json.loads((home / migrate.RECORD).read_text())
        rec["steps"] = [s for s in rec["steps"] if s["step"] not in ("toml", "start")]
        (home / migrate.RECORD).write_text(json.dumps(rec))
        self.assertIn(b'runner = "sdk"', (home / "cousin.toml").read_bytes())
        migrate.rollback(home, root=root, **live.kw())
        self.assertEqual((home / "cousin.toml").read_bytes(), TOML.encode())

    def test_a_rollback_releases_the_runner_hold(self):
        root, home = _root(self)
        live = Live()
        migrate.apply(home, root=root, account="team", **live.kw())
        migrate.rollback(home, root=root, **live.kw())
        self.assertEqual(_names(live)[-1], "release")


class TestReMigration(HermeticCase):
    def test_what_the_cousin_wrote_on_tmux_between_migrations_is_not_held(self):
        """Review round 2 N1: apply, rollback, writes on tmux, apply again:
        the gate's cursor opens afresh, and the start sweep holds none."""
        from cousin_lib import memory, review_gate
        root, home = _root(self)
        live = Live()
        migrate.apply(home, root=root, account="team", **live.kw())
        migrate.rollback(home, root=root, **live.kw())
        with open(pathlib.Path(home, *review_gate.STATE), "w") as fh:     # days pass on tmux
            json.dump({"cursor": time.time() - 5 * 86400, "seen": []}, fh)
        for i in range(5):
            memory.remember(home, "pantry %d" % i, "Shelf %d holds the quokka tins." % i)
        live.tmux = True
        rec = migrate.apply(home, root=root, account="team", **live.kw())
        self.assertEqual(rec["state"], "migrated")
        review_gate.begin(home)                                 # the runner's start sweep
        self.assertEqual(review_gate.hold_new(home), [])

    def test_rollback_refuses_while_rows_wait_or_the_inbox_is_unreadable(self):
        root, home = _root(self)
        live = Live()
        migrate.apply(home, root=root, account="team", **live.kw())
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
            rc, out, err = self._main("plan", "wren", "--account", "team")
            self.assertEqual(rc, 0, err)
            self.assertIn("ready", out)
            rc, out, err = self._main("apply", "wren", "--account", "team", "--yes")
            self.assertEqual(rc, 0, err)
            self.assertIn("migrated", out)


if __name__ == "__main__":
    unittest.main()
