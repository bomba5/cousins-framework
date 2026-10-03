"""Reaching a cousin on another framework instance on the same host.

A peer that is not under <root>/cousins/ is reachable when
config/external-peers.toml names its chat server's base URL and send
route. These tests stand up a real loopback HTTP server as that other
instance and drive cousin-chat and the MCP send tool against it.
"""
import contextlib
import http.server
import io
import ipaddress
import json
import os
import pathlib
import tempfile
import threading
import unittest
from unittest import mock

from cousin_lib import chat
from cousin_lib import mcp_server
from cousin_lib.chat import (ExternalPeer, PeerAddressRefused,
                             check_peer_address, chat_main)
from cousin_lib.config import MissingConfigError
from cousin_lib.server.netguard import NetGuard
from tests.test_mcp_server import ROOT as REPO, _resolved_registry


@contextlib.contextmanager
def peer_server(status=200, location=None):
    """A stand-in chat server of another instance. Yields (base_url,
    received) where received collects (path, json body)."""
    received = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length)
            try:
                received.append((self.path, json.loads(body or b"{}")))
            except ValueError:
                received.append((self.path, body))
            if location:
                self.send_response(302)
                self.send_header("Location", location)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            payload = json.dumps({"ok": True, "id": 41}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d" % server.server_address[1], received
    finally:
        server.shutdown()
        server.server_close()


class ExternalCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        (self.root / "config").mkdir()
        self.home = self._cousin("wren", 18601)
        self._cousin("testa", 18602)
        patcher = mock.patch.dict(os.environ, {
            "FRAMEWORK_ROOT": str(self.root),
            "COUSIN_HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _cousin(self, slug, port, extra=""):
        home = self.root / "cousins" / slug
        home.mkdir(parents=True, exist_ok=True)
        (home / "cousin.toml").write_text(
            '[cousin]\nslug = "%s"\nname = "%s"\n%s[chat]\nport = %d\n'
            % (slug, slug.capitalize(), extra, port))
        return home

    def _peers(self, text):
        (self.root / "config" / "external-peers.toml").write_text(text)

    def _main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = chat_main(argv)
        return rc, out.getvalue(), err.getvalue()


class TestSend(ExternalCase):
    def test_send_posts_user_and_message_to_the_configured_route(self):
        with peer_server() as (url, received):
            self._peers('[peers.kestrel]\nurl = "%s"\n'
                        'send_path = "/api/inbox"\n' % url)
            rc, out, err = self._main(["send", "kestrel", "hello there"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(received,
                         [("/api/inbox", {"user": "Wren",
                                          "message": "hello there"})])
        self.assertEqual(json.loads(out),
                         {"ok": True, "to": "kestrel", "id": 41})

    def test_send_path_defaults_to_api_send_and_from_names_the_sender(self):
        with peer_server() as (url, received):
            self._peers('[peers.kestrel]\nurl = "%s/"\n' % url)
            rc, _out, err = self._main(
                ["send", "kestrel", "hi", "--from", "wren"])
            self.assertEqual(rc, 0, err)
            # --from is the sender's own name or slug, external peer or not
            rc, _out, err = self._main(
                ["send", "kestrel", "hi", "--from", "Wren of testbed"])
            self.assertEqual(rc, 2)
            self.assertIn("own name or slug", err)
        self.assertEqual(received, [("/api/send", {"user": "wren",
                                                    "message": "hi"})])

    def test_unknown_slug_is_still_an_error(self):
        with peer_server() as (url, received):
            self._peers('[peers.kestrel]\nurl = "%s"\n' % url)
            rc, _out, err = self._main(["send", "nobody", "hi"])
        self.assertEqual(rc, 2)
        self.assertIn("nobody", err)
        self.assertIn("external-peers.toml", err)
        self.assertEqual(received, [])

    def test_no_file_means_no_external_peers(self):
        rc, _out, err = self._main(["send", "kestrel", "hi"])
        self.assertEqual(rc, 2)
        self.assertIn("kestrel", err)

    def test_a_local_cousin_wins_over_an_external_entry(self):
        from cousin_lib.runner.inbox import Inbox
        home = self._cousin("testa", 18602, extra="")
        (home / "cousin.toml").write_text(
            (home / "cousin.toml").read_text() + '[agent]\nrunner = "fake"\n')
        with peer_server() as (url, received):
            self._peers('[peers.testa]\nurl = "%s"\n' % url)
            rc, _out, err = self._main(["send", "testa", "hi"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(received, [])
        self.assertEqual(Inbox(home).pending(), 1)       # delivered locally
        rc, _out, err = self._main(["list"])
        self.assertIn("local cousin", err)

    def test_an_address_outside_the_guard_is_refused_before_any_request(self):
        self._peers('[peers.kestrel]\nurl = "http://203.0.113.9:8085"\n')
        with mock.patch.object(chat.urllib.request.OpenerDirector,
                               "open", side_effect=AssertionError("sent")):
            rc, _out, err = self._main(["send", "kestrel", "hi"])
        self.assertEqual(rc, 2)
        self.assertIn("outside the network guard", err)

    def test_a_redirect_is_not_followed(self):
        with peer_server() as (target, hit):
            with peer_server(location=target + "/api/send") as (url, first):
                self._peers('[peers.kestrel]\nurl = "%s"\n' % url)
                rc, _out, err = self._main(["send", "kestrel", "hi"])
        self.assertEqual(rc, 1)
        self.assertEqual(len(first), 1)
        self.assertEqual(hit, [])

    def test_a_proxy_in_the_environment_is_bypassed(self):
        with peer_server() as (url, received):
            self._peers('[peers.kestrel]\nurl = "%s"\n' % url)
            with mock.patch.dict(os.environ, {
                    "http_proxy": "http://127.0.0.1:9",
                    "HTTP_PROXY": "http://127.0.0.1:9",
                    "no_proxy": "", "NO_PROXY": ""}):
                rc, _out, err = self._main(["send", "kestrel", "hi"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(received), 1)

    def test_an_http_error_from_the_peer_is_a_failed_send(self):
        with peer_server(status=500) as (url, _received):
            self._peers('[peers.kestrel]\nurl = "%s"\n' % url)
            rc, _out, err = self._main(["send", "kestrel", "hi"])
        self.assertEqual(rc, 1)
        self.assertIn("500", err)


class TestList(ExternalCase):
    def test_list_marks_external_peers(self):
        self._peers('[peers.kestrel]\nurl = "http://127.0.0.1:8085"\n')
        rc, out, err = self._main(["list"])
        self.assertEqual(rc, 0, err)
        lines = out.splitlines()
        kestrel = [l for l in lines if l.split()[0] == "kestrel"]
        self.assertEqual(len(kestrel), 1)
        self.assertIn("(external)", kestrel[0])
        self.assertIn("http://127.0.0.1:8085", kestrel[0])
        self.assertTrue(any(l.split()[0] == "testa" for l in lines))

    def test_a_cousin_that_is_not_peer_visible_sees_no_external_peer(self):
        self._cousin("wren", 18601, extra="peer_visible = false\n")
        self._peers('[peers.kestrel]\nurl = "http://127.0.0.1:8085"\n')
        rc, out, _err = self._main(["list"])
        self.assertEqual(rc, 0)
        self.assertNotIn("kestrel", out)

    def test_a_malformed_file_is_loud(self):
        for text in ('[peers.kestrel]\nurl = "ftp://127.0.0.1/x"\n',
                     '[peers.kestrel]\nsend_path = "/api/send"\n',
                     '[peers.kestrel]\nurl = "http://127.0.0.1:1"\n'
                     'send_path = "api/send"\n',
                     'peers = [\n'):
            self._peers(text)
            rc, _out, err = self._main(["list"])
            self.assertEqual(rc, 2, text)
            self.assertIn("external-peers.toml", err)
            with self.assertRaises(MissingConfigError):
                chat.load_external_peers(self.root)


class TestGuard(unittest.TestCase):
    def test_loopback_and_private_pass(self):
        guard = NetGuard()
        # a host in the private range, built from the range constant
        private = ipaddress.ip_network("192.168.0.0/16")[258]
        for url in ("http://127.0.0.1:8085", "http://localhost:8085",
                    "http://%s:8085" % private):
            check_peer_address(ExternalPeer("kestrel", url), guard)

    def test_public_needs_the_allowlist(self):
        peer = ExternalPeer("kestrel", "http://203.0.113.9:8085")
        with self.assertRaises(PeerAddressRefused):
            check_peer_address(peer, NetGuard())
        check_peer_address(peer, NetGuard(extra_cidrs=["203.0.113.0/24"]))


class TestMcpSend(ExternalCase):
    """The MCP send tool routes by the peer list cousin-chat prints, so
    an external peer is discovered and delivered to through the same
    CLI, with nothing resolved through PATH."""

    def test_send_tool_reaches_the_external_peer(self):
        reg = mcp_server.load_registry(_resolved_registry(self.root))
        env = dict(os.environ, PYTHONPATH=str(REPO))
        with peer_server() as (url, received):
            self._peers('[peers.kestrel]\nurl = "%s"\n' % url)
            peers = mcp_server.discover_peers(reg, env)
            self.assertIn("kestrel", peers)
            text, is_error = mcp_server.call_tool(
                reg, "send", {"to": "kestrel", "text": "from the tool"},
                env, peers)
        self.assertFalse(is_error, text)
        self.assertEqual(received, [("/api/send", {"user": "Wren",
                                                    "message": "from the tool"})])


if __name__ == "__main__":
    unittest.main()
