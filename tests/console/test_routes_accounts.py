"""The console's accounts routes (WP-C): the list and the per-row status
(no model call), the validated accounts.toml writer, the write-only keys,
the claude-login / claude-token flows through the relay and await_code
hooks (the URL to the operator, the code to its own write-only route, one
code used once), the opencode OAuth method, and check-auth / validate as
a cousin's long operation. Fakes only: no real login, no model call."""
import json
import os
import pathlib
import stat
import sys
import threading
import time
from unittest import mock

from cousin_lib import accounts
from cousin_lib.console import longop, routes_accounts
from tests.console._harness import ConsoleCase
from tests.test_accounts_login import CODE, LOGIN_SCREENS, TOKEN_SCREENS, URL, FakePty

TOML = """[accounts.fleet]
kind = "claude-login"

[accounts.nightly]
kind = "claude-token"

[accounts.metered]
kind = "anthropic-key"

[accounts.keyed]
kind = "opencode"
providers = ["openai", "mistral"]

[accounts.local]
kind = "opencode"
endpoint = "http://127.0.0.1:11434/v1"
endpoint_model = "qwen3-coder"
"""

KEY = "sk-ant-api03-FAKEKEYVALUE0123456789"
OC_URL = "https://auth.example.test/codex/device"
GO = ("\n┌  Add credential\n│\n●  Go to: %s\n│\n●  Enter code: WXYZ-1234\n"
      "│\n◒  Waiting for authorization..." % OC_URL)
DONE = "◇  Login successful\n│\n└  Done\n"
HEADLESS = "ChatGPT Pro/Plus (headless)"


class GatedPty(FakePty):
    """FakePty whose wait for `gate_on` blocks until the test opens the
    gate (a CLI still waiting on the operator), until close(), or until
    its stand-in CLI process (`proc`, a real child whose pid a cancel
    signals) has ended, which ends the read as a real pty's EOF does."""

    def __init__(self, screens, gate_on, proc=None):
        super().__init__(screens)
        self.gate_on, self.gate, self.proc = gate_on, threading.Event(), proc
        if proc is not None:
            self.pid = proc.pid

    def read_until(self, pattern, timeout):
        if pattern == self.gate_on:
            deadline = time.monotonic() + 10
            while not self.gate.is_set() and time.monotonic() < deadline:
                if self.proc is not None and self.proc.poll() is not None:
                    from cousin_lib import pty_driver
                    raise pty_driver.PtyTimeout("the process ended before %r" % pattern)
                self.gate.wait(0.02)
        return super().read_until(pattern, timeout)

    def close(self, timeout=5):
        self.gate.set()
        return super().close(timeout)


