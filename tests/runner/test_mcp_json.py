"""A runner cousin's own MCP servers: <home>/.mcp.json, Claude Code's own
format, merged beside the in-process `cousin` server. Invented cast and
invented values only; nothing here starts a server or a model."""
import asyncio
import json
import os
import unittest
from unittest import mock

try:
    import claude_agent_sdk  # noqa: F401
except ImportError:  # pragma: no cover
    raise unittest.SkipTest("claude-agent-sdk not installed")

from cousin_lib.runner.sdk import SdkRunner
from tests._hermetic import HermeticCase
from tests.runner._home import temp_home
from tests.runner.test_sdk import ScriptedClient

SECRET = "tok-4b1d-never-in-the-stream"


class McpCase(HermeticCase):
    def setUp(self):
        super().setUp()
        self.home = temp_home(self, runner="sdk")
        root = self.home.parent.parent
        (root / "config").mkdir(exist_ok=True)
        os.environ["FRAMEWORK_ROOT"] = str(root)

    def write(self, servers=None, *, raw=None):
        path = self.home / ".mcp.json"
        path.write_text(raw if raw is not None else json.dumps({"mcpServers": servers}))

    def runner(self, cls=SdkRunner, **kw):
        r = cls(self.home, client_factory=lambda o: ScriptedClient(o, []), **kw)
        self.addCleanup(lambda: r.stop(timeout=5))
        return r

    def events(self, r):
        return [e["payload"] for e in r.events() if e["kind"] == "mcp_config"]

    def stream_bytes(self, r):
        return r.stream.path.read_bytes()


class TestShapes(McpCase):
    def test_a_stdio_and_an_http_server_reach_the_options(self):
        self.write({
            "notes": {"command": "/opt/notes-mcp", "args": ["--root", "/srv/notes"],
                      "env": {"NOTES_MODE": "ro"}},
            "ha": {"type": "http", "url": "http://ha.example:8123/mcp",
                   "headers": {"X-Client": "wren"}},
            "feed": {"type": "sse", "url": "http://feed.example/sse"},
        })
        servers = self.runner().options().mcp_servers
        self.assertEqual(servers["notes"], {"type": "stdio", "command": "/opt/notes-mcp",
                                            "args": ["--root", "/srv/notes"],
                                            "env": {"NOTES_MODE": "ro"}})
        self.assertEqual(servers["ha"], {"type": "http", "url": "http://ha.example:8123/mcp",
                                         "headers": {"X-Client": "wren"}})
        self.assertEqual(servers["feed"], {"type": "sse", "url": "http://feed.example/sse"})
        # the cousin's own server is untouched and still always loaded; the
        # user servers are not (the CLI defers them behind its tool search)
        self.assertEqual(servers["cousin"]["type"], "sdk")
        self.assertIs(servers["cousin"]["alwaysLoad"], True)
        for name in ("notes", "ha", "feed"):
            self.assertNotIn("alwaysLoad", servers[name])

    def test_the_event_names_each_server_and_its_type_once_per_runner(self):
        self.write({"ha": {"type": "http", "url": "http://ha.example/mcp"},
                    "notes": {"command": "notes-mcp"}})
        r = self.runner()
        r.options()
        r.options()                      # a reconnect: the same set, said once
        said = self.events(r)
        self.assertEqual(len(said), 1, said)
        self.assertEqual(said[0]["servers"], [{"name": "ha", "type": "http"},
                                              {"name": "notes", "type": "stdio"}])
        self.assertEqual(said[0]["skipped"], [])

    def test_no_file_is_only_cousin_and_says_nothing(self):
        r = self.runner()
        self.assertEqual(list(r.options().mcp_servers), ["cousin"])
        self.assertEqual(self.events(r), [])

    def test_a_key_the_sdk_type_lacks_is_dropped_and_named(self):
        self.write({"ha": {"type": "http", "url": "http://ha.example/mcp",
                           "headersHelper": "/bin/make-headers"}})
        r = self.runner()
        self.assertNotIn("headersHelper", r.options().mcp_servers["ha"])
        self.assertEqual(self.events(r)[0]["servers"],
                         [{"name": "ha", "type": "http", "ignored_keys": ["headersHelper"]}])


class TestReserved(McpCase):
    def test_a_cousin_entry_is_skipped_and_the_event_says_why(self):
        # what spawn writes for the tmux lane's cousin-mcp
        self.write({"cousin": {"command": "/usr/bin/cousin-mcp", "args": ["--home", "/x"]},
                    "ha": {"type": "http", "url": "http://ha.example/mcp"}})
        r = self.runner()
        servers = r.options().mcp_servers
        self.assertEqual(servers["cousin"]["type"], "sdk")      # never the stdio one
        self.assertNotIn("command", servers["cousin"])
        self.assertIn("ha", servers)
        skipped = self.events(r)[0]["skipped"]
        self.assertEqual([s["name"] for s in skipped], ["cousin"])
        self.assertIn("reserved", skipped[0]["reason"])


