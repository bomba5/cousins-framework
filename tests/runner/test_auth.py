"""Detection kept apart from rate limits; the login file; the runner waits,
spends no turn while it waits, and never dies for a login."""
import os
import threading
import time
import unittest
from unittest import mock

from cousin_lib import accounts
from cousin_lib.runner import auth
from tests._hermetic import HermeticCase
from tests.runner._home import temp_home

LOGGED_OUT = "Not logged in \u00b7 Please run /login"     # the CLI's own text, a middle dot


def _wait(pred, timeout=10.0):
    t = time.monotonic()
    while time.monotonic() - t < timeout:
        if pred():
            return True
        time.sleep(0.02)
    return False


class TestSignals(HermeticCase):
    def test_the_typed_assistant_error_is_the_primary_signal(self):
        self.assertEqual(auth.assistant_signal("authentication_failed", LOGGED_OUT)["reason"],
                         auth.LOGIN)
        self.assertEqual(auth.assistant_signal("billing_error")["reason"], auth.BILLING)
        for other in ("rate_limit", "invalid_request", "server_error", "unknown", None):
            self.assertIsNone(auth.assistant_signal(other, "x"), other)

    def test_a_401_retry_is_auth_on_the_first_attempt(self):
        sig = auth.retry_signal({"error_status": 401, "error": "authentication_failed",
                                 "attempt": 1})
        self.assertEqual(sig["reason"], auth.LOGIN)
        self.assertIsNone(auth.retry_signal({"error_status": 529, "error": "overloaded",
                                             "attempt": 1}))

    def test_the_clis_own_wording_is_auth(self):
        for text in (LOGGED_OUT, "Not logged in. Run claude auth login to authenticate.",
                     "API Error: 401 Invalid API key - Please run /login",
                     "API key is invalid", "OAuth access token is invalid",
                     "Session expired. Please run /login to sign in again.",
                     "OAuth token revoked", "Invalid bearer token"):
            self.assertTrue(auth.is_auth_text(text), text)

    def test_a_401_or_the_wording_in_a_result_is_auth_a_429_never(self):
        self.assertEqual(auth.result_signal(True, 401, "", None)["reason"], auth.LOGIN)
        self.assertEqual(auth.result_signal(True, None, None, ["Please run /login"])["reason"],
                         auth.LOGIN)
        self.assertIsNone(auth.result_signal(False, 401, "", None))
        self.assertIsNone(auth.result_signal(True, 429, "rate limited", None))
        self.assertFalse(auth.is_auth_text("API Error: 429 rate_limit_error"))

    def test_backoff_doubles_and_caps_at_five_minutes(self):
        self.assertEqual([auth.backoff_s(n) for n in range(10)],
                         [1, 2, 4, 8, 16, 32, 64, 128, 256, 300])


class TestCredentialMarkAndHost(HermeticCase):
    def test_the_mark_moves_only_when_the_file_does(self):
        home = temp_home(self); root = home.parent.parent
        path = root / ".secrets" / "accounts" / "nightly"
        acc = accounts.Account("nightly", "claude-token", None, path)
        self.assertEqual(auth.credential_mark(acc, root), ("missing",))
        path.parent.mkdir(parents=True); path.write_text("tok-1\n")
        first = auth.credential_mark(acc, root)
        self.assertEqual(auth.credential_mark(acc, root), first)
        path.write_text("tok-2\n")
        self.assertNotEqual(auth.credential_mark(acc, root), first)
        login = accounts.Account("fleet", "claude-login", root / "data" / "accounts" / "fleet", None)
        (root / "data" / "accounts" / "fleet").mkdir(parents=True)
        (root / "data" / "accounts" / "fleet" / ".credentials.json").write_text("{}")
        self.assertNotEqual(auth.credential_mark(login, root), ("missing",))

    def test_host_label_from_harness_config_else_the_hostname(self):
        import socket
        home = temp_home(self); root = home.parent.parent
        self.assertEqual(auth.host_label(root), socket.gethostname())
        (root / "config").mkdir(exist_ok=True)
        (root / "config" / "harness.toml").write_text('host_label = "rack-2"\n')
        self.assertEqual(auth.host_label(root), "rack-2")


