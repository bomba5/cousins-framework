"""The runner's tools as a remote MCP server on loopback (phase 9 R7).

opencode reaches the framework's tools over MCP streamable HTTP; every
call must land in tools.call with the runner's own ToolContext (the live
Turn), exactly as the SDK lane's in-process server does. The live test
at the bottom runs the real opencode binary against it (opt-in)."""
import http.client
import json
import os
import pathlib
import signal
import socket
import sqlite3
import subprocess
import tempfile
import time
import unittest
from base64 import b64encode
from unittest import mock
from urllib.parse import urlsplit

from cousin_lib import mcp_server
from cousin_lib.runner import mcp_http, tools
from cousin_lib.runner.base import RunnerError
from cousin_lib.runner.turn import Turn
from tests._hermetic import HermeticCase
from tests.runner.test_tools import _ctx

REPO = pathlib.Path(__file__).resolve().parents[2]
ACCEPT = "application/json, text/event-stream"


def _registry(extra=""):
    return mcp_server.parse_registry(mcp_server.shipped_default_registry(REPO) + extra, "t")


def _request(url, method, *, token=None, body=None, headers=None, timeout=10):
    """(status, headers, body bytes): one bounded HTTP exchange."""
    parts = urlsplit(url)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=timeout)
    try:
        h = {"Accept": ACCEPT}
        if body is not None:
            h["Content-Type"] = "application/json"
        if token is not None:
            h["Authorization"] = "Bearer %s" % token
        h.update(headers or {})
        data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode()
        conn.request(method, parts.path or "/", body=data, headers=h)
        resp = conn.getresponse()
        return resp.status, dict(resp.getheaders()), resp.read()
    finally:
        conn.close()


class McpCase(HermeticCase):
    def setUp(self):
        super().setUp()
        self.turn = Turn()
        self.ctx = _ctx(self, self.turn)
        self.registry = _registry()
        self.server = mcp_http.McpHttpServer(self.ctx, self.registry)
        self.server.start()
        self.addCleanup(self.server.stop)
        self.ids = iter(range(1, 1000))

    def post(self, body, **kw):
        kw.setdefault("token", self.server.token)
        return _request(self.server.url, "POST", body=body, **kw)

    def rpc(self, method, params=None):
        msg = {"jsonrpc": "2.0", "id": next(self.ids), "method": method}
        if params is not None:
            msg["params"] = params
        status, headers, body = self.post(msg)
        self.assertEqual(status, 200, body)
        self.assertTrue(headers["Content-Type"].startswith("application/json"), headers)
        reply = json.loads(body)
        self.assertEqual((reply["jsonrpc"], reply["id"]), ("2.0", msg["id"]))
        return reply

    def chat_rows(self):
        db = self.ctx.home / "data" / "chat.db"
        if not db.exists():
            return []
        conn = sqlite3.connect(db)
        try:
            return conn.execute("SELECT chat_user, user, message FROM messages ORDER BY id").fetchall()
        finally:
            conn.close()


