"""Cousin-to-cousin chat: filesystem-resolved targets, filtered outbound.

The peer-visibility gate is bidirectional: a non-visible cousin neither
appears in peer lists nor sees peers itself. Operator surfaces do not use
this gate - it exists so co-located cousins can be isolated from each
other, not from the operator.
"""
import http.server
import json
import pathlib
import tempfile
import threading
import unittest

from cousin_lib.chat import NoContextError, list_peers, send_message
from cousin_lib.config import CousinConfig, FrameworkConfig
from cousin_lib.outbound_filter import FilterBlocked, OutboundPolicy


class _Capture(http.server.BaseHTTPRequestHandler):
    received = None

    def do_POST(self):
        length = int(self.headers["Content-Length"])
        _Capture.received = {
            "path": self.path,
            "payload": json.loads(self.rfile.read(length)),
        }
        body = json.dumps({"ok": True, "id": 3}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class _ChatCase(unittest.TestCase):
    def setUp(self):
        _Capture.received = None
        self.server = http.server.HTTPServer(("127.0.0.1", 0), _Capture)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

    def _fw(self, cousins):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = pathlib.Path(tmp.name)
        for slug, extra in cousins.items():
            d = root / "cousins" / slug
            d.mkdir(parents=True)
            (d / "cousin.toml").write_text(
                '[cousin]\nslug = "%s"\n%s[chat]\nport = %d\n'
                % (slug, extra, self.port)
            )
        return FrameworkConfig(root)

    def _sender(self, fw, slug):
        return CousinConfig.load(fw.root / "cousins" / slug)


class TestSendMessage(_ChatCase):
    def test_posts_to_target_send_endpoint_with_sender_name(self):
        fw = self._fw({"wren": "", "toki": ""})
        result = send_message(fw, self._sender(fw, "wren"), "toki", "hello")
        self.assertEqual(result["id"], 3)
        self.assertEqual(_Capture.received["path"], "/api/send")
        self.assertEqual(
            _Capture.received["payload"], {"user": "Wren", "message": "hello"}
        )

    def test_sending_to_self_is_refused(self):
        fw = self._fw({"wren": ""})
        with self.assertRaises(ValueError):
            send_message(fw, self._sender(fw, "wren"), "wren", "echo")
        self.assertIsNone(_Capture.received)

    def test_unknown_target_is_a_loud_error(self):
        fw = self._fw({"wren": ""})
        with self.assertRaises(NoContextError):
            send_message(fw, self._sender(fw, "wren"), "ghost", "boo")

    def test_blocked_text_never_reaches_the_wire(self):
        fw = self._fw({"wren": "", "toki": ""})
        policy = OutboundPolicy(terms=["zorblatt"])
        with self.assertRaises(FilterBlocked):
            send_message(
                fw, self._sender(fw, "wren"), "toki", "about zorblatt", policy=policy
            )
        self.assertIsNone(_Capture.received)


class TestListPeers(_ChatCase):
    def test_non_visible_cousin_is_absent_from_peer_lists(self):
        fw = self._fw({"wren": "", "quiet": "peer_visible = false\n"})
        self.assertEqual([c.slug for c in list_peers(fw, "wren")], ["wren"])

    def test_non_visible_cousin_sees_no_peers(self):
        fw = self._fw({"wren": "", "quiet": "peer_visible = false\n"})
        self.assertEqual(list_peers(fw, "quiet"), [])


if __name__ == "__main__":
    unittest.main()
