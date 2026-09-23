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
    _multipart,
    _upload_spec,
    load_cursors,
    pump_inbound,
    pump_outbound,
    relay_inbound,
    relay_outbound,
    save_cursors,
)
from tests._hermetic import HermeticCase


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

    def test_unauthorized_sender_is_dropped_silently_on_the_wire(self):
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

    def test_a_rejected_sender_is_logged_loudly_with_the_id(self):
        # Silent on the wire, loud in the log: an operator who typoed
        # their own chat id must be able to tell "not on the list" from
        # "bridge down". The attacker still learns nothing.
        cfg = self._bridge()
        logged = []
        relay_inbound(
            cfg,
            update={"message": {"from": {"id": 999, "first_name": "X"},
                                "text": "let me in"}},
            chat_send=lambda **kw: None,
            log=logged.append)
        self.assertTrue(any("999" in line for line in logged),
                        "the rejected id must reach the log")


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


def _http_error(code, body=None):
    import io
    import urllib.error
    return urllib.error.HTTPError(
        "http://x", code, "err", {},
        io.BytesIO(body) if body is not None else None)


class TestErrorText(unittest.TestCase):
    """A rejection logs what the server said, not only the status line."""

    def test_telegram_description_is_included(self):
        from cousin_lib.telegram import _describe
        err = _http_error(400, b'{"ok": false, "error_code": 400,'
                               b' "description": "Bad Request: chat not found"}')
        self.assertIn("chat not found", _describe(err))
        self.assertIn("400", _describe(err))

    def test_chat_server_error_field_is_included(self):
        from cousin_lib.telegram import _describe
        err = _http_error(400, b'{"error": "user and a non-empty message'
                               b' are required"}')
        self.assertIn("non-empty message", _describe(err))

    def test_describing_twice_keeps_the_text(self):
        # The body is a stream; a second log line must not come out bare.
        from cousin_lib.telegram import _describe
        err = _http_error(403, b'{"description": "Forbidden: bot was blocked"}')
        _describe(err)
        self.assertIn("blocked", _describe(err))

    def test_a_body_that_is_not_json_is_shown_raw(self):
        from cousin_lib.telegram import _describe
        self.assertIn("gateway down",
                      _describe(_http_error(502, b"gateway down")))

    def test_no_body_and_plain_errors_fall_back_to_str(self):
        from cousin_lib.telegram import _describe
        self.assertIn("400", _describe(_http_error(400)))
        self.assertEqual(_describe(OSError("boom")), "boom")


class TestCursors(_BridgeFixture):
    """Tracker #18: a cursor moves only past what was delivered, and it
    survives a restart."""

    def test_cursors_round_trip_through_the_home(self):
        cfg = self._bridge()
        save_cursors(cfg.home, {"tg_offset": 7, "threads": {"Sam": 41}})
        self.assertEqual(load_cursors(cfg.home),
                         {"tg_offset": 7, "threads": {"Sam": 41}})

    def test_no_cursor_file_is_a_fresh_start(self):
        cfg = self._bridge()
        self.assertEqual(load_cursors(cfg.home),
                         {"tg_offset": 0, "threads": {}})

    def test_a_corrupt_cursor_file_is_a_fresh_start(self):
        cfg = self._bridge()
        (cfg.home / "data" / "telegram-bridge.json").write_text("{nope")
        self.assertEqual(load_cursors(cfg.home),
                         {"tg_offset": 0, "threads": {}})


class TestPumpInbound(_BridgeFixture):
    def _update(self, uid, text="hi"):
        return {"update_id": uid,
                "message": {"from": {"id": 42}, "text": text}}

    def test_offset_advances_past_each_relayed_update(self):
        cfg = self._bridge()
        state = {"tg_offset": 0, "threads": {}}
        sent = []
        pump_inbound(cfg, state, [self._update(5), self._update(6)],
                     relay=lambda cfg, update: sent.append(update))
        self.assertEqual(len(sent), 2)
        self.assertEqual(state["tg_offset"], 7)
        self.assertEqual(load_cursors(cfg.home)["tg_offset"], 7)

    def test_a_transient_failure_keeps_the_update_for_the_retry(self):
        cfg = self._bridge()
        state = {"tg_offset": 0, "threads": {}}

        def relay(cfg, update):
            if update["update_id"] == 6:
                raise OSError("chat server down")
        with self.assertRaises(OSError):
            pump_inbound(cfg, state, [self._update(5), self._update(6)],
                         relay=relay)
        # 5 is done, 6 is not: the next getUpdates starts at 6.
        self.assertEqual(state["tg_offset"], 6)
        self.assertEqual(load_cursors(cfg.home)["tg_offset"], 6)

    def test_a_permanent_rejection_is_logged_and_skipped(self):
        # A 400 from the chat server will be a 400 forever; holding the
        # offset on it would wedge the bridge behind one bad update.
        cfg = self._bridge()
        state = {"tg_offset": 0, "threads": {}}
        logged = []

        def relay(cfg, update):
            raise _http_error(400)
        pump_inbound(cfg, state, [self._update(5)], relay=relay,
                     log=logged.append)
        self.assertEqual(state["tg_offset"], 6)
        self.assertTrue(any("5" in line for line in logged))

    def test_a_voice_message_is_skipped_with_a_log_line(self):
        cfg = self._bridge()
        sent, logged = [], []
        relay_inbound(
            cfg, update={"message": {"from": {"id": 42},
                                     "voice": {"file_id": "v"}}},
            chat_send=lambda **kw: sent.append(kw), log=logged.append)
        self.assertEqual(sent, [])
        self.assertTrue(logged)


