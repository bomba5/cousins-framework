"""The console as the hive's queen (cousin_lib/console/hive.py).

Off by default and absolutely: with no config/hive.toml there is no
/hive/ route, no remote row and no store on disk. On, the console's own
server answers the queen routes with the hive token as the only
credential (no session, no guard), shows remote cousins as cards,
proxies their chat with the node's own token, builds node archives
behind a one-time download link, and revokes and forgets.
"""
import http.server
import io
import json
import pathlib
import shutil
import tarfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from cousin_lib.console import auth
from cousin_lib.console import hive as console_hive
from cousin_lib.hive import HiveStore
from tests.console._harness import ConsoleCase

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_TEMPLATE_DIR = _REPO_ROOT / "templates" / "hive-node"
PUBLIC = "http://queen.example.invalid:8600"


class FakeNode:
    """A remote node's chat server: records the Authorization header of
    every call and answers /api/send and /api/history."""

    def __init__(self):
        self.calls = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _json(self, status, payload):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                outer.calls.append(("GET", self.path,
                                    self.headers.get("Authorization")))
                self._json(200, {"messages": [
                    {"id": 1, "user": "Kestrel", "message": "hello from afar",
                     "type": "kestrel", "attachment_path": "/etc/passwd"}],
                    "total": 1, "has_more": False})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
                outer.calls.append(("POST", self.path,
                                    self.headers.get("Authorization"),
                                    payload))
                self._json(200, {"ok": True, "id": 7, "timestamp": "t"})

        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class HiveConsoleCase(ConsoleCase):
    def enable(self, **extra):
        lines = ['enabled = true', 'public_url = "%s"' % PUBLIC]
        for key, value in extra.items():
            lines.append("%s = %s" % (key, json.dumps(value)))
        (self.root / "config" / "hive.toml").write_text("\n".join(lines)
                                                         + "\n")

    def with_templates(self):
        shutil.copytree(_TEMPLATE_DIR, self.root / "templates" / "hive-node")

    def store(self):
        return console_hive.store(self.server)

    def hive(self, method, path, token=None, body=None):
        url = "http://127.0.0.1:%d%s" % (self.server.port, path)
        req = urllib.request.Request(
            url, method=method,
            data=json.dumps(body).encode() if body is not None else None)
        if token:
            req.add_header("Authorization", "Bearer %s" % token)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as err:
            return err.code, err.read()

    def hive_json(self, *args, **kw):
        status, raw = self.hive(*args, **kw)
        return status, json.loads(raw)

    def row(self, slug):
        _, body = self.get("/api/cousins")
        for row in body["cousins"]:
            if row["slug"] == slug:
                return row
        return None


class TestOffByDefault(HiveConsoleCase):
    def _assert_off(self):
        self.assertEqual(self.hive("GET", "/hive/health")[0], 404)
        self.assertEqual(self.hive("POST", "/hive/checkin", body={})[0], 404)
        self.assertEqual(self.get("/api/hive")[1]["enabled"], False)
        self.assertEqual(self.post("/api/hive/nodes", {"slug": "kestrel"})[0],
                         404)
        self.assertEqual(self.post("/api/hive/nodes/kestrel/revoke")[0], 404)
        self.cousin("wren")
        rows = self.get("/api/cousins")[1]["cousins"]
        self.assertEqual([r["slug"] for r in rows], ["wren"])
        self.assertFalse((self.root / "shared" / "hive").exists(),
                         "an off hive created its store")

    def test_absent_file_is_zero_hive_surface(self):
        self.serve()
        self._assert_off()

    def test_disabled_file_is_the_same(self):
        (self.root / "config" / "hive.toml").write_text(
            'enabled = false\npublic_url = "%s"\n' % PUBLIC)
        self.serve()
        self._assert_off()

    def test_unusable_file_is_off_and_says_why(self):
        (self.root / "config" / "hive.toml").write_text("enabled = true\n")
        self.serve()
        self._assert_off()
        self.assertIn("public_url", self.get("/api/hive")[1]["error"])


