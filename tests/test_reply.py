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


class TestReplyImage(TestSendReply):
    """cousin-reply --image: the picture rides the reply. The console
    attaches <home>/chat/inbound/<message id>.<ext> to that message, so
    the reply is posted first and the file lands under the returned id.
    Canary: the migrated render-preview workflow (a cousin posting its own
    PNG) had no path in this framework (operator report 2026-09-18)."""

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = pathlib.Path(tmp.name)
        (self.home / "cousin.toml").write_text(
            '[cousin]\nslug = "wren"\n[chat]\nport = %d\n'
            '[operator]\nname = "Operator"\n' % self.port)

    def _png(self, name="render.png"):
        p = self.home / name
        p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
        return p

    def _main(self, argv):
        import os
        from unittest import mock
        from cousin_lib.reply import reply_main
        env = {"COUSIN_HOME": str(self.home)}
        import contextlib, io, sys
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(sys, "stdin", io.StringIO("")), \
                contextlib.redirect_stdout(io.StringIO()):
            return reply_main(argv)

    def test_image_lands_under_the_reply_id(self):
        png = self._png()
        rc = self._main(["--user", "Operator", "-m", "preview", "--image", str(png)])
        self.assertEqual(rc, 0)
        landed = self.home / "chat" / "inbound" / "7.png"
        self.assertTrue(landed.is_file())
        self.assertEqual(landed.read_bytes(), png.read_bytes())
        self.assertEqual(_Capture.received["payload"]["message"], "preview")

    def test_a_missing_image_fails_before_any_request(self):
        rc = self._main(["-m", "x", "--image", str(self.home / "nope.png")])
        self.assertNotEqual(rc, 0)
        self.assertIsNone(_Capture.received)

    def test_a_non_image_extension_is_refused_before_any_request(self):
        bad = self.home / "model.stl"
        bad.write_bytes(b"solid x")
        rc = self._main(["-m", "x", "--image", str(bad)])
        self.assertNotEqual(rc, 0)
        self.assertIsNone(_Capture.received)

    def test_image_only_reply_gets_a_default_body(self):
        rc = self._main(["--image", str(self._png("a.jpg"))])
        self.assertEqual(rc, 0)
        self.assertTrue((self.home / "chat" / "inbound" / "7.jpg").is_file())
        self.assertTrue(_Capture.received["payload"]["message"].strip())


class TestReplyOutboundFilter(TestSendReply):
    """cousin-reply crosses the outbound filter every outbound surface
    crosses (docs/operations.md: exit 3 on a block, nothing sent).
    Canary: before the fix the filter was wired into cousin-chat and
    the media captions only, and a reply naming a protected term went
    straight to the chat server."""

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        (self.root / "config").mkdir()
        (self.root / "config" / "outbound-filter.json").write_text(
            json.dumps({"terms": ["zorblatt"], "protected": ["kestrel"]}))
        self.home = self.root / "cousins" / "wren"
        self.home.mkdir(parents=True)
        (self.home / "cousin.toml").write_text(
            '[cousin]\nslug = "wren"\n[chat]\nport = %d\n'
            '[operator]\nname = "Operator"\n' % self.port)

    def _main(self, argv, *, root_env=True):
        import contextlib
        import io
        import os
        import sys
        from unittest import mock
        from cousin_lib.reply import reply_main
        env = {"COUSIN_HOME": str(self.home)}
        if root_env:
            env["FRAMEWORK_ROOT"] = str(self.root)
        err = io.StringIO()
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(sys, "stdin", io.StringIO("")), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err):
            if not root_env:
                os.environ.pop("FRAMEWORK_ROOT", None)
            rc = reply_main(argv)
        return rc, err.getvalue()

    def test_blocked_reply_exits_3_and_posts_nothing(self):
        rc, err = self._main(["-m", "ask Zorblatt about it"])
        self.assertEqual(rc, 3)
        self.assertIn("zorblatt", err)
        self.assertIsNone(_Capture.received)

    def test_protected_slug_is_blocked(self):
        rc, _ = self._main(["-m", "kestrel said hi"])
        self.assertEqual(rc, 3)
        self.assertIsNone(_Capture.received)

    def test_blocked_reply_with_image_posts_nothing_and_lands_nothing(self):
        png = self.home / "shot.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
        rc, _ = self._main(["-m", "zorblatt preview", "--image", str(png)])
        self.assertEqual(rc, 3)
        self.assertIsNone(_Capture.received)
        self.assertFalse((self.home / "chat" / "inbound").exists())

    def test_image_default_body_is_filtered_too(self):
        png = self.home / "zorblatt.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
        rc, _ = self._main(["--image", str(png)])
        self.assertEqual(rc, 3)
        self.assertIsNone(_Capture.received)

    def test_clean_reply_still_posts(self):
        rc, _ = self._main(["-m", "all good here"])
        self.assertEqual(rc, 0)
        self.assertEqual(_Capture.received["payload"]["message"],
                         "all good here")

    def test_root_falls_back_to_the_home_grandparent(self):
        # A shell that exported only COUSIN_HOME must not lose the filter.
        rc, _ = self._main(["-m", "zorblatt"], root_env=False)
        self.assertEqual(rc, 3)
        self.assertIsNone(_Capture.received)

    def test_override_is_the_documented_escape_hatch(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"COUSIN_FILTER_OVERRIDE": "1"}):
            rc, _ = self._main(["-m", "zorblatt"])
        self.assertEqual(rc, 0)

    def test_send_reply_checks_a_given_policy(self):
        from cousin_lib.outbound_filter import FilterBlocked, OutboundPolicy
        cfg = CousinConfig.load(self.home)
        with self.assertRaises(FilterBlocked):
            send_reply(cfg, "zorblatt", policy=OutboundPolicy.load(self.root))
        self.assertIsNone(_Capture.received)