class TestPumpOutbound(_BridgeFixture):
    def _rows(self):
        return [
            {"id": 10, "type": "user", "message": "q"},
            {"id": 11, "type": "wren", "message": "a1",
             "attachment_kind": None, "attachment_path": None},
            {"id": 12, "type": "wren", "message": "a2",
             "attachment_kind": None, "attachment_path": None},
        ]

    def test_cursor_advances_past_delivered_replies(self):
        cfg = self._bridge()
        state = {"tg_offset": 0, "threads": {"Sam": 9}}
        tg = []
        pump_outbound(cfg, state, "Sam", self._rows(),
                      tg_send_text=lambda **kw: tg.append(kw))
        self.assertEqual([m["text"] for m in tg], ["a1", "a2"])
        self.assertEqual(state["threads"]["Sam"], 12)
        self.assertEqual(load_cursors(cfg.home)["threads"]["Sam"], 12)

    def test_a_transient_failure_keeps_the_reply_for_the_retry(self):
        cfg = self._bridge()
        state = {"tg_offset": 0, "threads": {"Sam": 9}}

        def send(**kw):
            if kw["text"] == "a2":
                raise OSError("network down")
        with self.assertRaises(OSError):
            pump_outbound(cfg, state, "Sam", self._rows(),
                          tg_send_text=send)
        # a1 went out, a2 did not: the next poll starts after 11.
        self.assertEqual(state["threads"]["Sam"], 11)

    def test_a_permanent_rejection_is_logged_and_skipped(self):
        cfg = self._bridge()
        state = {"tg_offset": 0, "threads": {"Sam": 9}}
        logged = []

        def send(**kw):
            if kw["text"] == "a1":
                raise _http_error(400)
        pump_outbound(cfg, state, "Sam", self._rows(),
                      tg_send_text=send, log=logged.append)
        self.assertEqual(state["threads"]["Sam"], 12)
        self.assertTrue(logged)

    def test_the_rejection_log_carries_telegrams_description(self):
        cfg = self._bridge()
        state = {"tg_offset": 0, "threads": {"Sam": 9}}
        logged = []

        def send(**kw):
            raise _http_error(400, b'{"description":'
                                   b' "Bad Request: chat not found"}')
        pump_outbound(cfg, state, "Sam", self._rows()[:2],
                      tg_send_text=send, log=logged.append)
        self.assertIn("chat not found", logged[0])

    def test_rate_limit_is_transient(self):
        cfg = self._bridge()
        state = {"tg_offset": 0, "threads": {"Sam": 9}}

        def send(**kw):
            raise _http_error(429)
        with self.assertRaises(Exception):
            pump_outbound(cfg, state, "Sam", self._rows(),
                          tg_send_text=send)
        self.assertEqual(state["threads"]["Sam"], 10)


class TestOperatorThreads(TelegramCase):
    def test_each_thread_goes_to_its_own_operator(self):
        self._token("bottok")
        self._toml('[telegram]\nenabled = true\n'
                   'token_file = "config/telegram-wren.token"\n'
                   'operators = [{user_id = 42, name = "Sam"},'
                   ' {user_id = 43, name = "Ana"}]\n')
        cfg = load_bridge_config(self.home)
        self.assertEqual(cfg.threads(), {"Sam": {42}, "Ana": {43}})
        state = {"tg_offset": 0, "threads": {"Ana": 0}}
        tg = []
        pump_outbound(cfg, state, "Ana",
                      [{"id": 3, "type": "wren", "message": "hi Ana"}],
                      tg_send_text=lambda **kw: tg.append(kw))
        self.assertEqual([m["chat_id"] for m in tg], [43])


