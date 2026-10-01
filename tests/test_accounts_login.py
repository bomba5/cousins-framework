"""cousin-account login and token: the flows, the chat relay, the one-shot code capture."""
import contextlib
import io
import json
import os
import pathlib
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
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

    def test_the_last_words_never_keep_a_piece_of_the_code(self):
        tail = CODE[10:] + " " * 700 + "stuck at " + CODE + " end"   # the tail began inside it
        words = accounts._last_words(mock.Mock(text=lambda: tail), CODE)
        self.assertNotIn(CODE[10:], words); self.assertIn("stuck at [code] end", words)

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

    def test_a_stall_after_the_token_showed_never_reports_the_token(self):
        fake = FakePty([TOKEN_SCREENS[0],
                        "\nYour OAuth token (valid for 1 year):\nsk-ant-oat01-FAKETOKEN"])
        out = accounts.token_flow(self.acc["nightly"], self.root, relay=self.relayed.append,
                                  await_code=lambda t: CODE, spawn=fake)
        self.assertFalse(out["ok"])
        self.assertNotIn("FAKETOKEN", json.dumps(out))
        self.assertFalse((self.root / ".secrets" / "accounts" / "nightly").exists())

    def test_a_token_cut_by_the_tail_leaves_none_of_its_characters_in_the_reason(self):
        body = "oat01-" + "B" * 95                   # the tail began INSIDE the token
        for tail in (body + " " * 700 + "Store this",
                     "sk-ant-" + body + " " * 700 + "Store this"):
            fake = FakePty([TOKEN_SCREENS[0], "\n" + tail])
            out = accounts.token_flow(self.acc["nightly"], self.root,
                                      relay=self.relayed.append,
                                      await_code=lambda t: CODE, spawn=fake)
            self.assertFalse(out["ok"])
            reason = json.dumps(out)
            self.assertNotIn("BBBB", reason); self.assertNotIn("oat01", reason)
            self.assertIn("not shown", reason)


