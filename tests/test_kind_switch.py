"""Phase 11 Task 11a (R17, I9, P11-11, P11-13): `cousin-migrate --to sdk|tmux`,
the kind switch between runner kinds. The session id in
data/runner-session.json is the continuity: the source stops at idle
keeping it, the target resumes it."""
import json
import os
import pathlib
import tempfile
import tomllib
import unittest
from unittest import mock

from cousin_lib import migrate
from cousin_lib.harness_settings import settings_path
from cousin_lib.runner.inbox import Inbox
from tests._hermetic import HermeticCase


class SwitchCase(HermeticCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.home = self.root / "cousins" / "wren"
        (self.home / "data").mkdir(parents=True)
        (self.root / "config").mkdir()
        (self.root / "config" / "accounts.toml").write_text(
            '[accounts.team]\nkind = "claude-login"\nconfig_dir = "accounts/team"\n')
        self.config_dir = self.root / "accounts" / "team"
        self.config_dir.mkdir(parents=True)
        self.kind("sdk")
        (self.home / "data" / "runner-session.json").write_text(
            json.dumps({"session_id": "s-live", "lane": "login"}))
        p = mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": str(self.root)})
        p.start(); self.addCleanup(p.stop)
        self.calls = []
        self.verified = (True, "a turn start under s-live")

    def kind(self, kind, account="team"):
        (self.home / "cousin.toml").write_text(
            '[cousin]\nslug = "wren"\nname = "Wren"\n\n[agent]\nrunner = "%s"\naccount = "%s"\n'
            % (kind, account))

    def trust(self):
        (self.config_dir / ".claude.json").write_text(json.dumps(
            {"projects": {str(self.home): {"hasTrustDialogAccepted": True}}}))

    def live(self):
        def close(home, root):
            self.calls.append("close")

        def start(home, root):
            self.calls.append("start")

        def verify(home, root, session_id, to, since):
            self.calls.append(("verify", session_id, to))
            return self.verified

        def cursor_end(home, root, session_id, to):
            self.calls.append(("cursor", session_id, to))
            return 4096
        return dict(close=close, start=start, verify=verify, cursor_end=cursor_end,
                    supervisor_up=lambda root: True)

    def agent(self):
        return tomllib.loads((self.home / "cousin.toml").read_text())["agent"]


class TestPlan(SwitchCase):
    def test_an_sdk_born_home_is_ready_and_told_the_pane_asks_once(self):
        """Finding 2 (live proofs 09-25): ~/.claude.json is no gate. A live
        CLI rewrites it and may never record the trust for the home, so the
        plan cannot know in advance; the pane asks once and verify waits."""
        p = migrate.switch_plan(self.home, root=self.root, to="tmux", **self.live())
        self.assertEqual(p["steps"], ["trust", "close", "toml", "start", "verify"])
        self.assertTrue(p["ready"], p["checks"])
        trust = next(c for c in p["checks"] if c["check"] == "trust")
        self.assertTrue(trust["ok"])
        self.assertIn("not known in advance: the pane asks once", trust["detail"])
        self.assertIn("tmux-wren", trust["detail"])

    def test_a_trusted_home_is_ready_and_the_sdk_way_has_no_trust_step(self):
        self.trust()
        p = migrate.switch_plan(self.home, root=self.root, to="tmux", **self.live())
        self.assertTrue(p["ready"], p["checks"])
        trust = next(c for c in p["checks"] if c["check"] == "trust")
        self.assertIn("recorded", trust["detail"])            # the fast path, said
        self.kind("tmux")
        p = migrate.switch_plan(self.home, root=self.root, to="sdk", **self.live())
        self.assertEqual(p["steps"], ["close", "toml", "start", "verify"])
        self.assertTrue(p["ready"], p["checks"])

    def test_an_account_level_mcp_server_is_warned_and_its_absence_is_not(self):
        self.trust()
        p = migrate.switch_plan(self.home, root=self.root, to="tmux", **self.live())
        self.assertEqual(p["warnings"], [])
        data = json.loads((self.config_dir / ".claude.json").read_text())
        data["mcpServers"] = {"github": {"command": "gh-mcp"}}
        (self.config_dir / ".claude.json").write_text(json.dumps(data))
        p = migrate.switch_plan(self.home, root=self.root, to="tmux", **self.live())
        self.assertEqual(len(p["warnings"]), 1)
        self.assertIn("github", p["warnings"][0])
        self.assertIn("P11-13", p["warnings"][0])
        self.assertTrue(p["ready"])                            # a warning, not a refusal

    def test_what_cannot_switch_is_refused_by_name(self):
        self.trust()
        # same kind
        self.kind("tmux")
        p = migrate.switch_plan(self.home, root=self.root, to="tmux", **self.live())
        self.assertFalse(p["ready"]); self.assertIn("already", " ".join(c["detail"] for c in p["checks"]))
        # no recorded session
        self.kind("sdk")
        (self.home / "data" / "runner-session.json").unlink()
        p = migrate.switch_plan(self.home, root=self.root, to="tmux", **self.live())
        self.assertFalse(p["ready"]); self.assertIn("session", " ".join(
            c["detail"] for c in p["checks"] if not c["ok"]))
        # a tmux rollover's new id its CLI has not written yet (I5)
        self.kind("tmux")
        (self.home / "data" / "runner-session.json").write_text(
            json.dumps({"session_id": "s-new", "lane": "login", "fresh": True}))
        p = migrate.switch_plan(self.home, root=self.root, to="sdk", **self.live())
        self.assertFalse(p["ready"]); self.assertIn("rollover in flight", " ".join(
            c["detail"] for c in p["checks"] if not c["ok"]))
        self.kind("sdk")
        (self.home / "data" / "runner-session.json").write_text(
            json.dumps({"session_id": "s-live", "lane": "login"}))
        # the legacy lane is migrate's own path, not a kind switch
        (self.home / "cousin.toml").write_text('[cousin]\nslug = "wren"\nname = "Wren"\n')
        p = migrate.switch_plan(self.home, root=self.root, to="tmux", **self.live())
        self.assertFalse(p["ready"]); self.assertIn("legacy", " ".join(c["detail"] for c in p["checks"]))
        # a token or key account never reaches the tmux kind (P11-6)
        (self.root / "config" / "accounts.toml").write_text('[accounts.fleet]\nkind = "claude-token"\n')
        self.kind("sdk", account="fleet")
        p = migrate.switch_plan(self.home, root=self.root, to="tmux", **self.live())
        self.assertFalse(p["ready"]); self.assertIn("A4", " ".join(c["detail"] for c in p["checks"]))


class TestCheckWarns(SwitchCase):
    def test_check_warns_on_an_account_level_mcp_server_for_the_tmux_kind(self):
        self.kind("tmux")
        c = migrate.check(self.home, root=self.root, cli_version=lambda: "x")
        self.assertEqual(c["warnings"], [])
        (self.config_dir / ".claude.json").write_text(json.dumps(
            {"mcpServers": {"github": {"command": "gh-mcp"}}}))
        c = migrate.check(self.home, root=self.root, cli_version=lambda: "x")
        self.assertEqual(len(c["warnings"]), 1)
        self.assertIn("P11-13", c["warnings"][0])


class TestApply(SwitchCase):
    def test_apply_needs_no_recorded_trust_and_says_the_pane_asks(self):
        rec = migrate.switch_apply(self.home, root=self.root, to="tmux", **self.live())
        self.assertEqual(rec["state"], "switched")
        trust = next(s for s in rec["steps"] if s["step"] == "trust")
        self.assertIn("not known in advance", trust["detail"])

    def test_to_tmux_runs_every_step_and_keeps_what_rollback_needs(self):
        self.trust()
        rec = migrate.switch_apply(self.home, root=self.root, to="tmux", **self.live())
        self.assertEqual(rec["state"], "switched")
        self.assertEqual(rec["warnings"], [])
        self.assertEqual([s["step"] for s in rec["steps"]],
                         ["trust", "close", "toml", "cursor", "start", "notice", "verify"])
        self.assertEqual(self.agent()["runner"], "tmux")
        self.assertEqual(json.loads(settings_path(self.home).read_text())["editorMode"], "normal")
        # the cursor before the start (review minor): the target's first
        # turn end mines from the switch's EOF, never from the other kind's
        self.assertEqual(self.calls, ["close", ("cursor", "s-live", "tmux"), "start",
                                      ("verify", "s-live", "tmux")])
        notice = [r for r in (Inbox(self.home).get(i) for i in range(1, 5)) if r]
        self.assertEqual(len(notice), 1)
        self.assertEqual((notice[0]["thread_id"], notice[0]["source"]), ("system", "boot"))
        self.assertIn("sdk kind to the tmux kind", notice[0]["body"])
        self.assertEqual(json.loads((self.home / "data" / "extract-cursor.json").read_text()),
                         {"s-live": 4096})                     # nothing mined twice (P11-9)
        rec2 = migrate.read_switch_record(self.home)
        self.assertEqual((rec2["from"], rec2["to"], rec2["session_id"]), ("sdk", "tmux", "s-live"))
        self.assertIn("prior_toml_b64", rec2)

    def test_to_tmux_warns_on_a_policy_gap_deny_tools_cannot_close(self):
        """I5: deny_bash_patterns and ask never reach the pane; the switch
        itself surfaces that, not only a plan run before it."""
        self.trust()
        (self.home / "policy.toml").write_text('ask = ["Bash"]\n')
        rec = migrate.switch_apply(self.home, root=self.root, to="tmux", **self.live())
        self.assertEqual(rec["state"], "switched")
        self.assertEqual(len(rec["warnings"]), 1)
        self.assertIn("ask", rec["warnings"][0])
        self.assertIn("do not reach the tmux pane", rec["warnings"][0])

    def test_to_sdk_removes_the_kinds_settings(self):
        self.trust()
        migrate.switch_apply(self.home, root=self.root, to="tmux", **self.live())
        rec = migrate.switch_apply(self.home, root=self.root, to="sdk", **self.live())
        self.assertEqual([s["step"] for s in rec["steps"]],
                         ["close", "toml", "cursor", "start", "notice", "verify"])
        self.assertEqual(self.agent()["runner"], "sdk")
        self.assertNotIn("editorMode", json.loads(settings_path(self.home).read_text()))

    def test_a_verify_that_fails_is_recorded_and_raised(self):
        self.trust()
        self.verified = (False, "no turn start under s-live in 90s")
        with self.assertRaises(migrate.MigrateError) as err:
            migrate.switch_apply(self.home, root=self.root, to="tmux", **self.live())
        self.assertIn("no turn start", str(err.exception))
        self.assertEqual(migrate.read_switch_record(self.home)["state"], "failed")


class TestVerifyWaitsForTheTrustDialog(SwitchCase):
    """Finding 2: the pane shows the trust dialog on a home it never saw;
    the tmux runner types nothing, writes data/login-required.json {screen:
    trust} and emits `auth login_required`. Verify then neither fails nor
    rolls back: it says what it waits for, and waits for the operator up to
    TRUST_WAIT_S; the turn start after the acceptance completes it."""

    def setUp(self):
        super().setUp()
        self.now = 1000.0
        self.said = []
        self.since = 5000.0                       # wall time of the switch's start

    def clock(self):
        return self.now

    def sleep(self, s):
        self.now += s

    def dialog(self, screen="trust", ts=None):
        (self.home / "data" / "login-required.json").write_text(json.dumps(
            {"kind": "tmux", "screen": screen, "ts": self.since + 1 if ts is None else ts}))

    def wait(self, started):
        return migrate.wait_for_turn(self.home, started, root=self.root, since=self.since,
                                     what="turn start under s-live", say=self.said.append,
                                     clock=self.clock, sleep=self.sleep)

    def test_the_trust_dialog_extends_the_wait_and_the_acceptance_completes_it(self):
        t0 = self.now

        def started():
            if self.now - t0 >= 5 and not self.said:
                self.dialog()                      # the runner saw the dialog
            if self.now - t0 >= 300:               # well past VERIFY_S: the operator accepts
                (self.home / "data" / "login-required.json").unlink(missing_ok=True)
                return self.now - t0 >= 302
            return False
        ok, detail = self.wait(started)
        self.assertTrue(ok, detail)
        self.assertGreater(self.now - t0, migrate.VERIFY_S)
        self.assertEqual(len(self.said), 1)
        self.assertIn("waiting for the operator to accept the trust dialog in the pane",
                      self.said[0])
        self.assertIn("console", self.said[0])
        self.assertIn("tmux -S %s attach -t tmux-wren" % (self.root / "run" / "tmux.sock"),
                      self.said[0])
        self.assertIn("trust dialog", detail)

    def test_a_dialog_nobody_accepts_fails_at_the_longer_deadline_with_the_hint(self):
        self.dialog()
        t0 = self.now
        ok, detail = self.wait(lambda: False)
        self.assertFalse(ok)
        self.assertGreaterEqual(self.now - t0, migrate.TRUST_WAIT_S)
        self.assertLess(self.now - t0, migrate.TRUST_WAIT_S + 5)
        self.assertIn("no turn start under s-live", detail)
        self.assertIn("trust dialog", detail)
        self.assertIn("tmux-wren", detail)

    def test_no_dialog_fails_at_the_usual_deadline_without_a_hint(self):
        t0 = self.now
        ok, detail = self.wait(lambda: False)
        self.assertFalse(ok)
        self.assertLess(self.now - t0, migrate.VERIFY_S + 5)
        self.assertNotIn("dialog", detail)
        self.assertEqual(self.said, [])

    def test_a_dialog_file_older_than_the_switch_is_not_this_one(self):
        self.dialog(ts=self.since - 60)
        t0 = self.now
        ok, _detail = self.wait(lambda: False)
        self.assertFalse(ok)
        self.assertLess(self.now - t0, migrate.VERIFY_S + 5)
        self.assertEqual(self.said, [])

    def test_a_failure_after_the_wait_keeps_the_rollback_line(self):
        self.verified = (False, "no turn start under s-live within 600s; the pane showed the"
                                " trust dialog")
        with self.assertRaises(migrate.MigrateError) as err:
            migrate.switch_apply(self.home, root=self.root, to="tmux", **self.live())
        self.assertIn("trust dialog", str(err.exception))
        self.assertIn("roll back with --to sdk", str(err.exception))


class TestTrustScreenOnTheRunner(SwitchCase):
    def test_the_runner_types_nothing_and_records_the_trust_screen(self):
        """What wait_for_turn reads is what the tmux runner writes (P0)."""
        import time
        from cousin_lib.delivery import Item
        from cousin_lib.runner.tmux_runner import TmuxRunner
        from tests.runner._fake_pane import FakePane
        self.kind("tmux")
        panes = []

        def factory(path):
            panes.append(FakePane(path, attention="trust", context_home=self.home))
            return panes[-1]
        r = TmuxRunner(self.home, account=None, pane_factory=factory, config_dir=self.config_dir,
                       launch_argv=lambda sid, fresh: ["claude", sid] + (["--fresh"] if fresh else []))
        self.addCleanup(lambda: r.stop(timeout=5))
        since = time.time()
        r.start()
        migrate._switch_notice(self.home, "sdk", "tmux")
        t = time.monotonic()
        while time.monotonic() - t < 5 and migrate.pane_dialog(self.home, since) is None:
            time.sleep(0.02)
        self.assertEqual(migrate.pane_dialog(self.home, since), "trust")
        self.assertEqual(panes[0].typed, [])
        self.assertTrue(any(e["kind"] == "auth" and e["payload"].get("screen") == "trust"
                            for e in r.events()))


if __name__ == "__main__":
    unittest.main()


class TestRollback(SwitchCase):
    """Phase 11 Task 11b (R17): the way back from a kind switch, keeping the
    session: the runner stopped, cousin.toml and the kind's settings as they
    were, the runner started again on the same session id."""

    def test_a_switch_is_rolled_back_to_the_bytes_and_settings_it_left(self):
        self.trust()
        before = (self.home / "cousin.toml").read_bytes()
        session = (self.home / "data" / "runner-session.json").read_bytes()
        migrate.switch_apply(self.home, root=self.root, to="tmux", **self.live())
        self.calls.clear()
        rec = migrate.switch_rollback(self.home, root=self.root, to="sdk", **self.live())
        self.assertEqual(rec["state"], "rolled_back")
        self.assertEqual((self.home / "cousin.toml").read_bytes(), before)
        self.assertNotIn("editorMode", json.loads(settings_path(self.home).read_text()))
        self.assertEqual((self.home / "data" / "runner-session.json").read_bytes(), session)
        self.assertEqual(self.calls, ["close", ("cursor", "s-live", "sdk"), "start"])
        with self.assertRaises(migrate.MigrateError) as err:
            migrate.switch_rollback(self.home, root=self.root, to="sdk", **self.live())
        self.assertIn("already", str(err.exception))

    def test_a_failed_switch_rolls_back_too_and_the_tmux_way_restores_its_settings(self):
        self.trust()
        self.kind("tmux")
        from cousin_lib import harness_settings
        harness_settings.apply_project_settings(self.home, root=self.root, kind="tmux")
        before = (self.home / "cousin.toml").read_bytes()
        self.verified = (False, "no session_init under s-live within 90s")
        with self.assertRaises(migrate.MigrateError):
            migrate.switch_apply(self.home, root=self.root, to="sdk", **self.live())
        self.assertNotIn("editorMode", json.loads(settings_path(self.home).read_text()))
        rec = migrate.switch_rollback(self.home, root=self.root, to="tmux", **self.live())
        self.assertEqual(rec["state"], "rolled_back")
        self.assertEqual((self.home / "cousin.toml").read_bytes(), before)
        self.assertEqual(json.loads(settings_path(self.home).read_text())["editorMode"], "normal")

    def test_what_cannot_be_rolled_back_is_refused(self):
        with self.assertRaises(migrate.MigrateError) as err:
            migrate.switch_rollback(self.home, root=self.root, to="sdk", **self.live())
        self.assertIn("no kind switch", str(err.exception))
        self.trust()
        migrate.switch_apply(self.home, root=self.root, to="tmux", **self.live())
        with self.assertRaises(migrate.MigrateError) as err:
            migrate.switch_rollback(self.home, root=self.root, to="tmux", **self.live())
        self.assertIn("came from sdk", str(err.exception))


class TestSwitchMidTurn(SwitchCase):
    """Review C5: tmux -> sdk with a row mid-turn. The close is the
    supervisor's held stop; the target's start sweeps the claims the source
    left (runner.main._serve). The row is delivered exactly once."""

    def test_a_row_mid_turn_at_the_close_is_delivered_once(self):
        import time
        from cousin_lib.delivery import Item
        from cousin_lib.runner.fake import FakeRunner
        from cousin_lib.runner.tmux_runner import TmuxRunner
        from tests.runner._fake_pane import FakePane

        def wait(pred, timeout=10.0):
            t = time.monotonic()
            while time.monotonic() - t < timeout:
                if pred():
                    return True
                time.sleep(0.02)
            return False
        self.kind("tmux")
        panes = []

        def factory(path):
            panes.append(FakePane(path, slow=True, slow_s=5.0, context_home=self.home))
            return panes[-1]
        source = TmuxRunner(self.home, account=None, pane_factory=factory,
                            config_dir=self.config_dir,
                            launch_argv=lambda sid, fresh: ["claude", sid] + (["--fresh"] if fresh else []))
        self.addCleanup(lambda: source.stop(timeout=5))
        source.start()
        rec = source.enqueue(Item("operator:wren", "chat", "mid-turn at the switch", sender="Wren"))
        self.assertTrue(wait(lambda: source.state() == "running"))
        targets = []

        def close(home, root):              # the supervisor's stop: the hold, then SIGTERM
            (home / "run").mkdir(exist_ok=True)
            (home / "run" / "held").write_text("2026-09-24T23:00:00+00:00 cousin-migrate")
            source.stop(timeout=10)

        def start(home, root):              # the sdk kind's _serve: sweep, then run
            Inbox(home).requeue_stale(older_than_s=0.0)
            target = FakeRunner(home)
            targets.append(target)
            self.addCleanup(lambda: target.stop(timeout=5))
            target.start()
        live = dict(self.live(), close=close, start=start)
        migrate.switch_apply(self.home, root=self.root, to="sdk", **live)
        self.assertTrue(wait(lambda: Inbox(self.home).get(rec.inbox_id)["state"] == "done"))
        time.sleep(0.5)                     # the target had its chance to deliver it again
        again = [e for e in targets[0].events() if e["kind"] == "turn_start"
                 and rec.inbox_id in (e["payload"].get("inbox_ids") or [])]
        self.assertEqual(again, [], "delivered again by the target kind")
        typed = [t for p in panes for t in p.typed if "mid-turn at the switch" in t[1]]
        self.assertEqual(len(typed), 1)
        self.assertEqual(Inbox(self.home).get(rec.inbox_id)["outcome"], "delivered")
        self.assertEqual(panes[0].kills, 1, "the held stop killed the pane")
