"""The cousin inspector's auth-mode control: GET /api/cousins/<slug>/auth
shows the mode and whether a key is set (its last four characters at
most), POST .../auth/key writes the key file from one paste and never
echoes it, POST .../auth switches the mode (409 mid-turn unless
forced). Every one of them sits behind the console login."""
import json

from cousin_lib import agent_auth
from cousin_lib.console import auth
from tests.console._harness import ConsoleCase

KEY = "kst-console-0123456789abcdQRST"

_HARNESS = """
busy_patterns = ["esc to interrupt"]

[auth.api_key]
key_env = "KESTREL_KEY"
config_dir_env = "KESTREL_CONFIG_DIR"
source_dir = "%s"
exclude = [".credentials.json"]
login_files = [".credentials.json"]
"""


class AuthModeRoutes(ConsoleCase):
    def setUp(self):
        super().setUp()
        src = self.root / "kestrel-src"
        src.mkdir()
        (src / ".credentials.json").write_text("{}")
        (self.root / "config" / "harness.toml").write_text(_HARNESS % src)
        self.home = self.cousin("wren")

    def test_get_shows_the_modes_and_no_key(self):
        self.serve()
        status, body = self.get("/api/cousins/wren/auth")
        self.assertEqual(status, 200)
        self.assertEqual(body["mode"], agent_auth.DEFAULT_MODE)
        self.assertEqual(body["modes"], list(agent_auth.AUTH_MODES))
        self.assertTrue(body["configured"])
        self.assertFalse(body["key"]["set"])

    def test_the_key_goes_in_once_and_never_comes_back(self):
        self.serve()
        status, raw_headers, raw = self.request(
            "POST", "/api/cousins/wren/auth/key", {"key": KEY}, raw=True)
        self.assertEqual(status, 200, raw)
        self.assertNotIn(KEY.encode(), raw)
        body = json.loads(raw)
        self.assertEqual(body["key"], {"set": True, "last4": "QRST",
                                       "error": None})
        self.assertEqual(
            agent_auth.read_key(self.home, "KESTREL_KEY"), KEY)
        for path in ("/api/cousins/wren/auth", "/api/cousins"):
            _s, _h, raw = self.request("GET", path, raw=True)
            self.assertNotIn(KEY.encode(), raw, path)
            self.assertNotIn(KEY[:-4].encode(), raw, path)

    def test_a_bad_key_is_400_and_does_not_echo(self):
        self.serve()
        status, body = self.post("/api/cousins/wren/auth/key",
                                 {"key": "two words"})
        self.assertEqual(status, 400)
        self.assertNotIn("two words", json.dumps(body))

    def test_switch_on_a_stopped_cousin_and_the_row_shows_it(self):
        self.serve()
        self.post("/api/cousins/wren/auth/key", {"key": KEY})
        status, body = self.post("/api/cousins/wren/auth",
                                 {"mode": agent_auth.MODE_API_KEY})
        self.assertEqual(status, 200, body)
        self.assertFalse(body["restarted"])
        row = self.get("/api/cousins")[1]["cousins"][0]
        self.assertEqual(row["auth"], agent_auth.MODE_API_KEY)
        status, body = self.post("/api/cousins/wren/auth", {"mode": "x"})
        self.assertEqual(status, 400)

    def test_switch_without_a_key_is_400_and_changes_nothing(self):
        self.serve()
        status, body = self.post("/api/cousins/wren/auth",
                                 {"mode": agent_auth.MODE_API_KEY})
        self.assertEqual(status, 400)
        self.assertIn("no key file", body["error"])
        self.assertEqual(agent_auth.read_mode(self.home),
                         agent_auth.DEFAULT_MODE)

    def test_a_mid_turn_cousin_is_409(self):
        self.serve()
        self.post("/api/cousins/wren/auth/key", {"key": KEY})
        self.tmux_running()
        self.pane.write_text("* Thinking... (3s . esc to interrupt)\n")
        status, body = self.post("/api/cousins/wren/auth",
                                 {"mode": agent_auth.MODE_API_KEY})
        self.assertEqual(status, 409)
        self.assertTrue(body["busy"])
        self.assertNotIn("kill-session", self.tmux_log.read_text())


class AuthModeRoutesNeedLogin(ConsoleCase):
    def test_all_three_are_401_without_a_session(self):
        auth.Users(self.root / "config" / "console-users.json") \
            .set_password("ana", "correct horse")
        self.cousin("wren")
        self.serve()
        self.assertEqual(self.get("/api/cousins/wren/auth")[0], 401)
        self.assertEqual(self.post("/api/cousins/wren/auth/key",
                                   {"key": KEY})[0], 401)
        self.assertEqual(self.post("/api/cousins/wren/auth",
                                   {"mode": "claude"})[0], 401)
        self.assertFalse(agent_auth.key_file(
            self.root / "cousins" / "wren").exists())