class TestCapture(LoginCase):
    def test_arm_store_take_is_one_shot_private_and_outside_every_home(self):
        self.arm()
        path = accounts.capture_path(self.root, "fleet")
        self.assertEqual(path.parent, self.root / "run")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        threading.Timer(0.1, accounts.store_code, args=(self.root, "fleet", CODE)).start()
        self.assertEqual(accounts.take_code(self.root, "fleet", timeout=5, poll=0.02), CODE)
        left = accounts.read_capture(self.root, "fleet")
        self.assertEqual(left["state"], "done"); self.assertNotIn("code", left)
        self.assertFalse(accounts.store_code(self.root, "fleet", "again"))   # one code, once
        self.assertNotIn("code", accounts.read_capture(self.root, "fleet"))
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_a_non_object_capture_file_is_skipped_never_raised(self):
        # Tracker #84: captures_for runs on every operator message
        # (server/inbound.py divert_login_code); a capture file that
        # holds valid JSON that is not an object (here, a bare list)
        # must never take the whole check down with an AttributeError.
        self.arm()
        (self.root / "run" / "login-capture-broken.json").write_text("[]")
        caps = accounts.captures_for(self.root, "wren")
        self.assertEqual(len(caps), 1)
        self.assertEqual(caps[0]["account"], "fleet")

    def test_read_capture_of_a_non_object_json_file_is_none_not_a_raise(self):
        # Same shape, the sibling reader: read_capture is what
        # store_code/take_code/retire_capture and divert_login_code's
        # re-check all call.
        (self.root / "run").mkdir(parents=True, exist_ok=True)
        (self.root / "run" / "login-capture-broken.json").write_text("[]")
        self.assertIsNone(accounts.read_capture(self.root, "broken"))

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
        rid = accounts.relay_notice(self.home, operator="Priya", account_name="fleet", url=URL,
                                    timeout=300)
        store = ChatStore(self.home / "data" / "chat.db")
        try:
            row = store.conn.execute("SELECT user, message, reply_to_user FROM messages WHERE id=?",
                                     (rid,)).fetchone()
        finally:
            store.close()
        self.assertEqual(row[0], "cousin-account")
        self.assertIn(URL, row[1]); self.assertIn("code#state", row[1])
        self.assertIn("5 minutes", row[1]); self.assertNotIn("10 minutes", row[1])
        # only a code-shaped message is taken, never "your next message" whatever it says
        self.assertIn("paste the code as the next message; only the code is taken", row[1])
        self.assertNotIn("is taken as that code", row[1])
        self.assertIn("If you did not start this login from a host shell yourself, do not"
                      " answer this.", row[1])
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
        for _ in range(2):                           # every late code, not only the first
            late = divert_login_code(self.config(), "Priya", CODE)
            self.assertIn("late login code for account fleet was discarded", late)
        self.assertNotIn("code", accounts.read_capture(self.root, "fleet"))

    def test_a_duplicate_paste_after_a_take_is_diverted(self):
        from cousin_lib.server.inbound import divert_login_code
        self.arm()
        self.assertIsNotNone(divert_login_code(self.config(), "Priya", CODE))
        self.assertEqual(accounts.take_code(self.root, "fleet", timeout=1, poll=0.01), CODE)
        again = divert_login_code(self.config(), "Priya", CODE)     # "did it work?" + a re-paste
        self.assertIn("second login code for account fleet was discarded", again)
        self.assertIsNone(divert_login_code(self.config(), "Priya", "did it work?"))
        self.assertNotIn("code", accounts.read_capture(self.root, "fleet"))

    def test_an_ordinary_message_passes_through_while_a_capture_is_armed(self):
        from cousin_lib.server.inbound import divert_login_code
        self.arm()
        self.assertIsNone(divert_login_code(self.config(), "Priya", "ok, doing it now"))
        self.assertNotIn("code", accounts.read_capture(self.root, "fleet"))
        self.assertEqual(accounts.capture_window(accounts.read_capture(self.root, "fleet"),
                                                 time.time()), "armed")
        self.assertIsNotNone(divert_login_code(self.config(), "Priya", CODE))

    def arm_nightly(self):
        return accounts.arm_capture(self.root, via="wren", operator="Priya",
                                    account_name="nightly", ttl=60)

    def test_another_accounts_done_tombstone_never_swallows_the_next_code(self):
        from cousin_lib.server.inbound import divert_login_code
        self.arm()                                   # "fleet" sorts before "nightly"
        divert_login_code(self.config(), "Priya", CODE)
        self.assertEqual(accounts.take_code(self.root, "fleet", timeout=1, poll=0.01), CODE)
        self.arm_nightly()
        other = "zyxwvutsrqponmlkjihgfe#dcba98765"
        self.assertEqual(divert_login_code(self.config(), "Priya", other),
                         "[login code received for account nightly]")
        self.assertEqual(accounts.read_capture(self.root, "nightly")["code"], other)

    def test_another_accounts_late_tombstone_never_swallows_the_next_code(self):
        from cousin_lib.server.inbound import divert_login_code
        self.arm(ttl=-1)                             # fleet's window closed: a tombstone
        self.arm_nightly()
        self.assertEqual(divert_login_code(self.config(), "Priya", CODE),
                         "[login code received for account nightly]")
        self.assertEqual(accounts.read_capture(self.root, "nightly")["code"], CODE)
        self.assertNotIn("code", accounts.read_capture(self.root, "fleet"))

    def test_a_tombstone_past_its_hour_is_removed(self):
        from cousin_lib.server.inbound import divert_login_code
        self.arm()
        self.assertIsNone(accounts.take_code(self.root, "fleet", timeout=0.05, poll=0.01))
        later = accounts.read_capture(self.root, "fleet")["until"] + 1
        with mock.patch.object(accounts.time, "time", return_value=later):
            self.assertIsNone(divert_login_code(self.config(), "Priya", CODE))
        self.assertIsNone(accounts.read_capture(self.root, "fleet"))

    def dead_pid(self):
        proc = subprocess.Popen([sys.executable, "-c", "pass"]); proc.wait()
        return proc.pid

    def test_a_capture_whose_flow_died_is_a_tombstone_and_drops_its_code(self):
        from cousin_lib.server.inbound import divert_login_code
        self.arm()
        self.assertIsNotNone(divert_login_code(self.config(), "Priya", CODE))  # stored
        cap = accounts.read_capture(self.root, "fleet")
        cap["pid"] = self.dead_pid()                 # the flow was killed with the code waiting
        accounts._write_private(accounts.capture_path(self.root, "fleet"), cap)
        self.assertEqual(accounts.capture_window(cap, time.time()), "late")
        self.assertIsNone(divert_login_code(self.config(), "Priya", "hello again"))  # not swallowed
        self.assertNotIn("code", accounts.read_capture(self.root, "fleet"))
        self.assertIsNotNone(divert_login_code(self.config(), "Priya", CODE))  # still diverted

    def test_a_stored_code_expires_with_its_window(self):
        from cousin_lib.server.inbound import divert_login_code
        self.arm(ttl=60)
        self.assertIsNotNone(divert_login_code(self.config(), "Priya", CODE))
        cap = accounts.read_capture(self.root, "fleet")
        self.assertEqual(accounts.capture_window(cap, time.time()), "taken")
        later = time.time() + 61
        self.assertEqual(accounts.capture_window(cap, later), "late")
        with mock.patch.object(accounts.time, "time", return_value=later):
            self.assertIsNone(divert_login_code(self.config(), "Priya", "hello again"))
        self.assertNotIn("code", accounts.read_capture(self.root, "fleet"))

    def test_a_chat_send_answers_ok_stores_the_redaction_and_delivers_nothing(self):
        from cousin_lib.config import CousinConfig
        from cousin_lib.server import chat_api
        calls = []
        self.arm()
        body = chat_api.send(CousinConfig.load(self.home), {"user": "Priya", "message": CODE},
                             deliver=lambda **kw: calls.append(kw))
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
    def main(self, *argv, tty=True, ancestry=None):
        err = io.StringIO()
        stdin = mock.Mock(isatty=lambda: tty)
        with mock.patch.object(sys, "stdin", stdin), contextlib.redirect_stderr(err), \
                mock.patch.object(accounts, "_inside_cousin_ancestry", return_value=ancestry):
            rc = accounts.account_main([*argv, "--root", str(self.root)])
        return rc, err.getvalue()

    def test_login_refuses_under_a_cousin_ancestor_and_names_it(self):
        rc, err = self.main("login", "fleet", "--via", "wren", ancestry=4242)
        self.assertEqual(rc, 2); self.assertIn("operator-run", err); self.assertIn("4242", err)

    def test_the_ancestry_walk_reads_each_ancestors_environment(self):
        parents = {400: 300, 300: 200, 200: 1}
        envs = {400: [b"PATH=/bin"], 300: OSError("denied"),
                200: [b"PATH=/bin", b"COUSIN_HOME=/somewhere/cousins/wren"]}

        def environ_of(pid):
            if isinstance(envs[pid], Exception):
                raise envs[pid]
            return envs[pid]
        with mock.patch.object(os, "getppid", return_value=400):
            self.assertEqual(accounts._inside_cousin_ancestry(
                parent_of=parents.__getitem__, environ_of=environ_of), 200)
            envs[200] = [b"PATH=/bin"]
            self.assertIsNone(accounts._inside_cousin_ancestry(
                parent_of=parents.__getitem__, environ_of=environ_of))

            def no_proc(pid):
                raise FileNotFoundError("/proc")
            self.assertIsNone(accounts._inside_cousin_ancestry(parent_of=no_proc,
                                                               environ_of=no_proc))

    def test_a_timeout_that_is_not_positive_is_refused(self):
        for t in ("0", "-5"):
            rc, err = self.main("login", "fleet", "--timeout", t)
            self.assertEqual(rc, 2); self.assertIn("timeout", err)

    def test_a_relay_that_raises_is_exit_4_with_the_reason(self):
        def flow(account, root, *, relay, await_code, timeout):
            relay(URL)
        with mock.patch.object(accounts, "login_flow", side_effect=flow), \
                mock.patch.object(accounts, "relay_notice", side_effect=OSError("disk full")):
            rc, err = self.main("login", "fleet", "--via", "wren")
        self.assertEqual(rc, 4); self.assertIn("disk full", err)
        self.assertIsNone(accounts.read_capture(self.root, "fleet"))

    def test_a_sigterm_while_waiting_leaves_a_tombstone(self):
        class NotInstalled(Exception):
            pass

        def not_installed(signum, frame):
            raise NotInstalled()
        old = signal.signal(signal.SIGTERM, not_installed)   # never the default: never a kill
        self.addCleanup(signal.signal, signal.SIGTERM, old)

        def flow(account, root, *, relay, await_code, timeout):
            relay(URL)
            threading.Timer(0.2, os.kill, args=(os.getpid(), signal.SIGTERM)).start()
            return {"ok": bool(await_code(5))}
        with mock.patch.object(accounts, "login_flow", side_effect=flow):
            with self.assertRaises(SystemExit):
                self.main("login", "fleet", "--via", "wren")
        self.assertEqual(accounts.read_capture(self.root, "fleet")["state"], "tombstone")
        self.assertIs(signal.getsignal(signal.SIGTERM), not_installed)   # restored

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
        self.assertNotIn("code", accounts.read_capture(self.root, "fleet"))   # taken
        texts = self.chat_texts()
        self.assertEqual(len(texts), 1); self.assertIn(URL, texts[0])
        self.assertNotIn(CODE, "".join(texts) + err)

    def login_file(self, home, account):
        from cousin_lib.runner import auth
        auth.write_login_required(home, host="h", account=account, kind="claude-login",
                                  reason=auth.LOGIN, detail="Not logged in", action="log in")
        return home / auth.LOGIN_FILE

    def test_a_good_login_via_a_cousin_wakes_it_when_its_file_names_the_account(self):
        testa = self.root / "cousins" / "testa"; (testa / "data").mkdir(parents=True)
        mine, other = self.login_file(self.home, "fleet"), self.login_file(testa, "fleet")

        def flow(account, root, *, relay, await_code, timeout):
            self.assertTrue(mine.exists())               # cleared only after the login
            return {"ok": True}
        with mock.patch.object(accounts, "login_flow", side_effect=flow):
            rc, err = self.main("login", "fleet", "--via", "wren")
        self.assertEqual(rc, 0, err)
        self.assertFalse(mine.exists())                  # the runner's manual retry
        self.assertTrue(other.exists())                  # another cousin is not the via
        self.assertIn("wren", err)

    def test_login_via_leaves_a_file_for_another_account_or_after_a_failed_login(self):
        path = self.login_file(self.home, "nightly")
        with mock.patch.object(accounts, "login_flow", return_value={"ok": True}):
            self.assertEqual(self.main("login", "fleet", "--via", "wren")[0], 0)
        self.assertTrue(path.exists())                   # names another account
        path = self.login_file(self.home, "fleet")
        with mock.patch.object(accounts, "login_flow",
                               return_value={"ok": False, "reason": "not logged in"}):
            self.assertEqual(self.main("login", "fleet", "--via", "wren")[0], 4)
        self.assertTrue(path.exists())                   # no login, no retry
        with mock.patch.object(accounts, "login_flow", return_value={"ok": True}):
            self.assertEqual(self.main("login", "fleet")[0], 0)
        self.assertTrue(path.exists())                   # no --via, no cousin named

    def test_a_good_token_via_a_cousin_wakes_it_too(self):
        path = self.login_file(self.home, "nightly")
        with mock.patch.object(accounts, "token_flow", return_value={"ok": True}):
            rc, err = self.main("token", "nightly", "--via", "wren")
        self.assertEqual(rc, 0, err)
        self.assertFalse(path.exists())

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