class TestMediaUpload(_BridgeFixture):
    """Tracker #13: an attachment reply uploads the file, the method and
    field matching its kind."""

    def test_kind_picks_the_method_and_field(self):
        self.assertEqual(_upload_spec("image"), ("sendPhoto", "photo"))
        self.assertEqual(_upload_spec("video"), ("sendVideo", "video"))
        self.assertEqual(_upload_spec("voice"), ("sendAudio", "audio"))

    def test_multipart_carries_the_fields_and_the_file_bytes(self):
        asset = self.home / "a.png"
        asset.write_bytes(b"\x89PNGDATA")
        body, ctype = _multipart({"chat_id": 42, "caption": "look"},
                                 "photo", asset)
        boundary = ctype.split("boundary=")[1].encode()
        self.assertTrue(ctype.startswith("multipart/form-data"))
        self.assertIn(b"--" + boundary, body)
        self.assertIn(b'name="chat_id"\r\n\r\n42', body)
        self.assertIn(b'name="caption"\r\n\r\nlook', body)
        self.assertIn(b'name="photo"; filename="a.png"', body)
        self.assertIn(b"\x89PNGDATA", body)
        self.assertTrue(body.endswith(b"--" + boundary + b"--\r\n"))

    def test_a_relative_attachment_path_resolves_under_the_home(self):
        cfg = self._bridge()
        (self.home / "chat" / "images").mkdir(parents=True)
        (self.home / "chat" / "images" / "x.png").write_bytes(b"x")
        media = []
        relay_outbound(
            cfg,
            new_replies=[{"message": "", "attachment_kind": "image",
                          "attachment_path": "chat/images/x.png"}],
            tg_send_text=lambda **kw: None,
            tg_send_media=lambda **kw: media.append(kw))
        self.assertEqual(media[0]["path"],
                         str(self.home / "chat" / "images" / "x.png"))


class TestUploadGuards(_BridgeFixture):
    """A file the bridge can never upload is logged and skipped up
    front: missing on disk (an OSError would otherwise read as transient
    and retry forever) or over Telegram's bot upload limit."""

    def _reply(self, path, kind="video"):
        return [{"id": 5, "message": "clip", "attachment_kind": kind,
                 "attachment_path": str(path)}]

    def test_a_missing_file_is_skipped_not_retried(self):
        cfg = self._bridge()
        media, logged = [], []
        relay_outbound(cfg, new_replies=self._reply(self.home / "gone.mp4"),
                       tg_send_text=lambda **kw: None,
                       tg_send_media=lambda **kw: media.append(kw),
                       log=logged.append)
        self.assertEqual(media, [])
        self.assertTrue(any("gone.mp4" in line for line in logged))

    def test_a_video_over_50_mb_is_skipped(self):
        cfg = self._bridge()
        big = self.home / "big.mp4"
        with open(big, "wb") as f:
            f.truncate(50 * 1024 * 1024 + 1)
        media, logged = [], []
        relay_outbound(cfg, new_replies=self._reply(big),
                       tg_send_text=lambda **kw: None,
                       tg_send_media=lambda **kw: media.append(kw),
                       log=logged.append)
        self.assertEqual(media, [])
        self.assertTrue(any("50 MB" in line for line in logged))

    def test_a_photo_over_10_mb_is_skipped(self):
        cfg = self._bridge()
        big = self.home / "big.png"
        with open(big, "wb") as f:
            f.truncate(10 * 1024 * 1024 + 1)
        media = []
        relay_outbound(cfg, new_replies=self._reply(big, "image"),
                       tg_send_text=lambda **kw: None,
                       tg_send_media=lambda **kw: media.append(kw),
                       log=lambda line: None)
        self.assertEqual(media, [])

    def test_a_video_at_the_limit_goes_out(self):
        cfg = self._bridge()
        ok = self.home / "ok.mp4"
        with open(ok, "wb") as f:
            f.truncate(50 * 1024 * 1024)
        media = []
        relay_outbound(cfg, new_replies=self._reply(ok),
                       tg_send_text=lambda **kw: None,
                       tg_send_media=lambda **kw: media.append(kw))
        self.assertEqual([m["kind"] for m in media], ["video"])


class TestInboundPhoto(_BridgeFixture):
    def test_a_photo_goes_in_as_an_image_with_its_caption(self):
        cfg = self._bridge()
        sent = []
        relay_inbound(
            cfg,
            update={"message": {"from": {"id": 42}, "caption": "my board",
                                "photo": [{"file_id": "small"},
                                          {"file_id": "big"}]}},
            chat_send=lambda **kw: sent.append(kw),
            tg_fetch=lambda file_id: (b"JPEGBYTES", "photos/f.jpg")
            if file_id == "big" else None)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["message"], "my board")
        self.assertEqual(sent[0]["user"], "Sam")
        self.assertTrue(sent[0]["attachment"].startswith(
            "data:image/jpeg;base64,"))

    def test_a_captionless_photo_still_has_a_message(self):
        # The chat server refuses an empty message.
        cfg = self._bridge()
        sent = []
        relay_inbound(
            cfg,
            update={"message": {"from": {"id": 42},
                                "photo": [{"file_id": "big"}]}},
            chat_send=lambda **kw: sent.append(kw),
            tg_fetch=lambda file_id: (b"x", "photos/f.png"))
        self.assertTrue(sent[0]["message"])
        self.assertTrue(sent[0]["attachment"].startswith(
            "data:image/png;base64,"))

    def test_a_photo_from_a_stranger_is_never_downloaded(self):
        cfg = self._bridge()
        fetched = []
        relay_inbound(
            cfg,
            update={"message": {"from": {"id": 999},
                                "photo": [{"file_id": "big"}]}},
            chat_send=lambda **kw: None,
            tg_fetch=lambda file_id: fetched.append(file_id),
            log=lambda line: None)
        self.assertEqual(fetched, [])


