"""Phase 11 Task 6: the in-pane launcher (I6, R3, R13). The pane runs
`exec env -i <env_base> <python> <launcher> --home H [--fresh] -- claude ...`;
the launcher adds the account and the kind's switches, applies the hard
deny again, and execs the CLI. No secret in any argv."""
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

from cousin_lib.runner import tmux_launch
from tests._hermetic import HermeticCase
from tests.runner._home import temp_home

FORBIDDEN = ("-p", "--print", "--output-format", "--input-format", "--strict-mcp-config")
RUNNER_ENV = {"HOME": "/srv/wren", "PATH": "/usr/bin:/bin", "USER": "wren", "LANG": "C.UTF-8",
              "LC_TIME": "C", "TZ": "UTC", "SSH_AUTH_SOCK": "/run/agent.sock",
              "CLAUDE_CODE_ENTRYPOINT": "sdk-py", "CLAUDECODE": "1",
              "ANTHROPIC_API_KEY": "sk-ant-fixture-runner", "CLAUDE_EXTRA": "x",
              "OPENAI_API_KEY": "sk-fixture-openai", "EDITOR": "vi", "KEEP_ME": "1"}


class TestEnvBase(unittest.TestCase):
    def test_the_base_allowlist_lc_and_env_allow(self):
        env = tmux_launch.env_base(RUNNER_ENV, env_allow=("KEEP_ME",))
        self.assertEqual(env, {"HOME": "/srv/wren", "PATH": "/usr/bin:/bin", "USER": "wren",
                               "LANG": "C.UTF-8", "LC_TIME": "C", "TZ": "UTC",
                               "SSH_AUTH_SOCK": "/run/agent.sock", "KEEP_ME": "1"})

    def test_the_hard_deny_beats_env_allow(self):
        env = tmux_launch.env_base(RUNNER_ENV, env_allow=("CLAUDE_EXTRA", "ANTHROPIC_API_KEY",
                                                          "CLAUDECODE"))
        for name in ("CLAUDE_EXTRA", "ANTHROPIC_API_KEY", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
            self.assertNotIn(name, env)


class TestEnvAllow(unittest.TestCase):
    def test_env_allow_is_a_list_of_names_the_deny_does_not_take(self):
        self.assertEqual(tmux_launch.env_allow_of({}), ())
        self.assertEqual(tmux_launch.env_allow_of({"env_allow": ["KEEP_ME", "GH_HOST"]}),
                         ("KEEP_ME", "GH_HOST"))
        for bad in ("KEEP_ME", ["bad-name"], [3], ["CLAUDE_EXTRA"], ["ANTHROPIC_BASE_URL"]):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError) as err:
                    tmux_launch.env_allow_of({"env_allow": bad})
                self.assertIn("env_allow", str(err.exception))


class TestArgv(unittest.TestCase):
    def test_a_fresh_start_mints_the_session_and_asks_for_the_context(self):
        argv = tmux_launch.argv(launcher=pathlib.Path("cousin_lib/runner/tmux_launch.py"),
                                home=pathlib.Path("/srv/fw/cousins/wren"),
                                session=("new", "0f7c6a2e-1111-4222-8333-444455556666"),
                                model="opus", effort="high", fresh=True)
        self.assertEqual(argv[0], sys.executable)
        self.assertTrue(os.path.isabs(argv[1]))
        self.assertEqual(argv[2:], [
            "--home", "/srv/fw/cousins/wren", "--fresh", "--", "claude", "--model", "opus",
            "--effort", "high", "--session-id", "0f7c6a2e-1111-4222-8333-444455556666",
            "--dangerously-skip-permissions", "--setting-sources", "project,local"])

    def test_a_resume_without_a_model_or_effort_leaves_the_clis_defaults(self):
        argv = tmux_launch.argv(launcher=pathlib.Path("/opt/l.py"), home=pathlib.Path("/h"),
                                session=("resume", "s-1"), model=None, effort=None, fresh=False)
        self.assertEqual(argv[2:], ["--home", "/h", "--", "claude", "--resume", "s-1",
                                    "--dangerously-skip-permissions", "--setting-sources",
                                    "project,local"])
        for flag in FORBIDDEN:
            self.assertNotIn(flag, argv)
        with self.assertRaises(ValueError):
            tmux_launch.argv(launcher=pathlib.Path("/opt/l.py"), home=pathlib.Path("/h"),
                             session=("adopt", "s-1"), model=None, effort=None, fresh=False)


