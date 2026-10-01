"""The MCP console routes: the MCP registries, .mcp.json, cousin-mcp's
diagnostics and policy.toml. Every edit is validated with the parser the
runner reads the file with, a literal secret never reaches .mcp.json or
an answer, and nothing here touches a real home, harness file or model.
Invented names and values only."""
import json
import os
import stat
import tomllib

from cousin_lib.runner import policy as runner_policy
from tests.console._harness import ConsoleCase

REG = """# a test registry
ceiling = 2
timeout = 60
max_output = 5000

[tools.alpha]
command = "true"
description = "the first"

[tools.alpha.commands.run]
argv = ["run"]

[tools.beta]
command = "true"
enabled = false

[tools.beta.commands.run]
argv = ["run"]

[tools.gamma]
command = "true"
enabled = false

[tools.gamma.commands.run]
argv = ["run"]
"""

# a runner-lane registry: memory.search has an in-process handler, alpha none
RUNNER_REG = """ceiling = 4

[tools.memory]
command = "cousin-memory"

[tools.memory.commands.search]
argv = ["search"]

[tools.alpha]
command = "true"
enabled = false

[tools.alpha.commands.run]
argv = ["run"]
"""

EXAMPLE = """# the shipped example
ceiling = 12

[tools.send]
kind = "send"
operators = []

[tools.send.commands.peer]
command = "cousin-chat"
argv = ["send"]

[tools.send.commands.operator]
command = "cousin-reply"
argv = ["--user"]
"""

LITERAL = "sk-" + "Q7x" * 12          # shaped like a key, invented
OPAQUE = "Zq8" * 10                    # mixed case and digits, 30 characters


class McpCase(ConsoleCase):
    def setUp(self):
        super().setUp()
        (self.root / "config" / "mcp-registry.toml.example").write_text(EXAMPLE)

    def wren(self, registry=REG, runner=None, operator="Ana"):
        extra = '\n[agent]\nrunner = "%s"\n' % runner if runner else ""
        home = self.cousin("wren", extra=extra, operator=operator)
        if registry is not None:
            (home / "mcp-registry.toml").write_text(registry)
        return home

    def harness(self, **keys):
        lines = ['mcp_logs_dir = "%s/cache/{home_encoded}/mcp-logs-{server}"' % self.root]
        lines += ['%s = "%s"' % (k, v) for k, v in keys.items()]
        (self.root / "config" / "harness.toml").write_text("\n".join(lines) + "\n")

    def write(self, home, servers, extra=None):
        doc = dict(extra or {}, mcpServers=servers)
        (home / ".mcp.json").write_text(json.dumps(doc, indent=2))

    def stream(self, home, *events):
        path = home / "data" / "stream" / "s1.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [{"seq": 1, "ts": 1.0, "kind": "runner", "payload": {}}]
        rows += [{"seq": i + 2, "ts": 2.0 + i, "kind": k, "payload": p}
                 for i, (k, p) in enumerate(events)]
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))