class TestQueenOnTheConsole(HiveConsoleCase):
    def test_health_and_status(self):
        self.enable(checkin_seconds=30)
        self.serve()
        self.assertEqual(self.hive_json("GET", "/hive/health"),
                         (200, {"status": "ok"}))
        _, body = self.get("/api/hive")
        self.assertEqual(body, {"enabled": True, "public_url": PUBLIC,
                                "checkin_seconds": 30, "home_chat_url": None,
                                "default_port": 8210})

    def test_the_standalone_queens_routes_answer_here(self):
        self.enable()
        self.serve()
        token = self.store().mint_token("kestrel", scope=("own", "shared"))
        other = self.store().mint_token("wren", scope=("own", "shared"))
        self.assertEqual(self.hive("GET", "/hive/inbox?since=0")[0], 401)
        status, body = self.hive_json("POST", "/hive/memory", token,
                                      {"text": "the kettle is blue",
                                       "kind": "fact"})
        self.assertEqual(status, 200)
        self.assertIsInstance(body["id"], int)
        status, body = self.hive_json("GET", "/hive/recall?q=kettle", token)
        self.assertEqual(body["memories"], ["the kettle is blue"])
        self.assertEqual(body["results"][0]["slug"], "kestrel")
        self.hive("POST", "/hive/msg", other,
                  {"to": "kestrel", "id": "m1", "body": "hi"})
        _, body = self.hive_json("GET", "/hive/inbox?since=0", token)
        self.assertEqual(body["messages"][0]["from"], "wren")

    def test_hive_routes_need_no_session_and_pass_the_guard(self):
        """Nodes have no console session and may sit on any network:
        /hive/ skips both the login and the address guard. Canary: the
        door placed after either check."""
        auth.Users(self.root / "config" / "console-users.json") \
            .set_password("ana", "correct horse")
        self.enable()
        self.serve(guard=lambda address: False)
        token = self.store().mint_token("kestrel", scope=("own",))
        self.assertEqual(self.hive("GET", "/hive/health")[0], 200)
        self.assertEqual(self.hive("GET", "/hive/inbox?since=0", token)[0],
                         200)
        self.assertEqual(self.get("/api/version")[0], 403)

    def test_an_oversized_body_is_refused_unread(self):
        self.enable()
        self.serve()
        import socket
        token = self.store().mint_token("kestrel", scope=("own",))
        # Declare a body over the cap and send none of it: the answer
        # must come from the headers alone (the body is never read).
        with socket.create_connection(("127.0.0.1", self.server.port),
                                      timeout=10) as sock:
            sock.sendall((
                "POST /hive/memory HTTP/1.1\r\nHost: x\r\n"
                "Authorization: Bearer %s\r\n"
                "Content-Type: application/json\r\n"
                "Content-Length: %d\r\n\r\n"
                % (token, console_hive.MAX_BODY_BYTES + 10)).encode())
            answer = sock.recv(4096).decode()
        self.assertTrue(answer.startswith("HTTP/1.0 413")
                        or answer.startswith("HTTP/1.1 413"), answer[:40])
        self.assertEqual(self.store().conn.execute(
            "SELECT COUNT(*) FROM memory").fetchone()[0], 0)

    def test_a_hive_token_opens_no_operator_route(self):
        auth.Users(self.root / "config" / "console-users.json") \
            .set_password("ana", "correct horse")
        self.enable()
        self.serve()
        token = self.store().mint_token("kestrel", scope=("own", "shared"))
        for method, path in (("GET", "/api/cousins"), ("GET", "/api/hive"),
                             ("POST", "/api/hive/nodes"),
                             ("POST", "/api/hive/nodes/kestrel/revoke")):
            status, _ = self.hive(method, path, token,
                                  {} if method == "POST" else None)
            self.assertEqual(status, 401, path)

    def test_a_long_poll_does_not_block_the_console(self):
        self.enable()
        self.serve()
        token = self.store().mint_token("kestrel", scope=("own",))
        other = self.store().mint_token("wren", scope=("own",))
        result = {}

        def poll():
            started = time.time()
            result["answer"] = self.hive_json(
                "GET", "/hive/inbox?since=0&wait=20", token)
            result["took"] = time.time() - started

        thread = threading.Thread(target=poll)
        thread.start()
        time.sleep(0.3)
        started = time.time()
        self.assertEqual(self.get("/api/version")[0], 200)
        self.assertLess(time.time() - started, 2)
        self.hive("POST", "/hive/msg", other,
                  {"to": "kestrel", "id": "m1", "body": "wake"})
        thread.join(10)
        self.assertEqual(result["answer"][1]["messages"][0]["body"], "wake")
        self.assertLess(result["took"], 5)


