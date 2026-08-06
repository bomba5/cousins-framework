"""Chat server HTTP contract.

Every test boots a real server on an ephemeral loopback port and speaks
real HTTP to it. Terminal delivery and the network guard are injected
seams here; their own behavior is tested in their own modules.
"""
import json
import pathlib
import tempfile
import unittest
import urllib.error
import urllib.request

from cousin_lib.config import CousinConfig
from cousin_lib.server.app import ChatServer


class ServerCase(unittest.TestCase):
    def _boot(self, *, deliver=None, guard=None, notify=None, toml_extra=""):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = pathlib.Path(tmp.name)
        (home / "cousin.toml").write_text(
            '[cousin]\nslug = "wren"\nname = "Wren"\n'
            "[chat]\nport = 0\n" + toml_extra
        )
        config = CousinConfig.load(home)
        server = ChatServer(config, deliver=deliver, guard=guard,
                            notify=notify)
        server.start()
        self.addCleanup(server.stop)
        self.home = home
        return server

    def _request(self, server, path, body=None, method=None):
        url = "http://127.0.0.1:%d%s" % (server.port, path)
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, dict(resp.headers), json.loads(resp.read())
        except urllib.error.HTTPError as err:
            raw = err.read()
            try:
                parsed = json.loads(raw)
            except ValueError:
                parsed = raw
            return err.code, dict(err.headers), parsed


class TestHealth(ServerCase):
    def test_health_reports_ok_slug_and_port(self):
        server = self._boot()
        status, headers, body = self._request(server, "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["slug"], "wren")
        self.assertEqual(body["port"], server.port)

    def test_json_responses_carry_no_store_and_cors_headers(self):
        server = self._boot()
        _, headers, _ = self._request(server, "/health")
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), "*")