class AccountsCase(ConsoleCase):
    def setUp(self):
        super().setUp()
        (self.root / "config" / "accounts.toml").write_text(TOML)
        # the console runs as the operator: never inside a cousin (the
        # test process itself may descend from one)
        for target, value in ((accounts, "_inside_cousin_ancestry"),):
            patcher = mock.patch.object(target, value, return_value=None)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(accounts, "_cli", return_value="/opt/fake/claude")
        patcher.start()
        self.addCleanup(patcher.stop)
        # a console with a user: the credential routes want a logged-in one
        from cousin_lib.console import auth
        users = auth.Users(self.root / "config" / "console-users.json")
        users.set_password("ana", "correct horse")
        users.set_password("bo", "battery staple")
        self.serve()
        self.login("ana")
        self.events = []
        self.server.listeners.append(lambda kind, data: self.events.append((kind, data)))

    def login(self, user):
        password = {"ana": "correct horse", "bo": "battery staple"}[user]
        status, body = self.post("/api/auth/login", {"user": user, "password": password})
        self.assertEqual(status, 200, body)

    def as_other(self, method, path, payload=None):
        """The same request from another console session (user bo)."""
        import http.cookiejar
        import urllib.request
        mine = self.opener
        if not hasattr(self, "_other"):
            self._other = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
            self.opener = self._other
            try:
                self.login("bo")
            finally:
                self.opener = mine
        self.opener = self._other
        try:
            return self.request(method, path, payload)
        finally:
            self.opener = mine

    def audit_rows(self):
        path = routes_accounts.audit_path(self.root)
        return [json.loads(ln) for ln in path.read_text().splitlines()] if path.exists() else []

    def accounts_text(self):
        return (self.root / "config" / "accounts.toml").read_text()

    def status(self, payload):
        return mock.patch.object(accounts, "status", return_value=payload)

    def wait_op(self, key, timeout=5.0, *, running=False):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            op = longop.status(self.server, key)
            if op and (op["status"] == "running") == running:
                return op
            time.sleep(0.02)
        self.fail("the op on %s did not get there" % key)

    def wait_flow(self, name, test, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status, body = self.get("/api/accounts/%s/flow" % name)
            if status == 200 and test(body):
                return body
            time.sleep(0.02)
        self.fail("the flow of %s did not get there: %r" % (name, body))

    def runner_cousin(self, slug, runner="sdk", account=None):
        extra = '\n[agent]\nrunner = "%s"\n' % runner
        if account:
            extra += 'account = "%s"\n' % account
        if runner == "opencode":
            extra += 'model = "openai/gpt-5"\n'
        return self.cousin(slug, extra=extra)

    def everything_served(self):
        """Every body and event the console produced, as one string."""
        return json.dumps(self.events, default=str)


class List(AccountsCase):
    def test_lists_host_and_every_entry_never_a_secret(self):
        self.runner_cousin("wren", account="fleet")
        self.post("/api/accounts/metered/key", {"key": KEY})
        status, body = self.get("/api/accounts")
        self.assertEqual(status, 200)
        rows = {r["name"]: r for r in body["accounts"]}
        self.assertEqual(list(rows), ["host", "fleet", "keyed", "local", "metered", "nightly"])
        self.assertEqual(rows["fleet"]["kind"], "claude-login")
        self.assertEqual(rows["fleet"]["where"], "data/accounts/fleet")
        self.assertEqual(rows["fleet"]["cousins"], ["wren"])
        self.assertEqual(rows["fleet"]["lanes"], ["sdk", "fake", "tmux"])
        self.assertEqual(rows["keyed"]["lanes"], ["opencode"])
        self.assertEqual(rows["keyed"]["entry"]["providers"], ["openai", "mistral"])
        self.assertEqual(rows["metered"]["where"], ".secrets/accounts/metered")
        self.assertEqual(rows["metered"]["secret"], {"set": True, "last4": KEY[-4:], "error": None})
        self.assertEqual(rows["nightly"]["secret"]["set"], False)
        self.assertTrue(rows["host"]["implicit"])
        self.assertIn("claude-login", body["fields"])
        self.assertNotIn(KEY, json.dumps(body))
        self.assertNotIn(KEY[:-4], json.dumps(body))

    def test_a_broken_file_lists_host_and_says_why(self):
        (self.root / "config" / "accounts.toml").write_text('[accounts.x]\nkind = "nope"\n')
        status, body = self.get("/api/accounts")
        self.assertEqual(status, 200)
        self.assertEqual([r["name"] for r in body["accounts"]], ["host"])
        self.assertIn("kind", body["error"])


class Status(AccountsCase):
    def test_a_claude_account_reads_the_cli_status_no_model_call(self):
        with self.status({"loggedIn": True, "authMethod": "claude.ai",
                          "email": "someone@example.test"}) as st:
            status, body = self.get("/api/accounts/fleet/status")
        self.assertEqual(status, 200)
        st.assert_called_once()
        self.assertEqual((body["loggedIn"], body["method"], body["ok"]), (True, "claude.ai", True))
        self.assertNotIn("someone@example.test", json.dumps(body))

    def test_logged_out_carries_the_action_line(self):
        with self.status({"loggedIn": False, "authMethod": "none"}):
            status, body = self.get("/api/accounts/nightly/status")
        self.assertFalse(body["ok"])
        self.assertIn("cousin-account token nightly", body["action"])

    def test_an_opencode_account_reads_its_auth_json(self):
        status, body = self.get("/api/accounts/keyed/status")
        self.assertEqual(status, 200)
        self.assertEqual((body["ok"], body["missing"]), (False, ["openai", "mistral"]))

    def test_opencodes_own_hosted_provider_needs_no_key(self):
        with open(self.root / "config" / "accounts.toml", "a") as fh:
            fh.write('\n[accounts.zen]\nkind = "opencode"\nproviders = ["opencode"]\n')
        status, body = self.get("/api/accounts/zen/status")
        self.assertEqual(status, 200, body)
        self.assertEqual((body["ok"], body["missing"], body["providers"], body["action"]),
                         (True, [], ["opencode"], None))

    def test_unknown_and_bad_names(self):
        self.assertEqual(self.get("/api/accounts/ghost/status")[0], 404)
        self.assertEqual(self.get("/api/accounts/Bad..Name/status")[0], 400)


class Write(AccountsCase):
    def test_add(self):
        status, body = self.post("/api/accounts", {"name": "spare", "entry": {"kind": "claude-login"}})
        self.assertEqual(status, 201, body)
        self.assertEqual(accounts.load(self.root)["spare"].kind, "claude-login")
        self.assertTrue(self.accounts_text().startswith(TOML))
        self.assertTrue(any(k == "accounts-change" for k, _ in self.events))

    def test_add_refusals_write_nothing(self):
        for payload, code in (({"name": "fleet", "entry": {"kind": "claude-login"}}, 409),
                              ({"name": "host", "entry": {"kind": "claude-login"}}, 400),
                              ({"name": "x", "entry": {"kind": "claude-token",
                                                       "secret_file": "/etc/passwd"}}, 400),
                              ({"name": "x", "entry": {"kind": "opencode",
                                                       "providers": ["anthropic"]}}, 400),
                              ({"name": "x"}, 400)):
            with self.subTest(payload=payload):
                status, body = self.post("/api/accounts", payload)
                self.assertEqual(status, code, body)
                self.assertEqual(self.accounts_text(), TOML)

    def test_edit_replaces_the_entry(self):
        status, body = self.post("/api/accounts/keyed", {"entry": {"kind": "opencode",
                                                                   "providers": ["openai"]}})
        self.assertEqual(status, 200, body)
        self.assertEqual(accounts.load(self.root)["keyed"].providers, ("openai",))
        self.assertEqual(self.post("/api/accounts/ghost", {"entry": {"kind": "claude-login"}})[0],
                         404)

    def test_an_edit_that_breaks_a_cousins_lane_is_refused(self):
        self.runner_cousin("wren", account="fleet")
        status, body = self.post("/api/accounts/fleet", {"entry": {"kind": "opencode",
                                                                   "providers": ["openai"]}})
        self.assertEqual(status, 400)
        self.assertIn("wren", body["error"])
        self.assertEqual(self.accounts_text(), TOML)

    def test_remove_needs_the_typed_name_and_no_cousin_on_it(self):
        self.runner_cousin("wren", account="fleet")
        self.assertEqual(self.post("/api/accounts/nightly/remove", {})[0], 400)
        self.assertEqual(self.post("/api/accounts/nightly/remove", {"confirm": "fleet"})[0], 400)
        status, body = self.post("/api/accounts/fleet/remove", {"confirm": "fleet"})
        self.assertEqual(status, 409)
        self.assertIn("wren", body["error"])
        status, body = self.post("/api/accounts/nightly/remove", {"confirm": "nightly"})
        self.assertEqual(status, 200, body)
        self.assertNotIn("nightly", accounts.load(self.root))

    def test_writes_are_post_only(self):
        self.assertEqual(self.request("DELETE", "/api/accounts/nightly")[0], 405)


class Keys(AccountsCase):
    def test_a_key_account_takes_its_key_write_only(self):
        status, body = self.post("/api/accounts/metered/key", {"key": KEY})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["secret"], {"set": True, "last4": KEY[-4:], "error": None})
        path = self.root / ".secrets" / "accounts" / "metered"
        self.assertEqual(path.read_text(), KEY + "\n")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.root / ".secrets").stat().st_mode), 0o700)
        self.assertNotIn(KEY, json.dumps(body))
        self.assertNotIn(KEY, self.everything_served())

    def test_a_token_account_takes_a_pasted_token(self):
        status, body = self.post("/api/accounts/nightly/key", {"key": "sk-ant-oat01-" + "x" * 30})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["secret"]["set"])

    def test_an_opencode_provider_key_goes_to_auth_json(self):
        status, body = self.post("/api/accounts/keyed/key", {"provider": "openai",
                                                             "key": "sk-openai-0123456789abcdef"})
        self.assertEqual(status, 200, body)
        auth = self.root / ".secrets" / "accounts" / "keyed.opencode" / "data" / "opencode" / "auth.json"
        self.assertEqual(json.loads(auth.read_text())["openai"],
                         {"type": "api", "key": "sk-openai-0123456789abcdef"})
        self.assertNotIn("sk-openai-0123456789abcdef", json.dumps(body))
        self.assertEqual(body["status"]["missing"], ["mistral"])

    def test_refusals(self):
        cases = (
            ("keyed", {"provider": "anthropic", "key": "sk-ant-x0123456789"}, "Claude"),
            ("keyed", {"provider": "claude", "key": "sk-x0123456789"}, None),
            ("keyed", {"provider": "groq", "key": "sk-x0123456789"}, "does not name"),
            ("keyed", {"key": "sk-x0123456789"}, None),
            ("local", {"provider": "openai", "key": "sk-x0123456789"}, "endpoint"),
            ("fleet", {"key": "sk-x0123456789"}, "log in"),
            ("metered", {"key": "two words"}, None),
            ("metered", {"key": 12}, None),
            ("host", {"key": "sk-x0123456789"}, None),
        )
        for name, payload, words in cases:
            with self.subTest(name=name, payload=payload):
                status, body = self.post("/api/accounts/%s/key" % name, payload)
                self.assertEqual(status, 400, body)
                if words:
                    self.assertIn(words, body["error"])
                self.assertNotIn(str(payload.get("key")), json.dumps(body))
        self.assertFalse((self.root / ".secrets" / "accounts" / "metered").exists())

    def test_a_secret_file_outside_secrets_is_left_to_a_hand_write(self):
        (self.root / "config" / "accounts.toml").write_text(
            TOML + '\n[accounts.odd]\nkind = "anthropic-key"\nsecret_file = "data/odd.key"\n')
        status, body = self.post("/api/accounts/odd/key", {"key": KEY})
        self.assertEqual(status, 400)
        self.assertIn(".secrets", body["error"])
        self.assertFalse((self.root / "data" / "odd.key").exists())

    def test_never_from_inside_a_cousin(self):
        with mock.patch.dict(os.environ, {"COUSIN_HOME": str(self.root / "cousins" / "x")}):
            status, body = self.post("/api/accounts/metered/key", {"key": KEY})
        self.assertEqual(status, 403)
        with mock.patch.object(accounts, "_inside_cousin_ancestry", return_value=4242):
            self.assertEqual(self.post("/api/accounts/metered/key", {"key": KEY})[0], 403)
        self.assertFalse((self.root / ".secrets" / "accounts" / "metered").exists())


