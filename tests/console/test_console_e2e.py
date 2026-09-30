"""The console end to end, in one process and on loopback only.

The route tests exercise each module against a minimal request object;
this file walks the operator's first session through the console's
real HTTP surface instead: a temp root with one cousin created by the
spawn library, an upstream chat server for it (a test double over the
chat API, as a hive node's would answer) on an ephemeral loopback port
with a fake delivery seam, a fake tmux binary standing in for the
pane, a console user written through the auth module, and the network
guard built from the root's (absent) allowlist. Then, as a browser
would: login, `me`, the fleet with its health, a chat message through
the proxy and back out of history, the pane capture and one frame of
its stream, jobs, loops, the memory tree, a tracker round trip, and
the static bundle with its traversal refusal.
"""
import http.client
import json
import pathlib
import re
import shutil
import unittest

from cousin_lib.config import CousinConfig
from cousin_lib.console import auth
from cousin_lib.console.toml_edit import write_key
from cousin_lib.server.netguard import NetGuard
from cousin_lib.spawn import create_cousin
from tests._fakes import FakeChatUpstream
from tests.console._harness import ConsoleCase

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SLUG = "testa"
OPERATOR = "Sam"
USER = "ana"
PASSWORD = "correct horse battery"


class ConsoleEndToEnd(ConsoleCase):
    def setUp(self):
        super().setUp()
        # 1. the cousin, through the spawn library (the template is the
        #    one identity source, so the temp root needs the real one)
        (self.root / "templates").mkdir()
        shutil.copy(_REPO_ROOT / "templates" / "cousin-CLAUDE.template.md",
                    self.root / "templates" / "cousin-CLAUDE.template.md")
        created = create_cousin(self.root, slug=SLUG, name="Testa",
                                role="test cousin",
                                voice="Plain and helpful.",
                                operator=OPERATOR)
        self.home = created["home"]
        # a 1.x home, as this walk was written for (the console's legacy
        # paths stay until the console task retires them): create_cousin
        # makes an sdk cousin since 2.0.0 (R4), so its [agent] table goes
        toml_path = self.home / "cousin.toml"
        toml_path.write_text(re.sub(r"(?ms)^\[agent\][ \t]*\n.*?(?=^\[|\Z)", "",
                                    toml_path.read_text()))
        # 2. an upstream chat server on an ephemeral port, delivery faked
        self.delivered = []
        self.chat = FakeChatUpstream(
            CousinConfig.load(self.home),
            deliver=lambda **kw: self.delivered.append(kw))
        self.chat.start()
        self.addCleanup(self.chat.stop)
        write_key(self.home, "chat", "port", self.chat.port)
        # 3. the pane: the fake tmux says the session is up and shows
        #    this text
        self.tmux_running(True)
        self.pane.write_text("$ cousin-memory search tea\n1 hit\n")
        # 4. the console user, the way `cousin-console adduser` writes it
        auth.Users(self.root / "config" / "console-users.json") \
            .set_password(USER, PASSWORD)
        # 5. the console behind the guard the entry point would build
        self.serve(guard=NetGuard.from_config(self.root))

    # -- helpers -----------------------------------------------------------

    def login(self):
        status, body = self.post("/api/auth/login",
                                 {"user": USER, "password": PASSWORD})
        self.assertEqual((status, body.get("user")), (200, USER), body)
        return self.cookie()

    def cookie(self):
        for c in self.jar:
            if c.name == auth.COOKIE:
                return "%s=%s" % (c.name, c.value)
        self.fail("no session cookie in the jar")

    def read_one_sse_frame(self, path):
        """Open the stream over a raw connection, read up to the first
        blank line, close. Returns (status, headers, frame_text)."""
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port,
                                          timeout=10)
        try:
            conn.request("GET", path, headers={"Cookie": self.cookie()})
            resp = conn.getresponse()
            lines = []
            if resp.status == 200:
                while True:
                    line = resp.fp.readline()
                    if not line or line in (b"\n", b"\r\n"):
                        break
                    lines.append(line.decode())
            return resp.status, dict(resp.getheaders()), "".join(lines)
        finally:
            conn.close()

    # -- the session -------------------------------------------------------

    def test_auth_gates_the_api_but_not_the_page(self):
        status, body = self.get("/api/cousins")
        self.assertEqual(status, 401)
        self.assertEqual(body, {"ok": False, "error": "login required"})
        status, body = self.get("/api/auth/me")
        self.assertEqual(status, 200)
        self.assertEqual((body["configured"], body["user"]), (True, None))
        self.assertEqual(self.get("/", raw=True)[0], 200)
        status, body = self.post("/api/auth/login",
                                 {"user": USER, "password": "wrong"})
        self.assertEqual(status, 401)
        self.login()
        status, body = self.get("/api/auth/me")
        self.assertEqual((status, body["user"], body["users"]),
                         (200, USER, [USER]))
        self.assertEqual(self.get("/api/cousins")[0], 200)

    def test_fleet_lists_the_spawned_cousin_with_its_health(self):
        self.login()
        status, body = self.get("/api/cousins")
        self.assertEqual(status, 200, body)
        rows = {row["slug"]: row for row in body["cousins"]}
        self.assertEqual(list(rows), [SLUG])
        row = rows[SLUG]
        self.assertEqual(row["name"], "Testa")
        self.assertEqual(row["role"], "test cousin")
        self.assertEqual(row["type"], "cousin")
        self.assertNotIn("port", row)
        self.assertEqual(row["operator"], OPERATOR)
        self.assertEqual(row["home"], str(self.home))
        self.assertEqual(row["tmuxSession"], SLUG)
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["chat"], "ok")
        self.assertFalse(row["hidden"])
        self.assertIsInstance(row["tokensSpent"], int)
        # the chat server is asked, not assumed: stop it and the row says so
        self.chat.stop()
        _, body = self.get("/api/cousins")
        self.assertEqual(body["cousins"][0]["chat"], "down")

    def test_chat_send_goes_through_the_proxy_and_back_out_of_history(self):
        self.login()
        status, body = self.post("/api/chat/send", {
            "cousin": SLUG, "user": OPERATOR, "message": "hello testa"})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["ok"])
        self.assertIsInstance(body["id"], int)
        # the cousin's server delivered it through its own seam
        self.assertEqual(len(self.delivered), 1)
        self.assertEqual(self.delivered[0]["user"], OPERATOR)
        self.assertTrue(self.delivered[0]["message"].startswith("hello testa"))
        self.assertEqual(self.delivered[0]["message_id"], body["id"])
        # and the console reads it back from that server, storing nothing
        status, hist = self.get("/api/messages?cousin=%s&user=%s"
                                % (SLUG, OPERATOR))
        self.assertEqual(status, 200, hist)
        self.assertEqual(hist["cousin"], SLUG)
        self.assertEqual(hist["total"], 1)
        msg = hist["messages"][0]
        self.assertEqual((msg["id"], msg["message"], msg["user"]),
                         (body["id"], "hello testa", OPERATOR))
        self.assertEqual(msg["type"], "user")
        self.assertTrue((self.home / "data" / "chat.db").is_file())
        self.assertFalse(list(self.root.glob("data/chat*")),
                         "the console grew a chat store")
        # the fleet's unread column sees the thread too
        _, fleet = self.get("/api/cousins")
        self.assertIsInstance(fleet["cousins"][0]["lastMsgTs"], int)

    def test_pane_capture_and_one_stream_frame(self):
        self.login()
        status, body = self.get("/api/pane?cousin=%s" % SLUG)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["text"], "$ cousin-memory search tea\n1 hit")
        status, headers, frame = self.read_one_sse_frame(
            "/api/pane/stream?cousin=%s&lines=50" % SLUG)
        self.assertEqual(status, 200, frame)
        self.assertTrue(headers.get("Content-Type", "")
                        .startswith("text/event-stream"), headers)
        self.assertEqual(headers.get("Cache-Control"), "no-cache")
        self.assertTrue(frame.startswith("event: pane\n"), frame)
        data = json.loads(frame.split("data: ", 1)[1])
        self.assertTrue(data["text"].startswith("$ cousin-memory search tea"))
        self.assertTrue(data["changed"])
        self.assertIn("ts", data)
        # the capture asked the fake tmux for this cousin's session
        calls = self.tmux_log.read_text().splitlines()
        self.assertTrue(any("capture-pane" in c and "-t %s" % SLUG in c
                            and "-S -50" in c for c in calls), calls)
        # and the console still answers after the stream was dropped
        self.assertEqual(self.get("/api/pane?cousin=%s" % SLUG)[0], 200)

    def test_pane_reports_a_stopped_session_as_409(self):
        self.login()
        self.tmux_running(False)
        status, body = self.get("/api/pane?cousin=%s" % SLUG)
        self.assertEqual(status, 409)
        self.assertFalse(body["ok"])
        _, fleet = self.get("/api/cousins")
        self.assertEqual(fleet["cousins"][0]["status"], "stopped")

    def test_jobs_loops_and_memory_are_projections_of_the_root(self):
        self.login()
        status, body = self.get("/api/jobs")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["jobs"], [])
        status, body = self.get("/api/loops")
        self.assertEqual(status, 200, body)
        self.assertIn("daemon", body)
        beats = [r for r in body["loops"] if r["cousin"] == SLUG]
        self.assertTrue(beats, body)
        self.assertEqual(beats[0]["source"], "framework")
        self.assertEqual(body.get("errors", []), [])
        status, body = self.get("/api/memory")
        self.assertEqual(status, 200, body)
        self.assertIn("shared/", body["tree"])
        self.assertIn(SLUG + "/", body["tree"])
        (self.home / "memory" / "tea.md").write_text("# tea\n\ndescale monthly\n")
        _, body = self.get("/api/memory")
        entry = body["tree"][SLUG + "/"]["tea.md"]
        self.assertEqual(entry["size"], len("# tea\n\ndescale monthly\n"))
        self.assertIn("descale", entry["preview"])

    def test_tracker_round_trip(self):
        self.login()
        seen = []
        self.server.listeners.append(lambda k, d: seen.append((k, d)))
        status, body = self.post("/api/tracker", {
            "title": "wire the console", "domain": "infra", "tags": ["q4"]})
        self.assertEqual(status, 200, body)
        item = body["item"]
        self.assertEqual((item["title"], item["state"], item["domain"],
                          item["tags"]),
                         ("wire the console", "open", "infra", ["q4"]))
        self.assertEqual(seen, [("tracker-change",
                                 {"id": item["id"], "op": "add"})])
        status, body = self.get("/api/tracker")
        self.assertEqual(status, 200)
        self.assertEqual([i["id"] for i in body["items"]], [item["id"]])
        status, body = self.get("/api/tracker/%d" % item["id"])
        self.assertEqual((status, body["item"]["title"]),
                         (200, "wire the console"))
        self.assertTrue((self.root / "data" / "tracker.db").is_file())

    def test_static_bundle_and_traversal(self):
        # no session needed for the page: the login form is part of it
        status, headers, body = self.get("/", raw=True)
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/html"))
        self.assertEqual(headers["Cache-Control"], "no-store")
        page = body.decode()
        for name in ("data.jsx", "ui.jsx", "chat.jsx", "cousins.jsx",
                     "views.jsx", "app.jsx"):
            self.assertRegex(page, r'<script type="text/babel" src="%s\?v=\d+">'
                             % name.replace(".", r"\."))
        status, headers, body = self.get("/app.jsx", raw=True)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/jsx; charset=utf-8")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn(b"React", body)
        status, headers, _ = self.get("/styles.css", raw=True)
        self.assertEqual((status, headers["Content-Type"]),
                         (200, "text/css; charset=utf-8"))
        for path in ("/../pyproject.toml", "/%2e%2e/pyproject.toml",
                     "/..%2f..%2fpyproject.toml", "/console_static/../app.py"):
            status, _, body = self.get(path, raw=True)
            self.assertIn(status, (403, 404), path)
            self.assertNotIn(b"[project]", body, path)
            self.assertNotIn(b"ConsoleServer", body, path)
        self.assertEqual(self.get("/__init__.py", raw=True)[0], 404)


if __name__ == "__main__":
    unittest.main()
