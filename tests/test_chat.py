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
from tests._hermetic import HermeticCase


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
    def test_deliver_to_a_cousin_with_no_runner_fails_with_the_line_and_opens_no_socket(self):
        # no per-cousin chat server to post to; the target is refused
        # by name (delivery.lane_refusal), nothing is opened or stored
        import contextlib
        import io
        import os
        from unittest import mock
        from cousin_lib import chat
        from cousin_lib.delivery import lane_refusal
        fw = self._fw({"wren": "", "toki": ""})
        toki = fw.root / "cousins" / "toki"
        with mock.patch("socket.create_connection", side_effect=AssertionError), \
                mock.patch("urllib.request.urlopen", side_effect=AssertionError):
            with self.assertRaises(chat.DeliveryRefused) as ctx:
                send_message(fw, self._sender(fw, "wren"), "toki", "hello")
            self.assertEqual(str(ctx.exception), lane_refusal(toki))
            err = io.StringIO()
            with mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": str(fw.root),
                                              "COUSIN_HOME": str(fw.root / "cousins" / "wren")}), \
                    contextlib.redirect_stderr(err):
                self.assertEqual(chat.chat_main(["send", "toki", "hello"]), 1)
            self.assertIn(lane_refusal(toki), err.getvalue())
        self.assertIsNone(_Capture.received)
        self.assertFalse((toki / "data" / "chat.db").exists())

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

    def test_list_prints_the_kind(self):
        # a cousin has no chat port any more: the list names its kind
        import contextlib
        import io
        import os
        from unittest import mock
        from cousin_lib import chat
        fw = self._fw({"wren": "", "toki": ""})
        toki = fw.root / "cousins" / "toki" / "cousin.toml"
        toki.write_text(toki.read_text() + '[agent]\nrunner = "fake"\n')
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": str(fw.root),
                                          "COUSIN_HOME": str(fw.root / "cousins" / "wren")}), \
                contextlib.redirect_stdout(out):
            self.assertEqual(chat.chat_main(["list"]), 0)
        lines = {line.split()[0]: line for line in out.getvalue().splitlines()}
        self.assertIn("kind=fake", lines["toki"])
        self.assertIn("kind=none", lines["wren"])
        self.assertIn("(self)", lines["wren"])
        self.assertNotIn("port=", out.getvalue())


class TestLoginMarker(HermeticCase):
    def test_list_marks_a_cousin_whose_account_needs_a_login_or_billing(self):
        from cousin_lib import chat
        from cousin_lib.runner import auth
        from tests.runner._home import temp_home
        home = temp_home(self)
        self.assertEqual(chat.login_marker(home), "")
        kw = dict(host="h1", account="fleet", kind="claude-login", detail="x", action="y")
        auth.write_login_required(home, reason=auth.LOGIN, **kw)
        self.assertEqual(chat.login_marker(home), " LOGIN REQUIRED (account fleet on h1)")
        auth.clear_login_required(home)
        auth.write_login_required(home, reason=auth.BILLING, **kw)
        self.assertEqual(chat.login_marker(home), " BILLING (account fleet on h1)")


if __name__ == "__main__":
    unittest.main()
