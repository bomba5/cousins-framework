"""Telegram bridge: the first send-to-a-third-party surface.

Perimeter tests lead: unconfigured refuses to start, an unauthorized
sender is dropped, and what leaves the box goes only to the injected
Telegram client. Both the Telegram API and the cousin's chat server
are seams here - the relay logic is exercised without a real network.
"""
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from cousin_lib.telegram import (
    TelegramConfigError,
    load_bridge_config,
    relay_inbound,
    relay_outbound,
)


class TelegramCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        (self.root / "config").mkdir()
        self.home = self.root / "cousins" / "wren"
        (self.home / "data").mkdir(parents=True)
        patcher = mock.patch.dict(os.environ, {
            "FRAMEWORK_ROOT": str(self.root),
            "COUSIN_HOME": str(self.home),
        })
        patcher.start()
        self.addCleanup(patcher.stop)

    def _toml(self, telegram_block):
        (self.home / "cousin.toml").write_text(
            '[cousin]\nslug = "wren"\n[chat]\nport = 8100\n'
            + telegram_block)

    def _token(self, value="123:abc"):
        (self.root / "config" / "telegram-wren.token").write_text(value)


class TestOffByDefault(TelegramCase):
    def test_no_telegram_section_refuses(self):
        self._toml("")
        with self.assertRaises(TelegramConfigError):
            load_bridge_config(self.home)

    def test_disabled_refuses(self):
        self._token()
        self._toml('[telegram]\nenabled = false\n'
                   'token_file = "config/telegram-wren.token"\n'
                   'operators = [{user_id = 1, name = "Sam"}]\n')
        with self.assertRaises(TelegramConfigError):
            load_bridge_config(self.home)

    def test_missing_token_refuses_naming_the_file(self):
        self._toml('[telegram]\nenabled = true\n'
                   'token_file = "config/telegram-wren.token"\n'
                   'operators = [{user_id = 1, name = "Sam"}]\n')
        with self.assertRaises(TelegramConfigError) as ctx:
            load_bridge_config(self.home)
        self.assertIn("telegram-wren.token", str(ctx.exception))

    def test_empty_operator_list_refuses(self):
        self._token()
        self._toml('[telegram]\nenabled = true\n'
                   'token_file = "config/telegram-wren.token"\n'
                   'operators = []\n')
        with self.assertRaises(TelegramConfigError) as ctx:
            load_bridge_config(self.home)
        self.assertIn("operator", str(ctx.exception).lower())

    def test_valid_config_loads(self):
        self._token("bottok")
        self._toml('[telegram]\nenabled = true\n'
                   'token_file = "config/telegram-wren.token"\n'
                   'operators = [{user_id = 42, name = "Sam"}]\n')
        cfg = load_bridge_config(self.home)
        self.assertEqual(cfg.token, "bottok")
        self.assertEqual(cfg.operator_ids, {42})


class _BridgeFixture(TelegramCase):
    def _bridge(self):
        self._token("bottok")
        self._toml('[telegram]\nenabled = true\n'
                   'token_file = "config/telegram-wren.token"\n'
                   'operators = [{user_id = 42, name = "Sam"}]\n')
        return load_bridge_config(self.home)


class TestInboundGate(_BridgeFixture):
    def test_authorized_message_reaches_the_chat_server(self):
        cfg = self._bridge()
        sent = []
        relay_inbound(
            cfg,
            update={"message": {"from": {"id": 42, "first_name": "Sam"},
                                "text": "hello cousin"}},
            chat_send=lambda **kw: sent.append(kw))
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["message"], "hello cousin")
        self.assertEqual(sent[0]["user"], "Sam")

    def test_unauthorized_sender_is_dropped_silently(self):
        cfg = self._bridge()
        sent, replied = [], []
        relay_inbound(
            cfg,
            update={"message": {"from": {"id": 999, "first_name": "X"},
                                "text": "let me in"}},
            chat_send=lambda **kw: sent.append(kw),
            tg_send=lambda **kw: replied.append(kw))
        # Nothing forwarded, and no acknowledgement to the stranger.
        self.assertEqual(sent, [])
        self.assertEqual(replied, [])


class TestOutbound(_BridgeFixture):
    def test_cousin_reply_is_relayed_to_telegram(self):
        cfg = self._bridge()
        tg = []
        relay_outbound(
            cfg,
            new_replies=[{"message": "on it", "attachment_kind": None,
                          "attachment_path": None}],
            tg_send_text=lambda **kw: tg.append(kw))
        self.assertEqual(len(tg), 1)
        self.assertEqual(tg[0]["chat_id"], 42)
        self.assertEqual(tg[0]["text"], "on it")

    def test_media_reply_relays_the_local_file(self):
        cfg = self._bridge()
        asset = self.home / "chat" / "images" / "wren_1_ab.png"
        asset.parent.mkdir(parents=True)
        asset.write_bytes(b"\x89PNG")
        media = []
        relay_outbound(
            cfg,
            new_replies=[{"message": "", "attachment_kind": "image",
                          "attachment_path": str(asset)}],
            tg_send_text=lambda **kw: None,
            tg_send_media=lambda **kw: media.append(kw))
        self.assertEqual(len(media), 1)
        self.assertEqual(media[0]["kind"], "image")
        self.assertEqual(media[0]["path"], str(asset))


class TestCli(TelegramCase):
    def test_unconfigured_main_refuses_with_exit_2(self):
        self._toml("")  # no [telegram]
        from cousin_lib.telegram import telegram_main
        import contextlib
        import io
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = telegram_main(["--home", str(self.home)])
        self.assertEqual(rc, 2)
        self.assertIn("off for this cousin", err.getvalue())

    def test_no_home_refuses(self):
        from cousin_lib.telegram import telegram_main
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(telegram_main([]), 2)


if __name__ == "__main__":
    unittest.main()
