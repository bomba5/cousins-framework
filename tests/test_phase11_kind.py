"""Phase 11 Task 1: the `tmux` runner kind and the lane branches around it.
(The kind's runner is TmuxRunner, Tasks 2-4; here: the kind itself, what
runner_for refuses for it, the serve loop's claim recovery, spawn, and the
migrate plan's lane check.)"""
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from cousin_lib import delivery, migrate, spawn
from cousin_lib.delivery import Item
from cousin_lib.runner import main as runner_main
from cousin_lib.runner.base import RunnerError
from tests._hermetic import HermeticCase
from tests.runner._home import temp_home
from tests.test_migrate import Live, _root
from tests.test_spawn_supervisor import _CreateCase


def _accounts(root, text):
    (root / "config").mkdir(exist_ok=True)
    (root / "config" / "accounts.toml").write_text(text)


class TestTheKind(HermeticCase):
    def test_tmux_is_a_runner_kind(self):
        self.assertEqual(delivery.RUNNER_KINDS, ("sdk", "fake", "opencode", "tmux"))
        self.assertIs(runner_main.KINDS, delivery.RUNNER_KINDS)

    def test_on_a_subscription_login_runner_for_builds_the_tmux_runner(self):
        from cousin_lib.runner.tmux_runner import TmuxRunner
        home = temp_home(self, runner="tmux")
        with open(home / "cousin.toml", "a") as f:
            f.write('model = "opus"\neffort = "high"\n')
        r = runner_main.runner_for(home)
        self.assertIsInstance(r, TmuxRunner)
        self.assertEqual((r.model, r.effort), ("opus", "high"))
        self.assertTrue(r.recovers_claims)
        self.assertFalse(r.worker_alive())                     # built, not started

    def test_agent_env_allow_reaches_the_pane_allowlist_and_a_credential_is_refused(self):
        home = temp_home(self, runner="tmux")
        with open(home / "cousin.toml", "a") as f:
            f.write('env_allow = ["PGHOST"]\n')
        r = runner_main.runner_for(home)
        self.assertEqual(r.env_allow, ("PGHOST",))
        os.environ["PGHOST"] = "db.local"
        self.addCleanup(os.environ.pop, "PGHOST", None)
        self.assertIn("PGHOST", r._env_base())
        bad = temp_home(self, runner="tmux")
        with open(bad / "cousin.toml", "a") as f:
            f.write('env_allow = ["GH_TOKEN"]\n')
        with self.assertRaises(RunnerError) as err:
            runner_main.runner_for(bad)
        self.assertIn("env_allow", str(err.exception))

    def test_the_tmux_kind_refuses_side_sessions(self):
        home = temp_home(self, runner="tmux")
        with open(home / "cousin.toml", "a") as f:
            f.write('\n[agent.sessions]\npeer = "own"\n')
        with self.assertRaises(RunnerError) as err:
            runner_main.runner_for(home)
        self.assertIn("side sessions need", str(err.exception))

    def test_the_tmux_kind_runs_on_a_subscription_login_only(self):
        """P11-6: a token or key account is refused until a login-free
        config dir is shown to start with no menu."""
        root_kinds = (("fleet", 'kind = "claude-token"\n'), ("metered", 'kind = "anthropic-key"\n'))
        for name, body in root_kinds:
            with self.subTest(account=name):
                home = temp_home(self, runner="tmux")
                root = home.parent.parent
                _accounts(root, "[accounts.%s]\n%s" % (name, body))
                secrets = root / ".secrets" / "accounts"
                secrets.mkdir(parents=True)
                os.chmod(root / ".secrets", 0o700); os.chmod(secrets, 0o700)
                (secrets / name).write_text("sk-fixture\n"); os.chmod(secrets / name, 0o600)
                with open(home / "cousin.toml", "a") as f:
                    f.write('account = "%s"\n' % name)
                with self.assertRaises(RunnerError) as err:
                    runner_main.runner_for(home)
                self.assertIn("subscription login", str(err.exception))
                self.assertIn("P11-6", str(err.exception))
                self.assertNotIn("sk-fixture", str(err.exception))


class TestServeRecovery(HermeticCase):
    def test_a_runner_that_recovers_its_claims_is_not_swept_by_serve(self):
        """P11-9: the tmux kind's claims can be live in a pane that outlived
        its runner; it recovers them itself in start(), so _serve must not
        requeue them first."""
        home = temp_home(self, runner="fake")
        runner = runner_main.runner_for(home)
        runner.recovers_claims = True
        with mock.patch.object(runner.inbox, "requeue_stale") as sweep:
            self.assertEqual(runner_main._serve(runner, True), 0)
        sweep.assert_not_called()
        plain = runner_main.runner_for(home)
        with mock.patch.object(plain.inbox, "requeue_stale", return_value=0) as sweep:
            self.assertEqual(runner_main._serve(plain, True), 0)
        sweep.assert_called_once_with(older_than_s=0.0)


class TestSpawn(_CreateCase):
    def test_spawn_takes_the_tmux_kind(self):
        self.create(runner="tmux")
        self.assertIn('runner = "tmux"', self.toml())

    def test_a_tmux_born_cousin_gets_the_kinds_settings_and_bridge_hooks(self):
        import json
        from cousin_lib.harness_settings import TMUX_HOOK_EVENTS, TMUX_HOOK_MODULE, TMUX_KEYS
        self.create(runner="tmux")
        data = json.loads((self.home / ".claude" / "settings.json").read_text())
        for key, value in TMUX_KEYS.items():
            self.assertEqual(data.get(key), value, key)
        for event in TMUX_HOOK_EVENTS:
            self.assertIn(TMUX_HOOK_MODULE, json.dumps(data["hooks"].get(event)), event)

    def test_an_sdk_born_cousin_gets_none_of_them(self):
        import json
        from cousin_lib.harness_settings import TMUX_KEYS
        self.create(runner="sdk")
        data = json.loads((self.home / ".claude" / "settings.json").read_text())
        self.assertFalse(set(TMUX_KEYS) & set(data))


class TestMigratePlan(HermeticCase):
    def test_a_runner_kind_is_never_read_as_the_legacy_lane(self):
        """The lane check read every runner kind but sdk and fake as "on the
        tmux lane"; the tmux kind is a runner kind, switched with --to."""
        for kind in ("tmux", "opencode"):
            with self.subTest(kind=kind):
                root, home = _root(self)
                with open(home / "cousin.toml", "a") as f:
                    f.write('\n[agent]\nrunner = "%s"\n' % kind)
                p = migrate.plan(home, root=root, **Live().kw())
                lane = next(c for c in p["checks"] if c["check"] == "lane")
                self.assertFalse(lane["ok"], lane)
                self.assertNotIn("on the tmux lane", lane["detail"])
                self.assertIn(kind, lane["detail"])
                if kind == "tmux":
                    self.assertIn("runner kind", lane["detail"])
                    self.assertIn("--to", lane["detail"])


if __name__ == "__main__":
    unittest.main()