class ClaudeFlows(AccountsCase):
    def start_login(self, name="fleet", screens=LOGIN_SCREENS, route="login", body=None):
        self.fake = FakePty(screens)
        self.server.state["accounts.pty"] = self.fake
        return self.post("/api/accounts/%s/%s" % (name, route), body or {})

    def test_the_url_goes_to_the_operator_and_the_code_to_its_route_once(self):
        with self.status({"loggedIn": True, "authMethod": "claude.ai"}):
            status, body = self.start_login()
            self.assertEqual(status, 202, body)
            self.assertEqual(body["op"]["kind"], "login")
            flow = self.wait_flow("fleet", lambda b: b.get("awaiting_code"))
            self.assertEqual(flow["url"], URL)
            status, body = self.post("/api/accounts/fleet/code", {"code": CODE})
            self.assertEqual(status, 200, body)
            op = self.wait_op("account:fleet")
        self.assertEqual(op["status"], "done", op)
        self.assertEqual(self.fake.written, [CODE + "\r"])
        # one code, once: a second (or a late) one is never delivered
        status, body = self.post("/api/accounts/fleet/code", {"code": CODE})
        self.assertEqual(status, 409)
        self.assertEqual(self.fake.written, [CODE + "\r"])
        _, flow = self.get("/api/accounts/fleet/flow")
        self.assertIsNone(flow["url"])                    # spent: no longer shown
        self.assertNotIn(CODE, self.everything_served())
        self.assertNotIn(CODE, json.dumps(flow))
        self.assertNotIn(CODE.split("#")[0], json.dumps(op))

    def test_a_code_that_is_not_code_state_is_refused_and_the_window_stays(self):
        with self.status({"loggedIn": True, "authMethod": "claude.ai"}):
            self.start_login()
            self.wait_flow("fleet", lambda b: b.get("awaiting_code"))
            status, body = self.post("/api/accounts/fleet/code", {"code": "abcdefghijklmnop"})
            self.assertEqual(status, 400)
            self.assertNotIn("abcdefghijklmnop", json.dumps(body))
            self.assertEqual(self.post("/api/accounts/fleet/code", {"code": CODE})[0], 200)
            self.assertEqual(self.wait_op("account:fleet")["status"], "done")

    def test_no_flow_no_code(self):
        self.assertEqual(self.post("/api/accounts/fleet/code", {"code": CODE})[0], 409)

    def test_cancel(self):
        self.start_login()
        self.wait_flow("fleet", lambda b: b.get("awaiting_code"))
        self.assertEqual(self.post("/api/accounts/fleet/cancel")[0], 200)
        op = self.wait_op("account:fleet")
        self.assertEqual(op["status"], "failed")
        self.assertIn("cancelled", op["error"])
        self.assertEqual(self.fake.written, [])
        self.assertEqual(self.post("/api/accounts/fleet/code", {"code": CODE})[0], 409)

    def test_one_flow_per_account(self):
        self.start_login()
        self.wait_flow("fleet", lambda b: b.get("awaiting_code"))
        self.assertEqual(self.start_login()[0], 409)
        self.post("/api/accounts/fleet/cancel")
        self.wait_op("account:fleet")

    def test_the_wrong_kind_and_the_host(self):
        self.assertEqual(self.post("/api/accounts/nightly/login")[0], 400)
        self.assertEqual(self.post("/api/accounts/fleet/token")[0], 400)
        self.assertEqual(self.post("/api/accounts/metered/login")[0], 400)
        status, body = self.post("/api/accounts/host/login")
        self.assertEqual(status, 400)
        self.assertIn("confirm_host", body["error"])

    def test_never_from_inside_a_cousin(self):
        with mock.patch.dict(os.environ, {"COUSIN_SLUG": "wren"}):
            self.assertEqual(self.start_login()[0], 403)

    def test_the_token_is_saved_and_never_served(self):
        with self.status({"loggedIn": True, "authMethod": "oauth_token"}):
            status, body = self.start_login("nightly", TOKEN_SCREENS, "token")
            self.assertEqual(status, 202, body)
            self.wait_flow("nightly", lambda b: b.get("awaiting_code"))
            self.post("/api/accounts/nightly/code", {"code": CODE})
            op = self.wait_op("account:nightly")
        self.assertEqual(op["status"], "done", op)
        path = self.root / ".secrets" / "accounts" / "nightly"
        self.assertEqual(path.read_text(), "sk-ant-oat01-FAKETOKEN\n")
        self.assertNotIn("FAKETOKEN", self.everything_served())
        self.assertNotIn("FAKETOKEN", json.dumps(op))
        self.assertNotIn("FAKETOKEN", json.dumps(self.get("/api/accounts/nightly/flow")[1]))

    def test_a_failed_login_is_the_ops_error(self):
        with self.status({"loggedIn": False, "authMethod": "none"}):
            self.start_login()
            self.wait_flow("fleet", lambda b: b.get("awaiting_code"))
            self.post("/api/accounts/fleet/code", {"code": CODE})
            op = self.wait_op("account:fleet")
        self.assertEqual(op["status"], "failed")
        self.assertIn("logged out", op["error"])