class TestAuthAndTransport(McpCase):
    def test_the_bearer_token_is_required(self):
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                           "clientInfo": {"name": "t", "version": "0"}}}
        with mock.patch.object(mcp_http.tools, "call") as call:
            for token, extra in ((None, None), ("wrong", None), (None, {
                    "Authorization": "Basic %s" % b64encode(b"opencode:x").decode()})):
                status, _h, body = self.post(init, token=token, headers=extra)
                self.assertEqual(status, 401, (token, extra, body))
                status, _h, _b = _request(self.server.url, "GET", token=token, headers=extra)
                self.assertEqual(status, 401)
            self.post({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                       "params": {"name": "memory", "arguments": {"command": "recall"}}}, token="nope")
        call.assert_not_called()

    def test_the_url_is_loopback_and_the_token_is_fresh_per_start(self):
        self.assertRegex(self.server.url, r"^http://127\.0\.0\.1:\d+/mcp$")
        first = self.server.token
        self.assertGreaterEqual(len(first), 32)
        self.server.stop()
        self.server.start()
        self.assertNotEqual(self.server.token, first)
        status, _h, _b = self.post({"jsonrpc": "2.0", "id": 1, "method": "ping"}, token=first)
        self.assertEqual(status, 401)
        self.assertEqual(self.rpc("ping")["result"], {})

    def test_a_given_token_is_used(self):
        other = mcp_http.McpHttpServer(self.ctx, self.registry, token="t0ken-for-the-test")
        other.start()
        self.addCleanup(other.stop)
        self.assertEqual(other.token, "t0ken-for-the-test")
        status, _h, _b = _request(other.url, "POST", token="t0ken-for-the-test",
                                  body={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        self.assertEqual(status, 200)

    def test_a_get_is_405_there_is_no_server_stream(self):
        status, headers, _b = _request(self.server.url, "GET", token=self.server.token,
                                       headers={"Accept": "text/event-stream"})
        self.assertEqual(status, 405)
        self.assertEqual(headers.get("Allow"), "POST")

    def test_the_edges_are_http_errors_not_crashes(self):
        base = self.server.url.rsplit("/", 1)[0]
        status, _h, _b = _request(base + "/other", "POST", token=self.server.token,
                                  body={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        self.assertEqual(status, 404)
        status, _h, body = self.post(b"{not json")
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], -32700)
        status, _h, body = self.post({"id": 1, "method": "ping"})       # no jsonrpc
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], -32600)
        status, _h, _b = self.post({"jsonrpc": "2.0", "id": 1, "method": "ping"},
                                   headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        status, _h, _b = self.post({"jsonrpc": "2.0", "id": 1, "method": "ping"},
                                   headers={"MCP-Protocol-Version": "1999-01-01"})
        self.assertEqual(status, 400)
        # Still serving after all of that.
        self.assertEqual(self.rpc("ping")["result"], {})


class TestProtocol(McpCase):
    def test_initialize_echoes_a_supported_version(self):
        reply = self.rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                        "clientInfo": {"name": "opencode", "version": "x"}})
        result = reply["result"]
        self.assertEqual(result["protocolVersion"], "2025-03-26")
        self.assertEqual(result["capabilities"], {"tools": {}})
        self.assertEqual(result["serverInfo"]["name"], mcp_server.SERVER_NAME)
        reply = self.rpc("initialize", {"protocolVersion": "1999-01-01", "capabilities": {},
                                        "clientInfo": {"name": "t", "version": "0"}})
        self.assertEqual(reply["result"]["protocolVersion"], mcp_http.PROTOCOL_VERSIONS[0])

    def test_notifications_and_responses_are_202_with_no_body(self):
        for msg in ({"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "method": "notifications/cancelled",
                     "params": {"requestId": 7}},
                    {"jsonrpc": "2.0", "id": 9, "result": {}}):
            status, _h, body = self.post(msg)
            self.assertEqual((status, body), (202, b""), msg)

    def test_tools_list_equals_tool_definitions(self):
        listed = self.rpc("tools/list")["result"]["tools"]
        want = [{"name": d["name"], "description": d["description"],
                 "inputSchema": d["inputSchema"]} for d in tools.tool_definitions(self.registry)]
        self.assertEqual(listed, want)
        self.assertIn("reply", [t["name"] for t in listed])
        self.assertIn("handoff", [t["name"] for t in listed])

    def test_ping(self):
        self.assertEqual(self.rpc("ping")["result"], {})

    def test_an_unknown_method_is_32601(self):
        reply = self.rpc("resources/list")
        self.assertEqual(reply["error"]["code"], -32601)
        self.assertNotIn("result", reply)

    def test_bad_call_params_are_32602(self):
        for params in ({}, {"name": 3}, {"name": "memory", "arguments": [1]}):
            reply = self.rpc("tools/call", params)
            self.assertEqual(reply["error"]["code"], -32602, params)