class TestSend(ServerCase):
    def test_send_stores_and_acknowledges(self):
        server = self._boot()
        status, _, body = self._request(
            server, "/api/send", {"user": "Sam", "message": "hello"}
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertIsInstance(body["id"], int)
        self.assertIn("timestamp", body)

    def test_send_requires_user_and_nonempty_message(self):
        server = self._boot()
        for payload in ({"message": "hi"}, {"user": "Sam"},
                        {"user": "Sam", "message": ""}):
            status, _, _ = self._request(server, "/api/send", payload)
            self.assertEqual(status, 400, "accepted %r" % (payload,))

    def test_malformed_json_is_400_never_an_empty_object(self):
        server = self._boot()
        status, _, _ = self._request(
            server, "/api/send", b"{not json", method="POST"
        )
        self.assertEqual(status, 400)

    def test_send_invokes_the_delivery_seam(self):
        calls = []
        server = self._boot(deliver=lambda **kw: calls.append(kw))
        self._request(server, "/api/send", {"user": "Sam", "message": "hi"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["user"], "Sam")
        self.assertEqual(calls[0]["message"], "hi")

    def test_send_touches_the_presence_marker_after_delivery(self):
        # The marker's mtime feeds the NEXT delivery's time prefix, so the
        # touch must happen after this delivery composed its text: at
        # deliver() time the marker still carries the previous mtime.
        marker_mtime_at_delivery = []

        def deliver(**kw):
            marker = self.home / "data" / ".last-user-msg"
            marker_mtime_at_delivery.append(
                marker.stat().st_mtime if marker.exists() else None
            )

        server = self._boot(deliver=deliver)
        self._request(server, "/api/send", {"user": "Sam", "message": "a"})
        marker = self.home / "data" / ".last-user-msg"
        self.assertTrue(marker.exists())
        self.assertIsNone(marker_mtime_at_delivery[0])


class TestReply(ServerCase):
    def test_reply_stores_under_recipient_thread_as_own_type(self):
        server = self._boot()
        status, _, body = self._request(
            server, "/api/wren_reply",
            {"message": "on it", "reply_to_user": "Sam Vimes"},
        )
        self.assertEqual(status, 200)
        _, _, hist = self._request(
            server, "/api/history?user=Sam%20Vimes"
        )
        msg = hist["messages"][0]
        self.assertEqual(msg["chat_user"], "sam_vimes")
        self.assertEqual(msg["user"], "Wren")
        self.assertEqual(msg["type"], "wren")

    def test_reply_requires_reply_to_user_no_default_recipient(self):
        # An install with no configured operator has nobody to default to.
        server = self._boot()
        status, _, _ = self._request(
            server, "/api/wren_reply", {"message": "hello"}
        )
        self.assertEqual(status, 400)

    def test_misrouted_reply_is_404_not_a_silent_wrong_history(self):
        server = self._boot()
        status, _, _ = self._request(
            server, "/api/other_reply",
            {"message": "hi", "reply_to_user": "Sam"},
        )
        self.assertEqual(status, 404)

    def test_reply_does_not_echo_into_the_terminal(self):
        calls = []
        server = self._boot(deliver=lambda **kw: calls.append(kw))
        self._request(
            server, "/api/wren_reply",
            {"message": "own words", "reply_to_user": "Sam"},
        )
        self.assertEqual(calls, [])


class TestHistoryEndpoint(ServerCase):
    def test_user_is_required(self):
        server = self._boot()
        status, _, _ = self._request(server, "/api/history")
        self.assertEqual(status, 400)

    def test_non_integer_paging_params_are_400(self):
        server = self._boot()
        status, _, _ = self._request(
            server, "/api/history?user=Sam&since=abc"
        )
        self.assertEqual(status, 400)

    def test_since_polls_forward_over_http(self):
        server = self._boot()
        for i in range(3):
            self._request(server, "/api/send",
                          {"user": "Sam", "message": "m%d" % i})
        _, _, first = self._request(server, "/api/history?user=Sam&limit=1")
        anchor = first["messages"][0]["id"]
        _, _, out = self._request(
            server, "/api/history?user=Sam&since=%d" % (anchor - 2)
        )
        self.assertEqual([m["message"] for m in out["messages"]],
                         ["m1", "m2"])


class TestSearchEndpoint(ServerCase):
    def test_q_is_required(self):
        server = self._boot()
        status, _, _ = self._request(server, "/api/search")
        self.assertEqual(status, 400)

    def test_searches_across_threads_and_within_one(self):
        server = self._boot()
        self._request(server, "/api/send",
                      {"user": "Sam", "message": "the needle"})
        self._request(server, "/api/send",
                      {"user": "Pat", "message": "a needle too"})
        _, _, all_hits = self._request(server, "/api/search?q=needle")
        self.assertEqual(len(all_hits["messages"]), 2)
        _, _, one = self._request(server, "/api/search?q=needle&user=Pat")
        self.assertEqual(len(one["messages"]), 1)


class TestReactionsEndpoint(ServerCase):
    def _send_one(self, server):
        _, _, body = self._request(
            server, "/api/send", {"user": "Sam", "message": "hello"}
        )
        return body["id"]

    def test_tap_returns_full_reaction_state(self):
        server = self._boot()
        mid = self._send_one(server)
        status, _, body = self._request(server, "/api/reactions", {
            "message_id": mid, "user": "Sam", "emoji": "🔥",
            "action": "tap",
        })
        self.assertEqual(status, 200)
        self.assertEqual(body["reactions"],
                         [{"user": "Sam", "emoji": "🔥", "tap_count": 1}])

    def test_add_and_bump_notify_with_one_structured_line(self):
        lines = []
        server = self._boot(notify=lines.append)
        mid = self._send_one(server)
        payload = {"message_id": mid, "user": "Sam", "emoji": "🔥",
                   "action": "tap"}
        self._request(server, "/api/reactions", payload)
        self._request(server, "/api/reactions", payload)
        self.assertEqual(len(lines), 2)
        self.assertEqual(
            lines[0],
            "[fw-reaction] msg-id=%d emoji=🔥 user=Sam tap_count=1"
            " op=added" % mid,
        )
        self.assertIn("tap_count=2 op=bumped", lines[1])

    def test_remove_does_not_notify(self):
        lines = []
        server = self._boot(notify=lines.append)
        mid = self._send_one(server)
        self._request(server, "/api/reactions", {
            "message_id": mid, "user": "Sam", "emoji": "🔥",
            "action": "tap",
        })
        self._request(server, "/api/reactions", {
            "message_id": mid, "user": "Sam", "emoji": "🔥",
            "action": "remove",
        })
        self.assertEqual(len(lines), 1)

    def test_garbage_is_400_including_non_integer_ids(self):
        server = self._boot()
        for payload in (
            {"message_id": "seven", "user": "S", "emoji": "x",
             "action": "tap"},
            {"message_id": 1, "user": "S", "emoji": "x",
             "action": "toggle"},
            {"message_id": 1, "user": "", "emoji": "x", "action": "tap"},
            {"message_id": 1, "user": "S", "emoji": "", "action": "tap"},
        ):
            status, _, _ = self._request(server, "/api/reactions", payload)
            self.assertEqual(status, 400, "accepted %r" % (payload,))


class TestArchiveEndpoint(ServerCase):
    def test_archives_all_but_keep_and_reports_count(self):
        server = self._boot()
        for i in range(4):
            self._request(server, "/api/send",
                          {"user": "Sam", "message": "m%d" % i})
        status, _, body = self._request(
            server, "/api/archive", {"user": "Sam", "keep": 1}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["archived"], 3)
        _, _, hist = self._request(server, "/api/history?user=Sam")
        self.assertEqual(hist["total"], 1)

    def test_keep_is_validated(self):
        server = self._boot()
        status, _, _ = self._request(
            server, "/api/archive", {"user": "Sam", "keep": "lots"}
        )
        self.assertEqual(status, 400)


class TestInboundFiles(ServerCase):
    # A 1x1 transparent PNG.
    _PNG = (
        "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAf"
        "FcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
    )

    def test_data_image_lands_in_inbox_and_marks_the_delivery(self):
        calls = []
        server = self._boot(deliver=lambda **kw: calls.append(kw))
        _, _, body = self._request(server, "/api/send", {
            "user": "Sam", "message": "look", "image": self._PNG,
        })
        expected = self.home / "chat" / "inbound" / ("%d.png" % body["id"])
        self.assertTrue(expected.is_file())
        self.assertEqual(
            calls[0]["attachments"],
            ["[image attached -> Read %s]" % expected],
        )

    def test_unknown_image_subtype_normalizes_to_bin(self):
        server = self._boot()
        _, _, body = self._request(server, "/api/send", {
            "user": "Sam", "message": "x",
            "image": "data:image/weird;base64,aGVsbG8=",
        })
        expected = self.home / "chat" / "inbound" / ("%d.bin" % body["id"])
        self.assertTrue(expected.is_file())

    def test_undecodable_image_is_flagged_not_silently_dropped(self):
        calls = []
        server = self._boot(deliver=lambda **kw: calls.append(kw))
        self._request(server, "/api/send", {
            "user": "Sam", "message": "x", "image": "data:image/png;base64,@@@",
        })
        self.assertEqual(calls[0]["attachments"],
                         ["[image attached, decode failed]"])
        self.assertFalse((self.home / "chat" / "inbound").exists())


class TestGuardFirst(ServerCase):
    def test_denied_address_is_403_on_every_route_with_cors(self):
        server = self._boot(guard=lambda addr: False)
        for path, body in (("/health", None),
                           ("/api/send", {"user": "S", "message": "x"}),
                           ("/api/history?user=S", None)):
            status, headers, _ = self._request(server, path, body)
            self.assertEqual(status, 403, path)
            # The CORS header on the denial is what lets a browser show
            # the status instead of an opaque network error.
            self.assertEqual(headers.get("Access-Control-Allow-Origin"),
                             "*", path)

    def test_guard_receives_the_client_address(self):
        seen = []
        server = self._boot(guard=lambda addr: seen.append(addr) or True)
        self._request(server, "/health")
        self.assertEqual(seen, ["127.0.0.1"])


class TestStaticFiles(ServerCase):
    def _raw_get(self, server, path):
        url = "http://127.0.0.1:%d%s" % (server.port, path)
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as err:
            return err.code, err.read()

    def test_serves_allowlisted_files_from_www(self):
        server = self._boot()
        www = self.home / "www"
        www.mkdir()
        (www / "index.html").write_text("<h1>hi</h1>")
        status, body = self._raw_get(server, "/index.html")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"<h1>hi</h1>")

    def test_path_traversal_cannot_escape_the_root(self):
        server = self._boot()
        (self.home / "www").mkdir()
        # cousin.toml sits one level above www/ and must be unreachable.
        status, body = self._raw_get(server, "/../cousin.toml")
        self.assertNotEqual(status, 200)
        self.assertNotIn(b"slug", body)

    def test_disallowed_extension_is_not_served(self):
        server = self._boot()
        www = self.home / "www"
        www.mkdir()
        (www / "notes.py").write_text("secrets = 1")
        status, body = self._raw_get(server, "/notes.py")
        self.assertNotEqual(status, 200)
        self.assertNotIn(b"secrets", body)

    def test_no_www_directory_means_no_pages(self):
        server = self._boot()
        status, _ = self._raw_get(server, "/index.html")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
