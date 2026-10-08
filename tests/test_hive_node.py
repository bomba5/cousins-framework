"""The hive node runtime: a stdlib cousin on another machine.

The template under templates/hive-node/ is loaded as a module and run
in-process against a loopback queen (build_queen) with a fake agent
command; one test runs it as a subprocess the way install.sh would.
The contract: the node's chat server mirrors this framework's /health,
/api/send and /api/history shapes; the brain recalls from and
remembers to the queen every turn; AGENT_CMD from the environment is
the backend, else the placeholder brain; a node with no reachable
queen keeps answering.
"""
import http.server
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from cousin_lib.hive import HiveStore, build_queen, hive_send

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_TEMPLATE_DIR = _REPO_ROOT / "templates" / "hive-node"
_NODE_PY = _TEMPLATE_DIR / "cousin_node.py"

# The fake agent: echoes what it was given, prefixed, and honours a
# reply the test plants through the environment so a test can make the
# brain emit any marker it likes.
_FAKE_AGENT = """\
import os, sys
prompt = sys.stdin.read()
planted = os.environ.get("FAKE_REPLY")
if os.environ.get("FAKE_FAIL"):
    sys.exit(3)
print(planted if planted else "ECHO " + prompt)
"""


def _load_node_module():
    spec = importlib.util.spec_from_file_location("cousin_node", _NODE_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _HomeChat(http.server.BaseHTTPRequestHandler):
    """A stand-in for the home chat server: records what the gateway
    posts and answers in this framework's /api/send shape."""
    received = []

    def do_POST(self):
        length = int(self.headers["Content-Length"])
        _HomeChat.received.append(
            {"path": self.path, "payload": json.loads(self.rfile.read(length))})
        body = json.dumps({"ok": True, "id": 1, "timestamp": "t"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class NodeCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = pathlib.Path(tmp.name)
        self.store = HiveStore(self.tmp / "hive")
        self.addCleanup(self.store.close)
        self.token = self.store.mint_token("testa", scope=["own", "shared"])
        self.queen = build_queen(self.store)
        self.queen.start()
        self.addCleanup(self.queen.stop)
        self.queen_url = "http://127.0.0.1:%d" % self.queen.port
        self.agent = self.tmp / "fake_agent.py"
        self.agent.write_text(_FAKE_AGENT)
        self.node_dir = self.tmp / "testa-node"
        self.node_dir.mkdir()
        (self.node_dir / "CLAUDE.md").write_text(
            "# Testa\nYou are Testa, the test node.\n")
        self.module = _load_node_module()

    def _env(self, **overrides):
        env = {
            "COUSIN_SLUG": "testa",
            "NODE_NAME": "Testa",
            "NODE_PORT": "0",
            "NODE_HOST": "127.0.0.1",
            "NODE_DIR": str(self.node_dir),
            "QUEEN_URL": self.queen_url,
            "HIVE_TOKEN": self.token,
            "HOME_CHAT_URL": "",
            "AGENT_CMD": "%s %s" % (sys.executable, self.agent),
            "NODE_POLL_SECONDS": "0.2",
        }
        env.update(overrides)
        return env

    def _node(self, **overrides):
        node = self.module.build_node(self._env(**overrides),
                                      log=lambda text: None)
        node.start()
        self.addCleanup(node.stop)
        return node

    def _call(self, node, path, method="GET", body=None):
        req = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (node.port, path),
            data=json.dumps(body).encode() if body is not None else None,
            method=method, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read())

    def _wait_for_reply(self, node, user, count=2, timeout=8):
        deadline = time.time() + timeout
        while time.time() < deadline:
            _, body = self._call(node, "/api/history?user=" + user)
            if len(body["messages"]) >= count:
                return body["messages"]
            time.sleep(0.05)
        self.fail("no reply within %ss: %s" % (timeout, body))

    def _memories(self):
        return [dict(r) for r in self.store.conn.execute(
            "SELECT slug, text, scope FROM memory ORDER BY id")]


class TestPureHelpers(unittest.TestCase):
    def setUp(self):
        self.module = _load_node_module()

    def test_markers_are_parsed_and_stripped(self):
        actions, cleaned = self.module.parse_markers(
            "Done. [remember: the gate code is 4321] "
            "[tell wren: deploy is green] [tell-home: up]")
        self.assertEqual(actions, [
            ("remember", None, "the gate code is 4321"),
            ("tell", "wren", "deploy is green"),
            ("tell-home", None, "up"),
        ])
        self.assertEqual(cleaned, "Done.")

    def test_a_quoted_placeholder_is_documentation_not_an_instruction(self):
        # The identity file and the doctrine spell the syntax out as
        # [remember: <fact>]; an agent quoting it back must not write
        # "<fact>" into the shared corpus.
        text = "Use [remember: <fact>] to keep things."
        actions, cleaned = self.module.parse_markers(text)
        self.assertEqual(actions, [])
        self.assertEqual(cleaned, text)

    def test_a_reply_that_is_only_markers_keeps_its_text(self):
        actions, cleaned = self.module.parse_markers("[remember: x]")
        self.assertEqual(actions, [("remember", None, "x")])
        self.assertEqual(cleaned, "[remember: x]")

    def test_recall_terms_split_a_message_into_substring_queries(self):
        # The queen matches substrings; "espresso?" as one query finds
        # nothing, "espresso" does.
        self.assertEqual(
            self.module.recall_terms("Is the espresso machine broken?"),
            ["espresso", "machine", "broken"])

    def test_recall_terms_are_bounded_and_fall_back_to_the_message(self):
        terms = self.module.recall_terms(
            "alpha beta gamma delta epsilon zeta eta theta", limit=3)
        self.assertEqual(terms, ["alpha", "beta", "gamma"])
        self.assertEqual(self.module.recall_terms("hi"), ["hi"])


class TestChatServerShapes(NodeCase):
    def test_health_mirrors_the_framework_shape(self):
        node = self._node()
        status, body = self._call(node, "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["slug"], "testa")
        self.assertEqual(body["port"], node.port)

    def test_send_needs_user_and_message(self):
        node = self._node()
        status, body = self._call(node, "/api/send", "POST",
                                  {"user": "Sam"})
        self.assertEqual(status, 400)
        self.assertIn("error", body)
        status, _ = self._call(node, "/api/send", "POST",
                               {"message": "hi"})
        self.assertEqual(status, 400)

    def test_malformed_json_is_400_not_an_empty_object(self):
        node = self._node()
        req = urllib.request.Request(
            "http://127.0.0.1:%d/api/send" % node.port,
            data=b"{not json", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 400)

    def test_send_answers_ok_id_timestamp(self):
        node = self._node()
        status, body = self._call(node, "/api/send", "POST",
                                  {"user": "Sam", "message": "hello"})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["id"], 1)
        self.assertIn("timestamp", body)

    def test_history_needs_user(self):
        node = self._node()
        status, body = self._call(node, "/api/history")
        self.assertEqual(status, 400)
        self.assertIn("user", body["error"])

    def test_history_carries_the_thread_in_the_framework_row_shape(self):
        node = self._node()
        self._call(node, "/api/send", "POST",
                   {"user": "Sam", "message": "hello there"})
        messages = self._wait_for_reply(node, "Sam")
        first, reply = messages[0], messages[1]
        for key in ("id", "chat_user", "user", "message", "timestamp",
                    "type", "reactions"):
            self.assertIn(key, first)
        self.assertEqual(first["user"], "Sam")
        self.assertEqual(first["chat_user"], "sam")
        self.assertEqual(first["type"], "user")
        # The reply is the node's own row under the same thread, typed
        # by its slug, as the framework's reply path stores it.
        self.assertEqual(reply["user"], "Testa")
        self.assertEqual(reply["chat_user"], "sam")
        self.assertEqual(reply["type"], "testa")
        self.assertEqual(reply["reply_to_user"], "Sam")
        _, body = self._call(node, "/api/history?user=sam")
        self.assertEqual(body["total"], 2)
        self.assertFalse(body["has_more"])

    def test_history_since_polls_forward(self):
        node = self._node()
        self._call(node, "/api/send", "POST",
                   {"user": "Sam", "message": "one"})
        self._wait_for_reply(node, "Sam")
        _, body = self._call(node, "/api/history?user=Sam&since=1")
        self.assertEqual([m["id"] for m in body["messages"]], [2])

    def test_threads_are_per_user(self):
        node = self._node()
        self._call(node, "/api/send", "POST",
                   {"user": "Sam", "message": "one"})
        self._call(node, "/api/send", "POST",
                   {"user": "Ana", "message": "two"})
        self._wait_for_reply(node, "Sam")
        self._wait_for_reply(node, "Ana")
        _, body = self._call(node, "/api/history?user=Ana")
        self.assertEqual([m["message"] for m in body["messages"]
                          if m["type"] == "user"], ["two"])

    def test_unknown_routes_are_404(self):
        node = self._node()
        self.assertEqual(self._call(node, "/nope")[0], 404)
        self.assertEqual(self._call(node, "/nope", "POST", {})[0], 404)


class TestBrainLoop(NodeCase):
    def test_agent_command_gets_the_message_and_its_reply_is_stored(self):
        node = self._node()
        self._call(node, "/api/send", "POST",
                   {"user": "Sam", "message": "what is the plan"})
        reply = self._wait_for_reply(node, "Sam")[1]["message"]
        self.assertTrue(reply.startswith("ECHO "))
        self.assertIn("what is the plan", reply)
        self.assertIn("Sam", reply)

    def test_identity_file_rides_in_the_prompt(self):
        node = self._node()
        self._call(node, "/api/send", "POST",
                   {"user": "Sam", "message": "who are you"})
        reply = self._wait_for_reply(node, "Sam")[1]["message"]
        self.assertIn("You are Testa, the test node.", reply)

    def test_recall_from_the_queen_rides_in_the_prompt(self):
        self.store.append_memory(
            "other", "the espresso machine descales every 200 shots",
            scope="shared")
        node = self._node()
        self._call(node, "/api/send", "POST",
                   {"user": "Sam", "message": "espresso?"})
        reply = self._wait_for_reply(node, "Sam")[1]["message"]
        self.assertIn("descales every 200 shots", reply)

    def test_every_turn_is_remembered_to_the_queen_in_own_scope(self):
        node = self._node()
        self._call(node, "/api/send", "POST",
                   {"user": "Sam", "message": "the gate is blue"})
        self._wait_for_reply(node, "Sam")
        own = [m for m in self._memories() if m["scope"] == "own"]
        self.assertEqual(len(own), 1)
        self.assertEqual(own[0]["slug"], "testa")
        self.assertIn("the gate is blue", own[0]["text"])

    def test_remember_marker_lands_in_shared_scope_and_is_stripped(self):
        node = self._node()
        os.environ["FAKE_REPLY"] = (
            "Noted. [remember: the gate code is 4321]")
        self.addCleanup(os.environ.pop, "FAKE_REPLY", None)
        self._call(node, "/api/send", "POST",
                   {"user": "Sam", "message": "gate code is 4321"})
        reply = self._wait_for_reply(node, "Sam")[1]["message"]
        self.assertEqual(reply, "Noted.")
        shared = [m for m in self._memories() if m["scope"] == "shared"]
        self.assertEqual([m["text"] for m in shared],
                         ["the gate code is 4321"])

    def test_tell_marker_goes_through_the_queen_bus(self):
        # No direct node-to-peer post: the only cross-machine path is
        # the authed queen, and the sender is the token's slug.
        self.store.mint_token("wren", scope=["own"])
        node = self._node()
        os.environ["FAKE_REPLY"] = "Done. [tell wren: the deploy is green]"
        self.addCleanup(os.environ.pop, "FAKE_REPLY", None)
        self._call(node, "/api/send", "POST",
                   {"user": "Sam", "message": "tell wren"})
        self._wait_for_reply(node, "Sam")
        deadline = time.time() + 5
        while time.time() < deadline:
            inbox = self.store.read_inbox("wren", since=0)
            if inbox:
                break
            time.sleep(0.05)
        self.assertEqual(inbox[0]["from"], "testa")
        self.assertEqual(inbox[0]["body"], "the deploy is green")

    def test_a_home_chat_url_is_ignored_and_nothing_is_posted(self):
        """2.0.0: no home chat server; only TELL_HOME=1 reaches home."""
        _HomeChat.received = []
        home = http.server.HTTPServer(("127.0.0.1", 0), _HomeChat)
        threading.Thread(target=home.serve_forever, daemon=True).start()
        self.addCleanup(home.server_close)
        self.addCleanup(home.shutdown)
        node = self._node(
            HOME_CHAT_URL="http://127.0.0.1:%d" % home.server_address[1])
        os.environ["FAKE_REPLY"] = "Sure. [tell-home: node is up]"
        self.addCleanup(os.environ.pop, "FAKE_REPLY", None)
        self._call(node, "/api/send", "POST",
                   {"user": "Sam", "message": "say hi home"})
        reply = self._wait_for_reply(node, "Sam")[1]["message"]
        self.assertEqual(reply, "Sure.")
        time.sleep(0.5)
        self.assertEqual(_HomeChat.received, [])

    def test_tell_home_with_no_home_configured_is_a_no_op(self):
        node = self._node()
        os.environ["FAKE_REPLY"] = "Sure. [tell-home: nobody hears]"
        self.addCleanup(os.environ.pop, "FAKE_REPLY", None)
        self._call(node, "/api/send", "POST",
                   {"user": "Sam", "message": "x"})
        reply = self._wait_for_reply(node, "Sam")[1]["message"]
        self.assertEqual(reply, "Sure.")

    def test_a_failing_agent_yields_a_stated_line_not_a_dead_node(self):
        node = self._node()
        os.environ["FAKE_FAIL"] = "1"
        self.addCleanup(os.environ.pop, "FAKE_FAIL", None)
        self._call(node, "/api/send", "POST",
                   {"user": "Sam", "message": "x"})
        reply = self._wait_for_reply(node, "Sam")[1]["message"]
        self.assertIn("brain", reply)
        self.assertEqual(self._call(node, "/health")[0], 200)

    def test_a_missing_agent_binary_is_the_same_stated_line(self):
        node = self._node(AGENT_CMD="/nonexistent/agent --flag")
        self._call(node, "/api/send", "POST",
                   {"user": "Sam", "message": "x"})
        reply = self._wait_for_reply(node, "Sam")[1]["message"]
        self.assertIn("brain", reply)


class TestPlaceholderBrain(NodeCase):
    def test_no_agent_cmd_means_the_placeholder_answers(self):
        node = self._node(AGENT_CMD="")
        self.assertEqual(self._call(node, "/health")[1]["brain"],
                         "placeholder")
        self._call(node, "/api/send", "POST",
                   {"user": "Sam", "message": "hello"})
        reply = self._wait_for_reply(node, "Sam")[1]["message"]
        self.assertIn("Testa", reply)

    def test_placeholder_echoes_and_still_remembers(self):
        node = self._node(AGENT_CMD="")
        self._call(node, "/api/send", "POST",
                   {"user": "Sam", "message": "the roof leaks"})
        reply = self._wait_for_reply(node, "Sam")[1]["message"]
        self.assertIn("the roof leaks", reply)
        self.assertTrue(any("the roof leaks" in m["text"]
                            for m in self._memories()))

    def test_placeholder_surfaces_a_recalled_memory(self):
        self.store.append_memory("other", "the roof leaks when it rains",
                                 scope="shared")
        node = self._node(AGENT_CMD="")
        self._call(node, "/api/send", "POST",
                   {"user": "Sam", "message": "roof"})
        reply = self._wait_for_reply(node, "Sam")[1]["message"]
        self.assertIn("the roof leaks when it rains", reply)


class TestFailsTowardLocal(NodeCase):
    def test_no_reachable_queen_still_answers(self):
        node = self._node(QUEEN_URL="http://127.0.0.1:9",
                          NODE_POLL_SECONDS="0")
        self._call(node, "/api/send", "POST",
                   {"user": "Sam", "message": "anyone home"})
        reply = self._wait_for_reply(node, "Sam")[1]["message"]
        self.assertIn("anyone home", reply)
        self.assertEqual(self._call(node, "/health")[0], 200)

    def test_wrong_token_is_a_local_fallback_not_a_crash(self):
        node = self._node(HIVE_TOKEN="hive_wrong", NODE_POLL_SECONDS="0")
        self._call(node, "/api/send", "POST",
                   {"user": "Sam", "message": "x"})
        self._wait_for_reply(node, "Sam")
        self.assertEqual(self._memories(), [])


class TestInboxPoll(NodeCase):
    def _wait_inbox(self, slug, timeout=6):
        deadline = time.time() + timeout
        while time.time() < deadline:
            inbox = self.store.read_inbox(slug, since=0)
            if inbox:
                return inbox
            time.sleep(0.05)
        self.fail("nothing in %s's inbox" % slug)

    def test_a_hive_message_becomes_a_turn_answered_over_the_bus(self):
        wren = self.store.mint_token("wren", scope=["own"])
        node = self._node()
        hive_send(queen_url=self.queen_url, token=wren, to="testa",
                  body="ping from wren", msg_id="w1")
        # The node stores it as a thread from the sender slug ...
        messages = self._wait_for_reply(node, "wren")
        self.assertEqual(messages[0]["user"], "wren")
        self.assertIn("ping from wren", messages[0]["message"])
        # ... and its reply goes back through the queen, sender from
        # the token.
        inbox = self._wait_inbox("wren")
        self.assertEqual(inbox[0]["from"], "testa")
        self.assertIn("ping from wren", inbox[0]["body"])

    def test_the_cursor_survives_a_restart(self):
        wren = self.store.mint_token("wren", scope=["own"])
        node = self._node()
        hive_send(queen_url=self.queen_url, token=wren, to="testa",
                  body="once", msg_id="w1")
        self._wait_for_reply(node, "wren")
        node.stop()
        cursor = (self.node_dir / "data" / "inbox-cursor").read_text()
        self.assertEqual(cursor.strip(), "1")
        again = self._node()
        time.sleep(0.6)
        _, body = self._call(again, "/api/history?user=wren")
        self.assertEqual(body["total"], 2, "the message was replayed")


class TestSubprocess(NodeCase):
    def test_runs_as_a_process_and_announces_its_port(self):
        env = dict(os.environ)
        env.update(self._env(AGENT_CMD=""))
        proc = subprocess.Popen(
            [sys.executable, str(_NODE_PY)], env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        def _end():
            proc.kill()
            proc.communicate(timeout=5)

        self.addCleanup(_end)
        line = proc.stdout.readline()
        self.assertIn("listening on 127.0.0.1:", line)
        port = int(line.rsplit(":", 1)[1].split()[0])
        with urllib.request.urlopen(
                "http://127.0.0.1:%d/health" % port, timeout=5) as resp:
            self.assertEqual(json.loads(resp.read())["slug"], "testa")

    def test_refuses_to_start_without_slug_or_token(self):
        env = dict(os.environ)
        env.update(self._env(COUSIN_SLUG=""))
        proc = subprocess.run([sys.executable, str(_NODE_PY)], env=env,
                              capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("COUSIN_SLUG", proc.stderr)


class TestCheckin(NodeCase):
    def test_the_node_checks_in_on_start(self):
        node = self._node(NODE_ROLE="greenhouse")
        deadline = time.time() + 5
        while time.time() < deadline and self.store.node("testa") is None:
            time.sleep(0.05)
        row = self.store.node("testa")
        self.assertIsNotNone(row, "no checkin reached the queen")
        self.assertEqual((row["port"], row["name"], row["role"],
                          row["version"], row["host"]),
                         (node.port, "Testa", "greenhouse",
                          self.module.NODE_VERSION, "127.0.0.1"))

    def test_the_period_is_the_queens_answer(self):
        node = self.module.build_node(self._env(), log=lambda text: None)
        self.addCleanup(node.httpd.server_close)
        self.assertEqual(node.checkin.period, 60)
        self.queen.context.checkin_seconds = 17
        self.assertTrue(node.checkin.once())
        self.assertEqual(node.checkin.period, 17)

    def test_a_failed_checkin_is_logged_and_not_fatal(self):
        logged = []
        node = self.module.build_node(
            self._env(QUEEN_URL="http://127.0.0.1:9"), log=logged.append)
        self.addCleanup(node.httpd.server_close)
        self.assertFalse(node.checkin.once())
        self.assertFalse(node.checkin.once())
        self.assertEqual(len(logged), 1, logged)  # once per reason
        self.assertIn("checkin failed", logged[0])
        self.store.revoke("testa")
        node.hive.queen_url = self.queen_url
        self.assertFalse(node.checkin.once())
        self.assertIn("HTTP 401", logged[-1])


class TestOffLoopbackGate(unittest.TestCase):
    def setUp(self):
        self.module = _load_node_module()

    def test_loopback_forms(self):
        for address in ("127.0.0.1", "127.8.9.1", "::1", "::ffff:127.0.0.1"):
            self.assertTrue(self.module.is_loopback(address), address)
        for address in ("198.51.100.7", "192.0.2.1", "::ffff:198.51.100.7", "junk"):
            self.assertFalse(self.module.is_loopback(address), address)

    def test_off_loopback_needs_the_nodes_own_token(self):
        allowed = self.module.caller_allowed
        self.assertTrue(allowed("127.0.0.1", "", "hive_t"))
        self.assertTrue(allowed("198.51.100.7", "Bearer hive_t", "hive_t"))
        self.assertFalse(allowed("198.51.100.7", "", "hive_t"))
        self.assertFalse(allowed("198.51.100.7", "Bearer hive_other", "hive_t"))
        self.assertFalse(allowed("198.51.100.7", "hive_t", "hive_t"))
        self.assertFalse(allowed("198.51.100.7", "Bearer ", ""))


class TestInstallScript(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = pathlib.Path(tmp.name)
        for name in ("install.sh", "cousin_node.py"):
            (self.dir / name).write_text((_TEMPLATE_DIR / name).read_text())
        (self.dir / "install.sh").chmod(0o755)

    def _run(self, *args):
        return subprocess.run(
            ["bash", str(self.dir / "install.sh"), *args],
            capture_output=True, text=True, timeout=20, cwd=self.dir)

    def test_refuses_without_node_env(self):
        result = self._run("--print-unit")
        self.assertEqual(result.returncode, 1)
        self.assertIn("node.env", result.stderr)

    def test_refuses_an_env_missing_the_token(self):
        (self.dir / "node.env").write_text(
            "COUSIN_SLUG=testa\nNODE_PORT=8210\n"
            "QUEEN_URL=http://queen.example.invalid:8101\n")
        result = self._run("--print-unit")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("HIVE_TOKEN", result.stderr)

    def test_print_unit_renders_the_service_and_touches_nothing(self):
        (self.dir / "node.env").write_text(
            "COUSIN_SLUG=testa\nNODE_PORT=8210\n"
            "QUEEN_URL=http://queen.example.invalid:8101\n"
            "HIVE_TOKEN=hive_x\n")
        result = self._run("--print-unit")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("cousin_node.py", result.stdout)
        self.assertIn("EnvironmentFile=%s" % (self.dir / "node.env"),
                      result.stdout)
        self.assertIn("Restart=always", result.stdout)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()),
                         ["cousin_node.py", "install.sh", "node.env"])


if __name__ == "__main__":
    unittest.main()


class TestRetryAndDedup(NodeCase):
    """#251: what the queen does not take for now is sent again under the
    same id, and the node's /api/send keeps one row per msg_id."""

    def test_a_send_retried_with_the_same_msg_id_stores_one_row(self):
        node = self._node()
        body = {"user": "Sam", "message": "hello", "msg_id": "console-00000001"}
        s1, b1 = self._call(node, "/api/send", "POST", body)
        s2, b2 = self._call(node, "/api/send", "POST", body)
        self.assertEqual((s1, s2), (200, 200))
        self.assertEqual(b1["id"], b2["id"])
        self.assertTrue(b2["duplicate"])
        msgs = self._wait_for_reply(node, "Sam")
        self.assertEqual([m["message"] for m in msgs if m["type"] == "user"], ["hello"])

    def test_a_bad_msg_id_is_400(self):
        node = self._node()
        status, _ = self._call(node, "/api/send", "POST",
                               {"user": "Sam", "message": "hi", "msg_id": "x"})
        self.assertEqual(status, 400)

    def _retrier(self, outcomes):
        sent = []

        class Fake:
            def post(self_inner, path, body):
                sent.append((path, body))
                return outcomes.pop(0)
        now = [1000.0]
        r = self.module.Retrier(Fake(), clock=lambda: now[0])
        return r, sent, now

    def test_the_retrier_keeps_the_id_and_refreshes_sent_at(self):
        stamps = iter([1.0, 2.0])
        r, sent, now = self._retrier(["transient", "ok"])
        r.add("/hive/tell-home", lambda: {"msg_id": "testa-1", "sent_at": next(stamps)}, "testa-1")
        now[0] += 15
        self.assertEqual(r.run_once(), {"testa-1": "retrying"})
        now[0] += 30
        self.assertEqual(r.run_once(), {"testa-1": "delivered"})
        self.assertEqual([b["msg_id"] for _, b in sent], ["testa-1", "testa-1"])
        self.assertEqual([b["sent_at"] for _, b in sent], [1.0, 2.0])
        self.assertEqual(r.pending(), [])

    def test_a_permanent_answer_or_the_deadline_drops_it(self):
        r, sent, now = self._retrier(["permanent"])
        r.add("/hive/msg", lambda: {"id": "testa-2"}, "testa-2")
        now[0] += 15
        self.assertEqual(r.run_once(), {"testa-2": "gave_up"})
        r2, sent2, now2 = self._retrier([])
        r2.add("/hive/msg", lambda: {"id": "testa-3"}, "testa-3")
        now2[0] += r2.RETRY_DEADLINE_S + 1
        self.assertEqual(r2.run_once(), {"testa-3": "gave_up"})
        self.assertEqual(sent2, [])

    def test_tell_home_with_the_queen_away_is_queued_not_dropped(self):
        node = self._node(QUEEN_URL="http://127.0.0.1:9", TELL_HOME="1")
        self.assertTrue(node.brain._tell_home("is the build done?"))
        self.assertEqual(len(node.retrier.pending()), 1)

    def test_the_deadline_stays_inside_the_queens_id_memory(self):
        from cousin_lib import peer_inbound
        self.assertLess(self.module.Retrier.RETRY_DEADLINE_S, peer_inbound.SEEN_KEEP_S)


class TestConsoleAndCliRetries(unittest.TestCase):
    """#251: the console's send to a remote node and `cousin-hive send`
    try again, under one id, when the other side did not answer."""

    def test_retried_send_retries_no_answer_and_5xx_only(self):
        from cousin_lib.console import proxy
        calls, slept = [], []

        def flaky():
            calls.append(1)
            if len(calls) == 1:
                raise proxy.RouteError(502, {"error": "unreachable"})
            if len(calls) == 2:
                return 503, {"error": "busy"}
            return 200, {"ok": True}
        out = proxy.retried_send(flaky, unreachable=lambda e: getattr(e, "status", 0) == 502,
                                 sleep=slept.append)
        self.assertEqual(out, (200, {"ok": True}))
        self.assertEqual(slept, [1.0, 2.0])
        calls.clear()
        self.assertEqual(proxy.retried_send(lambda: (400, {"error": "bad"}),
                                            unreachable=lambda e: True, sleep=slept.append),
                         (400, {"error": "bad"}))

    def test_hive_send_retries_under_the_same_id(self):
        from unittest import mock
        from cousin_lib import hive
        seen = []

        def call(url, path, token, method="GET", body=None):
            seen.append(body["id"])
            if len(seen) < 3:
                raise hive.HiveError("away")
            return {"ok": True}
        with mock.patch.object(hive, "_client_call", call):
            hive.hive_send(queen_url="http://q", token="t", to="sam", body="hi",
                           msg_id="cli-0001", sleep=lambda s: None)
        self.assertEqual(seen, ["cli-0001"] * 3)


class TestNodeProcess(NodeCase):
    """main() is what a node runs: it starts the whole node (the retrier
    included) and a SIGTERM or Ctrl-C ends in node.stop()."""

    def test_main_starts_the_node_and_a_sigterm_stops_it(self):
        import signal as _signal
        from unittest import mock
        node = mock.Mock(port=1)
        node.config.home_chat_url, node.config.tell_home = "", False
        handlers = {}
        with mock.patch.object(self.module, "build_node", return_value=node), \
                mock.patch.object(self.module.signal, "signal",
                                  side_effect=lambda sig, fn: handlers.__setitem__(sig, fn)):
            stopper = threading.Timer(0.2, lambda: handlers[_signal.SIGTERM](_signal.SIGTERM, None))
            stopper.start()
            self.assertEqual(self.module.main([]), 0)
        node.start.assert_called_once_with()
        node.stop.assert_called_once_with()
        self.assertIn(_signal.SIGINT, handlers)

    def test_a_real_node_exits_cleanly_on_sigterm(self):
        import signal as _signal
        import subprocess
        env = dict(os.environ, **self._env())
        node_py = pathlib.Path(self.module.__file__)
        proc = subprocess.Popen([sys.executable, str(node_py)], env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        self.assertIn("listening on", proc.stdout.readline())
        proc.send_signal(_signal.SIGTERM)
        self.assertEqual(proc.wait(timeout=15), 0)
        self.assertNotIn("Traceback", proc.stderr.read())


class TestRetryReviewFixes(NodeCase):
    """#251 review: concurrent tries of one send store once; no queen is
    not an outage; the window starts at the first try; nothing pending is
    dropped silently; a permanent hive answer is not retried."""

    def test_two_concurrent_tries_of_one_send_store_once(self):
        node = self._node()
        real_add = node.store.add

        def slow_add(**kw):
            time.sleep(0.5)                 # the first try is still storing
            return real_add(**kw)
        node.store.add = slow_add
        body = {"user": "Sam", "message": "hello", "msg_id": "console-00000002"}
        results = []
        threads = [threading.Thread(target=lambda: results.append(
            self._call(node, "/api/send", "POST", body))) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(r[0] for r in results), [200, 200])
        self.assertEqual(len({r[1]["id"] for r in results}), 1)
        self.assertEqual(sum(1 for r in results if r[1].get("duplicate")), 1)

    def test_no_queen_is_not_queued(self):
        node = self._node(QUEEN_URL="", TELL_HOME="1")
        self.assertFalse(node.brain._tell_home("anyone there?"))
        self.assertEqual(node.retrier.pending(), [])

    def test_the_window_starts_at_the_first_try(self):
        node = self._node(QUEEN_URL="http://127.0.0.1:9", TELL_HOME="1")
        before = time.time()
        node.brain._tell_home("is the build done?")
        [row] = node.retrier._rows
        self.assertLessEqual(row["created"], before + 1)
        self.assertGreaterEqual(row["created"], before - 1)

    def test_stop_says_what_it_drops(self):
        logged = []
        hive = self.module.Hive("", "t")
        r = self.module.Retrier(hive, log=logged.append)
        r.add("/hive/msg", lambda: {}, "testa-9")
        r.stop()
        self.assertTrue(any("testa-9" in line and "dropped" in line for line in logged))

    def test_hive_send_does_not_retry_a_refusal(self):
        from unittest import mock
        from cousin_lib import hive
        calls = []

        def call(*a, **kw):
            calls.append(1)
            raise hive.HiveError("refused", transient=False)
        with mock.patch.object(hive, "_client_call", call):
            with self.assertRaises(hive.HiveError):
                hive.hive_send(queen_url="http://q", token="t", to="sam", body="hi",
                               msg_id="cli-0002", sleep=lambda s: None)
        self.assertEqual(len(calls), 1)


class TestClaimWaitBounds(NodeCase):
    """#251 re-look: a try that waits out a still-storing first one is a
    503, not an ok, and the wait stays under the console's 15 s a try."""

    def test_a_still_pending_first_try_is_503(self):
        node = self._node()
        node.SEND_CLAIM_WAIT_S = 0.2
        real_add = node.store.add

        def slow_add(**kw):
            time.sleep(1.0)
            return real_add(**kw)
        node.store.add = slow_add
        body = {"user": "Sam", "message": "hello", "msg_id": "console-00000003"}
        first = []
        t = threading.Thread(target=lambda: first.append(self._call(node, "/api/send", "POST", body)))
        t.start()
        time.sleep(0.1)
        status, answer = self._call(node, "/api/send", "POST", body)
        t.join()
        self.assertEqual(status, 503)
        self.assertFalse(answer["ok"])
        self.assertEqual(first[0][0], 200)

    def test_the_wait_is_under_the_console_timeout(self):
        from cousin_lib.console import proxy
        self.assertLess(self.module.Node.SEND_CLAIM_WAIT_S, proxy._SEND_TIMEOUT)