class TestExpansion(McpCase):
    FILE = {
        "ha": {"type": "http", "url": "${HA_URL:-http://ha.example:8123}/mcp",
               "headers": {"Authorization": "Bearer ${HA_TOKEN}"}},
        "notes": {"command": "${NOTES_BIN}", "args": ["--key", "${HA_TOKEN}"],
                  "env": {"TOKEN": "${HA_TOKEN}", "MODE": "${NOTES_MODE:-ro}"}},
    }

    def test_variables_pass_through_unexpanded_for_the_cli_to_expand(self):
        # measured: the bundled CLI (2.1.281) expands ${VAR} and ${VAR:-d} in
        # command, args, env, url and headers from its own environment
        self.write(self.FILE)
        with mock.patch.dict(os.environ, {"HA_TOKEN": SECRET, "NOTES_BIN": "/opt/notes"}):
            r = self.runner()
            servers = r.options().mcp_servers
        self.assertEqual(servers["ha"], {"type": "http",
                                         "url": "${HA_URL:-http://ha.example:8123}/mcp",
                                         "headers": {"Authorization": "Bearer ${HA_TOKEN}"}})
        self.assertEqual(servers["notes"], {"type": "stdio", "command": "${NOTES_BIN}",
                                            "args": ["--key", "${HA_TOKEN}"],
                                            "env": {"TOKEN": "${HA_TOKEN}",
                                                    "MODE": "${NOTES_MODE:-ro}"}})

    def test_the_secret_is_never_in_the_options_the_cli_argv_or_the_home(self):
        from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport
        self.write(self.FILE)
        with mock.patch.dict(os.environ, {"HA_TOKEN": SECRET, "NOTES_BIN": "/opt/notes"}):
            r = self.runner()
            opts = r.options()
            transport = SubprocessCLITransport(prompt="", options=opts)
            transport._cli_path = "/nonexistent/claude"
            argv = transport._build_command()
        user = {k: v for k, v in opts.mcp_servers.items() if k != "cousin"}
        self.assertEqual(sorted(user), ["ha", "notes"])
        self.assertNotIn(SECRET, json.dumps(user))
        config = argv[argv.index("--mcp-config") + 1]
        self.assertIn("${HA_TOKEN}", config)       # the CLI gets the name ...
        self.assertNotIn(SECRET, config)           # ... never the value
        self.assertFalse([a for a in argv if SECRET in a])
        self.assertTrue(self.events(r))
        self.assertNotIn(SECRET.encode(), self.stream_bytes(r))
        for path in self.home.rglob("*"):
            if path.is_file() and path.name != ".mcp.json":
                self.assertNotIn(SECRET.encode(), path.read_bytes(), path)

    def test_a_reference_with_a_default_loads_even_when_unset(self):
        self.write({"ha": {"type": "http", "url": "${WREN_UNSET_URL:-http://fallback}/mcp"}})
        servers = self.runner().options().mcp_servers
        self.assertEqual(servers["ha"]["url"], "${WREN_UNSET_URL:-http://fallback}/mcp")

    def test_an_account_variable_skips_the_entry_even_with_a_default(self):
        # the CLI's environment carries the cousin's own credential under
        # these names (options.env); a server must never be handed it
        self.write({"leak": {"type": "http", "url": "http://x.example/mcp",
                             "headers": {"X-Key": "${ANTHROPIC_API_KEY:-none}"}},
                    "tok": {"command": "t", "env": {"T": "${CLAUDE_CODE_OAUTH_TOKEN}"}},
                    "ok": {"command": "ok-mcp"}})
        r = SdkRunner(self.home, client_factory=lambda o: ScriptedClient(o, []),
                      api_key="sk-test-account")
        self.addCleanup(lambda: r.stop(timeout=5))
        opts = r.options()
        self.assertEqual(list(opts.mcp_servers), ["cousin", "ok"])
        skipped = {s["name"]: s["reason"] for s in self.events(r)[0]["skipped"]}
        self.assertIn("ANTHROPIC_API_KEY", skipped["leak"])
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", skipped["tok"])

    def test_an_unset_variable_with_no_default_skips_the_entry_naming_the_variable(self):
        self.write({"ha": {"type": "http", "url": "http://ha.example/mcp",
                           "headers": {"Authorization": "Bearer ${WREN_UNSET_TOKEN}"}},
                    "notes": {"command": "notes-mcp"}})
        r = self.runner()
        servers = r.options().mcp_servers
        self.assertEqual(list(servers), ["cousin", "notes"])
        skipped = self.events(r)[0]["skipped"]
        self.assertEqual(skipped[0]["name"], "ha")
        self.assertIn("WREN_UNSET_TOKEN", skipped[0]["reason"])