class TestCousinRegistry(McpCase):
    def test_get_shows_tools_numbers_and_an_etag(self):
        self.wren()
        self.serve()
        status, body = self.get("/api/cousins/wren/mcp/registry")
        self.assertEqual(status, 200, body)
        self.assertEqual((body["exists"], body["source"], body["error"]), (True, "own", None))
        self.assertEqual((body["ceiling"], body["timeout"], body["max_output"]), (2, 60, 5000))
        self.assertEqual([(t["name"], t["enabled"]) for t in body["tools"]],
                         [("alpha", True), ("beta", False), ("gamma", False)])
        self.assertTrue(body["etag"])

    def test_a_toggle_and_a_number_are_written_in_place(self):
        home = self.wren()
        self.serve()
        etag = self.get("/api/cousins/wren/mcp/registry")[1]["etag"]
        status, body = self.post("/api/cousins/wren/mcp/registry", {
            "etag": etag, "timeout": 90, "tools": {"beta": True, "alpha": True}})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["restart_required"])
        text = (home / "mcp-registry.toml").read_text()
        self.assertTrue(text.startswith("# a test registry\n"))
        data = tomllib.loads(text)
        self.assertEqual((data["timeout"], data["tools"]["beta"]["enabled"]), (90, True))
        self.assertNotIn("enabled", data["tools"]["alpha"])     # unchanged, not rewritten
        self.assertEqual([(t["name"], t["enabled"]) for t in body["tools"]][:2],
                         [("alpha", True), ("beta", True)])

    def test_the_registry_parser_refuses_more_tools_than_the_ceiling(self):
        home = self.wren()
        self.serve()
        etag = self.get("/api/cousins/wren/mcp/registry")[1]["etag"]
        status, body = self.post("/api/cousins/wren/mcp/registry", {
            "etag": etag, "tools": {"beta": True, "gamma": True}})
        self.assertEqual(status, 400, body)
        self.assertIn("ceiling", body["error"])
        self.assertEqual((home / "mcp-registry.toml").read_text(), REG)

    def test_bad_values_a_stale_etag_and_unknown_tools_are_refused(self):
        self.wren()
        self.serve()
        etag = self.get("/api/cousins/wren/mcp/registry")[1]["etag"]
        for payload in ({"timeout": 0}, {"timeout": "9"}, {"ceiling": True},
                        {"max_output": 10 ** 9}, {"tools": {"nobody": True}},
                        {"tools": {"beta": "yes"}}, {"tools": []}):
            status, body = self.post("/api/cousins/wren/mcp/registry",
                                     dict(payload, etag=etag))
            self.assertEqual(status, 400, (payload, body))
        status, body = self.post("/api/cousins/wren/mcp/registry",
                                 {"etag": "stale", "timeout": 30})
        self.assertEqual(status, 409, body)
        self.assertEqual(body["etag"], etag)

    def test_a_runner_cousin_cannot_enable_a_tool_without_a_handler(self):
        self.wren(RUNNER_REG, runner="fake")
        self.serve()
        etag = self.get("/api/cousins/wren/mcp/registry")[1]["etag"]
        status, body = self.post("/api/cousins/wren/mcp/registry",
                                 {"etag": etag, "tools": {"alpha": True}})
        self.assertEqual(status, 400, body)
        self.assertIn("alpha.run", body["error"])
        status, body = self.post("/api/cousins/wren/mcp/registry",
                                 {"etag": etag, "timeout": 30})
        self.assertEqual(status, 200, body)

    def test_a_cousin_without_its_own_gets_a_copy_of_the_install_default(self):
        home = self.wren(registry=None)
        self.serve()
        status, body = self.get("/api/cousins/wren/mcp/registry")
        self.assertEqual((status, body["exists"], body["source"]), (200, False, "example"))
        self.assertEqual([t["name"] for t in body["tools"]], ["send"])
        status, body = self.post("/api/cousins/wren/mcp/registry", {"etag": body["etag"],
                                                                     "timeout": 30})
        self.assertEqual(status, 409, body)
        status, body = self.post("/api/cousins/wren/mcp/registry/copy-default", {})
        self.assertEqual((status, body["exists"], body["source"]), (200, True, "own"), body)
        data = tomllib.loads((home / "mcp-registry.toml").read_text())
        self.assertEqual(data["tools"]["send"]["operators"], ["Ana"])
        self.assertEqual(self.post("/api/cousins/wren/mcp/registry/copy-default", {})[0], 409)
        status, body = self.post("/api/cousins/wren/mcp/registry/copy-default",
                                 {"replace": True, "etag": "stale"})
        self.assertEqual(status, 409, body)
        status, body = self.post("/api/cousins/wren/mcp/registry/copy-default",
                                 {"replace": True, "etag": body["etag"]})
        self.assertEqual(status, 200, body)

    def test_an_unknown_cousin_is_404(self):
        self.serve()
        self.assertEqual(self.get("/api/cousins/nobody/mcp/registry")[0], 404)
        self.assertEqual(self.get("/api/cousins/Bad!/policy")[0], 400)


