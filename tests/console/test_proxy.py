"""The chat proxy: every chat route forwards to the cousin's own chat
server by its configured port and stores nothing (docs/reference/console-api.md,
"Chat: a proxy over each cousin's chat server"). The upstream here is a
real ChatServer on an ephemeral loopback port; the console side is
exercised through the router with a minimal request object."""
import base64
import json
import pathlib
import socket
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

from cousin_lib.config import CousinConfig
from cousin_lib.console import proxy, router
from cousin_lib.server.app import ChatServer

# A 1x1 transparent PNG: a real decodable image for the inbox path.
_PNG = base64.b64encode(bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
)).decode()


def _closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class ProxyCase(unittest.TestCase):
    def setUp(self):
        router.clear()
        proxy.register()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.home = self.root / "cousins" / "testa"
        self.home.mkdir(parents=True)
        self._write_toml(0)
        server = ChatServer(CousinConfig.load(self.home))
        server.start()
        self.addCleanup(server.stop)
        self.server = server
        self._write_toml(server.port)

    def _write_toml(self, port, slug="testa"):
        (self.home / "cousin.toml").write_text(
            '[cousin]\nslug = "%s"\nname = "Testa"\n[chat]\nport = %d\n'
            % (slug, port))

    def _req(self, query=None, body=None):
        return SimpleNamespace(root=self.root, query=query or {},
                               body=body or {})

    def _get(self, path, **query):
        return router.dispatch("GET", path, req=self._req(query=query))

    def _post(self, path, **body):
        return router.dispatch("POST", path, req=self._req(body=body))


class TestValidation(ProxyCase):
    def test_messages_requires_cousin_and_user(self):
        self.assertEqual(self._get("/api/messages", user="Sam")[0], 400)
        self.assertEqual(self._get("/api/messages", cousin="testa")[0], 400)

    def test_bad_slug_is_400_before_any_lookup(self):
        status, body = self._get("/api/messages", cousin="../x", user="Sam")
        self.assertEqual((status, body["error"]), (400, "bad slug"))

    def test_unknown_cousin_is_404(self):
        self.assertEqual(
            self._get("/api/messages", cousin="nobody", user="Sam")[0], 404)

    def test_send_requires_cousin_and_user(self):
        self.assertEqual(self._post("/api/chat/send", user="Sam",
                                    message="x")[0], 400)
        self.assertEqual(self._post("/api/chat/send", cousin="testa",
                                    message="x")[0], 400)


class TestRoundTrip(ProxyCase):
    def test_send_then_messages_reads_the_servers_history(self):
        status, body = self._post("/api/chat/send", cousin="testa",
                                  user="Sam", message="hello there")
        self.assertEqual(status, 200, body)
        self.assertTrue(body["ok"])
        self.assertIsInstance(body["id"], int)
        status, hist = self._get("/api/messages", cousin="testa", user="Sam")
        self.assertEqual(status, 200)
        self.assertEqual(hist["cousin"], "testa")
        self.assertEqual(hist["total"], 1)
        self.assertIn("has_more", hist)
        msg = hist["messages"][0]
        self.assertEqual(msg["message"], "hello there")
        self.assertEqual(msg["reactions"], [])
        self.assertNotIn("attachment", msg)

    def test_console_stores_nothing(self):
        self._post("/api/chat/send", cousin="testa", user="Sam", message="x")
        files = {p.name for p in self.root.rglob("*") if p.is_file()}
        # only the cousin's own store and its config exist; no console db
        self.assertTrue(all(n.startswith("chat.db") or n == "cousin.toml"
                            or n == ".last-user-msg" for n in files), files)

    def test_history_parameters_pass_through(self):
        for i in range(3):
            self._post("/api/chat/send", cousin="testa", user="Sam",
                       message="m%d" % i)
        status, hist = self._get("/api/messages", cousin="testa",
                                 user="Sam", limit="2")
        self.assertEqual([m["message"] for m in hist["messages"]],
                         ["m1", "m2"])
        self.assertTrue(hist["has_more"])
        status, hist = self._get("/api/messages", cousin="testa",
                                 user="Sam", since=str(hist["messages"][0]["id"]))
        self.assertEqual([m["message"] for m in hist["messages"]], ["m2"])

    def test_inbound_image_is_annotated_with_its_console_url(self):
        status, body = self._post(
            "/api/chat/send", cousin="testa", user="Sam", message="pic",
            image="data:image/png;base64," + _PNG)
        self.assertEqual(status, 200, body)
        mid = body["id"]
        self.assertTrue((self.home / "chat" / "inbound"
                         / ("%d.png" % mid)).is_file())
        _, hist = self._get("/api/messages", cousin="testa", user="Sam")
        self.assertEqual(hist["messages"][0]["attachment"],
                         {"url": "/api/chat/inbound/testa/%d.png" % mid,
                          "kind": "image"})

    def test_search_returns_messages_key_newest_first(self):
        self._post("/api/chat/send", cousin="testa", user="Sam",
                   message="alpha one")
        self._post("/api/chat/send", cousin="testa", user="Sam",
                   message="alpha two")
        self._post("/api/chat/send", cousin="testa", user="Sam",
                   message="beta")
        status, body = self._get("/api/search", cousin="testa", q="alpha",
                                 user="Sam")
        self.assertEqual(status, 200)
        self.assertEqual(body["cousin"], "testa")
        self.assertEqual([m["message"] for m in body["messages"]],
                         ["alpha two", "alpha one"])
        self.assertNotIn("results", body)

    def test_reactions_default_to_tap_and_target_the_public_route(self):
        _, sent = self._post("/api/chat/send", cousin="testa", user="Sam",
                             message="react to me")
        status, body = self._post("/api/chat/reactions", cousin="testa",
                                  message_id=sent["id"], user="Sam",
                                  emoji="+1")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["op"], "added")
        self.assertEqual(body["reactions"][0]["tap_count"], 1)
        status, body = self._post("/api/chat/reactions", cousin="testa",
                                  message_id=sent["id"], user="Sam",
                                  emoji="+1", action="remove")
        self.assertEqual(body["op"], "removed")

    def test_archive_reports_the_servers_count(self):
        for i in range(3):
            self._post("/api/chat/send", cousin="testa", user="Sam",
                       message="m%d" % i)
        status, body = self._post("/api/chat/archive", cousin="testa",
                                  user="Sam", keep=1)
        self.assertEqual(status, 200, body)
        self.assertEqual(body, {"ok": True, "archived": 2})
        status, body = self._post("/api/chat/archive", cousin="testa",
                                  user="Sam")
        self.assertEqual(body["archived"], 1)


