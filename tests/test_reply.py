"""cousin-reply behavior, tested against a real local HTTP server."""
import http.server
import json
import pathlib
import tempfile
import threading
import unittest

from cousin_lib.config import CousinConfig, MissingConfigError
from cousin_lib.reply import send_reply


class _Capture(http.server.BaseHTTPRequestHandler):
    received = None

    def do_POST(self):
        length = int(self.headers["Content-Length"])
        _Capture.received = {
            "path": self.path,
            "payload": json.loads(self.rfile.read(length)),
        }
        body = json.dumps({"ok": True, "id": 7}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class TestSendReply(unittest.TestCase):
    def setUp(self):
        _Capture.received = None
        self.server = http.server.HTTPServer(("127.0.0.1", 0), _Capture)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

    def _cfg(self, operator=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = pathlib.Path(tmp.name)
        toml = '[cousin]\nslug = "wren"\n[chat]\nport = %d\n' % self.port
        if operator:
            toml += '[operator]\nname = "%s"\n' % operator
        (home / "cousin.toml").write_text(toml)
        return CousinConfig.load(home)

    def test_posts_message_to_own_reply_endpoint(self):
        result = send_reply(self._cfg(), "hello there", user="Sam")
        self.assertEqual(result["id"], 7)
        self.assertEqual(_Capture.received["path"], "/api/wren_reply")
        self.assertEqual(
            _Capture.received["payload"],
            {"message": "hello there", "reply_to_user": "Sam"},
        )

    def test_user_falls_back_to_configured_operator(self):
        send_reply(self._cfg(operator="Sam"), "hi")
        self.assertEqual(_Capture.received["payload"]["reply_to_user"], "Sam")

    def test_no_user_and_no_operator_fails_loud(self):
        # Null-operator profile: no configured operator means no recipient
        # to default to - never a hardcoded human.
        with self.assertRaises(MissingConfigError):
            send_reply(self._cfg(), "hi")
        self.assertIsNone(_Capture.received)

    def test_reply_to_is_passed_through(self):
        send_reply(self._cfg(), "hi", user="Sam", reply_to=41)
        self.assertEqual(_Capture.received["payload"]["reply_to"], {"id": 41})

    def test_empty_body_is_refused_before_any_request(self):
        with self.assertRaises(ValueError):
            send_reply(self._cfg(), "   \n", user="Sam")
        self.assertIsNone(_Capture.received)


if __name__ == "__main__":
    unittest.main()