class LaunchCase(HermeticCase):
    def home(self, accounts_toml=None, account=None):
        home = temp_home(self, runner="tmux")
        root = home.parent.parent
        os.environ["FRAMEWORK_ROOT"] = str(root)
        if accounts_toml:
            (root / "config").mkdir(exist_ok=True)
            (root / "config" / "accounts.toml").write_text(accounts_toml)
        if account:
            with open(home / "cousin.toml", "a") as f:
                f.write('account = "%s"\n' % account)
        (home / "data" / "run").mkdir(parents=True, exist_ok=True)
        (home / "data" / "run" / "tmux-context.md").write_text("# Framework law\n\nthe block\n")
        return home

    def run_main(self, home, *, fresh, environ):
        claude = ["claude", "--model", "opus", "--session-id" if fresh else "--resume", "s-1",
                  "--dangerously-skip-permissions", "--setting-sources", "project,local"]
        args = ["--home", str(home)] + (["--fresh"] if fresh else []) + ["--"] + claude
        seen = {}

        def execvpe(file, argv, env):
            seen.update(file=file, argv=list(argv), env=dict(env))
            raise SystemExit(0)
        with mock.patch.dict(os.environ, environ, clear=True), \
                mock.patch.object(tmux_launch.os, "execvpe", execvpe):
            try:
                rc = tmux_launch.main(args)
            except SystemExit as done:
                rc = done.code
        return rc, seen


class TestMain(LaunchCase):
    def test_the_clis_environment_and_argv_after_the_runners(self):
        """The Global Constraints' test: a runner environment holding the SDK's
        markers, a key and an env_allow naming a CLAUDE* variable reaches the
        CLI with none of them and none of the print-mode flags."""
        home = self.home()
        base = tmux_launch.env_base(RUNNER_ENV, env_allow=("CLAUDE_EXTRA",))
        base["FRAMEWORK_ROOT"] = os.environ["FRAMEWORK_ROOT"]
        rc, seen = self.run_main(home, fresh=True, environ=base)
        self.assertEqual(rc, 0)
        self.assertEqual(seen["file"], "claude")
        for name in ("CLAUDE_CODE_ENTRYPOINT", "CLAUDECODE", "ANTHROPIC_API_KEY", "CLAUDE_EXTRA"):
            self.assertNotIn(name, seen["env"])
        self.assertEqual(seen["env"]["CLAUDE_CODE_DISABLE_AUTO_MEMORY"], "1")
        self.assertEqual(seen["env"]["DISABLE_AUTOUPDATER"], "1")
        for flag in FORBIDDEN:
            self.assertNotIn(flag, seen["argv"])
        at = seen["argv"].index("--append-system-prompt")
        self.assertEqual(seen["argv"][at + 1], "# Framework law\n\nthe block\n")
        self.assertNotIn("sk-", " ".join(seen["argv"]))

    def test_a_resume_gets_no_appended_block(self):
        home = self.home()
        rc, seen = self.run_main(home, fresh=False, environ={"PATH": "/usr/bin",
                                                              "FRAMEWORK_ROOT": os.environ["FRAMEWORK_ROOT"]})
        self.assertEqual(rc, 0)
        self.assertNotIn("--append-system-prompt", seen["argv"])

    def test_a_named_login_keeps_its_own_config_dir_through_the_deny(self):
        home = self.home('[accounts.team]\nkind = "claude-login"\nconfig_dir = "accounts/team"\n',
                         account="team")
        rc, seen = self.run_main(home, fresh=False, environ={"PATH": "/usr/bin",
                                                              "FRAMEWORK_ROOT": os.environ["FRAMEWORK_ROOT"],
                                                              "CLAUDE_CONFIG_DIR": "/stray"})
        self.assertEqual(rc, 0)
        self.assertTrue(seen["env"]["CLAUDE_CONFIG_DIR"].endswith("accounts/team"))

    def test_a_token_or_key_account_is_refused_naming_a4(self):
        for name, kind in (("fleet", "claude-token"), ("metered", "anthropic-key")):
            with self.subTest(kind=kind):
                home = self.home('[accounts.%s]\nkind = "%s"\n' % (name, kind), account=name)
                err = []
                with mock.patch.object(tmux_launch, "_say", err.append):
                    rc, seen = self.run_main(home, fresh=False, environ={
                        "PATH": "/usr/bin", "FRAMEWORK_ROOT": os.environ["FRAMEWORK_ROOT"]})
                self.assertEqual(rc, 2)
                self.assertEqual(seen, {})                     # never exec'd
                self.assertIn("A4", err[0])
                self.assertIn("P11-6", err[0])

    def test_a_print_mode_flag_is_refused(self):
        home = self.home()
        err = []
        with mock.patch.object(tmux_launch, "_say", err.append), \
                mock.patch.dict(os.environ, {"PATH": "/usr/bin",
                                             "FRAMEWORK_ROOT": os.environ["FRAMEWORK_ROOT"]}, clear=True), \
                mock.patch.object(tmux_launch.os, "execvpe") as ex:
            rc = tmux_launch.main(["--home", str(home), "--", "claude", "-p", "hi"])
        self.assertEqual(rc, 2)
        ex.assert_not_called()


if __name__ == "__main__":
    unittest.main()