class TestLoginFile(HermeticCase):
    def test_write_keeps_since_read_and_clear(self):
        home = temp_home(self)
        kw = dict(host="h1", account="fleet", kind="claude-login", reason=auth.LOGIN,
                  action="run `cousin-account login fleet --via wren`")
        first = auth.write_login_required(home, detail="Not logged in", **kw)
        time.sleep(0.01)
        second = auth.write_login_required(home, detail="Session expired", **kw)
        self.assertEqual(first["since"], second["since"])
        self.assertEqual(auth.read_login_required(home)["detail"], "Session expired")
        self.assertTrue(auth.clear_login_required(home))
        self.assertIsNone(auth.read_login_required(home))
        self.assertFalse(auth.clear_login_required(home))


try:
    from claude_agent_sdk import (AssistantMessage, CLIConnectionError, ResultMessage,
                                  SystemMessage, TextBlock)
except ImportError:
    AssistantMessage = None


def _said(error, text):
    return AssistantMessage(content=[TextBlock(text=text)], model="m", error=error)


def _retry_401():
    return SystemMessage(subtype="api_retry", data={"error_status": 401, "attempt": 1,
                                                    "error": "authentication_failed"})


def _result_401():
    return ResultMessage(subtype="success", duration_ms=10, duration_api_ms=5, is_error=True,
                         num_turns=1, session_id="s-1", total_cost_usd=0.0, usage=None,
                         api_error_status=401)