class TestCalls(McpCase):
    def test_a_call_reaches_tools_call_with_the_runners_own_ctx(self):
        seen = []
        real = tools.call

        def spy(ctx, name, args):
            seen.append((ctx, ctx.turn, name, args))
            return real(ctx, name, args)

        self.turn.begin({"id": 1, "thread_id": "operator:priya", "sender": "Priya"})
        with mock.patch.object(mcp_http.tools, "call", spy):
            reply = self.rpc("tools/call", {"name": "reply", "arguments": {"text": "hello Priya"}})
        result = reply["result"]
        self.assertFalse(result["isError"], result)
        self.assertEqual(result["content"][0]["type"], "text")
        self.assertIn("replied to priya", result["content"][0]["text"])
        self.assertEqual(len(seen), 1)
        self.assertIs(seen[0][0], self.ctx)
        self.assertIs(seen[0][1], self.turn)
        self.assertEqual(seen[0][2:], ("reply", {"text": "hello Priya"}))
        self.assertEqual(self.chat_rows(), [("priya", "Wren", "hello Priya")])

    def test_a_call_the_policy_denies_never_reaches_the_tool(self):
        """Review minor: the plugin's veto runs inside opencode, but the MCP
        server is reachable over loopback with its bearer token, so it
        applies policy.toml too (the SDK lane's names), as defence in depth."""
        from cousin_lib.runner.policy import Policy
        (self.ctx.home / "policy.toml").write_text(
            'deny_tools = ["mcp__cousin__send"]\nask = ["mcp__cousin__memory"]\n')
        self.ctx.policy = Policy.load(self.ctx.home)
        self.turn.begin({"id": 1, "thread_id": "operator:priya", "sender": "Priya"})
        with mock.patch.object(mcp_http.tools, "call") as call:
            for name, needle in (("send", "deny_tools lists mcp__cousin__send"),
                                 ("memory", "ask lists mcp__cousin__memory")):
                result = self.rpc("tools/call", {"name": name, "arguments": {"to": "sam"}})["result"]
                self.assertTrue(result["isError"], result)
                self.assertTrue(result["content"][0]["text"].startswith("denied by policy: "))
                self.assertIn(needle, result["content"][0]["text"])
            call.assert_not_called()
        reply = self.rpc("tools/call", {"name": "reply", "arguments": {"text": "allowed"}})
        self.assertFalse(reply["result"]["isError"], reply)

    def test_the_live_turn_is_read_at_call_time(self):
        # The same server, the turn moving under it: a reply outside a turn
        # is refused, the next turn's thread is the one answered.
        reply = self.rpc("tools/call", {"name": "reply", "arguments": {"text": "early"}})
        self.assertTrue(reply["result"]["isError"])
        self.turn.begin({"id": 2, "thread_id": "person:sam", "sender": "Sam"})
        reply = self.rpc("tools/call", {"name": "reply", "arguments": {"text": "now"}})
        self.assertFalse(reply["result"]["isError"], reply)
        self.assertEqual(self.chat_rows(), [("sam", "Wren", "now")])

    def test_a_handler_error_is_is_error_true_never_a_jsonrpc_error(self):
        reply = self.rpc("tools/call", {"name": "memory", "arguments": {"command": "levitate"}})
        self.assertNotIn("error", reply)
        self.assertTrue(reply["result"]["isError"])
        self.assertIn("unknown command", reply["result"]["content"][0]["text"])
        reply = self.rpc("tools/call", {"name": "no_such_tool", "arguments": {}})
        self.assertTrue(reply["result"]["isError"])
        with mock.patch.object(mcp_http.tools, "call", side_effect=RuntimeError("boom")):
            reply = self.rpc("tools/call", {"name": "memory", "arguments": {"command": "recall"}})
        self.assertNotIn("error", reply)
        self.assertTrue(reply["result"]["isError"])
        self.assertIn("RuntimeError: boom", reply["result"]["content"][0]["text"])
        self.assertEqual(self.rpc("ping")["result"], {})

    def test_a_call_with_no_arguments_is_an_empty_dict(self):
        seen = []
        with mock.patch.object(mcp_http.tools, "call",
                               lambda c, n, a: seen.append(a) or ("ok", False)):
            self.rpc("tools/call", {"name": "memory"})
        self.assertEqual(seen, [{}])