class TestInvalid(McpCase):
    def test_a_malformed_file_is_skipped_and_the_cousin_still_has_cousin(self):
        self.write(raw='{"mcpServers": {"ha": ')
        r = self.runner()
        self.assertEqual(list(r.options().mcp_servers), ["cousin"])
        skipped = self.events(r)[0]["skipped"]
        self.assertEqual(len(skipped), 1)
        self.assertIsNone(skipped[0]["name"])
        self.assertIn(".mcp.json", skipped[0]["reason"])

    def test_a_file_the_parser_cannot_even_recurse_through_is_not_fatal(self):
        self.write(raw="[" * 100000 + "]" * 100000)       # RecursionError, not ValueError
        r = self.runner()
        self.assertEqual(list(r.options().mcp_servers), ["cousin"])
        self.assertIsNone(self.events(r)[0]["skipped"][0]["name"])

    def test_a_file_that_is_not_an_object_of_servers_is_skipped(self):
        for raw in ('[]', '{"mcpServers": []}', '{"mcpServers": {"ha": "http://x"}}'):
            self.write(raw=raw)
            r = self.runner()
            self.assertEqual(list(r.options().mcp_servers), ["cousin"], raw)
            self.assertTrue(self.events(r)[0]["skipped"], raw)

    def test_an_unknown_type_or_a_missing_field_skips_only_that_entry(self):
        self.write({"ws": {"type": "websocket", "url": "ws://x.example"},
                    "nocmd": {"args": ["x"]},
                    "nourl": {"type": "http"},
                    "badargs": {"command": "x", "args": "--flag"},
                    "ok": {"command": "ok-mcp"}})
        r = self.runner()
        self.assertEqual(list(r.options().mcp_servers), ["cousin", "ok"])
        skipped = {s["name"]: s["reason"] for s in self.events(r)[0]["skipped"]}
        self.assertEqual(sorted(skipped), ["badargs", "nocmd", "nourl", "ws"])
        self.assertIn("websocket", skipped["ws"])


class TestOrder(McpCase):
    def test_the_set_is_cousin_first_then_by_name_whatever_the_file_order(self):
        self.write({"zeta": {"command": "z"}, "alpha": {"command": "a"},
                    "mid": {"type": "http", "url": "http://m.example"}})
        a = self.runner().options().mcp_servers
        self.write({"mid": {"type": "http", "url": "http://m.example"},
                    "alpha": {"command": "a"}, "zeta": {"command": "z"}})
        b = self.runner().options().mcp_servers
        self.assertEqual(list(a), ["cousin", "alpha", "mid", "zeta"])

        def user(servers):
            return json.dumps({k: v for k, v in servers.items() if k != "cousin"})
        self.assertEqual(list(a), list(b))
        self.assertEqual(user(a), user(b))

    def test_the_system_prompt_does_not_depend_on_the_file(self):
        r1 = self.runner()
        without = r1.options().system_prompt
        self.write({"ha": {"type": "http", "url": "http://ha.example/mcp"}})
        with_file = self.runner().options().system_prompt
        self.assertEqual(without, with_file)


class TestSideSession(McpCase):
    def test_a_side_session_gets_the_same_set_as_the_primary(self):
        from cousin_lib.runner import sessions
        self.write({"ha": {"type": "http", "url": "http://ha.example/mcp"},
                    "notes": {"command": "notes-mcp"}})
        s = sessions.Sessions(self.home, kinds=("peer",),
                              client_factory=lambda o: ScriptedClient(o, []))
        self.addCleanup(lambda: s.stop(timeout=5))
        primary = s.primary.options().mcp_servers
        side = s.sides["peer"].options().mcp_servers
        self.assertEqual(list(primary), ["cousin", "ha", "notes"])
        self.assertEqual(list(side), list(primary))
        self.assertEqual({k: v for k, v in side.items() if k != "cousin"},
                         {k: v for k, v in primary.items() if k != "cousin"})
        # the side stream still opens with its own head, the event after it
        kinds = [e["kind"] for e in s.sides["peer"].events()]
        self.assertEqual(kinds[0], sessions.SIDE_HEAD)
        self.assertIn("mcp_config", kinds)


class TestPolicy(McpCase):
    def test_a_policy_deny_on_a_user_server_tool_is_enforced(self):
        self.write({"ha": {"type": "http", "url": "http://ha.example/mcp"}})
        (self.home / "policy.toml").write_text(
            'deny_tools = ["mcp__ha__call_service"]\nask = ["mcp__ha__*"]\n')
        opts = self.runner().options()
        self.assertIn("ha", opts.mcp_servers)
        gate = opts.hooks["PreToolUse"][0].hooks[0]

        def decide(tool):
            out = asyncio.run(gate({"hook_event_name": "PreToolUse", "tool_name": tool,
                                    "tool_input": {"entity_id": "light.hall"}}, "t", {}))
            return (out or {}).get("hookSpecificOutput", {}).get("permissionDecision")
        self.assertEqual(decide("mcp__ha__call_service"), "deny")
        self.assertEqual(decide("mcp__ha__get_state"), "deny")    # ask is enforced as deny
        self.assertIn(decide("mcp__notes__read"), (None, "allow"))


if __name__ == "__main__":
    unittest.main()