class TestErrorMapping(ProxyCase):
    def test_upstream_400_passes_through_with_its_body(self):
        status, body = self._post("/api/chat/send", cousin="testa",
                                  user="Sam", message="")
        self.assertEqual(status, 400)
        self.assertIn("error", body)

    def test_unreachable_server_is_502(self):
        self._write_toml(_closed_port())
        status, body = self._get("/api/messages", cousin="testa", user="Sam")
        self.assertEqual(status, 502)
        self.assertFalse(body["ok"])
        self.assertIn("error", body)

    def test_no_chat_port_is_502_not_a_crash(self):
        (self.home / "cousin.toml").write_text(
            '[cousin]\nslug = "testa"\nname = "Testa"\n')
        status, body = self._get("/api/messages", cousin="testa", user="Sam")
        self.assertEqual(status, 502)

    def test_non_json_upstream_is_502(self):
        class Plain(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"not json at all")

            def log_message(self, *a):
                pass

        httpd = HTTPServer(("127.0.0.1", 0), Plain)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        self._write_toml(httpd.server_address[1])
        status, body = self._get("/api/messages", cousin="testa", user="Sam")
        self.assertEqual(status, 502)
        self.assertIn("error", body)


class TestInboundFiles(ProxyCase):
    def _send_image(self):
        _, body = self._post(
            "/api/chat/send", cousin="testa", user="Sam", message="pic",
            image="data:image/png;base64," + _PNG)
        return body["id"]

    def test_serves_the_inbox_file_read_only(self):
        mid = self._send_image()
        status, body = self._get("/api/chat/inbound/testa/%d.png" % mid)
        self.assertEqual(status, 200)
        self.assertIsInstance(body, proxy.RawResponse)
        headers = {k.lower(): v for k, v in body.headers}
        self.assertEqual(headers["content-type"], "image/png")
        self.assertEqual(headers["cache-control"], "private, max-age=3600")
        self.assertEqual(body.body, base64.b64decode(_PNG))

    def test_bad_names_and_traversal_are_404(self):
        self._send_image()
        for name in ("cousin.toml", "..%2Fcousin.toml", "1.exe",
                     "x.png", "999.png"):
            status, body = self._get("/api/chat/inbound/testa/" + name)
            self.assertEqual(status, 404, name)
            self.assertNotIsInstance(body, proxy.RawResponse)

    def test_unknown_cousin_is_404(self):
        status, _ = self._get("/api/chat/inbound/nobody/1.png")
        self.assertEqual(status, 404)

    def test_registered_routes_match_the_contract(self):
        have = set(router.routes())
        for want in (("GET", "/api/messages"), ("GET", "/api/search"),
                     ("POST", "/api/chat/send"),
                     ("POST", "/api/chat/archive"),
                     ("POST", "/api/chat/reactions"),
                     ("GET", "/api/chat/inbound/{slug}/{name}")):
            self.assertIn(want, have)