class TestInstallRegistry(McpCase):
    def test_copy_from_example_then_edit(self):
        self.serve()
        status, body = self.get("/api/mcp/registry")
        self.assertEqual((status, body["exists"], body["example_exists"]), (200, False, True))
        self.assertEqual([t["name"] for t in body["tools"]], ["send"])
        self.assertEqual(self.post("/api/mcp/registry", {"etag": body["etag"],
                                                         "ceiling": 5})[0], 409)
        status, body = self.post("/api/mcp/registry/copy-example", {})
        self.assertEqual((status, body["exists"]), (200, True), body)
        path = self.root / "config" / "mcp-registry.toml"
        self.assertEqual(path.read_text(), EXAMPLE)
        self.assertEqual(self.post("/api/mcp/registry/copy-example", {})[0], 409)
        status, body = self.post("/api/mcp/registry", {"etag": body["etag"], "ceiling": 5})
        self.assertEqual(status, 200, body)
        self.assertEqual(tomllib.loads(path.read_text())["ceiling"], 5)
        self.assertTrue(path.read_text().startswith("# the shipped example\n"))


class TestMcpJson(McpCase):
    def base(self, home):
        self.write(home, {
            "cousin": {"command": "/opt/cousin-mcp", "env": {"COUSIN_SLUG": "wren"}},
            "notes": {"command": "/opt/notes", "args": ["--root", "/srv/notes"],
                      "env": {"NOTES_TOKEN": LITERAL, "LANG": "C"}},
            "ha": {"type": "http", "url": "http://ha.lan/api/mcp",
                   "headers": {"Authorization": "Bearer ${HA_TOKEN}"},
                   "headersHelper": "/opt/helper"},
            "odd": {"type": "ftp"},
        })

    def test_get_masks_a_literal_secret_and_keeps_what_it_cannot_edit(self):
        home = self.wren()
        self.base(home)
        self.stream(home, ("mcp_config", {"file": ".mcp.json",
                                          "servers": [{"name": "ha", "type": "http"}],
                                          "skipped": [{"name": "notes", "reason": "unset"}]}))
        self.serve()
        status, body = self.get("/api/cousins/wren/mcp/servers")
        self.assertEqual(status, 200, body)
        self.assertNotIn(LITERAL, json.dumps(body))
        servers = {s["name"]: s for s in body["servers"]}
        self.assertEqual(sorted(servers), ["ha", "notes"])
        env = {e["name"]: e for e in servers["notes"]["env"]}
        self.assertEqual((env["NOTES_TOKEN"]["value"], env["NOTES_TOKEN"]["masked"]), (None, True))
        self.assertEqual((env["LANG"]["value"], env["LANG"]["masked"]), ("C", False))
        self.assertEqual(servers["ha"]["headers"][0]["value"], "Bearer ${HA_TOKEN}")
        self.assertEqual(servers["ha"]["ignored_keys"], ["headersHelper"])
        self.assertEqual(sorted(k["name"] for k in body["kept"]), ["cousin", "odd"])
        self.assertEqual(body["last_event"]["payload"]["skipped"][0]["name"], "notes")

    def test_a_literal_secret_is_refused_with_a_reference_to_use(self):
        home = self.wren()
        self.base(home)
        before = (home / ".mcp.json").read_text()
        self.serve()
        view = self.get("/api/cousins/wren/mcp/servers")[1]
        servers = view["servers"]
        for s in servers:
            for e in s.get("env", []):
                if e["masked"]:
                    e["value"] = "Bearer " + OPAQUE
        status, body = self.post("/api/cousins/wren/mcp/servers",
                                 {"etag": view["etag"], "servers": servers})
        self.assertEqual(status, 400, body)
        problem = body["problems"][0]
        self.assertEqual((problem["server"], problem["field"], problem["key"]),
                         ("notes", "env", "NOTES_TOKEN"))
        self.assertEqual(problem["suggest"], "${NOTES_TOKEN}")
        self.assertNotIn(OPAQUE, json.dumps(body))
        self.assertEqual((home / ".mcp.json").read_text(), before)
        # a masked value sent back as it came is refused too
        view["servers"][1]["env"][0]["value"] = None
        status, body = self.post("/api/cousins/wren/mcp/servers",
                                 {"etag": view["etag"], "servers": view["servers"]})
        self.assertEqual(status, 400, body)

    def test_references_are_written_and_unknown_parts_kept(self):
        home = self.wren()
        self.base(home)
        self.serve()
        view = self.get("/api/cousins/wren/mcp/servers")[1]
        servers = {s["name"]: s for s in view["servers"]}
        servers["notes"]["env"] = [{"name": "NOTES_TOKEN", "value": "${NOTES_TOKEN}"},
                                   {"name": "LANG", "value": "C"}]
        new = {"name": "files", "type": "stdio", "command": "/opt/files",
               "args": ["--ro"], "env": []}
        status, body = self.post("/api/cousins/wren/mcp/servers", {
            "etag": view["etag"], "servers": list(servers.values()) + [new],
            "drop": ["odd"]})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["restart_required"])
        status, _ = self.post("/api/cousins/wren/mcp/servers", {
            "etag": body["etag"], "servers": [], "drop": ["cousin"]})
        self.assertEqual(status, 400)
        doc = json.loads((home / ".mcp.json").read_text())
        got = doc["mcpServers"]
        self.assertEqual(sorted(got), ["cousin", "files", "ha", "notes"])
        self.assertEqual(got["cousin"], {"command": "/opt/cousin-mcp",
                                         "env": {"COUSIN_SLUG": "wren"}})
        self.assertEqual(got["notes"]["env"]["NOTES_TOKEN"], "${NOTES_TOKEN}")
        self.assertEqual(got["ha"]["headersHelper"], "/opt/helper")
        self.assertEqual(got["files"], {"type": "stdio", "command": "/opt/files",
                                        "args": ["--ro"]})

    def test_shapes_names_and_account_variables_are_refused(self):
        home = self.wren()
        self.write(home, {})
        self.serve()
        etag = self.get("/api/cousins/wren/mcp/servers")[1]["etag"]
        bad = [
            [{"name": "cousin", "type": "stdio", "command": "/x"}],
            [{"name": "a", "type": "stdio", "command": "/x"},
             {"name": "a", "type": "stdio", "command": "/y"}],
            [{"name": "a b", "type": "stdio", "command": "/x"}],
            [{"name": "a", "type": "ftp", "url": "x"}],
            [{"name": "a", "type": "stdio", "command": ""}],
            [{"name": "a", "type": "http", "url": ""}],
            [{"name": "a", "type": "http", "url": "http://x",
              "headers": [{"name": "X-Key", "value": "${ANTHROPIC_API_KEY}"}]}],
            [{"name": "a", "type": "http", "url": "https://u:hunter2hunter2@x/mcp"}],
            [{"name": "a", "type": "stdio", "command": "/x", "args": ["--token", "abcdefgh1234"]}],
            [{"name": "a", "type": "stdio", "command": "/x",
              "env": [{"name": "A", "value": "line\nbreak"}]}],
        ]
        for servers in bad:
            status, body = self.post("/api/cousins/wren/mcp/servers",
                                     {"etag": etag, "servers": servers})
            self.assertEqual(status, 400, (servers, body))
        self.assertEqual(json.loads((home / ".mcp.json").read_text()), {"mcpServers": {}})

    def test_a_new_file_and_a_broken_one(self):
        home = self.wren()
        self.serve()
        view = self.get("/api/cousins/wren/mcp/servers")[1]
        self.assertEqual((view["exists"], view["servers"]), (False, []))
        server = {"name": "a", "type": "sse", "url": "https://x/sse", "headers": []}
        status, body = self.post("/api/cousins/wren/mcp/servers",
                                 {"etag": view["etag"], "servers": [server]})
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads((home / ".mcp.json").read_text()),
                         {"mcpServers": {"a": {"type": "sse", "url": "https://x/sse"}}})
        (home / ".mcp.json").write_text("{broken")
        view = self.get("/api/cousins/wren/mcp/servers")[1]
        self.assertIn("does not parse", view["parse_error"])
        status, body = self.post("/api/cousins/wren/mcp/servers",
                                 {"etag": view["etag"], "servers": [server]})
        self.assertEqual(status, 409, body)
        status, body = self.post("/api/cousins/wren/mcp/servers",
                                 {"etag": view["etag"], "servers": [server],
                                  "replace_broken": True})
        self.assertEqual(status, 200, body)
        self.assertIn("a", json.loads((home / ".mcp.json").read_text())["mcpServers"])