class OpencodeOAuth(AccountsCase):
    def auth_path(self):
        return self.root / ".secrets" / "accounts" / "keyed.opencode" / "data" / "opencode" / "auth.json"

    def test_the_url_and_instruction_line_are_shown_and_auth_json_decides(self):
        fake = GatedPty([GO + DONE], accounts.OC_DONE_RX)
        self.server.state["accounts.pty"] = fake
        self.server.state["accounts.opencode_bin"] = "/opt/fake/opencode"
        status, body = self.post("/api/accounts/keyed/login", {"provider": "openai",
                                                               "method": HEADLESS})
        self.assertEqual(status, 202, body)
        self.assertEqual(body["op"]["kind"], "opencode-login")
        flow = self.wait_flow("keyed", lambda b: b.get("url"))
        self.assertEqual(flow["url"], OC_URL)
        self.assertIn("WXYZ-1234", flow["instructions"])
        self.assertFalse(flow["awaiting_code"])             # nothing is pasted back
        self.assertEqual(fake.argv[1:], ["auth", "login", "--pure", "--provider", "openai",
                                         "--method", HEADLESS])
        # what opencode does once the operator signs in
        path = self.auth_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"openai": {"type": "oauth", "refresh": "r", "access": "a",
                                               "expires": 1}}))
        os.chmod(path, 0o600)
        fake.gate.set()
        op = self.wait_op("account:keyed")
        self.assertEqual(op["status"], "done", op)
        self.assertEqual(op["result"]["status"]["missing"], ["mistral"])

    def test_claude_is_refused_before_anything_runs(self):
        self.server.state["accounts.pty"] = FakePty([GO + DONE])
        for payload in ({"provider": "anthropic", "method": "Claude Pro/Max"},
                        {"provider": "openai", "method": "Claude via proxy"},
                        {"provider": "openai"}):
            with self.subTest(payload=payload):
                self.assertEqual(self.post("/api/accounts/keyed/login", payload)[0], 400)
        self.assertIsNone(longop.status(self.server, "account:keyed"))

    def test_cancel_ends_the_wait(self):
        import subprocess
        proc = subprocess.Popen(["sleep", "30"])
        self.addCleanup(lambda: (proc.poll() is None and proc.kill(), proc.wait()))
        fake = GatedPty([GO + DONE], accounts.OC_DONE_RX, proc=proc)
        self.server.state["accounts.pty"] = fake
        self.server.state["accounts.opencode_bin"] = "/opt/fake/opencode"
        self.post("/api/accounts/keyed/login", {"provider": "openai", "method": HEADLESS})
        self.wait_flow("keyed", lambda b: b.get("url"))
        self.post("/api/accounts/keyed/cancel")
        op = self.wait_op("account:keyed")
        self.assertEqual(op["status"], "failed")
        self.assertIn("cancelled", op["error"])
        self.assertEqual(proc.wait(5), -15)                 # the CLI got its SIGTERM


