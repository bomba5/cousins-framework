"""[agent] env_secrets (cousin_lib/env_secrets.py): a runner cousin's own
secret variables, each value in <home>/.secrets/env/<NAME>. The sdk
runner reads them at its start (a missing one is exit 2), hands them to
the CLI's environment and checks its .mcp.json against them; they never
reach the runner's own process environment. The invented cast; no model
or server is started."""
import json
import os
import pathlib

from cousin_lib import agent_settings, env_secrets
from cousin_lib.runner import main as runner_main
from cousin_lib.runner.base import RunnerError
from tests._hermetic import HermeticCase
from tests.runner._home import temp_home

VALUE = "lin_api_0123456789abcdef"
MCP = {"mcpServers": {"ledger": {"type": "http", "url": "https://ledger.invalid/mcp",
                                 "headers": {"Authorization": "Bearer ${LEDGER_KEY}"}}}}


def write_value(home, name, value=VALUE, mode=0o600, dir_mode=0o700):
    folder = pathlib.Path(home).joinpath(*env_secrets.DIR)
    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(folder.parent, 0o700)
    os.chmod(folder, dir_mode)
    path = folder / name
    path.write_text(value + "\n")
    os.chmod(path, mode)
    return path


def list_names(home, *names):
    with open(home / "cousin.toml", "a") as fh:
        fh.write("env_secrets = %s\n" % json.dumps(list(names)))


class SecretCase(HermeticCase):
    def setUp(self):
        super().setUp()
        self.home = temp_home(self)
        self.root = self.home.parent.parent
        (self.root / "config").mkdir(exist_ok=True)
        os.environ["FRAMEWORK_ROOT"] = str(self.root)
        os.environ.pop("LEDGER_KEY", None)


class TestNamesAndValues(SecretCase):
    def test_a_listed_value_is_read_through_the_private_reader(self):
        write_value(self.home, "LEDGER_KEY")
        self.assertEqual(env_secrets.load(self.home, {"env_secrets": ["LEDGER_KEY"]}),
                         {"LEDGER_KEY": VALUE})
        self.assertEqual(env_secrets.load(self.home, {}), {})

    def test_names_the_framework_sets_are_refused(self):
        for name in ("ANTHROPIC_API_KEY", "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN",
                     "COUSIN_HOME", "FRAMEWORK_ROOT", "PATH", "HOME", "LD_PRELOAD",
                     "PYTHONPATH", "LC_ALL"):
            with self.subTest(name=name):
                with self.assertRaisesRegex(env_secrets.EnvSecretError, name):
                    env_secrets.names_of({"env_secrets": [name]})
        for bad in ("LEDGER-KEY", "", 7, "1KEY"):
            with self.subTest(bad=bad):
                with self.assertRaises(env_secrets.EnvSecretError):
                    env_secrets.names_of({"env_secrets": [bad]})
        with self.assertRaisesRegex(env_secrets.EnvSecretError, "twice"):
            env_secrets.names_of({"env_secrets": ["LEDGER_KEY", "LEDGER_KEY"]})

    def test_a_missing_open_or_malformed_value_names_the_file_never_the_value(self):
        with self.assertRaisesRegex(env_secrets.EnvSecretError, "LEDGER_KEY does not exist"):
            env_secrets.read(self.home, "LEDGER_KEY")
        path = write_value(self.home, "LEDGER_KEY", mode=0o644)
        with self.assertRaisesRegex(env_secrets.EnvSecretError, "chmod 600") as caught:
            env_secrets.read(self.home, "LEDGER_KEY")
        self.assertNotIn(VALUE, str(caught.exception))
        os.chmod(path, 0o600)
        path.write_text("two words\n")
        with self.assertRaisesRegex(env_secrets.EnvSecretError, "no spaces"):
            env_secrets.read(self.home, "LEDGER_KEY")


class TestRunnerStart(SecretCase):
    def test_a_listed_name_without_its_value_is_exit_2_naming_the_file(self):
        list_names(self.home, "LEDGER_KEY")
        with self.assertRaisesRegex(RunnerError, r"\.secrets/env/LEDGER_KEY"):
            runner_main.runner_for(self.home)

    def test_the_values_reach_the_cli_env_and_the_mcp_check_never_this_process(self):
        list_names(self.home, "LEDGER_KEY")
        write_value(self.home, "LEDGER_KEY")
        (self.home / ".mcp.json").write_text(json.dumps(MCP))
        r = runner_main.runner_for(self.home)
        self.addCleanup(lambda: r.stop(timeout=5))
        opts = r.options()
        self.assertEqual(opts.env["LEDGER_KEY"], VALUE)
        self.assertNotIn("LEDGER_KEY", os.environ)
        # the entry names a variable this process does not have: it loads
        # because the runner sets it for the CLI, and stays `${...}` on file
        self.assertEqual(r._user_mcp.skipped, [])
        on_file = json.loads(pathlib.Path(opts.extra_args["mcp-config"]).read_text())
        self.assertEqual(on_file["mcpServers"]["ledger"]["headers"]["Authorization"],
                         "Bearer ${LEDGER_KEY}")
        self.assertNotIn(VALUE, json.dumps(on_file))
        self.assertEqual(r._server_environ()["LEDGER_KEY"], VALUE)

    def test_without_env_secrets_the_entry_is_skipped_as_before(self):
        (self.home / ".mcp.json").write_text(json.dumps(MCP))
        r = runner_main.runner_for(self.home)
        self.addCleanup(lambda: r.stop(timeout=5))
        opts = r.options()
        self.assertNotIn("LEDGER_KEY", opts.env)
        self.assertEqual([s["name"] for s in r._user_mcp.skipped], ["ledger"])


class TestSettings(SecretCase):
    def test_the_key_is_the_sdk_lanes_and_a_name_needs_its_value_first(self):
        self.assertIn("env_secrets", agent_settings.describe(self.home, self.root)["settings"])
        with self.assertRaises(agent_settings.SettingsError) as caught:
            agent_settings.validate(self.home, self.root, {"env_secrets": ["LEDGER_KEY"]})
        self.assertIn("does not exist", caught.exception.errors["env_secrets"])
        with self.assertRaises(agent_settings.SettingsError) as caught:
            agent_settings.validate(self.home, self.root, {"env_secrets": ["ANTHROPIC_API_KEY"]})
        self.assertIn("account variable", caught.exception.errors["env_secrets"])
        write_value(self.home, "LEDGER_KEY")
        self.assertEqual(agent_settings.validate(self.home, self.root,
                                                 {"env_secrets": ["LEDGER_KEY"]}),
                         {"env_secrets": ["LEDGER_KEY"]})

    def test_the_tmux_kind_does_not_read_it(self):
        home = temp_home(self, slug="testa", runner="tmux")
        self.assertNotIn("env_secrets", agent_settings.describe(home, self.root)["settings"])