class TestMcpJsonReplace(McpCase):
    def test_replacing_a_broken_file_on_the_tmux_lane_keeps_the_cousin_entry(self):
        home = self.wren()
        (home / ".mcp.json").write_text("{broken")
        self.serve()
        view = self.get("/api/cousins/wren/mcp/servers")[1]
        server = {"name": "a", "type": "sse", "url": "https://x/sse"}
        status, body = self.post("/api/cousins/wren/mcp/servers", {
            "etag": view["etag"], "servers": [server], "replace_broken": True})
        self.assertEqual(status, 200, body)
        got = json.loads((home / ".mcp.json").read_text())["mcpServers"]
        self.assertEqual(sorted(got), ["a", "cousin"])
        self.assertEqual(got["cousin"]["env"]["COUSIN_SLUG"], "wren")
        self.assertIn("--registry", got["cousin"]["args"])

    def test_a_runner_lane_replacement_writes_no_cousin_entry(self):
        home = self.wren(runner="fake")
        (home / ".mcp.json").write_text("{broken")
        self.serve()
        view = self.get("/api/cousins/wren/mcp/servers")[1]
        status, body = self.post("/api/cousins/wren/mcp/servers", {
            "etag": view["etag"], "servers": [], "replace_broken": True})
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads((home / ".mcp.json").read_text()), {"mcpServers": {}})

    def test_a_secret_shaped_command_is_masked_and_a_masked_url_gets_a_reference(self):
        home = self.wren()
        self.write(home, {"a": {"command": "/opt/run " + OPAQUE},
                          "b": {"type": "http", "url": "https://x/mcp?token=abcdefgh1234"}})
        self.serve()
        status, body = self.get("/api/cousins/wren/mcp/servers")
        self.assertNotIn(OPAQUE, json.dumps(body))
        self.assertNotIn("abcdefgh1234", json.dumps(body))
        servers = {s["name"]: s for s in body["servers"]}
        self.assertEqual((servers["a"]["command"], servers["a"]["command_masked"]), (None, True))
        self.assertIn("command", servers["a"]["masked"])
        status, out = self.post("/api/cousins/wren/mcp/servers",
                                {"etag": body["etag"], "servers": body["servers"]})
        self.assertEqual(status, 400, out)
        by_field = {p["field"]: p for p in out["problems"]}
        self.assertEqual(by_field["command"]["suggest"], "${A_COMMAND}")
        self.assertEqual(by_field["url"]["suggest"], "${B_URL}")

    def test_changing_the_type_drops_the_old_types_keys(self):
        home = self.wren()
        self.write(home, {"a": {"command": "/opt/a", "args": ["x"], "env": {"L": "C"},
                                "headersHelper": "/h"}})
        self.serve()
        view = self.get("/api/cousins/wren/mcp/servers")[1]
        status, body = self.post("/api/cousins/wren/mcp/servers", {
            "etag": view["etag"], "servers": [{"name": "a", "type": "http", "url": "https://x"}]})
        self.assertEqual(status, 200, body)
        got = json.loads((home / ".mcp.json").read_text())["mcpServers"]["a"]
        self.assertEqual(got, {"headersHelper": "/h", "type": "http", "url": "https://x"})


