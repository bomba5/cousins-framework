"""One inbound surface: a runner cousin with no chat
server and no [chat] port is reachable on every inbound path, and its
replies leave on every outbound one. Each path's own tests are in
test_reply, test_media, test_chat_local_runner, test_telegram_no_server,
test_peer_inbound, console/test_hive_tell_home and console/test_peer_send;
this one walks them on one home."""
import contextlib
import os
import pathlib
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

from cousin_lib import chat, peer_inbound, telegram
from cousin_lib.config import CousinConfig, FrameworkConfig
from cousin_lib.reply import send_reply
from tests._hermetic import HermeticCase


class TestNoChatServer(HermeticCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        (self.root / "config").mkdir()
        (self.root / "config" / "telegram-wren.token").write_text("123:abc")
        for slug, extra in (("wren", '[agent]\nrunner = "sdk"\n\n[telegram]\nenabled = true\n'
                                     'token_file = "config/telegram-wren.token"\n'
                                     'operators = [{user_id = 42, name = "Priya"}]\n'),
                            ("sam", '[agent]\nrunner = "sdk"\n')):
            home = self.root / "cousins" / slug
            (home / "data").mkdir(parents=True)
            (home / "cousin.toml").write_text(
                '[cousin]\nslug = "%s"\nname = "%s"\n\n[operator]\nname = "Priya"\n\n%s'
                % (slug, slug.capitalize(), extra))
        p = mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": str(self.root),
                                         "COUSIN_HOME": str(self.root / "cousins" / "wren")})
        p.start(); self.addCleanup(p.stop)
        self.home = self.root / "cousins" / "wren"

    def test_every_path_works_with_no_network_at_all(self):
        """No inet connection is made: refused at the socket, not only at
        urlopen; the CLI's own entry is walked too."""
        import contextlib
        import io
        import socket
        from cousin_lib.reply import reply_main
        real_connect = socket.socket.connect

        def connect(sock, address):
            if sock.family in (socket.AF_INET, socket.AF_INET6):
                raise AssertionError("an inet connection to %r" % (address,))
            return real_connect(sock, address)
        with mock.patch.object(socket.socket, "connect", connect), \
                mock.patch("urllib.request.urlopen", side_effect=AssertionError("no HTTP")):
            # in: a peer, an external sender (through the gate its routes use)
            chat.send_message(FrameworkConfig(self.root),
                              CousinConfig.load(self.root / "cousins" / "sam"), "wren", "hi")
            peer_inbound.accept(self.root, identity="peer:kestrel", display="Kestrel", to="wren",
                                message="from afar", msg_id="kestrel-00000001",
                                sent_at=time.time(), allowed=lambda slug: True)
            # out: a reply, and the Telegram bridge reading it back
            send_reply(CousinConfig.load(self.home), "noted", user="Priya")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(reply_main(["--user", "Priya", "-m", "and filed"]), 0)
            relayed = telegram._history(telegram.load_bridge_config(self.home), "Priya")
        with contextlib.closing(sqlite3.connect(self.home / "data" / "chat.db")) as db:
            rows = db.execute("SELECT user, message FROM messages ORDER BY id").fetchall()
        self.assertEqual(rows, [("Sam", "hi"), ("Kestrel", "from afar"), ("Wren", "noted"),
                                ("Wren", "and filed")])
        self.assertEqual([m["message"] for m in relayed], ["and filed"])   # the newest reply
        with contextlib.closing(sqlite3.connect(self.home / "data" / "inbox.db")) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM inbox").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