class TestRemoteCards(HiveConsoleCase):
    def test_built_then_online_then_offline(self):
        self.enable(checkin_seconds=10)
        self.serve()
        token = self.store().mint_token("kestrel", scope=("own", "shared"),
                                        name="Kestrel", role="greenhouse")
        row = self.row("kestrel")
        self.assertEqual((row["type"], row["remote"], row["remoteState"],
                          row["status"], row["chat"]),
                         ("remote", True, "pending", "stopped", "none"))
        self.assertEqual((row["name"], row["role"]), ("Kestrel", "greenhouse"))
        status, body = self.hive_json(
            "POST", "/hive/checkin", token,
            {"port": 8210, "name": "Kestrel", "role": "greenhouse",
             "version": "0.2.0"})
        self.assertEqual(body, {"ok": True, "checkin_seconds": 10})
        row = self.row("kestrel")
        self.assertEqual((row["remoteState"], row["online"], row["status"],
                          row["host"], row["port"], row["version"]),
                         ("online", True, "running", "127.0.0.1", 8210,
                          "0.2.0"))
        self.store().conn.execute(
            "UPDATE nodes SET last_seen=? WHERE slug='kestrel'",
            (time.time() - 26,))
        self.store().conn.commit()
        row = self.row("kestrel")
        self.assertEqual((row["remoteState"], row["online"], row["chat"]),
                         ("offline", False, "down"))

    def test_a_local_cousin_keeps_its_slug(self):
        self.enable()
        self.cousin("wren")
        self.serve()
        self.store().mint_token("wren", scope=("own",))
        rows = [r for r in self.get("/api/cousins")[1]["cousins"]
                if r["slug"] == "wren"]
        self.assertEqual(len(rows), 1)
        self.assertNotEqual(rows[0]["type"], "remote")

    def test_coming_online_pushes_a_refresh(self):
        self.enable()
        server = self.serve()
        events = []
        server.listeners.append(lambda kind, data: events.append((kind, data)))
        token = self.store().mint_token("kestrel", scope=("own",))
        self.hive("POST", "/hive/checkin", token,
                  {"port": 8210, "name": "Kestrel"})
        deadline = time.time() + 5
        while time.time() < deadline:
            rows = [r for kind, data in events if kind == "cousins-refresh"
                    for r in data if r["slug"] == "kestrel"]
            if rows and rows[-1]["online"]:
                break
            time.sleep(0.05)
        else:
            self.fail("no cousins-refresh carried the node online: %r"
                      % events)
        # A checkin while already online pushes nothing new.
        events.clear()
        self.hive("POST", "/hive/checkin", token,
                  {"port": 8210, "name": "Kestrel"})
        time.sleep(0.3)
        self.assertEqual([k for k, _ in events if k == "cousins-refresh"], [])

    def test_the_remote_row_pure_function(self):
        base = {"slug": "kestrel", "name": "Kestrel", "role": "r",
                "host": "198.51.100.7", "port": 8210, "version": "0.2.0",
                "last_seen": 1000.0, "checked_in": True, "revoked": False}
        self.assertEqual(console_hive.remote_row(base, 60, now=1149)[
            "remoteState"], "online")
        self.assertEqual(console_hive.remote_row(base, 60, now=1151)[
            "remoteState"], "offline")
        self.assertEqual(console_hive.remote_row(
            dict(base, revoked=True), 60, now=1001)["remoteState"], "revoked")
        self.assertFalse(console_hive.remote_row(
            dict(base, revoked=True), 60, now=1001)["online"])
        pending = console_hive.remote_row(
            dict(base, checked_in=False, last_seen=None), 60, now=1001)
        self.assertEqual(pending["remoteState"], "pending")


