"""The chat routes for a runner cousin: no
chat server runs, so the console serves history, search, send, archive
and reactions itself over the cousin's `chat.db`, through
server/chat_api.py. Parity: each route returns what it returns when the
console forwards to an upstream chat server (a hive node's, here a test
double over the same API), for a chat.db the console did not create."""
import pathlib
import socket
import tempfile
import unittest
from types import SimpleNamespace

from cousin_lib.config import CousinConfig
from cousin_lib.console import proxy, router
from cousin_lib.runner.inbox import Inbox
from tests._fakes import FakeChatUpstream as ChatServer


def _closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class RunnerProxyCase(unittest.TestCase):
    def setUp(self):
        router.clear()
        proxy.register()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        (self.root / "config").mkdir()
        self.home = self.root / "cousins" / "testa"
        self.home.mkdir(parents=True)

    def _toml(self, port, runner=None):
        agent = '[agent]\nrunner = "%s"\n' % runner if runner else ""
        (self.home / "cousin.toml").write_text(
            '[cousin]\nslug = "testa"\nname = "Testa"\n[chat]\nport = %d\n'
            '[operator]\nname = "Priya"\n%s' % (port, agent))

    def _req(self, query=None, body=None):
        return SimpleNamespace(root=self.root, query=query or {}, body=body or {})

    def _get(self, path, **query):
        return router.dispatch("GET", path, req=self._req(query=query))

    def _post(self, path, **body):
        return router.dispatch("POST", path, req=self._req(body=body))

    def _through_a_chat_server(self, calls):
        """Run `calls` through the proxy's upstream path (a chat server
        that creates the chat.db), then stop it; returns their answers."""
        self._toml(0)
        server = ChatServer(CousinConfig.load(self.home))
        server.start()
        try:
            self._toml(server.port)
            return calls()
        finally:
            server.stop()


class TestParity(RunnerProxyCase):
    def test_every_read_answers_the_chat_servers_body(self):
        def calls():
            self._post("/api/chat/send", cousin="testa", user="Priya", message="the quokka ledger")
            self._post("/api/chat/send", cousin="testa", user="Priya", message="ferns")
            return (self._get("/api/messages", cousin="testa", user="Priya"),
                    self._get("/api/messages", cousin="testa", user="Priya", limit="1"),
                    self._get("/api/search", cousin="testa", q="quokka"),
                    self._get("/api/messages", cousin="testa", user="Priya", limit="ten"))
        via_server = self._through_a_chat_server(calls)
        self._toml(_closed_port(), runner="fake")       # now a runner cousin: nothing listens
        local = (self._get("/api/messages", cousin="testa", user="Priya"),
                 self._get("/api/messages", cousin="testa", user="Priya", limit="1"),
                 self._get("/api/search", cousin="testa", q="quokka"),
                 self._get("/api/messages", cousin="testa", user="Priya", limit="ten"))
        self.assertEqual(local, via_server)
        self.assertEqual(local[3], (400, {"error": "limit must be an integer"}))

    def test_archive_and_reactions_answer_the_chat_servers_body(self):
        def calls():
            ids = [self._post("/api/chat/send", cousin="testa", user="Priya",
                              message=m)[1]["id"] for m in ("a", "b", "c")]
            return ids, self._post("/api/chat/reactions", cousin="testa", message_id=ids[0],
                                   user="Priya", emoji="+1")
        (ids, reacted) = self._through_a_chat_server(calls)
        self._toml(_closed_port(), runner="fake")
        again = self._post("/api/chat/reactions", cousin="testa", message_id=ids[0],
                           user="Priya", emoji="+1")
        self.assertEqual(reacted[1]["op"], "added")
        self.assertEqual((again[0], again[1]["op"], again[1]["reactions"][0]["tap_count"]),
                         (200, "bumped", 2))
        self.assertEqual(self._post("/api/chat/archive", cousin="testa", user="Priya", keep=1),
                         (200, {"ok": True, "archived": 2}))


class TestRunnerSend(RunnerProxyCase):
    def setUp(self):
        super().setUp()
        self._toml(_closed_port(), runner="fake")

    def test_send_stores_the_row_and_queues_it_for_the_runner(self):
        status, body = self._post("/api/chat/send", cousin="testa", user="Priya",
                                  message="hello Testa")
        self.assertEqual(status, 200, body)
        self.assertEqual(set(body), {"ok", "id", "timestamp"})
        [row] = Inbox(self.home).open_rows("chat")
        self.assertEqual((row["thread_id"], row["body"], row["message_id"]),
                         ("operator:Priya", "hello Testa", body["id"]))
        _, hist = self._get("/api/messages", cousin="testa", user="Priya")
        self.assertEqual([m["message"] for m in hist["messages"]], ["hello Testa"])
        self.assertEqual(hist["cousin"], "testa")

    def test_a_tap_tells_the_runner(self):
        _, sent = self._post("/api/chat/send", cousin="testa", user="Priya", message="x")
        self._post("/api/chat/reactions", cousin="testa", message_id=sent["id"],
                   user="Priya", emoji="+1")
        [row] = Inbox(self.home).open_rows("reaction")
        self.assertIn("[fw-reaction] msg-id=%d" % sent["id"], row["body"])

    def test_a_bad_request_is_the_chat_servers_400(self):
        self.assertEqual(self._post("/api/chat/send", cousin="testa", user="Priya", message=""),
                         (400, {"error": "user and a non-empty message are required"}))


class TestTmuxCousinStillProxied(RunnerProxyCase):
    def test_a_tmux_cousin_with_no_server_is_a_502(self):
        self._toml(_closed_port())
        status, body = self._get("/api/messages", cousin="testa", user="Priya")
        self.assertEqual(status, 502, body)


if __name__ == "__main__":
    unittest.main()
