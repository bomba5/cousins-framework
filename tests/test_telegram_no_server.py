"""The Telegram bridge needs no chat server to relay a cousin's replies
(one inbound surface): it reads the operator thread from the cousin's own
chat store (chat_api.history, the function the chat server's /api/history
runs), and a runner cousin's cousin.toml needs no [chat] port for the
bridge to start."""
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from cousin_lib import telegram
from cousin_lib.config import CousinConfig
from cousin_lib.server import chat_api
from tests._hermetic import HermeticCase


class TestNoChatServer(HermeticCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        (self.root / "config").mkdir()
        (self.root / "config" / "telegram-wren.token").write_text("123:abc")
        self.home = self.root / "cousins" / "wren"
        (self.home / "data").mkdir(parents=True)
        (self.home / "cousin.toml").write_text(
            '[cousin]\nslug = "wren"\nname = "Wren"\n\n[agent]\nrunner = "sdk"\n\n'
            '[telegram]\nenabled = true\ntoken_file = "config/telegram-wren.token"\n'
            'operators = [{user_id = 42, name = "Priya"}]\n')
        p = mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": str(self.root),
                                         "COUSIN_HOME": str(self.home)})
        p.start(); self.addCleanup(p.stop)

    def test_a_runner_cousin_with_no_chat_port_loads(self):
        cfg = telegram.load_bridge_config(self.home)
        self.assertIsNone(cfg.port)

    def test_history_is_read_from_the_chat_store_not_over_http(self):
        cfg = telegram.load_bridge_config(self.home)
        cousin = CousinConfig.load(self.home)
        first = chat_api.reply(cousin, {"message": "the tins moved", "reply_to_user": "Priya"})
        second = chat_api.reply(cousin, {"message": "and the keys", "reply_to_user": "Priya"})
        with mock.patch("urllib.request.urlopen",
                        side_effect=AssertionError("no HTTP for history")):
            newest = telegram._history(cfg, "Priya")
            after = telegram._history(cfg, "Priya", first["id"])
        self.assertEqual([m["id"] for m in newest], [second["id"]])
        self.assertEqual([m["message"] for m in after], ["and the keys"])


if __name__ == "__main__":
    unittest.main()
