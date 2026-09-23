"""cousin-account login and token: the flows, the chat relay, the one-shot code capture."""
import contextlib
import io
import json
import os
import pathlib
import stat
import sys
import tempfile
import threading
import unittest
import urllib.request
from unittest import mock

from cousin_lib import accounts, pty_driver
from tests._hermetic import HermeticCase

URL = "https://claude.com/cai/oauth/authorize?code=true&client_id=c&state=s"
CODE = "abcdefghijklmnopqrstuv#wxyz012345"          # the page's code#state shape


class FakePty:
    """The scripted pty double: `screens[0]` is on screen at spawn; every
    write appends the next screen. read_until never waits."""
    instances = []

    def __init__(self, screens):
        self.screens, self.out, self.written, self.closed = list(screens), "", [], False
        self.out += self.screens.pop(0)

    def __call__(self, argv, env, **kw):
        self.argv, self.env = argv, env
        FakePty.instances.append(self)
        return self

    def read_until(self, pattern, timeout):
        import re
        m = re.search(pattern, self.out)
        if not m:
            raise pty_driver.PtyTimeout(pattern)
        return m

    def write(self, text):
        self.written.append(text)
        if self.screens:
            self.out += self.screens.pop(0)

    def text(self):
        return self.out

    def close(self, timeout=5):
        self.closed = True
        return 0


LOGIN_SCREENS = ["If the browser didn't open, visit: %s\nPaste code here if prompted > " % URL,
                 "\nLogin successful.\n"]
INVALID_SCREENS = [LOGIN_SCREENS[0],
                   "\nInvalid code. Please make sure the full code was copied\n"]
TOKEN_SCREENS = ["Browser didn't open? Use the url below to sign in (c to copy)\n%s\n"
                 "Paste code here if prompted > " % URL,
                 "\nLong-lived authentication token created successfully!\n"
                 "Your OAuth token (valid for 1 year):\nsk-ant-oat01-FAKETOKEN\n"
                 "Store this token securely. You won't be able to see it again.\n"]


