"""cousin-reply behavior: a reply is one row in the cousin's own chat
store, written in this process (chat_api.reply); no chat server runs."""
import contextlib
import json
import pathlib
import sqlite3
import tempfile
import unittest

from cousin_lib.config import CousinConfig, MissingConfigError
from cousin_lib.reply import send_reply


def _rows(home):
    """The messages in <home>/data/chat.db, oldest first, or [] when no
    reply was ever stored (no store created)."""
    path = pathlib.Path(home) / "data" / "chat.db"
    if not path.exists():
        return []
    with contextlib.closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        return [dict(r) for r in db.execute("SELECT * FROM messages ORDER BY id")]


class TestSendReply(unittest.TestCase):
    def _cfg(self, operator=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = pathlib.Path(tmp.name)
        toml = '[cousin]\nslug = "wren"\nname = "Wren"\n'
        if operator:
            toml += '[operator]\nname = "%s"\n' % operator
        (home / "cousin.toml").write_text(toml)
        self.home = home
        return CousinConfig.load(home)

    def test_stores_the_reply_in_its_own_chat_store(self):
        """No [chat] port, no server: the reply still lands."""
        result = send_reply(self._cfg(), "hello there", user="Sam")
        [row] = _rows(self.home)
        self.assertEqual(result["id"], row["id"])
        self.assertEqual((row["message"], row["reply_to_user"], row["user"], row["type"]),
                         ("hello there", "Sam", "Wren", "wren"))

    def test_user_falls_back_to_configured_operator(self):
        send_reply(self._cfg(operator="Sam"), "hi")
        self.assertEqual(_rows(self.home)[0]["reply_to_user"], "Sam")

    def test_no_user_and_no_operator_fails_loud(self):
        # Null-operator profile: no configured operator means no recipient
        # to default to - never a hardcoded human.
        with self.assertRaises(MissingConfigError):
            send_reply(self._cfg(), "hi")
        self.assertEqual(_rows(self.home), [])

    def test_reply_to_is_passed_through(self):
        send_reply(self._cfg(), "hi", user="Sam", reply_to=41)
        self.assertEqual(json.loads(_rows(self.home)[0]["reply_to"]), {"id": 41})

    def test_empty_body_is_refused_before_anything_is_stored(self):
        with self.assertRaises(ValueError):
            send_reply(self._cfg(), "   \n", user="Sam")
        self.assertEqual(_rows(self.home), [])


class TestReplyImage(TestSendReply):
    """cousin-reply --image / --video: the file rides the reply. It is
    copied into <home>/chat/images/ or chat/video/ and the reply row
    names it (attachment kind + path), the convention cousin-image uses
    and both the console and the Telegram bridge read.
    Canary: a cousin posting its own picture is a real workflow, and an
    --image row that carried no attachment would leave the bridge with
    nothing to relay."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = pathlib.Path(tmp.name)
        (self.home / "cousin.toml").write_text(
            '[cousin]\nslug = "wren"\n[operator]\nname = "Operator"\n')

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
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            return reply_main(argv)

    def test_image_is_staged_and_named_on_the_reply(self):
        png = self._png()
        rc = self._main(["--user", "Operator", "-m", "preview", "--image", str(png)])
        self.assertEqual(rc, 0)
        [row] = _rows(self.home)
        self.assertEqual(row["message"], "preview")
        self.assertEqual(row["attachment_kind"], "image")
        staged = pathlib.Path(row["attachment_path"])
        self.assertEqual(staged.parent, self.home / "chat" / "images")
        self.assertEqual(staged.suffix, ".png")
        self.assertEqual(staged.read_bytes(), png.read_bytes())

    def test_staged_name_is_one_the_console_serves(self):
        from cousin_lib.console.proxy import _MEDIA_NAME_RE
        self._main(["-m", "x", "--image", str(self._png("My Render.PNG"))])
        name = pathlib.Path(_rows(self.home)[0]["attachment_path"]).name
        self.assertRegex(name, _MEDIA_NAME_RE)
        self.assertTrue(name.endswith(".png"))

    def test_video_is_staged_under_chat_video(self):
        mp4 = self.home / "clip.mp4"
        mp4.write_bytes(b"\x00\x00\x00\x18ftypmp42")
        rc = self._main(["-m", "the run", "--video", str(mp4)])
        self.assertEqual(rc, 0)
        [row] = _rows(self.home)
        self.assertEqual(row["attachment_kind"], "video")
        self.assertEqual(pathlib.Path(row["attachment_path"]).parent,
                         self.home / "chat" / "video")

    def test_image_and_video_together_are_refused(self):
        mp4 = self.home / "clip.mp4"
        mp4.write_bytes(b"x")
        with self.assertRaises(SystemExit):
            self._main(["-m", "x", "--image", str(self._png()),
                        "--video", str(mp4)])
        self.assertEqual(_rows(self.home), [])

    def test_a_non_video_extension_is_refused_before_anything_is_stored(self):
        rc = self._main(["-m", "x", "--video", str(self._png())])
        self.assertEqual(rc, 2)
        self.assertEqual(_rows(self.home), [])

    def test_no_inbox_copy_is_written_any_more(self):
        self._main(["-m", "x", "--image", str(self._png())])
        self.assertFalse((self.home / "chat" / "inbound").exists())

    def test_a_missing_image_fails_before_anything_is_stored(self):
        rc = self._main(["-m", "x", "--image", str(self.home / "nope.png")])
        self.assertNotEqual(rc, 0)
        self.assertEqual(_rows(self.home), [])

    def test_a_non_image_extension_is_refused_before_anything_is_stored(self):
        bad = self.home / "model.stl"
        bad.write_bytes(b"solid x")
        rc = self._main(["-m", "x", "--image", str(bad)])
        self.assertNotEqual(rc, 0)
        self.assertEqual(_rows(self.home), [])

    def test_image_only_reply_gets_a_default_body(self):
        rc = self._main(["--image", str(self._png("a.jpg"))])
        self.assertEqual(rc, 0)
        [row] = _rows(self.home)
        self.assertTrue(row["message"].strip())
        self.assertTrue(pathlib.Path(row["attachment_path"]).is_file())


class TestReplyRefusedUnstages(unittest.TestCase):
    """A reply the store refuses leaves no staged file behind."""

    def test_a_refused_reply_removes_the_staged_copy(self):
        from unittest import mock
        from cousin_lib.server import chat_api
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = pathlib.Path(tmp.name)
        (home / "cousin.toml").write_text('[cousin]\nslug = "wren"\n')
        png = home / "a.png"
        png.write_bytes(b"\x89PNG")
        with mock.patch.object(chat_api, "reply", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                send_reply(CousinConfig.load(home), "x", user="Sam",
                           attachment=("image", png))
        self.assertEqual(list((home / "chat" / "images").iterdir()), [])


class TestReplyOutboundFilter(TestSendReply):
    """cousin-reply crosses the outbound filter every outbound surface
    crosses (docs/operations.md: exit 3 on a block, nothing sent).
    Canary: before the fix the filter was wired into cousin-chat and
    the media captions only, and a reply naming a protected term went
    straight to the chat server."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        (self.root / "config").mkdir()
        (self.root / "config" / "outbound-filter.json").write_text(
            json.dumps({"terms": ["zorblatt"], "protected": ["kestrel"]}))
        self.home = self.root / "cousins" / "wren"
        self.home.mkdir(parents=True)
        (self.home / "cousin.toml").write_text(
            '[cousin]\nslug = "wren"\n[operator]\nname = "Operator"\n')

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
        self.assertEqual(_rows(self.home), [])

    def test_protected_slug_is_blocked(self):
        rc, _ = self._main(["-m", "kestrel said hi"])
        self.assertEqual(rc, 3)
        self.assertEqual(_rows(self.home), [])

    def test_blocked_reply_with_image_posts_nothing_and_lands_nothing(self):
        png = self.home / "shot.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
        rc, _ = self._main(["-m", "zorblatt preview", "--image", str(png)])
        self.assertEqual(rc, 3)
        self.assertEqual(_rows(self.home), [])
        self.assertFalse((self.home / "chat" / "inbound").exists())
        self.assertFalse((self.home / "chat" / "images").exists())

    def test_image_default_body_is_filtered_too(self):
        png = self.home / "zorblatt.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
        rc, _ = self._main(["--image", str(png)])
        self.assertEqual(rc, 3)
        self.assertEqual(_rows(self.home), [])

    def test_clean_reply_still_posts(self):
        rc, _ = self._main(["-m", "all good here"])
        self.assertEqual(rc, 0)
        self.assertEqual(_rows(self.home)[0]["message"], "all good here")

    def test_root_falls_back_to_the_home_grandparent(self):
        # A shell that exported only COUSIN_HOME must not lose the filter.
        rc, _ = self._main(["-m", "zorblatt"], root_env=False)
        self.assertEqual(rc, 3)
        self.assertEqual(_rows(self.home), [])

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
        self.assertEqual(_rows(self.home), [])


if __name__ == "__main__":
    unittest.main()