@unittest.skipIf(AssistantMessage is None, "claude-agent-sdk not installed")
class TestRunnerWaitsForALogin(HermeticCase):
    def build(self, *, fail_connects=0, first_turn=None, per_client=None, account=None,
              watch_mark=True):
        """`per_client[n]`: the scripts client n runs before the defaults;
        `first_turn` is per_client {0: [first_turn]}. `account` may be a
        callable of the root. `self.logged_in` and `self.mark` are what
        `claude auth status` and the credential mark read, flipped by the
        test to play the operator."""
        from cousin_lib.runner.sdk import SdkRunner
        from tests.runner.test_sdk import ScriptedClient, assistant, init_msg, result
        self.home = temp_home(self)
        self.root = self.home.parent.parent
        (self.root / "config").mkdir(exist_ok=True)
        (self.root / "config" / "law.md").write_text("1. The law.\n")
        self.logged_in, self.mark = False, ("m", 1)
        p = mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": "/nonexistent/framework-root"})
        p.start(); self.addCleanup(p.stop)
        patches = [mock.patch.object(auth, "BACKOFF_BASE_S", 0.01),
                   mock.patch.object(auth, "BACKOFF_CAP_S", 0.05),
                   mock.patch.object(accounts, "status",
                                     side_effect=lambda a, r, **kw: {"loggedIn": self.logged_in,
                                                                     "authMethod": "claude.ai"})]
        if watch_mark:
            patches.append(mock.patch.object(auth, "credential_mark",
                                             side_effect=lambda a, r: self.mark))
        for patch in patches:
            patch.start(); self.addCleanup(patch.stop)
        per_client = dict(per_client or {})
        if first_turn:
            per_client.setdefault(0, []).insert(0, first_turn)
        self.clients = []

        def factory(options):
            n = len(self.clients)
            scripts = list(per_client.get(n, [])) + \
                [[init_msg(), assistant(text="ok"), result()] for _ in range(4)]
            client = ScriptedClient(options, scripts)
            if n < fail_connects:
                async def boom(prompt=None):
                    raise CLIConnectionError(LOGGED_OUT)
                client.connect = boom
            self.clients.append(client)
            return client
        if callable(account):
            account = account(self.root)
        r = SdkRunner(self.home, client_factory=factory, account=account)
        self.addCleanup(lambda: r.stop(timeout=5))
        return r

    def events(self, r, kind):
        return [e["payload"] for e in r.events() if e["kind"] == kind]

    def states(self, r):
        return [(e["payload"]["to"], e["payload"]["detail"]) for e in r.events()
                if e["kind"] == "state"]

    def op(self, r):
        from cousin_lib.delivery import Item
        return r.enqueue(Item("operator:priya", "chat", "hi", sender="Priya"))

    def test_a_logged_out_turn_waits_without_a_turn_then_recovers_when_the_status_flips(self):
        from tests.runner.test_sdk import init_msg, result
        r = self.build(first_turn=[init_msg(), _said("authentication_failed", LOGGED_OUT),
                                   result(is_error=True)])
        r.start()
        rec = self.op(r)
        self.assertTrue(_wait(lambda: auth.read_login_required(self.home) is not None))
        time.sleep(0.4)                                   # many backoff steps at these patches
        self.assertEqual(len(self.clients), 1)            # no reconnect, no turn, while logged out
        self.assertEqual(r.inbox.get(rec.inbox_id)["state"], "queued")
        self.assertIn(("errored", "login_required"), self.states(r))
        self.logged_in = True                             # the operator logged in
        self.assertTrue(_wait(lambda: r.inbox.get(rec.inbox_id)["outcome"] == "delivered"))
        first = self.events(r, "auth")[0]
        self.assertEqual((first["account"], first["reason"]), ("host", auth.LOGIN))
        self.assertIn("claude auth login", first["action"])
        self.assertIsNone(r.fatal)                        # never exits for a login
        self.assertTrue(any(e.get("restored") for e in self.events(r, "auth")))
        self.assertIsNone(auth.read_login_required(self.home))   # cleared by a good result

    def test_a_bad_key_fails_at_the_first_401_retry_not_after_the_retries(self):
        from tests.runner.test_sdk import init_msg
        key = accounts.Account("metered", "anthropic-key", None, None, secret_value="k-test")
        r = self.build(account=key, first_turn=[init_msg(source="ANTHROPIC_API_KEY"),
                                                _retry_401(), "WAIT_FOR_INTERRUPT"])
        r.start()
        rec = self.op(r)
        self.assertTrue(_wait(lambda: auth.read_login_required(self.home) is not None, 5))
        self.assertGreaterEqual(self.clients[0].interrupts, 1)     # the runner cut the retries
        self.assertTrue(_wait(lambda: r.inbox.get(rec.inbox_id)["state"] == "queued"))
        requeued = [e for e in self.events(r, "result") if e.get("requeued")]
        self.assertTrue(requeued[0]["repeat_in_transcript"])
        time.sleep(0.3)
        self.assertEqual(len(self.clients), 1)            # the same key: no retry, no turn
        self.mark = ("m", 2)                              # the operator replaced the key
        self.assertTrue(_wait(lambda: r.inbox.get(rec.inbox_id)["outcome"] == "delivered"))

    def test_a_revoked_login_reads_logged_in_so_only_new_credentials_retry(self):
        from tests.runner.test_sdk import init_msg
        r = self.build(first_turn=[init_msg(), _result_401()])
        self.logged_in = True                             # presence: a revoked login still reads in
        r.start()
        rec = self.op(r)
        self.assertTrue(_wait(lambda: auth.read_login_required(self.home) is not None))
        time.sleep(0.4)
        self.assertEqual(len(self.clients), 1)
        self.mark = ("m", 2)                              # a new .credentials.json
        self.assertTrue(_wait(lambda: r.inbox.get(rec.inbox_id)["outcome"] == "delivered"))

    def test_a_connect_refused_for_login_is_the_fallback_signal(self):
        r = self.build(fail_connects=1); r.start()
        rec = self.op(r)
        self.assertTrue(_wait(lambda: auth.read_login_required(self.home) is not None))
        self.assertTrue(r.worker_alive()); self.assertIsNone(r.fatal)
        self.logged_in = True
        self.assertTrue(_wait(lambda: r.inbox.get(rec.inbox_id)["outcome"] == "delivered"))

    def test_a_resume_refused_for_the_login_resumes_the_same_session_after_the_fix(self):
        import json
        r = self.build(fail_connects=1)
        (self.home / "data" / "runner-session.json").write_text(
            json.dumps({"session_id": "s-1", "lane": "login", "generation": 0, "updated": 0}))
        r.start()
        rec = self.op(r)
        self.assertTrue(_wait(lambda: auth.read_login_required(self.home) is not None))
        self.logged_in = True
        self.assertTrue(_wait(lambda: r.inbox.get(rec.inbox_id)["outcome"] == "delivered"))
        self.assertEqual([(c.options.extra_args or {}).get("resume") for c in self.clients],
                         ["s-1", "s-1"])                  # the saved session both times
        subtypes = [e.get("subtype") for e in self.events(r, "system")]
        self.assertIn("resumed", subtypes)
        self.assertNotIn("fresh", subtypes)               # never replaced by a fresh start

    def test_a_missing_secret_is_a_login_to_do_not_a_fatal_connect(self):
        r = self.build(watch_mark=False,                  # the REAL mark: the file appears
                       account=lambda root: accounts.Account(
                           "nightly", "claude-token", None, root / ".secrets" / "accounts" / "nightly"))
        r.start()
        rec = self.op(r)
        self.assertTrue(_wait(lambda: auth.read_login_required(self.home) is not None))
        self.assertIsNone(r.fatal); self.assertTrue(r.worker_alive())
        self.assertIn("cousin-account token nightly", auth.read_login_required(self.home)["action"])
        path = r.account.secret_file                      # the operator mints the token
        path.parent.mkdir(parents=True); os.chmod(path.parent, 0o700)
        path.write_text("sk-ant-oat01-FAKE\n"); os.chmod(path, 0o600)
        self.assertTrue(_wait(lambda: r.inbox.get(rec.inbox_id)["outcome"] == "delivered"))

    def test_billing_has_its_own_reason_and_the_manual_retry(self):
        from tests.runner.test_sdk import init_msg, result
        r = self.build(first_turn=[init_msg(), _said("billing_error", "Credit balance is too low"),
                                   result(is_error=True)])
        r.start()
        rec = self.op(r)
        self.assertTrue(_wait(lambda: auth.read_login_required(self.home) is not None))
        data = auth.read_login_required(self.home)
        self.assertEqual(data["reason"], auth.BILLING); self.assertIn("billing", data["action"])
        self.assertIn(("errored", auth.BILLING), self.states(r))
        time.sleep(0.3)
        self.assertEqual(len(self.clients), 1)            # a top-up changes no credential
        auth.clear_login_required(self.home)              # the operator's manual retry
        self.assertTrue(_wait(lambda: r.inbox.get(rec.inbox_id)["outcome"] == "delivered"))

    def test_the_account_not_taking_effect_is_an_auth_event_not_a_block(self):
        key = accounts.Account("metered", "anthropic-key", None, None, secret_value="k-test")
        r = self.build(account=key); r.start()                          # the scripted init says "none"
        rec = self.op(r)
        self.assertTrue(_wait(lambda: r.inbox.get(rec.inbox_id)["outcome"] == "delivered"))
        mism = [e for e in self.events(r, "auth") if e.get("mismatch")]
        self.assertEqual((mism[0]["account"], mism[0]["expected"], mism[0]["got"]),
                         ("metered", "ANTHROPIC_API_KEY", "none"))
        self.assertFalse(r.login_required())

    def test_a_rate_limit_never_takes_the_login_path(self):
        from claude_agent_sdk import RateLimitEvent, RateLimitInfo
        from tests.runner.test_sdk import init_msg, result
        limited = [init_msg(), RateLimitEvent(rate_limit_info=RateLimitInfo(
            status="rejected", resets_at=int(time.time()) + 1), uuid="u", session_id="s-1"),
            result(is_error=True)]
        r = self.build(first_turn=limited); r.start()
        self.op(r)
        self.assertTrue(_wait(lambda: any(e["kind"] == "rate_limit" for e in r.events())))
        time.sleep(0.3)
        self.assertEqual(self.events(r, "auth"), [])
        self.assertIsNone(auth.read_login_required(self.home))

    def test_an_auth_failure_in_the_handoff_postpones_the_rollover(self):
        from cousin_lib.runner import tools
        from tests.runner.test_sdk import assistant, init_msg, result
        args = {"position": "p", "next_action": "n", "status": "- s"}
        holder = {}
        # client 0: a work turn, then the handoff turn, logged out; client 1
        # (reconnected after the login): the handoff answered; client 2: the
        # new session
        r = self.build(per_client={
            0: [[init_msg(), assistant(text="work"), result()],
                [init_msg(), _said("authentication_failed", LOGGED_OUT), result(is_error=True)]],
            1: [[init_msg(), ("CALL", lambda: tools.call(holder["r"].tool_context, "handoff", args)),
                 assistant(text="handed off"), result()]]})
        holder["r"] = r
        rec = self.op(r)
        r.start()
        self.assertTrue(_wait(lambda: r.inbox.get(rec.inbox_id)["state"] == "done"))
        r._request_rollover("max_age")
        self.assertTrue(_wait(lambda: auth.read_login_required(self.home) is not None))
        phases = [(e.get("phase"), e.get("why")) for e in self.events(r, "rollover")]
        self.assertIn(("postponed", auth.LOGIN), phases)
        self.assertFalse(any(e.get("handoff") == "emergency" for e in self.events(r, "rollover")))
        self.assertFalse((self.home / "data" / "handoff.md").exists())   # no emergency handoff
        self.assertEqual([row["state"] for row in r.inbox.open_rows("flip")], ["queued"])
        self.assertIn(("errored", auth.LOGIN), self.states(r))
        self.logged_in = True                             # the login is fixed: the rollover runs
        self.assertTrue(_wait(lambda: any(e.get("phase") == "done"
                                          for e in self.events(r, "rollover")), 15))
        done = [e for e in self.events(r, "rollover") if e.get("phase") == "done"][0]
        self.assertEqual(done["handoff"], "clean")