class LoginCase(HermeticCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        (self.root / "config").mkdir()
        (self.root / "config" / "accounts.toml").write_text(
            '[accounts.fleet]\nkind = "claude-login"\n\n'
            '[accounts.nightly]\nkind = "claude-token"\n')
        self.acc = accounts.load(self.root)
        self.home = self.root / "cousins" / "wren"; (self.home / "data").mkdir(parents=True)
        (self.home / "cousin.toml").write_text('[cousin]\nslug = "wren"\nname = "Wren"\n\n'
                                               '[chat]\nport = 0\n\n'
                                               '[operator]\nname = "Priya"\n')
        self.relayed = []

    def status(self, payload):
        return mock.patch.object(accounts, "status", return_value=payload)

    def arm(self, ttl=60):
        return accounts.arm_capture(self.root, via="wren", operator="Priya",
                                    account_name="fleet", ttl=ttl)

    def config(self):
        from cousin_lib.config import CousinConfig
        return CousinConfig.load(self.home)

    def chat_texts(self):
        from cousin_lib.server.storage import ChatStore
        store = ChatStore(self.home / "data" / "chat.db")
        try:
            return [r[0] for r in store.conn.execute("SELECT message FROM messages ORDER BY id")]
        finally:
            store.close()


class TestLoginFlow(LoginCase):
    def test_url_relayed_code_pasted_status_read(self):
        fake = FakePty(LOGIN_SCREENS)
        with self.status({"loggedIn": True, "authMethod": "claude.ai"}):
            out = accounts.login_flow(self.acc["fleet"], self.root, relay=self.relayed.append,
                                      await_code=lambda t: CODE, spawn=fake)
        self.assertTrue(out["ok"], out)
        self.assertEqual(self.relayed, [URL])
        self.assertEqual(fake.written, [CODE + "\r"])
        self.assertEqual(fake.argv[-3:], ["auth", "login", "--claudeai"])
        self.assertTrue(os.path.isabs(fake.argv[0]))          # never a bare "claude"
        self.assertEqual(fake.env["CLAUDE_CONFIG_DIR"], str(self.root / "data" / "accounts" / "fleet"))
        for var in ("ANTHROPIC_API_KEY", "DISPLAY", "WAYLAND_DISPLAY"):
            self.assertNotIn(var, fake.env)
        self.assertTrue(fake.closed)

    def test_no_code_in_time_is_a_clean_failure(self):
        fake = FakePty(LOGIN_SCREENS)
        out = accounts.login_flow(self.acc["fleet"], self.root, relay=self.relayed.append,
                                  await_code=lambda t: None, spawn=fake)
        self.assertFalse(out["ok"]); self.assertIn("no code", out["reason"])
        self.assertEqual(fake.written, []); self.assertTrue(fake.closed)

    def test_the_status_decides_not_the_screen(self):
        fake = FakePty(LOGIN_SCREENS)
        with self.status({"loggedIn": False, "authMethod": "none"}):
            out = accounts.login_flow(self.acc["fleet"], self.root, relay=self.relayed.append,
                                      await_code=lambda t: CODE, spawn=fake)
        self.assertFalse(out["ok"])                  # "Login successful." on screen is not enough

    def test_an_invalid_code_is_reported_in_the_clis_own_words(self):
        fake = FakePty(INVALID_SCREENS)
        with mock.patch.object(accounts, "status") as st:
            out = accounts.login_flow(self.acc["fleet"], self.root, relay=self.relayed.append,
                                      await_code=lambda t: CODE, spawn=fake)
        self.assertFalse(out["ok"])
        self.assertIn("Invalid code. Please make sure the full code was copied", out["reason"])
        st.assert_not_called()                       # a re-login's old credentials must not pass it

    def test_a_stall_reports_the_clis_last_words_with_the_code_masked(self):
        fake = FakePty([LOGIN_SCREENS[0], "\nsomething the plan never saw\n"])
        out = accounts.login_flow(self.acc["fleet"], self.root, relay=self.relayed.append,
                                  await_code=lambda t: CODE, spawn=fake)
        self.assertFalse(out["ok"])
        self.assertIn("something the plan never saw", out["reason"])

    def test_the_wrong_kind_is_refused(self):
        with self.assertRaises(accounts.AccountsError):
            accounts.login_flow(self.acc["nightly"], self.root, relay=print,
                                await_code=lambda t: CODE, spawn=FakePty(LOGIN_SCREENS))


class TestTokenFlow(LoginCase):
    def test_the_token_is_verified_then_saved_0600_and_never_returned(self):
        fake = FakePty(TOKEN_SCREENS)
        with self.status({"loggedIn": True, "authMethod": "oauth_token"}) as st:
            out = accounts.token_flow(self.acc["nightly"], self.root, relay=self.relayed.append,
                                      await_code=lambda t: CODE, spawn=fake)
        self.assertTrue(out["ok"], out)
        path = self.root / ".secrets" / "accounts" / "nightly"
        self.assertEqual(path.read_text().strip(), "sk-ant-oat01-FAKETOKEN")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertNotIn("sk-ant-oat01-FAKETOKEN", json.dumps(out))
        probed = st.call_args[0][0]                  # the account the status ran under
        self.assertEqual(probed.secret_value, "sk-ant-oat01-FAKETOKEN")

    def test_an_unverified_token_is_not_saved(self):
        for payload in ({"loggedIn": False}, {"loggedIn": True, "authMethod": "claude.ai"}):
            fake = FakePty(TOKEN_SCREENS)
            with self.status(payload):
                out = accounts.token_flow(self.acc["nightly"], self.root,
                                          relay=self.relayed.append,
                                          await_code=lambda t: CODE, spawn=fake)
            self.assertFalse(out["ok"], payload)
            self.assertFalse((self.root / ".secrets" / "accounts" / "nightly").exists())

    def test_an_invalid_code_ends_the_token_flow_in_the_clis_words(self):
        fake = FakePty([TOKEN_SCREENS[0], "\nInvalid code. Please make sure the full code was copied\n"])
        out = accounts.token_flow(self.acc["nightly"], self.root, relay=self.relayed.append,
                                  await_code=lambda t: CODE, spawn=fake)
        self.assertFalse(out["ok"])
        self.assertIn("Invalid code", out["reason"])


class TestCapture(LoginCase):
    def test_arm_store_take_is_one_shot_private_and_outside_every_home(self):
        self.arm()
        path = accounts.capture_path(self.root, "fleet")
        self.assertEqual(path.parent, self.root / "run")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        threading.Timer(0.1, accounts.store_code, args=(self.root, "fleet", CODE)).start()
        self.assertEqual(accounts.take_code(self.root, "fleet", timeout=5, poll=0.02), CODE)
        self.assertFalse(path.exists())
        self.assertFalse(accounts.store_code(self.root, "fleet", "again"))   # never recreated
        self.assertFalse(path.exists())

    def test_a_timeout_leaves_a_tombstone_that_stores_nothing(self):
        self.arm()
        self.assertIsNone(accounts.take_code(self.root, "fleet", timeout=0.1, poll=0.02))
        cap = accounts.read_capture(self.root, "fleet")
        self.assertEqual(cap["state"], "tombstone")
        self.assertFalse(accounts.store_code(self.root, "fleet", CODE))
        self.assertNotIn("code", accounts.read_capture(self.root, "fleet"))

    def test_a_store_racing_a_take_never_leaves_a_code_on_disk(self):
        for i in range(25):
            self.arm()
            t = threading.Thread(target=accounts.store_code, args=(self.root, "fleet", "c-%d" % i))
            t.start()
            got = accounts.take_code(self.root, "fleet", timeout=0.05, poll=0.001)
            t.join()
            left = accounts.read_capture(self.root, "fleet")
            self.assertTrue(got == "c-%d" % i or (left is not None and "code" not in left),
                            (i, got, left))

    def test_the_relay_notice_is_a_chat_row_for_the_operator_that_names_the_shape(self):
        from cousin_lib.server.storage import ChatStore
        rid = accounts.relay_notice(self.home, operator="Priya", account_name="fleet", url=URL)
        store = ChatStore(self.home / "data" / "chat.db")
        try:
            row = store.conn.execute("SELECT user, message, reply_to_user FROM messages WHERE id=?",
                                     (rid,)).fetchone()
        finally:
            store.close()
        self.assertEqual(row[0], "cousin-account")
        self.assertIn(URL, row[1]); self.assertIn("code#state", row[1])
        self.assertEqual(row[2], "Priya")


class TestDivert(LoginCase):
    def test_the_operators_next_message_is_diverted_and_redacted(self):
        from cousin_lib.server.inbound import divert_login_code
        self.arm()
        self.assertIsNone(divert_login_code(self.config(), "Sam", "not the operator"))
        redacted = divert_login_code(self.config(), "Priya", "  %s  " % CODE)
        self.assertEqual(redacted, "[login code received for account fleet]")
        self.assertEqual(accounts.read_capture(self.root, "fleet")["code"], CODE)
        self.assertIsNone(divert_login_code(self.config(), "Priya", "a second message"))

    def test_a_late_code_is_diverted_and_discarded_other_messages_pass(self):
        from cousin_lib.server.inbound import divert_login_code
        self.arm(ttl=-1)                             # the window already closed
        self.assertIsNone(divert_login_code(self.config(), "Priya", "hello, how is it going"))
        late = divert_login_code(self.config(), "Priya", CODE)
        self.assertIn("late login code for account fleet was discarded", late)
        self.assertIsNone(accounts.read_capture(self.root, "fleet"))
        self.assertIsNone(divert_login_code(self.config(), "Priya", CODE))  # one late code only

    def test_api_send_answers_ok_stores_the_redaction_and_delivers_nothing(self):
        from cousin_lib.config import CousinConfig
        from cousin_lib.server.app import ChatServer
        calls = []
        server = ChatServer(CousinConfig.load(self.home), deliver=lambda **kw: calls.append(kw))
        server.start(); self.addCleanup(server.stop)
        self.arm()
        req = urllib.request.Request("http://127.0.0.1:%d/api/send" % server.port,
                                     data=json.dumps({"user": "Priya", "message": CODE}).encode())
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read())
        self.assertEqual((body["ok"], body["diverted"]), (True, True))
        self.assertEqual(calls, [])
        self.assertEqual(self.chat_texts(), ["[login code received for account fleet]"])
        self.assertEqual(accounts.read_capture(self.root, "fleet")["code"], CODE)

    def test_telegram_stores_the_redaction_and_delivers_nothing(self):
        from cousin_lib import telegram
        self.arm()
        cfg = mock.Mock(home=self.home, slug="wren")
        with mock.patch("cousin_lib.delivery.deliver") as deliver:
            telegram._store_and_deliver(cfg, user="Priya", message=CODE)
        deliver.assert_not_called()
        self.assertEqual(self.chat_texts(), ["[login code received for account fleet]"])