class TestRefusalsAndEncodings(McpCase):
    def test_an_exists_refusal_is_not_a_stale_etag(self):
        self.wren()
        self.serve()
        status, body = self.post("/api/cousins/wren/mcp/registry/copy-default", {})
        self.assertEqual(status, 409, body)
        self.assertNotIn("etag", body)
        self.assertNotIn("stale", body)
        status, body = self.post("/api/cousins/wren/mcp/registry",
                                 {"etag": "old", "timeout": 30})
        self.assertEqual((status, body["stale"]), (409, True))

    def test_files_that_are_not_utf8_answer_400_or_show_the_error(self):
        home = self.wren()
        (home / "mcp-registry.toml").write_bytes(b"\xff\xfe ceiling = 1\n")
        (home / "policy.toml").write_bytes(b"\xff\xfe\n")
        self.serve()
        status, view = self.get("/api/cousins/wren/mcp/registry")
        self.assertEqual(status, 200, view)
        self.assertTrue(view["error"])
        status, body = self.post("/api/cousins/wren/mcp/registry",
                                 {"etag": view["etag"], "timeout": 30})
        self.assertEqual(status, 400, body)
        self.assertEqual(self.get("/api/cousins/wren/mcp/selftest")[0], 200)
        status, view = self.get("/api/cousins/wren/policy")
        self.assertEqual(status, 200, view)
        self.assertTrue(view["error"])
        payload = {"etag": view["etag"], "deny_tools": [], "deny_bash_patterns": [], "ask": [],
                   "outbound_filter": True}
        self.assertEqual(self.post("/api/cousins/wren/policy", payload)[0], 409)
        status, body = self.post("/api/cousins/wren/policy", dict(payload, replace_broken=True))
        self.assertEqual(status, 200, body)