class TestGeneratedMedia(ProxyCase):
    """A cousin's own reply can carry a generated asset: the row's
    attachment_kind/attachment_path point at <home>/chat/<folder>/. The
    console projects it as {url, kind} and serves the file read-only,
    with byte ranges so a browser can seek a video."""

    _CLIP = bytes(range(256)) * 4   # 1024 bytes standing in for a video

    def _reply(self, kind, path, message="here"):
        import urllib.request
        body = json.dumps({"message": message, "reply_to_user": "Sam",
                           "attachment": {"kind": kind, "path": str(path)}})
        request = urllib.request.Request(
            "http://127.0.0.1:%d/api/testa_reply" % self.server.port,
            data=body.encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=5) as resp:
            return json.loads(resp.read())["id"]

    def _asset(self, folder, name, data):
        target = self.home / "chat" / folder / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return target

    def _attachments(self):
        _, hist = self._get("/api/messages", cousin="testa", user="Sam")
        return [m.get("attachment") for m in hist["messages"]]

    def test_each_kind_is_annotated_with_url_and_display_kind(self):
        self._reply("video", self._asset("video", "testa_1_ab.mp4", self._CLIP))
        self._reply("voice", self._asset("audio", "testa_2_cd.mp3", b"ID3"))
        self._reply("image", self._asset("images", "testa_3_ef.png",
                                          base64.b64decode(_PNG)))
        self.assertEqual(self._attachments(), [
            {"url": "/api/chat/media/testa/video/testa_1_ab.mp4", "kind": "video"},
            {"url": "/api/chat/media/testa/audio/testa_2_cd.mp3", "kind": "audio"},
            {"url": "/api/chat/media/testa/images/testa_3_ef.png", "kind": "image"},
        ])

    def test_search_rows_are_annotated_the_same_way(self):
        self._reply("video", self._asset("video", "c.mp4", self._CLIP),
                    message="the clip")
        _, found = self._get("/api/search", cousin="testa", q="clip", user="Sam")
        self.assertEqual(found["messages"][0]["attachment"]["kind"], "video")

    def test_a_path_outside_the_media_folders_is_not_projected(self):
        stray = self.root / "elsewhere" / "x.mp4"
        stray.parent.mkdir()
        stray.write_bytes(self._CLIP)
        self._reply("video", stray)
        self._reply("video", self.home / "chat" / "video" / "missing.mp4")
        self._reply("video", self._asset("video", "notes.txt", b"hi"))
        self.assertEqual(self._attachments(), [None, None, None])

    def test_serves_the_whole_file_with_its_type(self):
        self._asset("video", "c.mp4", self._CLIP)
        status, body = self._get("/api/chat/media/testa/video/c.mp4")
        self.assertEqual(status, 200)
        self.assertIsInstance(body, proxy.RawResponse)
        headers = {k.lower(): v for k, v in body.headers}
        self.assertEqual(headers["content-type"], "video/mp4")
        self.assertEqual(headers["accept-ranges"], "bytes")
        self.assertEqual(body.body, self._CLIP)

    def test_a_byte_range_is_a_206_with_its_slice(self):
        self._asset("video", "c.mp4", self._CLIP)
        req = self._req()
        req.headers = {"Range": "bytes=100-199"}
        status, body = router.dispatch(
            "GET", "/api/chat/media/testa/video/c.mp4", req=req)
        self.assertEqual(status, 206)
        headers = {k.lower(): v for k, v in body.headers}
        self.assertEqual(headers["content-range"], "bytes 100-199/1024")
        self.assertEqual(body.body, self._CLIP[100:200])
        req.headers = {"Range": "bytes=-24"}
        status, body = router.dispatch(
            "GET", "/api/chat/media/testa/video/c.mp4", req=req)
        self.assertEqual((status, body.body), (206, self._CLIP[-24:]))
        req.headers = {"Range": "bytes=5000-"}
        status, body = router.dispatch(
            "GET", "/api/chat/media/testa/video/c.mp4", req=req)
        self.assertEqual(status, 416)

    def test_bad_folders_names_and_traversal_are_404(self):
        self._asset("video", "c.mp4", self._CLIP)
        (self.home / "chat" / "video" / "c.exe").write_bytes(b"x")
        for tail in ("inbound/1.png", "video/c.exe", "video/..%2F..%2Fcousin.toml",
                     "video/.hidden.mp4", "images/c.mp4", "video/none.mp4"):
            status, body = self._get("/api/chat/media/testa/" + tail)
            self.assertEqual(status, 404, tail)
            self.assertNotIsInstance(body, proxy.RawResponse)
        status, _ = self._get("/api/chat/media/nobody/video/c.mp4")
        self.assertEqual(status, 404)

    def test_the_route_is_registered(self):
        self.assertIn(("GET", "/api/chat/media/{slug}/{folder}/{name}"),
                      set(router.routes()))


if __name__ == "__main__":
    unittest.main()