class TestRemoteChat(HiveConsoleCase):
    def test_chat_is_proxied_to_the_node_with_its_own_token(self):
        self.enable()
        self.serve()
        node = FakeNode()
        self.addCleanup(node.stop)
        token = self.store().mint_token("kestrel", scope=("own",))
        self.hive("POST", "/hive/checkin", token,
                  {"port": node.port, "name": "Kestrel"})
        status, body = self.get("/api/messages?cousin=kestrel&user=Sam")
        self.assertEqual(status, 200)
        self.assertEqual(body["messages"][0]["message"], "hello from afar")
        # A remote row names no file on this machine.
        self.assertNotIn("attachment", body["messages"][0])
        status, body = self.post("/api/chat/send",
                                 {"cousin": "kestrel", "user": "Sam",
                                  "message": "hi"})
        self.assertEqual((status, body["id"]), (200, 7))
        self.assertEqual(node.calls[-1][3], {"user": "Sam", "message": "hi"})
        for call in node.calls:
            self.assertEqual(call[2], "Bearer %s" % token)
        self.assertEqual(self.get(
            "/api/chat/inbound/kestrel/1.png")[0], 404)

    def test_a_node_that_has_not_checked_in_is_502(self):
        self.enable()
        self.serve()
        self.store().mint_token("kestrel", scope=("own",))
        status, body = self.get("/api/messages?cousin=kestrel&user=Sam")
        self.assertEqual(status, 502)
        self.assertIn("checked in", body["error"])


class TestRevokeForget(HiveConsoleCase):
    def test_revoke_is_401_and_forget_removes_the_card(self):
        self.enable()
        self.serve()
        token = self.store().mint_token("kestrel", scope=("own",))
        self.hive("POST", "/hive/checkin", token,
                  {"port": 8210, "name": "Kestrel"})
        self.assertEqual(self.delete("/api/hive/nodes/kestrel")[0], 409)
        status, body = self.post("/api/hive/nodes/kestrel/revoke")
        self.assertEqual((status, body["revoked"]), (200, 1))
        for method, path, payload in (
                ("GET", "/hive/inbox?since=0", None),
                ("GET", "/hive/recall?q=x", None),
                ("POST", "/hive/memory", {"text": "x"}),
                ("POST", "/hive/msg", {"to": "wren", "id": "1", "body": "x"}),
                ("POST", "/hive/checkin", {"port": 1, "name": "K"})):
            self.assertEqual(self.hive(method, path, token, payload)[0], 401,
                             path)
        row = self.row("kestrel")
        self.assertEqual((row["remoteState"], row["revoked"], row["online"]),
                         ("revoked", True, False))
        self.assertEqual(self.get("/api/messages?cousin=kestrel&user=S")[0],
                         404)
        self.assertEqual(self.post("/api/hive/nodes/kestrel/revoke")[0], 404)
        self.assertEqual(self.delete("/api/hive/nodes/kestrel")[0], 200)
        self.assertIsNone(self.row("kestrel"))
        self.assertEqual(self.delete("/api/hive/nodes/kestrel")[0], 404)