class TestCousinMcp(McpCase):
    def test_selftest_reports_commands_that_resolve_nowhere(self):
        self.wren(REG.replace('command = "true"\ndescription', 'command = "no-such-cmd-zz"\n'
                              'description'))
        self.harness()
        self.serve()
        status, body = self.get("/api/cousins/wren/mcp/selftest")
        self.assertEqual(status, 200, body)
        self.assertFalse(body["ok"])
        self.assertEqual(body["missing"], ["no-such-cmd-zz"])
        self.assertEqual([t["name"] for t in body["tools"]], ["alpha"])
        self.assertTrue(body["registry"].endswith("mcp-registry.toml"))

    def test_last_connection_reads_the_harness_log(self):
        home = self.wren()
        self.harness()
        self.serve()
        status, body = self.get("/api/cousins/wren/mcp/last-connection")
        self.assertEqual((status, body["last"]), (200, None))
        import re
        logs = (self.root / "cache" / re.sub(r"[^A-Za-z0-9]", "-", str(home))
                / "mcp-logs-cousin")
        logs.mkdir(parents=True)
        rows = [{"debug": "Starting connection with timeout of 30000ms",
                 "timestamp": "2026-09-20T02:00:54Z", "sessionId": "s1"},
                {"error": "Server stderr: registry: nope\n", "timestamp": "t", "sessionId": "s1"},
                {"error": "Connection failed (CONNECTION_CLOSED)", "timestamp": "t",
                 "sessionId": "s1"}]
        (logs / "a.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        status, body = self.get("/api/cousins/wren/mcp/last-connection")
        self.assertEqual(body["last"]["state"], "failed")
        self.assertIn("registry: nope", body["last"]["stderr"])

    def test_approve_edits_only_the_named_settings_file(self):
        home = self.wren()
        (home / ".mcp.json").write_text('{"mcpServers": {}}')
        self.harness()
        self.serve()
        status, body = self.post("/api/cousins/wren/mcp/approve", {})
        self.assertEqual(status, 409, body)
        self.assertIn("settings_file", body["error"])
        settings = self.root / "harness-settings.json"
        settings.write_text(json.dumps({"projects": {}, "other": 1}))
        self.harness(settings_file=str(settings))
        status, body = self.get("/api/cousins/wren/mcp/status")
        self.assertEqual((status, body["approved"]), (200, False), body)
        status, body = self.post("/api/cousins/wren/mcp/approve", {})
        self.assertEqual(status, 200, body)
        data = json.loads(settings.read_text())
        self.assertEqual(data["other"], 1)
        self.assertIn("cousin", data["projects"][str(home)]["enabledMcpjsonServers"])
        self.assertTrue(self.get("/api/cousins/wren/mcp/status")[1]["approved"])


class TestApproveLanes(McpCase):
    def test_approve_is_refused_on_a_runner_lane_and_allowed_on_tmux(self):
        settings = self.root / "harness-settings.json"
        settings.write_text("{}")
        self.harness(settings_file=str(settings))
        home = self.wren(runner="fake")
        (home / ".mcp.json").write_text('{"mcpServers": {}}')
        self.serve()
        status, body = self.post("/api/cousins/wren/mcp/approve", {})
        self.assertEqual(status, 409, body)
        self.assertIn("runner", body["error"])
        self.assertEqual(settings.read_text(), "{}")
        # the tmux runner kind approves like the legacy lane
        text = (home / "cousin.toml").read_text().replace('runner = "fake"', 'runner = "tmux"')
        (home / "cousin.toml").write_text(text)
        status, body = self.post("/api/cousins/wren/mcp/approve", {})
        self.assertEqual(status, 200, body)


class TestPolicy(McpCase):
    def test_an_absent_policy_is_created_from_the_editor(self):
        home = self.wren()
        self.serve()
        status, body = self.get("/api/cousins/wren/policy")
        self.assertEqual((status, body["exists"], body["protected"]),
                         (200, False, "mcp__cousin__handoff"))
        self.assertEqual((body["deny_tools"], body["outbound_filter"]), ([], True))
        status, body = self.post("/api/cousins/wren/policy", {
            "etag": body["etag"], "deny_tools": ["WebFetch"],
            "deny_bash_patterns": [r"\brm\s+-rf\s+/"], "ask": [], "outbound_filter": True})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["restart_required"])
        p = runner_policy.Policy.load(home)
        self.assertEqual(p.deny_tools, ("WebFetch",))
        self.assertEqual(p.deny_bash_patterns[0].pattern, r"\brm\s+-rf\s+/")
        self.assertTrue((home / "policy.toml").read_text().startswith("#"))

    def test_a_pattern_that_does_not_compile_is_refused(self):
        home = self.wren()
        self.serve()
        etag = self.get("/api/cousins/wren/policy")[1]["etag"]
        status, body = self.post("/api/cousins/wren/policy", {
            "etag": etag, "deny_tools": [], "deny_bash_patterns": ["ok", "("],
            "ask": [], "outbound_filter": True})
        self.assertEqual(status, 400, body)
        self.assertEqual(body["problems"][0]["entry"], "(")
        self.assertFalse((home / "policy.toml").exists())

    def test_the_handoff_can_never_be_denied_or_asked_for(self):
        self.wren()
        self.serve()
        etag = self.get("/api/cousins/wren/policy")[1]["etag"]
        for deny, ask in ((["mcp__cousin__*"], []), (["*"], []),
                          ([], ["mcp__cousin__handoff"])):
            status, body = self.post("/api/cousins/wren/policy", {
                "etag": etag, "deny_tools": deny, "deny_bash_patterns": [], "ask": ask,
                "outbound_filter": True})
            self.assertEqual(status, 400, body)
            self.assertIn("mcp__cousin__handoff", body["error"])

    def test_removing_a_deny_entry_needs_a_second_word(self):
        home = self.wren()
        text = ("# mine\ndeny_tools = [\"WebFetch\", \"WebSearch\"]  # web off\n"
                "deny_bash_patterns = [\n    '\\bgit\\s+push',  # no pushes\n]\n"
                "ask = []\noutbound_filter = true\n")
        (home / "policy.toml").write_text(text)
        self.serve()
        view = self.get("/api/cousins/wren/policy")[1]
        self.assertEqual(view["deny_bash_patterns"], [r"\bgit\s+push"])
        payload = {"etag": view["etag"], "deny_tools": ["WebFetch"],
                   "deny_bash_patterns": [r"\bgit\s+push"], "ask": ["Agent"],
                   "outbound_filter": False}
        status, body = self.post("/api/cousins/wren/policy", payload)
        self.assertEqual(status, 409, body)
        self.assertTrue(body["needs_confirm"])
        self.assertEqual(body["removed"], {"deny_tools": ["WebSearch"],
                                           "deny_bash_patterns": [], "ask": [],
                                           "outbound_filter": True})
        self.assertEqual((home / "policy.toml").read_text(), text)
        status, body = self.post("/api/cousins/wren/policy",
                                 dict(payload, confirm_loosening=True))
        self.assertEqual(status, 200, body)
        after = (home / "policy.toml").read_text()
        self.assertIn("# mine\n", after)
        self.assertIn("  # web off\n", after)
        self.assertIn("# no pushes", after)          # an untouched key keeps its lines
        p = runner_policy.Policy.load(home)
        self.assertEqual((p.deny_tools, p.ask, p.outbound_filter),
                         (("WebFetch",), ("Agent",), False))

    def test_a_broken_policy_is_shown_and_replaced_only_on_request(self):
        home = self.wren()
        (home / "policy.toml").write_text("deny_tools = \"WebFetch\"\n")
        os.chmod(home / "policy.toml", 0o600)
        self.stream(home, ("policy", {"describe": "policy.toml: 1 denied tools"}))
        self.serve()
        view = self.get("/api/cousins/wren/policy")[1]
        self.assertIn("deny_tools", view["error"])
        self.assertEqual(view["last_event"]["payload"]["describe"], "policy.toml: 1 denied tools")
        payload = {"etag": view["etag"], "deny_tools": ["WebFetch"], "deny_bash_patterns": [],
                   "ask": [], "outbound_filter": True}
        self.assertEqual(self.post("/api/cousins/wren/policy", payload)[0], 409)
        status, body = self.post("/api/cousins/wren/policy", dict(payload, replace_broken=True))
        self.assertEqual(status, 200, body)
        self.assertEqual(runner_policy.Policy.load(home).deny_tools, ("WebFetch",))
        self.assertEqual(stat.S_IMODE((home / "policy.toml").stat().st_mode), 0o600)

    def test_replacing_a_policy_that_parses_as_toml_still_checks_the_loosening(self):
        home = self.wren()
        (home / "policy.toml").write_text('deny_tools = ["WebFetch"]\nsurprise = 1\n')
        self.serve()
        view = self.get("/api/cousins/wren/policy")[1]
        self.assertIn("surprise", view["error"])
        payload = {"etag": view["etag"], "deny_tools": [], "deny_bash_patterns": [], "ask": [],
                   "outbound_filter": True, "replace_broken": True}
        status, body = self.post("/api/cousins/wren/policy", payload)
        self.assertEqual((status, body.get("needs_confirm")), (409, True), body)
        self.assertEqual(body["removed"]["deny_tools"], ["WebFetch"])
        status, body = self.post("/api/cousins/wren/policy", dict(payload, confirm_loosening=True))
        self.assertEqual(status, 200, body)
        self.assertNotIn("surprise", (home / "policy.toml").read_text())

    def test_every_list_must_be_strings(self):
        self.wren()
        self.serve()
        etag = self.get("/api/cousins/wren/policy")[1]["etag"]
        for bad in ({"deny_tools": "WebFetch"}, {"ask": [1]}, {"outbound_filter": "no"},
                    {"deny_tools": [""]}):
            payload = dict({"etag": etag, "deny_tools": [], "deny_bash_patterns": [],
                            "ask": [], "outbound_filter": True}, **bad)
            self.assertEqual(self.post("/api/cousins/wren/policy", payload)[0], 400, bad)