class TestRootFromHome(TelegramCase):
    def test_home_alone_finds_the_root(self):
        # FRAMEWORK_ROOT unset, COUSIN_HOME unset: --home must suffice.
        self._token("bottok")
        self._toml('[telegram]\nenabled = true\n'
                   'token_file = "config/telegram-wren.token"\n'
                   'operators = [{user_id = 42, name = "Sam"}]\n')
        with mock.patch.dict(os.environ, {}, clear=True):
            cfg = load_bridge_config(self.home)
        self.assertEqual(cfg.token, "bottok")

    def test_no_root_at_all_is_a_config_error_not_a_traceback(self):
        stray = self.root / "elsewhere" / "wren"
        stray.mkdir(parents=True)
        (stray / "cousin.toml").write_text(
            '[cousin]\nslug = "wren"\n[chat]\nport = 8100\n'
            '[telegram]\nenabled = true\ntoken_file = "t"\n'
            'operators = [{user_id = 42, name = "Sam"}]\n')
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(TelegramConfigError) as ctx:
                load_bridge_config(stray)
        self.assertIn("FRAMEWORK_ROOT", str(ctx.exception))


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


class TestInboundReachesTheInbox(HermeticCase):
    def test_a_telegram_message_is_stored_and_delivered_as_chat(self):
        from cousin_lib import telegram
        from cousin_lib.runner.inbox import Inbox
        from tests.runner._home import temp_home
        home = temp_home(self, runner="fake")
        (home / "cousin.toml").write_text(
            '[cousin]\nslug = "wren"\nname = "Wren"\n\n[operator]\nname = "Priya"\n'
            '\n[agent]\nrunner = "fake"\n\n[telegram]\noperators = [42]\n')
        # load_bridge_config refuses this toml (no [telegram] enabled/
        # token_file, no [chat] port): none of those are used by the
        # send path any more, so the dataclass is built directly rather
        # than padding the toml with fields this test does not exercise.
        cfg = telegram.BridgeConfig(slug="wren", token="unused",
                                    operator_ids={42},
                                    operator_name={42: "Priya"}, port=0,
                                    home=home)
        telegram._default_chat_send(cfg, user="Priya", message="from telegram")
        rows = Inbox(home).claim(limit=5)
        self.assertEqual([(r["source"], r["sender"], r["body"]) for r in rows],
                         [("chat", "Priya", "from telegram")])
        import sqlite3
        conn = sqlite3.connect(home / "data" / "chat.db")
        try:
            stored = conn.execute("SELECT user, message FROM messages").fetchall()
        finally:
            conn.close()
        self.assertEqual(stored, [("Priya", "from telegram")])
        self.assertEqual(rows[0]["message_id"], 1)

    def test_a_telegram_photo_is_stored_with_an_attachment_under_chat_images(self):
        from cousin_lib import telegram
        from cousin_lib.runner.inbox import Inbox
        from tests.runner._home import temp_home
        home = temp_home(self, runner="fake")
        (home / "cousin.toml").write_text(
            '[cousin]\nslug = "wren"\nname = "Wren"\n\n[operator]\nname = "Priya"\n'
            '\n[agent]\nrunner = "fake"\n\n[telegram]\noperators = [42]\n')
        cfg = telegram.BridgeConfig(slug="wren", token="unused",
                                    operator_ids={42},
                                    operator_name={42: "Priya"}, port=0,
                                    home=home)
        png = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAf"
              "FcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
        telegram._default_chat_send(cfg, user="Priya", message="[photo]",
                                    attachment=png)
        rows = Inbox(home).claim(limit=5)
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0]["attachments"]), 1)
        staged = pathlib.Path(rows[0]["attachments"][0])
        self.assertTrue(staged.is_file())
        self.assertEqual(staged.parent, home / "chat" / "images")
        import sqlite3
        conn = sqlite3.connect(home / "data" / "chat.db")
        try:
            kind, path = conn.execute(
                "SELECT attachment_kind, attachment_path FROM messages"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(kind, "image")
        self.assertEqual(path, str(staged))


if __name__ == "__main__":
    unittest.main()