class TestRegistry(HermeticCase):
    def test_the_registry_is_the_ctxs_and_is_checked_before_a_port_is_bound(self):
        turn = Turn()
        ctx = _ctx(self, turn)
        server = mcp_http.McpHttpServer(ctx)          # resolved like build_tool_server
        self.assertIsNotNone(ctx.registry)
        self.assertIs(server.registry, ctx.registry)
        broken = _registry('\n[tools.widget]\ncommand = "cousin-widget"\ndescription = "w"\n'
                           '\n[tools.widget.commands.frob]\nargv = ["frob"]\n')
        with self.assertRaises(RunnerError) as caught:
            mcp_http.McpHttpServer(_ctx(self, Turn()), broken)
        self.assertIn("widget.frob", str(caught.exception))

    def test_stop_before_start_and_twice_is_harmless(self):
        server = mcp_http.McpHttpServer(_ctx(self, Turn()), _registry())
        server.stop()
        server.start()
        url = server.url
        server.stop()
        server.stop()
        with self.assertRaises(OSError):
            _request(url, "POST", token="x", body={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                     timeout=2)


# ------------------------------------------------------------------ live

def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@unittest.skipUnless(os.environ.get("COUSIN_LIVE_OPENCODE") == "1" and os.environ.get("OPENCODE_BIN"),
                     "live: set COUSIN_LIVE_OPENCODE=1 and OPENCODE_BIN=<path to opencode>")
class TestLiveOpencode(McpCase):
    """The real opencode binary, in a throwaway HOME, no provider at all
    (disabled_providers, no credentials, no model call): it must connect
    to the runner's MCP server and list its tools."""

    def test_opencode_connects_to_the_runners_mcp_server(self):
        seen = []
        handle = self.server._handle

        def spy(msg):
            seen.append((msg.get("method"), (msg.get("params") or {}).get("protocolVersion")))
            return handle(msg)

        self.server._handle = spy
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        t = pathlib.Path(tmp.name)
        for d in ("config", "data", "cache", "state", "project"):
            (t / d).mkdir()
        config = {"$schema": "https://opencode.ai/config.json",
                  "disabled_providers": ["opencode"], "autoupdate": False, "share": "disabled",
                  "mcp": {"cousin": {"type": "remote", "url": self.server.url,
                                     "headers": {"Authorization": "Bearer %s" % self.server.token}}}}
        (t / "opencode.runner.json").write_text(json.dumps(config))
        password = "live-test-password"
        env = {"PATH": os.defpath, "HOME": str(t), "XDG_CONFIG_HOME": str(t / "config"),
               "XDG_DATA_HOME": str(t / "data"), "XDG_CACHE_HOME": str(t / "cache"),
               "XDG_STATE_HOME": str(t / "state"),
               "OPENCODE_CONFIG": str(t / "opencode.runner.json"),
               "OPENCODE_SERVER_PASSWORD": password,
               "OPENCODE_DISABLE_AUTOUPDATE": "1", "OPENCODE_DISABLE_SHARE": "1",
               "OPENCODE_DISABLE_CLAUDE_CODE": "1", "OPENCODE_DISABLE_PROJECT_CONFIG": "1"}
        port = _free_port()
        log = open(t / "serve.log", "wb")
        self.addCleanup(log.close)
        proc = subprocess.Popen([os.environ["OPENCODE_BIN"], "serve", "--hostname", "127.0.0.1",
                                 "--port", str(port)], cwd=t / "project", env=env,
                                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)

        def kill():
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(proc.pid, sig)
                except ProcessLookupError:
                    return
                try:
                    proc.wait(timeout=5)
                    return
                except subprocess.TimeoutExpired:
                    continue
        self.addCleanup(kill)
        auth = {"Authorization": "Basic %s" % b64encode(b"opencode:" + password.encode()).decode()}
        base = "http://127.0.0.1:%d" % port
        status = None
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                code, _h, body = _request(base + "/mcp", "GET", headers=auth, timeout=5)
                if code == 200:
                    status = json.loads(body)
                    if status.get("cousin", {}).get("status") == "connected":
                        break
            except OSError:
                pass
            self.assertIsNone(proc.poll(), (t / "serve.log").read_text(errors="replace"))
            time.sleep(0.25)
        self.assertEqual(status and status.get("cousin"), {"status": "connected"},
                         (status, (t / "serve.log").read_text(errors="replace")[-3000:]))
        methods = [m for m, _v in seen]
        self.assertIn("initialize", methods)
        self.assertIn("tools/list", methods)
        print("\nLIVE: opencode GET /mcp -> %s; methods seen: %s; client protocolVersion: %s"
              % (json.dumps(status), methods, [v for m, v in seen if m == "initialize"]))


if __name__ == "__main__":
    unittest.main()