class TestSecretShape(McpCase):
    """The heuristic refuses what would leak and lets configuration by."""

    def test_what_is_a_secret(self):
        from cousin_lib.console import routes_mcp as r
        for value, name in ((LITERAL, None), (OPAQUE, None), ("Bearer abcdefgh12", "Authorization"),
                            ("plainpassword", "DB_PASSWORD"), ("${X:-%s}" % LITERAL, "X"),
                            ("ghp_" + "a" * 20, None)):
            self.assertTrue(r.looks_secret(value, name), (value, name))
        for value, name in (("C", "LANG"), ("${NOTES_TOKEN}", "NOTES_TOKEN"),
                            ("Bearer ${HA_TOKEN}", "Authorization"),
                            ("/run/secrets/notes", "NOTES_TOKEN_FILE"),
                            ("123e4567-e89b-12d3-a456-426614174000", "REQUEST_ID"),
                            ("/opt/Server2024/node_modules/Thing9/index.js", None),
                            ("3600", "TOKEN_TTL"), ("service_account", "AUTH_TYPE")):
            self.assertFalse(r.looks_secret(value, name), (value, name))

    def test_urls_and_args(self):
        from cousin_lib.console import routes_mcp as r
        self.assertTrue(r.url_secret("https://x/mcp?api_key=abcdefgh1234"))
        self.assertTrue(r.url_secret("https://me:longpassword@x/mcp"))
        self.assertTrue(r.url_secret("https://hooks.x/services/" + OPAQUE))
        self.assertFalse(r.url_secret("${HA_URL:-http://ha.lan:8123}/api/mcp"))
        self.assertFalse(r.url_secret("https://x/mcp?token=${T}"))
        self.assertEqual(r.arg_secrets(["--root", "/srv", "--api-key", "abcdefgh1234",
                                        "--token=${T}", "--password=hunter2hunter2"]), [3, 5])


if __name__ == "__main__":
    import unittest
    unittest.main()