@unittest.skipIf(AssistantMessage is None, "claude-agent-sdk not installed")
class TestValidate(HermeticCase):
    def test_a_bare_throwaway_client_one_turn(self):
        from cousin_lib.runner.sdk import validate_account
        from tests.runner.test_sdk import ScriptedClient, assistant, init_msg, result
        home = temp_home(self)
        seen = []
        rc, line = validate_account(
            accounts.Account("host", "claude-login", None, None, implicit=True), home.parent.parent,
            client_factory=lambda o: seen.append(o) or ScriptedClient(
                o, [[init_msg(), assistant(text="OK"), result()]]))
        self.assertEqual(rc, 0, line)
        o = seen[0]
        self.assertEqual((o.setting_sources, o.max_turns, o.tools, o.mcp_servers), ([], 1, [], {}))
        self.assertIsNone(getattr(o, "session_store", None))
        self.assertIsNone(o.resume); self.assertFalse(o.hooks)
        self.assertNotEqual(o.cwd, str(home)); self.assertFalse(os.path.exists(o.cwd))
        self.assertEqual(list((home / "data").iterdir()), [])     # the cousin's runner never ran

    def test_a_logged_out_turn_or_a_401_retry_is_4(self):
        from cousin_lib.runner.sdk import validate_account
        from tests.runner.test_sdk import ScriptedClient, init_msg, result
        home = temp_home(self)
        host = accounts.Account("host", "claude-login", None, None, implicit=True)
        for turn in ([init_msg(), _said("authentication_failed", LOGGED_OUT), result(is_error=True)],
                     [init_msg(), _retry_401(), "WAIT_FOR_INTERRUPT"]):
            rc, line = validate_account(host, home.parent.parent, timeout=5,
                                        client_factory=lambda o: ScriptedClient(o, [turn]))
            self.assertEqual(rc, 4, line)


class TestPolicyGuardrail(HermeticCase):
    def test_the_shipped_policy_denies_cousin_account(self):
        import pathlib
        repo = pathlib.Path(__file__).resolve().parents[2]
        self.assertIn("cousin-account", (repo / "templates" / "policy.toml.example").read_text())


if __name__ == "__main__":
    unittest.main()