FAKE_CHECK = r'''
import os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_CHECK_LOG"], "a") as fh:
    fh.write(" ".join(args) + " ANTHROPIC_API_KEY=%s\n"
             % ("set" if "ANTHROPIC_API_KEY" in os.environ else "unset"))
rc = int(os.environ.get("FAKE_CHECK_RC", "0"))
if rc == 2:
    print("cousin-runner: config/accounts.toml is broken", file=sys.stderr)
    sys.exit(2)
print("account=fleet kind=claude-login loggedIn=%s method=claude.ai%s"
      % (rc == 0, "" if rc == 0 else " -> run `cousin-account login fleet`"))
if "--validate" in args and rc == 0:
    vrc = int(os.environ.get("FAKE_VALIDATE_RC", "0"))
    print("validate: ok" if vrc == 0 else "validate: rate_limit: the API said no")
    sys.exit(vrc)
sys.exit(rc)
'''


class CheckAuth(AccountsCase):
    def setUp(self):
        super().setUp()
        script = self.root / "fake_check.py"
        script.write_text(FAKE_CHECK)
        self.log = self.root / "check.log"
        self.server.state["accounts.check_auth_command"] = [sys.executable, str(script)]
        patcher = mock.patch.dict(os.environ, {"FAKE_CHECK_LOG": str(self.log),
                                               "ANTHROPIC_API_KEY": "sk-ant-from-the-shell"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.runner_cousin("wren", account="fleet")

    def test_check_auth_is_a_long_op_with_its_line(self):
        status, body = self.post("/api/cousins/wren/check-auth", {})
        self.assertEqual(status, 202, body)
        self.assertEqual(body["op"]["kind"], "check-auth")
        op = self.wait_op("wren")
        self.assertEqual(op["status"], "done", op)
        self.assertEqual([s["name"] for s in op["stages"]], ["status"])
        self.assertIn("loggedIn=True", op["stages"][0]["detail"])
        self.assertIn("--home %s --check-auth" % (self.root / "cousins" / "wren"),
                      self.log.read_text())

    def test_validate_spends_one_turn_and_says_so(self):
        status, body = self.post("/api/cousins/wren/check-auth", {"validate": True})
        self.assertEqual(status, 202, body)
        self.assertEqual(body["op"]["kind"], "validate")
        op = self.wait_op("wren")
        self.assertEqual(op["status"], "done", op)
        self.assertEqual([s["name"] for s in op["stages"]], ["status", "one model turn"])
        self.assertEqual(op["stages"][1]["detail"], "validate: ok")
        # one child: the status is checked once, not again before the turn
        self.assertEqual(len(self.log.read_text().splitlines()), 1)
        self.assertIn("--check-auth --validate", self.log.read_text())

    def test_logged_out_fails_with_the_action_and_skips_the_turn(self):
        with mock.patch.dict(os.environ, {"FAKE_CHECK_RC": "4"}):
            self.post("/api/cousins/wren/check-auth", {"validate": True})
            op = self.wait_op("wren")
        self.assertEqual(op["status"], "failed")
        self.assertIn("cousin-account login fleet", op["error"])
        self.assertEqual(op["stages"][1]["status"], "skipped")

    def test_a_failed_turn_is_the_ops_error(self):
        with mock.patch.dict(os.environ, {"FAKE_VALIDATE_RC": "4"}):
            self.post("/api/cousins/wren/check-auth", {"validate": True})
            op = self.wait_op("wren")
        self.assertEqual(op["status"], "failed")
        self.assertIn("rate_limit", op["error"])

    def test_the_child_never_inherits_an_auth_variable(self):
        self.post("/api/cousins/wren/check-auth", {})
        self.assertEqual(self.wait_op("wren")["status"], "done")
        self.assertIn("ANTHROPIC_API_KEY=unset", self.log.read_text())

    def test_a_configuration_error_is_the_runners_own_words(self):
        with mock.patch.dict(os.environ, {"FAKE_CHECK_RC": "2"}):
            self.post("/api/cousins/wren/check-auth", {})
            op = self.wait_op("wren")
        self.assertEqual(op["status"], "failed")
        self.assertIn("accounts.toml is broken", op["error"])

    def test_refusals(self):
        self.cousin("owl")                                       # tmux-legacy
        self.assertEqual(self.post("/api/cousins/owl/check-auth", {})[0], 400)
        self.runner_cousin("kit", runner="opencode", account="keyed")
        status, body = self.post("/api/cousins/kit/check-auth", {"validate": True})
        self.assertEqual(status, 400)
        self.assertIn("opencode", body["error"])
        self.assertEqual(self.post("/api/cousins/ghost/check-auth", {})[0], 404)
        self.assertEqual(self.post("/api/cousins/wren/check-auth", {"validate": "yes"})[0], 400)


class NoConsoleUsers(ConsoleCase):
    """A console with no users file is open, but not for credentials."""

    def setUp(self):
        super().setUp()
        (self.root / "config" / "accounts.toml").write_text(TOML)
        self.serve()

    def test_credential_routes_want_a_logged_in_user(self):
        for path, payload in (("/api/accounts/metered/key", {"key": KEY}),
                              ("/api/accounts/fleet/login", {}),
                              ("/api/accounts/nightly/token", {}),
                              ("/api/accounts/fleet/code", {"code": CODE}),
                              ("/api/accounts/fleet/cancel", {}),
                              ("/api/accounts", {"name": "x", "entry": {"kind": "claude-login"}}),
                              ("/api/accounts/fleet", {"entry": {"kind": "claude-login"}}),
                              ("/api/accounts/fleet/remove", {"confirm": "fleet"})):
            with self.subTest(path=path):
                status, body = self.post(path, payload)
                self.assertEqual(status, 403, body)
                self.assertIn("cousin-console adduser", body["error"])
        self.assertEqual((self.root / "config" / "accounts.toml").read_text(), TOML)
        self.assertEqual(self.get("/api/accounts")[0], 200)          # reading stays open


class Round1(AccountsCase):
    def start_login(self, name="fleet", screens=LOGIN_SCREENS, route="login"):
        self.fake = FakePty(screens)
        self.server.state["accounts.pty"] = self.fake
        return self.post("/api/accounts/%s/%s" % (name, route), {})

    def test_a_flow_belongs_to_the_session_that_started_it(self):
        self.start_login()
        mine = self.wait_flow("fleet", lambda b: b.get("awaiting_code"))
        self.assertTrue(mine["mine"])
        status, theirs = self.as_other("GET", "/api/accounts/fleet/flow")
        self.assertEqual(status, 200)
        self.assertEqual((theirs["mine"], theirs["url"], theirs["instructions"],
                          theirs["awaiting_code"]), (False, None, None, False))
        self.assertEqual(self.as_other("POST", "/api/accounts/fleet/code", {"code": CODE})[0], 403)
        self.assertEqual(self.as_other("POST", "/api/accounts/fleet/cancel", {})[0], 403)
        self.assertEqual(self.fake.written, [])
        # the op's events never carry the URL
        ops = [d for k, d in self.events if k == longop.EVENT]
        self.assertTrue(ops)
        self.assertNotIn("oauth/authorize", json.dumps(ops))
        self.assertEqual(self.post("/api/accounts/fleet/cancel")[0], 200)
        self.wait_op("account:fleet")

    def test_the_pty_session_and_the_code_are_dropped_when_it_ends(self):
        with self.status({"loggedIn": True, "authMethod": "oauth_token"}):
            self.start_login("nightly", TOKEN_SCREENS, "token")
            self.wait_flow("nightly", lambda b: b.get("awaiting_code"))
            flow = routes_accounts._flows(self.server)["nightly"]
            self.assertIsNotNone(flow.session)
            self.post("/api/accounts/nightly/code", {"code": CODE})
            self.assertEqual(self.wait_op("account:nightly")["status"], "done")
        self.assertIsNone(flow.session)
        self.assertIsNone(flow._code)
        self.assertTrue(self.fake.closed)

    def test_a_cancel_never_signals_a_closed_session(self):
        import subprocess
        proc = subprocess.Popen(["sleep", "30"])
        self.addCleanup(lambda: (proc.poll() is None and proc.kill(), proc.wait()))

        class Pid(FakePty):
            pid = proc.pid
        flow = routes_accounts.Flow("fleet", "login")
        self.server.state["accounts.pty"] = Pid(LOGIN_SCREENS)
        spawn = routes_accounts._spawn_for(self.server, flow)
        session = spawn(["/opt/fake/claude"], {})
        self.assertEqual(session.pid, proc.pid)
        session.close()
        flow.cancel()
        time.sleep(0.2)
        self.assertIsNone(proc.poll())                    # not signalled: the session was closed

    def test_key_edit_and_remove_wait_for_a_running_login(self):
        self.start_login()
        self.wait_flow("fleet", lambda b: b.get("awaiting_code"))
        self.assertEqual(self.post("/api/accounts/fleet", {"entry": {"kind": "claude-login"}})[0],
                         409)
        self.assertEqual(self.post("/api/accounts/fleet/remove", {"confirm": "fleet"})[0], 409)
        self.post("/api/accounts/fleet/cancel")
        self.wait_op("account:fleet")
        (self.root / "config" / "accounts.toml").write_text(TOML)
        self.server.state["accounts.pty"] = FakePty(TOKEN_SCREENS)
        self.post("/api/accounts/nightly/token", {})
        self.wait_flow("nightly", lambda b: b.get("awaiting_code"))
        self.assertEqual(self.post("/api/accounts/nightly/key", {"key": KEY})[0], 409)
        self.post("/api/accounts/nightly/cancel")
        self.wait_op("account:nightly")

    def test_the_console_writer_keeps_paths_where_the_framework_puts_them(self):
        for entry in ({"kind": "claude-token", "secret_file": "data/x.key"},
                      {"kind": "opencode", "providers": ["openai"], "data_dir": "data/oc"},
                      {"kind": "claude-login", "config_dir": ".secrets/x"}):
            with self.subTest(entry=entry):
                status, body = self.post("/api/accounts", {"name": "x", "entry": entry})
                self.assertEqual(status, 400, body)
        self.assertEqual(self.accounts_text(), TOML)
        for entry in ({"kind": "claude-token", "secret_file": ".secrets/tokens/x"},
                      {"kind": "claude-login", "config_dir": "data/accounts/other"}):
            status, body = self.post("/api/accounts", {"name": "x", "entry": entry})
            self.assertEqual(status, 201, body)
            self.post("/api/accounts/x/remove", {"confirm": "x"})

    def test_a_file_outside_secrets_never_shows_its_tail(self):
        (self.root / "config" / "accounts.toml").write_text(
            TOML + '\n[accounts.odd]\nkind = "anthropic-key"\nsecret_file = "data/keys/odd"\n')
        d = self.root / "data" / "keys"
        d.mkdir(parents=True)
        os.chmod(d, 0o700)
        (d / "odd").write_text(KEY + "\n")
        os.chmod(d / "odd", 0o600)
        rows = {r["name"]: r for r in self.get("/api/accounts")[1]["accounts"]}
        self.assertEqual(rows["odd"]["secret"]["set"], True)
        self.assertIsNone(rows["odd"]["secret"]["last4"])

    def test_an_endpoint_with_a_secret_in_its_query_is_refused(self):
        status, body = self.post("/api/accounts", {"name": "x", "entry": {
            "kind": "opencode", "endpoint": "http://127.0.0.1:1/v1?api_key=hunter2hunter2",
            "endpoint_model": "m"}})
        self.assertEqual(status, 400)
        self.assertNotIn("hunter2", json.dumps(body))

    def test_an_existing_secrets_dir_is_tightened(self):
        (self.root / ".secrets").mkdir(mode=0o755)
        os.chmod(self.root / ".secrets", 0o755)
        self.assertEqual(self.post("/api/accounts/metered/key", {"key": KEY})[0], 200)
        self.assertEqual(stat.S_IMODE((self.root / ".secrets").stat().st_mode), 0o700)

    def test_one_status_check_per_account_at_a_time(self):
        gate, entered = threading.Event(), threading.Event()
        self.addCleanup(gate.set)

        def slow(account, root):
            entered.set()
            gate.wait(5)
            return {"loggedIn": True, "authMethod": "claude.ai"}
        results = []
        with mock.patch.object(accounts, "status", side_effect=slow):
            t = threading.Thread(target=lambda: results.append(
                self.get("/api/accounts/fleet/status")[0]))
            t.start()
            self.assertTrue(entered.wait(5))
            self.assertEqual(self.get("/api/accounts/fleet/status")[0], 409)
            gate.set()
            t.join(5)
        self.assertEqual(results, [200])

    def test_who_started_a_login_and_wrote_a_key_is_audited_never_the_value(self):
        self.post("/api/accounts/metered/key", {"key": KEY})
        self.start_login()
        self.wait_flow("fleet", lambda b: b.get("awaiting_code"))
        self.post("/api/accounts/fleet/cancel")
        self.wait_op("account:fleet")
        rows = self.audit_rows()
        self.assertEqual([(r["kind"], r["actor"], r["account"]) for r in rows],
                         [("key", "ana", "metered"), ("login-start", "ana", "fleet"),
                          ("login-cancel", "ana", "fleet")])
        text = routes_accounts.audit_path(self.root).read_text()
        self.assertNotIn(KEY, text)
        self.assertNotIn(KEY[-4:], text)
        self.assertNotIn("oauth", text)


class OpencodeUrl(AccountsCase):
    def test_a_url_that_is_not_https_is_not_served_as_a_link(self):
        fake = GatedPty([GO.replace("https://", "http://") + DONE], accounts.OC_DONE_RX)
        self.server.state["accounts.pty"] = fake
        self.server.state["accounts.opencode_bin"] = "/opt/fake/opencode"
        self.post("/api/accounts/keyed/login", {"provider": "openai", "method": HEADLESS})
        flow = self.wait_flow("keyed", lambda b: b.get("url"))
        self.assertFalse(flow["url_is_https"])
        self.post("/api/accounts/keyed/cancel")
        fake.gate.set()
        self.wait_op("account:keyed")
