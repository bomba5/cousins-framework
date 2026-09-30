"""Plugin MCP servers reach the runner (docs/plugins.md): on the sdk kind in
the options beside `cousin` and the home's .mcp.json, on the opencode kind
in the rendered config as `local` entries. A fake plugin ("clock"); the
invented cast; no server or model is started."""
import json
import os
import pathlib
import unittest

from cousin_lib.runner import mcp_config, opencode
from tests import _plugins as fake
from tests._hermetic import HermeticCase
from tests.runner._home import temp_home


class PluginMcpCase(HermeticCase):
    kind = "sdk"

    def setUp(self):
        super().setUp()
        self.home = temp_home(self, runner=self.kind)
        self.root = self.home.parent.parent
        (self.root / "config").mkdir(exist_ok=True)
        os.environ["FRAMEWORK_ROOT"] = str(self.root)
        self.dir = fake.write_plugin(self.root, "clock", port=18190).resolve()
        fake.write_plugin(self.root, "dial", mcp=False, console=False, port=18191)
        fake.declare(self.root, "clock", "dial")


class TestLoad(PluginMcpCase):
    def test_not_enabled_adds_nothing(self):
        loaded = mcp_config.load(self.home, root=self.root)
        self.assertEqual((loaded.servers, loaded.listed, loaded.plugins), ({}, [], False))

    def test_an_enabled_plugin_is_a_stdio_server_named_after_it(self):
        fake.enable(self.home, "clock", "dial")
        loaded = mcp_config.load(self.home, root=self.root)
        self.assertEqual(list(loaded.servers), ["clock"])       # dial declares no [mcp]
        server = loaded.servers["clock"]
        self.assertEqual(server["type"], "stdio")
        self.assertEqual(server["args"], ["%s/mcp_server.py" % self.dir])
        self.assertEqual(server["env"]["COUSIN_SLUG"], "wren")
        self.assertEqual(server["env"]["COUSIN_HOME"], str(self.home))
        self.assertEqual(server["env"]["FRAMEWORK_ROOT"], str(self.root))
        self.assertEqual(server["env"]["PLUGIN_DIR"], str(self.dir))
        self.assertEqual(server["env"]["CLOCK_URL"], "http://127.0.0.1:18190")
        self.assertEqual(loaded.event()["servers"],
                         [{"name": "clock", "type": "stdio", "source": "plugin"}])

    def test_a_mcp_json_server_of_the_same_name_wins(self):
        fake.enable(self.home, "clock")
        (self.home / ".mcp.json").write_text(json.dumps(
            {"mcpServers": {"clock": {"command": "/opt/other-clock"}}}))
        loaded = mcp_config.load(self.home, root=self.root)
        self.assertEqual(loaded.servers["clock"]["command"], "/opt/other-clock")
        self.assertEqual(loaded.listed, [{"name": "clock", "type": "stdio"}])
        skip = [s for s in loaded.skipped if s.get("source") == "plugin"]
        self.assertEqual(len(skip), 1)
        self.assertEqual(skip[0]["name"], "clock")
        self.assertIn(".mcp.json declares a server named clock", skip[0]["reason"])

    def test_an_enabled_name_that_does_not_load_is_skipped_with_why(self):
        fake.enable(self.home, "ghost")
        loaded = mcp_config.load(self.home, root=self.root)
        self.assertEqual(loaded.servers, {})
        self.assertTrue(loaded.plugins)
        self.assertEqual(loaded.skipped, [{"name": "ghost", "source": "plugin",
                                           "reason": "not an enabled plugin in"
                                                     " config/plugins.toml"}])


try:
    import claude_agent_sdk  # noqa: F401
    HAVE_SDK = True
except ImportError:  # pragma: no cover
    HAVE_SDK = False


@unittest.skipUnless(HAVE_SDK, "claude-agent-sdk not installed")
class TestSdk(PluginMcpCase):
    def runner(self):
        from cousin_lib.runner.sdk import SdkRunner
        from tests.runner.test_sdk import ScriptedClient
        r = SdkRunner(self.home, client_factory=lambda o: ScriptedClient(o, []))
        self.addCleanup(lambda: r.stop(timeout=5))
        return r

    def events(self, r):
        return [e["payload"] for e in r.events() if e["kind"] == "mcp_config"]

    def test_the_plugin_server_reaches_the_options_and_the_event(self):
        fake.enable(self.home, "clock")
        r = self.runner()
        servers = r.options().mcp_servers
        self.assertEqual(list(servers), ["cousin", "clock"])
        self.assertEqual(servers["clock"]["type"], "stdio")
        self.assertNotIn("alwaysLoad", servers["clock"])
        said = self.events(r)
        self.assertEqual(len(said), 1)
        self.assertEqual(said[0]["servers"], [{"name": "clock", "type": "stdio",
                                               "source": "plugin"}])

    def test_without_plugins_nothing_changes(self):
        r = self.runner()
        self.assertEqual(list(r.options().mcp_servers), ["cousin"])
        self.assertEqual(self.events(r), [])


class TestOpencode(PluginMcpCase):
    kind = "opencode"

    def test_render_config_adds_local_entries(self):
        from cousin_lib import accounts
        account = accounts.Account("lab", "opencode", None, None,
                                   data_dir=self.root / "acct", endpoint="http://127.0.0.1:9/v1",
                                   endpoint_model="m1")
        fake.enable(self.home, "clock")
        loaded = mcp_config.add_plugins(mcp_config.Loaded(False), self.home, self.root)
        server = loaded.servers["clock"]
        config = opencode.render_config(account, model="local/m1", small_model="local/m1",
                                        mcp_url="http://127.0.0.1:9/mcp", mcp_token="t",
                                        servers={"clock": server})
        self.assertEqual(sorted(config["mcp"]), ["clock", "cousin"])
        entry = config["mcp"]["clock"]
        self.assertEqual(entry["type"], "local")
        self.assertEqual(entry["command"], [server["command"], "%s/mcp_server.py" % self.dir])
        self.assertEqual(entry["environment"]["COUSIN_SLUG"], "wren")
        self.assertIs(entry["enabled"], True)
        # the effective-config check expects exactly the rendered set
        opencode.check_effective_config(config, {}, account=account, model="local/m1",
                                        small_model="local/m1", servers=["clock"])
        with self.assertRaisesRegex(Exception, "not only the cousin's"):
            opencode.check_effective_config(config, {}, account=account, model="local/m1",
                                            small_model="local/m1")

    def test_the_runner_renders_and_announces_them(self):
        from tests.runner.test_opencode import OpencodeCase, Factory, _wait
        fake.enable(self.home, "clock")
        toml = (self.home / "cousin.toml").read_text()
        (self.home / "cousin.toml").write_text(toml.replace(
            'runner = "opencode"\n', 'runner = "opencode"\nmodel = "local/m1"\n'))
        case = OpencodeCase()
        case.root = self.root
        factory = Factory()
        r = opencode.OpencodeRunner(self.home, server_factory=factory, account=case.account())
        self.addCleanup(lambda: r.stop(timeout=5))
        r.start()
        self.assertTrue(_wait(lambda: r.opencode_session is not None or r.fatal, 10))
        self.assertIsNone(r.fatal)
        config = json.loads(pathlib.Path(factory.calls[0]["config_path"]).read_text())
        self.assertEqual(sorted(config["mcp"]), ["clock", "cousin"])
        said = [e["payload"] for e in r.events() if e["kind"] == "mcp_config"]
        self.assertEqual(len(said), 1)
        self.assertEqual(said[0]["servers"], [{"name": "clock", "type": "stdio",
                                               "source": "plugin"}])
