"""CLI contracts for cousin-reply and cousin-chat.

Exit codes are the interface: 0 sent, 1 transport failure, 2 usage or
configuration error, 3 blocked by the outbound filter.
"""
import contextlib
import http.server
import io
import json
import pathlib
import tempfile
import threading
import unittest
from unittest import mock

from cousin_lib.chat import chat_main
from cousin_lib.reply import reply_main


class _Ok(http.server.BaseHTTPRequestHandler):
    received = None

    def do_POST(self):
        length = int(self.headers["Content-Length"])
        _Ok.received = {"path": self.path, "payload": json.loads(self.rfile.read(length))}
        body = json.dumps({"ok": True, "id": 5}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class _CliCase(unittest.TestCase):
    def setUp(self):
        _Ok.received = None
        self.server = http.server.HTTPServer(("127.0.0.1", 0), _Ok)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        for slug, agent in (("wren", ""), ("toki", '[agent]\nrunner = "fake"\n')):
            d = self.root / "cousins" / slug
            d.mkdir(parents=True)
            (d / "cousin.toml").write_text(
                '[cousin]\nslug = "%s"\n[operator]\nname = "Sam"\n%s' % (slug, agent)
            )
        self.env = {
            "COUSIN_HOME": str(self.root / "cousins" / "wren"),
            "FRAMEWORK_ROOT": str(self.root),
        }


def _reply_rows(home):
    """cousin-reply stores the reply in the cousin's own chat store
    (chat_api.reply): no request reaches a server."""
    import sqlite3
    with contextlib.closing(sqlite3.connect(pathlib.Path(home) / "data" / "chat.db")) as db:
        return db.execute("SELECT message, reply_to_user FROM messages ORDER BY id").fetchall()


class TestReplyCli(_CliCase):
    def test_message_flag_posts_and_exits_zero(self):
        with mock.patch.dict("os.environ", self.env):
            with contextlib.redirect_stdout(io.StringIO()):
                code = reply_main(["--message", "hi there"])
        self.assertEqual(code, 0)
        self.assertEqual(_reply_rows(self.root / "cousins" / "wren"), [("hi there", "Sam")])
        self.assertIsNone(_Ok.received)

    def test_stdin_body_preserves_newlines(self):
        with mock.patch.dict("os.environ", self.env):
            with mock.patch("sys.stdin", io.StringIO("line one\nline two\n")):
                with contextlib.redirect_stdout(io.StringIO()):
                    code = reply_main([])
        self.assertEqual(code, 0)
        self.assertEqual(_reply_rows(self.root / "cousins" / "wren"),
                         [("line one\nline two", "Sam")])

    def test_missing_cousin_home_is_a_config_error(self):
        err = io.StringIO()
        with mock.patch.dict("os.environ", {}, clear=True):
            with contextlib.redirect_stderr(err):
                code = reply_main(["--message", "hi"])
        self.assertEqual(code, 2)
        self.assertIn("COUSIN_HOME", err.getvalue())


class TestChatCli(_CliCase):
    def test_send_delivers_to_a_runner_peer_and_exits_zero(self):
        # a local runner peer is written in-process (its store and inbox);
        # nothing reaches a server
        out = io.StringIO()
        with mock.patch.dict("os.environ", self.env):
            with contextlib.redirect_stdout(out):
                code = chat_main(["send", "toki", "hello"])
        self.assertEqual(code, 0)
        self.assertIsNone(_Ok.received)
        self.assertEqual(_reply_rows(self.root / "cousins" / "toki"), [("hello", None)])

    def test_blocked_send_exits_three(self):
        (self.root / "config").mkdir()
        (self.root / "config" / "outbound-filter.json").write_text(
            json.dumps({"terms": ["zorblatt"]})
        )
        err = io.StringIO()
        with mock.patch.dict("os.environ", self.env):
            with contextlib.redirect_stderr(err):
                code = chat_main(["send", "toki", "about zorblatt"])
        self.assertEqual(code, 3)
        self.assertIsNone(_Ok.received)

    def test_list_prints_visible_peers(self):
        out = io.StringIO()
        with mock.patch.dict("os.environ", self.env):
            with contextlib.redirect_stdout(out):
                code = chat_main(["list"])
        self.assertEqual(code, 0)
        self.assertIn("toki", out.getvalue())
        self.assertIn("wren", out.getvalue())


if __name__ == "__main__":
    unittest.main()