class TestOperatorOnly(LoginCase):
    def main(self, *argv, tty=True):
        err = io.StringIO()
        stdin = mock.Mock(isatty=lambda: tty)
        with mock.patch.object(sys, "stdin", stdin), contextlib.redirect_stderr(err):
            rc = accounts.account_main([*argv, "--root", str(self.root)])
        return rc, err.getvalue()

    def test_login_refuses_inside_a_cousin(self):
        with mock.patch.dict(os.environ, {"COUSIN_HOME": str(self.home)}):
            rc, err = self.main("login", "fleet", "--via", "wren")
        self.assertEqual(rc, 2); self.assertIn("operator-run", err)

    def test_login_and_token_want_a_terminal(self):
        for cmd, name in (("login", "fleet"), ("token", "nightly")):
            rc, err = self.main(cmd, name, "--via", "wren", tty=False)
            self.assertEqual(rc, 2); self.assertIn("terminal", err)

    def test_a_missing_via_home_or_a_via_with_no_operator_is_refused_upfront(self):
        with mock.patch.object(accounts, "login_flow") as flow:
            rc, err = self.main("login", "fleet", "--via", "nobody")
            self.assertEqual(rc, 2); self.assertIn("nobody", err)
            (self.home / "cousin.toml").write_text('[cousin]\nslug = "wren"\nname = "Wren"\n')
            rc, err = self.main("login", "fleet", "--via", "wren")
            self.assertEqual(rc, 2); self.assertIn("operator", err)
        flow.assert_not_called()
        self.assertFalse((self.root / "run").exists())   # nothing armed

    def test_login_host_warns_that_it_relogs_the_hosts_own_login(self):
        with mock.patch.object(accounts, "login_flow", return_value={"ok": True}):
            rc, err = self.main("login", "host")
        self.assertEqual(rc, 0); self.assertIn("~/.claude", err)

    def test_via_relays_the_url_and_takes_the_diverted_code(self):
        got = {}

        def flow(account, root, *, relay, await_code, timeout):
            relay(URL)
            threading.Timer(0.1, accounts.store_code, args=(self.root, "fleet", CODE)).start()
            got["code"] = await_code(5)
            return {"ok": True}
        with mock.patch.object(accounts, "login_flow", side_effect=flow):
            rc, err = self.main("login", "fleet", "--via", "wren")
        self.assertEqual(rc, 0, err)
        self.assertEqual(got["code"], CODE)
        self.assertIsNone(accounts.read_capture(self.root, "fleet"))   # taken: gone
        texts = self.chat_texts()
        self.assertEqual(len(texts), 1); self.assertIn(URL, texts[0])
        self.assertNotIn(CODE, "".join(texts) + err)

    def test_a_relay_that_fails_leaves_nothing_armed(self):
        def flow(account, root, *, relay, await_code, timeout):
            with self.assertRaises(RuntimeError):
                relay(URL)
            return {"ok": False, "reason": "no relay"}
        with mock.patch.object(accounts, "login_flow", side_effect=flow), \
                mock.patch.object(accounts, "relay_notice", side_effect=RuntimeError("chat.db")):
            rc, err = self.main("login", "fleet", "--via", "wren")
        self.assertEqual(rc, 4)
        self.assertIsNone(accounts.read_capture(self.root, "fleet"))

    def test_no_registry_carries_it(self):
        from cousin_lib import mcp_server
        from cousin_lib.runner import tools
        repo = pathlib.Path(__file__).resolve().parents[1]
        reg = mcp_server.parse_registry(mcp_server.shipped_default_registry(repo), "t")
        names = {d["name"] for d in tools.tool_definitions(reg)}
        self.assertFalse({"account", "cousin-account", "login"} & names)


if __name__ == "__main__":
    unittest.main()
