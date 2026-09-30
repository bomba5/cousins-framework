"""Telegram provisioning: the write-only token, operators, refused
senders offered for adding, and the bridge's readiness."""
import json
import os
import pathlib
import stat
import tempfile
import tomllib
import unittest

from cousin_lib import telegram_admin as ta

TOKEN = "123456789:" + "A" * 35


class AdminCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.home = self.root / "cousins" / "wren"
        (self.home / "data").mkdir(parents=True)
        (self.home / "cousin.toml").write_text(
            '[cousin]\nslug = "wren"\nname = "Wren"\n\n'
            '[chat]\nport = 8100\n\n[memory]\nscope = "private"\n')

    def toml(self):
        return tomllib.loads((self.home / "cousin.toml").read_text())


class TestToken(AdminCase):
    def test_written_0600_referenced_never_returned(self):
        ta.set_token(self.home, self.root, "wren", TOKEN)
        path = self.root / "config" / "telegram" / "wren.token"
        self.assertEqual(path.read_text(), TOKEN)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(self.toml()["telegram"]["token_file"],
                         "config/telegram/wren.token")
        self.assertEqual(self.toml()["memory"]["scope"], "private")
        status = ta.status(self.home, self.root)
        self.assertTrue(status["token_set"])
        self.assertNotIn(TOKEN, json.dumps(status))

    def test_malformed_token_refused(self):
        with self.assertRaises(ta.TelegramAdminError):
            ta.set_token(self.home, self.root, "wren", "not a token")

    def test_check_never_echoes_the_token(self):
        ta.set_token(self.home, self.root, "wren", TOKEN)

        def boom(token):
            raise RuntimeError("bad url https://x/bot%s/getMe" % token)
        out = ta.check_token(self.home, self.root, call=boom)
        self.assertFalse(out["ok"])
        self.assertNotIn(TOKEN, out["error"])
        out = ta.check_token(self.home, self.root,
                             call=lambda t: {"username": "WrenBot"})
        self.assertEqual(out, {"ok": True, "bot": "WrenBot"})


class TestOperatorsAndPending(AdminCase):
    def test_operators_round_trip_and_validate(self):
        ta.set_operators(self.home, [{"user_id": 42, "name": "Ana"}])
        self.assertEqual(self.toml()["telegram"]["operators"],
                         [{"user_id": 42, "name": "Ana"}])
        for bad in ([{"user_id": "x"}], [{"user_id": 1}, {"user_id": 1}],
                    [{"user_id": 1, "name": "a\nb"}]):
            with self.assertRaises(ta.TelegramAdminError):
                ta.set_operators(self.home, bad)

    def test_refused_senders_are_offered_until_added(self):
        for i in range(7):
            ta.note_refused(self.home, {"id": 100 + i, "username": "u%d" % i})
        rows = ta.pending(self.home)
        self.assertEqual([r["user_id"] for r in rows],
                         [102, 103, 104, 105, 106])
        ta.set_operators(self.home, [{"user_id": 106, "name": "Six"}])
        self.assertNotIn(106, [r["user_id"] for r in ta.pending(self.home)])


class TestDiscover(AdminCase):
    def test_first_time_start_press_is_offered_without_a_bridge(self):
        ta.set_token(self.home, self.root, "wren", TOKEN)
        updates = [{"update_id": 1, "message": {"from": {
            "id": 42, "username": "ana", "first_name": "Ana"}}},
            {"update_id": 2, "message": {"from": {"id": 7}}}]
        self.assertEqual(ta.discover(self.home, self.root,
                                     call=lambda t: updates), 2)
        self.assertEqual([r["user_id"] for r in ta.pending(self.home)],
                         [42, 7])
        ta.set_operators(self.home, [{"user_id": 42, "name": "Ana"}])
        self.assertEqual(ta.discover(self.home, self.root,
                                     call=lambda t: updates), 1)

    def test_no_token_or_a_live_bridge_means_no_look(self):
        self.assertIsNone(ta.discover(self.home, self.root,
                                      call=lambda t: 1 / 0))


class TestReady(AdminCase):
    def test_reasons_in_order_then_ready(self):
        self.assertEqual(ta.ready(self.home, self.root), "disabled")
        ta.set_enabled(self.home, True)
        self.assertEqual(ta.ready(self.home, self.root), "no token set")
        ta.set_token(self.home, self.root, "wren", TOKEN)
        self.assertIn("no operators", ta.ready(self.home, self.root))
        ta.set_operators(self.home, [{"user_id": 42, "name": "Ana"}])
        self.assertIsNone(ta.ready(self.home, self.root))

    def test_the_legacy_launcher_is_gone(self):
        # a bridge runs only as a cousin-supervisor child (R10); the
        # console still stops one left running outside any supervisor
        self.assertFalse(hasattr(ta, "start_bridge"))
        self.assertEqual(ta.stop_bridge(self.home), "not running")


if __name__ == "__main__":
    unittest.main()