class TestBuild(HiveConsoleCase):
    def setUp(self):
        super().setUp()
        self.with_templates()

    def _build(self, **body):
        payload = {"slug": "kestrel", "name": "Kestrel",
                   "role": "watches the greenhouse"}
        payload.update(body)
        return self.post("/api/hive/nodes", payload)

    def test_build_answers_a_one_time_link_and_the_commands(self):
        self.enable()
        self.serve()
        status, body = self._build()
        self.assertEqual(status, 201, body)
        nonce_path = body["download_path"]
        self.assertTrue(body["download_url"].startswith(
            PUBLIC + "/hive/download/"))
        self.assertEqual(body["download_url"], PUBLIC + nonce_path)
        self.assertEqual(body["install"], "tar xzf kestrel-node.tar.gz &&"
                         " cd kestrel-node && ./install.sh")
        self.assertIn(body["download_url"], body["curl"])
        self.assertIn(body["install"], body["curl"])
        self.assertEqual(body["expires_in"], 900)
        entry = next(iter(self.server.state["hive_downloads"].values()))
        directory = entry["dir"]
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        status, raw = self.hive("GET", nonce_path)
        self.assertEqual(status, 200)
        with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
            env_text = tar.extractfile("kestrel-node/node.env").read().decode()
        env = dict(line.split("=", 1) for line in env_text.splitlines()
                   if line and not line.startswith("#"))
        self.assertEqual(env["QUEEN_URL"], PUBLIC)
        self.assertEqual(env["HIVE_TOKEN"], self.store().token_for("kestrel"))
        self.assertEqual(env["NODE_HOST"], "0.0.0.0")
        self.assertEqual(env["AGENT_CMD"], "")
        # Once: the second download is a 404 and the file is gone.
        self.assertEqual(self.hive("GET", nonce_path)[0], 404)
        self.assertFalse(directory.exists())
        # The card is there, built and not checked in.
        self.assertEqual(self.row("kestrel")["remoteState"], "pending")

    def test_an_expired_link_is_404_and_its_file_deleted(self):
        self.enable()
        self.serve()
        _, body = self._build()
        entry = next(iter(self.server.state["hive_downloads"].values()))
        entry["expires"] = time.time() - 1
        self.assertEqual(self.hive("GET", body["download_path"])[0], 404)
        self.assertFalse(entry["dir"].exists())

    def test_stopping_the_console_deletes_pending_archives(self):
        self.enable()
        server = self.serve()
        self._build()
        entry = next(iter(server.state["hive_downloads"].values()))
        server.stop()
        self.assertFalse(entry["dir"].exists())

    def test_archives_wait_in_an_owner_only_directory_and_leftovers_go(self):
        import os
        self.enable()
        server = self.serve()
        self._build()
        base = self.root / "shared" / "hive" / "downloads"
        self.assertEqual(base.stat().st_mode & 0o777, 0o700)
        entry = next(iter(server.state["hive_downloads"].values()))
        self.assertEqual(entry["dir"].parent, base)
        # A directory a killed console left behind: swept once it is
        # older than a link lives; a fresh one is left alone.
        old = base / "node-leftover"
        old.mkdir()
        fresh = base / "node-fresh"
        fresh.mkdir()
        past = time.time() - 16 * 60
        os.utime(old, (past, past))
        console_hive.purge_expired(server)
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())
        self.assertTrue(entry["dir"].exists())

    def test_brain_home_chat_and_loopback_options(self):
        self.enable(home_chat_url="http://home.example.invalid:8090")
        self.serve()
        _, body = self._build(brain="agent", agent_cmd="/opt/agent --plain",
                              home_chat=True, reachable=False, port=8300)
        _, raw = self.hive("GET", body["download_path"])
        with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
            env_text = tar.extractfile("kestrel-node/node.env").read().decode()
        self.assertIn("AGENT_CMD='/opt/agent --plain'", env_text)
        self.assertIn("HOME_CHAT_URL=http://home.example.invalid:8090",
                      env_text)
        self.assertIn("NODE_HOST=127.0.0.1", env_text)
        self.assertIn("NODE_PORT=8300", env_text)

    def test_refusals_mint_nothing(self):
        self.enable()
        self.cousin("wren")
        self.serve()
        for body, status in (({"slug": "Bad Slug"}, 400),
                             ({"slug": "wren"}, 409),
                             ({"role": ""}, 400),
                             ({"port": 70000}, 400),
                             ({"brain": "agent"}, 400),
                             ({"brain": "oracle"}, 400),
                             ({"home_chat": True}, 400)):
            self.assertEqual(self._build(**body)[0], status, body)
        self.assertIsNone(self.store().token_for("kestrel"))
        self.assertEqual(self.server.state.get("hive_downloads", {}), {})

    def test_building_needs_a_session_when_auth_is_on(self):
        auth.Users(self.root / "config" / "console-users.json") \
            .set_password("ana", "correct horse")
        self.enable()
        self.serve()
        self.assertEqual(self._build()[0], 401)
        self.post("/api/auth/login", {"user": "ana",
                                      "password": "correct horse"})
        self.assertEqual(self._build()[0], 201)


class TestStoreSharing(HiveConsoleCase):
    def test_the_console_and_the_cli_share_one_store(self):
        self.enable()
        self.serve()
        cli_store = HiveStore(self.root / "shared" / "hive")
        self.addCleanup(cli_store.close)
        token = cli_store.mint_token("kestrel", scope=("own",))
        self.assertEqual(self.hive("GET", "/hive/inbox?since=0", token)[0],
                         200)


if __name__ == "__main__":
    unittest.main()
