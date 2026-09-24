"""A peer message to a local runner-lane cousin needs no chat server
(phase 10a, one inbound surface): `cousin-chat send`, the runner's `send`
tool (both through chat.send_message) and the console's peer route write
the target's chat store and inbox in-process, through chat_api.send, the
function its chat server's /api/send runs. A tmux-lane target is still
reached over HTTP until the tmux lane is gone."""
import os
import pathlib
import sqlite3
import tempfile
import unittest
from unittest import mock

from cousin_lib import chat
from cousin_lib.config import CousinConfig, FrameworkConfig
from tests._hermetic import HermeticCase
from tests.console._harness import ConsoleCase


def _messages(home):
    path = pathlib.Path(home) / "data" / "chat.db"
    if not path.exists():
        return []
    with sqlite3.connect(path) as db:
        return db.execute("SELECT user, message FROM messages ORDER BY id").fetchall()


def _inbox(home):
    path = pathlib.Path(home) / "data" / "inbox.db"
    if not path.exists():
        return []
    with sqlite3.connect(path) as db:
        return db.execute("SELECT thread_id, source, body, state FROM inbox ORDER BY id").fetchall()


class TestSendMessage(HermeticCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        (self.root / "config").mkdir()
        for slug, extra in (("wren", "[chat]\nport = 8090\n"),
                            ("testa", '[agent]\nrunner = "sdk"\n'),        # no chat port at all
                            ("sam", "[chat]\nport = 8092\n")):
            home = self.root / "cousins" / slug
            (home / "data").mkdir(parents=True)
            (home / "cousin.toml").write_text(
                '[cousin]\nslug = "%s"\nname = "%s"\n%s' % (slug, slug.capitalize(), extra))
        p = mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": str(self.root)})
        p.start(); self.addCleanup(p.stop)
        self.fw = FrameworkConfig(self.root)
        self.sender = CousinConfig.load(self.root / "cousins" / "wren")

    def test_a_runner_cousin_is_reached_with_no_chat_server(self):
        with mock.patch("urllib.request.urlopen",
                        side_effect=AssertionError("no HTTP for a runner cousin")):
            result = chat.send_message(self.fw, self.sender, "testa", "the tins moved")
        home = self.root / "cousins" / "testa"
        self.assertTrue(result["ok"])
        self.assertEqual(_messages(home), [("Wren", "the tins moved")])
        [(thread, source, body, state)] = _inbox(home)
        self.assertEqual((source, state), ("chat", "queued"))
        self.assertIn("the tins moved", body)

    def test_a_tmux_cousin_is_still_reached_over_http(self):
        seen = []

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b'{"ok": true, "id": 5}'

        def urlopen(req, timeout=None):
            seen.append(req.full_url)
            return _Resp()
        with mock.patch("urllib.request.urlopen", side_effect=urlopen):
            result = chat.send_message(self.fw, self.sender, "sam", "hi")
        self.assertEqual((seen, result["id"]), (["http://localhost:8092/api/send"], 5))
        self.assertEqual(_messages(self.root / "cousins" / "sam"), [])

    def test_the_outbound_filter_still_runs_first(self):
        from cousin_lib.outbound_filter import FilterBlocked, OutboundPolicy
        (self.root / "config" / "outbound-filter.json").write_text('{"terms": ["zorblatt"]}')
        with self.assertRaises(FilterBlocked):
            chat.send_message(self.fw, self.sender, "testa", "zorblatt",
                              policy=OutboundPolicy.load(self.root))
        self.assertEqual(_messages(self.root / "cousins" / "testa"), [])


class TestConsolePeerRoute(ConsoleCase):
    def test_the_console_reaches_a_runner_cousin_without_its_chat_server(self):
        self.cousin("wren")
        home = self.cousin("testa", port=None, extra='\n[agent]\nrunner = "sdk"\n')
        self.serve()
        with mock.patch("cousin_lib.console.routes_fleet.chat_call",
                        side_effect=AssertionError("no chat server for a runner cousin")):
            status, body = self.post("/api/cousins/wren/peer", {"to": "testa", "text": "hello"})
        self.assertEqual(status, 200, body)
        self.assertEqual(_messages(home), [("Wren", "hello")])
        self.assertEqual(len(_inbox(home)), 1)


if __name__ == "__main__":
    unittest.main()
