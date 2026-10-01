"""Detection kept apart from rate limits; the login file; the runner waits,
spends no turn while it waits, and never dies for a login."""
import json
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

    def test_the_bundled_clis_classifier_wordings_are_auth(self):
        for text in ("OAuth token has expired", "OAuth access token has expired",
                     "OAuth token has been revoked", "OAuth access token has been revoked",
                     "OAuth access token is invalid", "Invalid bearer token",
                     "OAuth token application has been deactivated", "Credential is invalid",
                     "invalid x-api-key", "Organization access has been revoked",
                     "Workspace access has been revoked", "API key is invalid"):
            self.assertTrue(auth.is_auth_text("API Error: 401 " + text), text)
        self.assertFalse(auth.is_auth_text("OAuth token has a long life"))

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


def _result_append_fails_once(r):
    """The runner's first `result` append raises (a full disk, say): the
    rows it names must still close as its branch closes them (#87 review)."""
    real, said = r.stream.append, []

    def append(kind, payload):
        if kind == "result" and not said:
            said.append(payload)
            raise OSError("No space left on device")
        return real(kind, payload)
    r.stream.append = append
    return said


@unittest.skipIf(AssistantMessage is None, "claude-agent-sdk not installed")
class TestRunnerWaitsForALogin(HermeticCase):
    def build(self, *, fail_connects=0, first_turn=None, per_client=None, account=None,
              watch_mark=True, before=None):
        """`per_client[n]`: the scripts client n runs before the defaults;
        `first_turn` is per_client {0: [first_turn]}. `account` may be a
        callable of the root. `self.logged_in` and `self.mark` are what
        `claude auth status` and the credential mark read, flipped by the
        test to play the operator. `before(home)` runs before the runner
        is made (a file an earlier runner left)."""
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
        if before:
            before(self.home)
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

    def test_the_host_login_line_names_the_host_outside_the_container(self):
        r = self.build()
        self.assertEqual(r._login_action(auth.LOGIN),
                         "run `claude auth login` as the host user on %s"
                         % auth.host_label(self.root))

    def test_in_the_container_the_line_names_no_container_id(self):
        """The hostname inside the image is the container's id, and there is
        no host user there: the line is the compose exec, said once."""
        r = self.build()
        with mock.patch.dict(os.environ, {"COUSIN_IN_CONTAINER": "1"}):
            action = r._login_action(auth.LOGIN)
        self.assertTrue(action.startswith(
            "run `docker compose exec framework cousin-account login host"), action)
        self.assertNotIn(" on %s" % auth.host_label(self.root), action)
        self.assertNotIn("host user", action)

    def test_a_login_file_left_by_an_earlier_runner_clears_on_the_first_good_result(self):
        """A restart after the login was fixed: the file on disk is armed at
        start, so the first good result clears it (it stayed forever)."""
        r = self.build(before=lambda home: auth.write_login_required(
            home, host="h", account="host", kind="claude-login", reason=auth.LOGIN,
            detail="old", action="claude auth login"))
        self.logged_in = True
        r.start()
        rec = self.op(r)
        self.assertTrue(_wait(lambda: r.inbox.get(rec.inbox_id)["outcome"] == "delivered"))
        self.assertTrue(_wait(lambda: auth.read_login_required(self.home) is None))
        self.assertTrue(any(e.get("restored") for e in self.events(r, "auth")))

    def test_a_bad_key_fails_at_the_first_401_retry_not_after_the_retries(self):
        from tests.runner.test_sdk import init_msg
        key = accounts.Account("metered", "anthropic-key", None, None, secret_value="k-test")
        r = self.build(account=key, first_turn=[init_msg(source="ANTHROPIC_API_KEY"),
                                                _retry_401(), "WAIT_FOR_INTERRUPT"])
        r.start()
        rec = self.op(r)
        self.assertTrue(_wait(lambda: auth.read_login_required(self.home) is not None, 5))
        self.assertGreaterEqual(self.clients[0].interrupts, 1)     # the runner cut the retries
        # the result is appended, then the row goes back to the queue (#87, #102)
        self.assertTrue(_wait(lambda: r.inbox.get(rec.inbox_id)["state"] == "queued"
                              and any(e.get("requeued") for e in self.events(r, "result"))))
        requeued = [e for e in self.events(r, "result") if e.get("requeued")]
        self.assertTrue(requeued[0]["repeat_in_transcript"])
        time.sleep(0.3)
        self.assertEqual(len(self.clients), 1)            # the same key: no retry, no turn
        self.mark = ("m", 2)                              # the operator replaced the key
        self.assertTrue(_wait(lambda: r.inbox.get(rec.inbox_id)["outcome"] == "delivered"))

    def test_a_login_result_closes_the_writer_before_the_after_turn_work(self):
        """#118 review item 3: the login branch requeued every open row, so
        no fold may still be written while _after_turn runs."""
        from tests.runner.test_sdk import init_msg
        r = self.build(first_turn=[init_msg(), _result_401()])
        seen = []
        real = r._after_turn

        async def spy(msg):
            seen.append(r._writer is None)
            return await real(msg)
        r._after_turn = spy
        r.start()
        self.op(r)
        self.assertTrue(_wait(lambda: seen))
        self.assertEqual(seen[0], True, "the writer was still open during _after_turn")

    def test_a_failing_result_append_still_requeues_and_waits_for_the_login(self):
        from tests.runner.test_sdk import init_msg
        r = self.build(first_turn=[init_msg(), _result_401()])
        said = _result_append_fails_once(r)
        r.start()
        rec = self.op(r)
        self.assertTrue(_wait(lambda: said and r.inbox.get(rec.inbox_id)["state"] != "claimed"))
        time.sleep(0.3)
        row = r.inbox.get(rec.inbox_id)
        self.assertEqual((row["state"], row["outcome"]), ("queued", None), "requeued, never failed")
        self.assertTrue(_wait(lambda: auth.read_login_required(self.home) is not None),
                        "the runner never waited for the login")

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

    def test_an_auth_signal_then_a_stream_that_ends_without_a_result_is_a_login(self):
        from tests.runner.test_sdk import init_msg
        r = self.build(first_turn=[init_msg(), _said("authentication_failed", LOGGED_OUT), "END"])
        r.start()
        rec = self.op(r)
        # the login file is written, then its `auth` event appended (#102)
        self.assertTrue(_wait(lambda: auth.read_login_required(self.home) is not None
                              and self.events(r, "auth")))
        self.assertEqual(r.inbox.get(rec.inbox_id)["state"], "queued")   # never failed
        self.assertEqual(self.events(r, "auth")[0]["reason"], auth.LOGIN)
        self.assertTrue(r.login_required()); self.assertIsNone(r.fatal)
        self.logged_in = True
        self.assertTrue(_wait(lambda: r.inbox.get(rec.inbox_id)["outcome"] == "delivered"))

    def test_a_login_accounts_401_retry_is_left_to_the_clis_own_refresh(self):
        # W11-1: a claude-login account refreshes its token at the next attempt;
        # an interrupt at the first 401 would cancel a refresh that works
        from tests.runner.test_sdk import assistant, init_msg, result
        r = self.build(first_turn=[init_msg(), _retry_401(), assistant(text="refreshed"),
                                   result()])
        r.start()
        rec = self.op(r)
        self.assertTrue(_wait(lambda: r.inbox.get(rec.inbox_id)["outcome"] == "delivered"))
        self.assertEqual(self.clients[0].interrupts, 0)
        self.assertEqual(self.events(r, "auth"), [])
        self.assertIsNone(auth.read_login_required(self.home))
        self.assertFalse(r.login_required())

    def test_a_retry_that_keeps_failing_for_another_reason_drops_the_resume_after_three(self):
        from tests.runner.test_sdk import asked_resume, init_msg, result
        r = self.build(first_turn=[init_msg(), _said("authentication_failed", LOGGED_OUT),
                                   result(is_error=True)])
        inner = r.client_factory

        def factory(options):
            client = inner(options)
            if len(self.clients) > 1 and asked_resume(options):
                async def gone(prompt=None):
                    raise RuntimeError("no such session")
                client.connect = gone          # the session is gone, the login is fine
            return client
        r.client_factory = factory
        details = []
        real_write = auth.write_login_required

        def write(home, **kw):
            details.append(kw["detail"])
            return real_write(home, **kw)
        patch = mock.patch.object(auth, "write_login_required", side_effect=write)
        patch.start(); self.addCleanup(patch.stop)
        r.start()
        rec = self.op(r)
        self.assertTrue(_wait(lambda: auth.read_login_required(self.home) is not None))
        self.logged_in = True
        self.assertTrue(_wait(lambda: r.inbox.get(rec.inbox_id)["outcome"] == "delivered"))
        asked = [asked_resume(c.options) for c in self.clients]
        self.assertEqual(asked[1:], ["s-1", "s-1", "s-1", None])   # three tries, then fresh
        self.assertTrue(any("no such session" in d for d in details))   # the file said why
        fresh = [e for e in self.events(r, "system") if e.get("subtype") == "fresh"]
        self.assertEqual(fresh, [{"subtype": "fresh", "digest": False},    # the first start
                                 {"subtype": "fresh", "digest": True}])    # after the fix

    def test_a_login_file_that_could_not_be_written_is_no_manual_retry(self):
        from tests.runner.test_sdk import init_msg, result
        r = self.build(first_turn=[init_msg(), _said("authentication_failed", LOGGED_OUT),
                                   result(is_error=True)])
        patch = mock.patch.object(auth, "write_login_required", side_effect=OSError("read-only"))
        patch.start(); self.addCleanup(patch.stop)
        r.start()
        rec = self.op(r)
        self.assertTrue(_wait(lambda: r.login_required()))
        time.sleep(0.4)                                   # many looks at these patches
        self.assertEqual(len(self.clients), 1)            # no file is not a deleted file
        self.logged_in = True
        self.assertTrue(_wait(lambda: r.inbox.get(rec.inbox_id)["outcome"] == "delivered"))

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
        # the flip row is requeued before the postponed phase is appended (#102)
        self.assertTrue(_wait(lambda: ("postponed", auth.LOGIN) in [
            (e.get("phase"), e.get("why")) for e in self.events(r, "rollover")]))
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

    def lost_resume_home(self):
        """A restart: a saved session, state to carry, a start hook that
        logs every run."""
        import json
        (self.home / "data" / "runner-session.json").write_text(
            json.dumps({"session_id": "s-saved", "lane": "login", "generation": 0, "updated": 0}))
        (self.home / "STATUS.md").write_text("## Open loops\n- carry this\n")
        with open(self.home / "cousin.toml", "a") as fh:
            fh.write('\n[session]\nstart_hooks = ["echo start >> %s/hooks.log"]\n' % self.home)

    def rows(self, r):
        return [row for row in (r.inbox.get(i) for i in range(1, 40)) if row]

    def digests(self, r):
        return [row for row in self.rows(r) if "STATE DIGEST" in (row["body"] or "")]

    def test_a_lost_resume_and_a_failed_login_in_one_chat_turn_hold_the_fresh_start(self):
        # the first turn after a restart names another session (the transcript
        # is gone) AND fails the login: the fresh start waits for the fix, it
        # never claims its digest into a machine that is `errored`
        from tests.runner.test_sdk import init_msg, result
        r = self.build(first_turn=[init_msg(session="s-new"),
                                   _said("authentication_failed", LOGGED_OUT),
                                   result(is_error=True, session="s-new")])
        self.lost_resume_home()
        r.start()
        rec = self.op(r)
        self.assertTrue(_wait(lambda: auth.read_login_required(self.home) is not None))
        time.sleep(0.3)                                   # many looks at these patches
        self.assertEqual([row for row in self.rows(r) if row["state"] == "claimed"], [])
        self.assertFalse((self.home / "hooks.log").exists())   # no start before the fix
        self.mark = ("m", 2)                              # the operator fixed the login
        self.assertTrue(_wait(lambda: r.inbox.get(rec.inbox_id)["outcome"] == "delivered"))
        self.assertTrue(_wait(lambda: [d["outcome"] for d in self.digests(r)] == ["delivered"]))
        self.assertEqual([row for row in self.rows(r) if row["state"] == "claimed"], [])
        self.assertEqual((self.home / "hooks.log").read_text().split(), ["start"])
        fresh = [e for e in self.events(r, "system") if e.get("subtype") == "fresh"]
        self.assertEqual(fresh, [{"subtype": "fresh", "digest": True}])
        self.assertIsNone(r.fatal)

    def test_a_lost_resume_and_a_failed_login_in_the_handoff_turn_leave_no_row_claimed(self):
        # the same pair in a rollover's handoff turn: the rollover is postponed
        # and the lost resume goes with it (the rollover starts the new
        # session); no start for a generation that never rolled over
        from cousin_lib.runner import rollover, tools
        from tests.runner.test_sdk import assistant, init_msg, result
        args = {"position": "p", "next_action": "n", "status": "- s"}
        holder = {}
        # client 0 (the resume, answered as another session): the handoff turn,
        # logged out; client 1 (after the fix): the handoff answered; client 2:
        # the new session
        r = self.build(per_client={
            0: [[init_msg(session="s-new"), _said("authentication_failed", LOGGED_OUT),
                 result(is_error=True, session="s-new")]],
            1: [[init_msg(session="s-new"),
                 ("CALL", lambda: tools.call(holder["r"].tool_context, "handoff", args)),
                 assistant(text="handed off"), result(session="s-new")]]})
        holder["r"] = r
        self.lost_resume_home()
        rollover.put_once(r.inbox, self.home, "max_age")
        r.start()
        self.assertTrue(_wait(lambda: auth.read_login_required(self.home) is not None))
        time.sleep(0.3)
        self.assertEqual([row for row in self.rows(r) if row["state"] == "claimed"], [])
        self.assertEqual([row["state"] for row in r.inbox.open_rows("flip")], ["queued"])
        self.assertFalse((self.home / "hooks.log").exists())   # nothing rolled over yet
        self.mark = ("m", 2)                              # the operator fixed the login
        self.assertTrue(_wait(lambda: any(e.get("phase") == "done"
                                          for e in self.events(r, "rollover")), 15))
        self.assertTrue(_wait(lambda: [d["outcome"] for d in self.digests(r)] == ["delivered"]))
        self.assertEqual([row for row in self.rows(r) if row["state"] == "claimed"], [])
        self.assertEqual((self.home / "hooks.log").read_text().split(), ["start"])
        done = [e for e in self.events(r, "rollover") if e.get("phase") == "done"][0]
        self.assertEqual(done["handoff"], "clean")
        self.assertIsNone(r.fatal)

    def test_a_turn_refused_before_its_send_requeues_its_row(self):
        # defensive: a turn whose move to `running` raises never wrote its row,
        # so the row goes back to the queue instead of staying `claimed`
        import asyncio
        from cousin_lib.delivery import Item
        r = self.build()
        rid = r.inbox.put(Item("operator:priya", "chat", "hi", sender="Priya"))
        row = r.inbox.claim_id(rid, claimant=r.session_id)
        r.machine.to("errored", "held")
        self.assertFalse(asyncio.run(r._turn(row)))
        self.assertEqual(r.inbox.get(rid)["state"], "queued")
        self.assertEqual([e["requeued"] for e in self.events(r, "result")], [[rid]])
        self.assertEqual(self.clients, [])                # no client was touched

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
        self.assertIn("no-session-persistence", o.extra_args)    # no transcript left behind
        self.assertIsNone(o.extra_args["no-session-persistence"])
        self.assertNotEqual(o.cwd, str(home)); self.assertFalse(os.path.exists(o.cwd))
        self.assertEqual(list((home / "data").iterdir()), [])     # the cousin's runner never ran

    def test_a_logged_out_turn_or_a_keys_401_retry_is_4(self):
        from cousin_lib.runner.sdk import validate_account
        from tests.runner.test_sdk import ScriptedClient, init_msg, result
        home = temp_home(self)
        host = accounts.Account("host", "claude-login", None, None, implicit=True)
        key = accounts.Account("metered", "anthropic-key", None, None, secret_value="k-test")
        for acc, turn in ((host, [init_msg(), _said("authentication_failed", LOGGED_OUT),
                                  result(is_error=True)]),
                          (key, [init_msg(), _retry_401(), "WAIT_FOR_INTERRUPT"])):
            rc, line = validate_account(acc, home.parent.parent, timeout=5,
                                        client_factory=lambda o: ScriptedClient(o, [turn]))
            self.assertEqual(rc, 4, line)

    def _one(self, account, turn, **kw):
        from cousin_lib.runner.sdk import validate_account
        from tests.runner.test_sdk import ScriptedClient
        home = temp_home(self)
        seen = []
        rc, line = validate_account(account, home.parent.parent, timeout=5,
                                    client_factory=lambda o: seen.append(
                                        (o, {**os.environ, **o.env})) or ScriptedClient(o, [turn]),
                                    **kw)
        return rc, line, seen

    def test_a_typed_turn_error_fails_validate_with_its_words(self):
        """#96: a model the bundled CLI is too old for answers with an
        invalid_request 400 in the turn, and a result not flagged is_error."""
        from claude_agent_sdk import TextBlock
        from tests.runner.test_sdk import init_msg, result
        host = accounts.Account("host", "claude-login", None, None, implicit=True)
        said = "API Error: 400 Claude Code 2.1.277 does not support this model"
        rc, line, _ = self._one(host, [init_msg(), AssistantMessage(
            content=[TextBlock(text=said)], model="<synthetic>", error="invalid_request"),
            result()], model="opus")
        self.assertEqual(rc, 4, line)
        self.assertIn("invalid_request", line); self.assertIn("does not support this model", line)

    def test_a_result_that_is_not_a_success_fails_validate_even_unflagged(self):
        from claude_agent_sdk import ResultMessage
        from tests.runner.test_sdk import assistant, init_msg
        host = accounts.Account("host", "claude-login", None, None, implicit=True)
        for subtype, status in (("error_during_execution", None), ("success", 500)):
            with self.subTest(subtype=subtype, status=status):
                res = ResultMessage(subtype=subtype, duration_ms=1, duration_api_ms=1,
                                    is_error=False, num_turns=1, session_id="s-1",
                                    api_error_status=status)
                rc, line, _ = self._one(host, [init_msg(), assistant(text="OK"), res])
                self.assertEqual(rc, 4, line)
                self.assertIn(subtype, line)

    def test_validate_carries_the_effort(self):
        from tests.runner.test_sdk import assistant, init_msg, result
        host = accounts.Account("host", "claude-login", None, None, implicit=True)
        rc, line, seen = self._one(host, [init_msg(), assistant(text="OK"), result()],
                                   model="opus", effort="max")
        self.assertEqual(rc, 0, line)
        self.assertEqual((seen[0][0].model, seen[0][0].effort), ("opus", "max"))

    def test_validate_never_inherits_the_shells_credentials(self):
        """#96 review I1: the SDK starts the CLI with {**os.environ,
        **options.env}; an inherited key, token or config dir would bill
        the wrong account, for every caller (cousin-migrate included)."""
        from tests.runner.test_sdk import assistant, init_msg, result
        shell = {"ANTHROPIC_API_KEY": "sk-shell", "CLAUDE_CODE_OAUTH_TOKEN": "oauth-shell",
                 "CLAUDE_CONFIG_DIR": "/shell/dir", "ANTHROPIC_BASE_URL": "http://shell.invalid",
                 "ANTHROPIC_AUTH_TOKEN": "tok-shell", "CLAUDE_CODE_USE_BEDROCK": "1"}
        os.environ.update(shell)
        turn = [init_msg(), assistant(text="OK"), result()]
        host = accounts.Account("host", "claude-login", None, None, implicit=True)
        rc, line, seen = self._one(host, turn)
        self.assertEqual(rc, 0, line)
        self.assertEqual({k: seen[0][1].get(k) for k in shell}, {k: None for k in shell})
        key = accounts.Account("metered", "anthropic-key", None, None, secret_value="k-own")
        rc, line, seen = self._one(key, turn)
        self.assertEqual(rc, 0, line)
        child = seen[0][1]
        self.assertEqual(child["ANTHROPIC_API_KEY"], "k-own")
        self.assertNotEqual(child.get("CLAUDE_CONFIG_DIR"), "/shell/dir")
        for k in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN",
                  "CLAUDE_CODE_USE_BEDROCK"):
            self.assertIsNone(child.get(k), k)
        # the caller gets its environment back
        self.assertEqual({k: os.environ.get(k) for k in shell}, shell)

    def test_a_login_accounts_401_retry_waits_for_the_refresh(self):
        # W11-1: the CLI refreshes a login's token at its next attempt
        from cousin_lib.runner.sdk import validate_account
        from tests.runner.test_sdk import ScriptedClient, assistant, init_msg, result
        home = temp_home(self)
        host = accounts.Account("host", "claude-login", None, None, implicit=True)
        turn = [init_msg(), _retry_401(), assistant(text="OK"), result()]
        rc, line = validate_account(host, home.parent.parent, timeout=5,
                                    client_factory=lambda o: ScriptedClient(o, [turn]))
        self.assertEqual(rc, 0, line)


@unittest.skipIf(AssistantMessage is None, "claude-agent-sdk not installed")
class TestValidateAttribution(HermeticCase):
    """Tracker #112: validate_account's own throwaway client composes the
    same --settings, for consistency with the cousin's own runner."""

    def test_default_carries_no_settings(self):
        from cousin_lib.runner.sdk import validate_account
        from tests.runner.test_sdk import ScriptedClient, assistant, init_msg, result
        home = temp_home(self)
        seen = []
        rc, line = validate_account(
            accounts.Account("host", "claude-login", None, None, implicit=True), home.parent.parent,
            client_factory=lambda o: seen.append(o) or ScriptedClient(
                o, [[init_msg(), assistant(text="OK"), result()]]))
        self.assertEqual(rc, 0, line)
        self.assertIsNone(seen[0].settings)

    def test_commit_attribution_false_composes_settings(self):
        from cousin_lib.runner.sdk import validate_account
        from tests.runner.test_sdk import ScriptedClient, assistant, init_msg, result
        home = temp_home(self)
        seen = []
        rc, line = validate_account(
            accounts.Account("host", "claude-login", None, None, implicit=True), home.parent.parent,
            commit_attribution=False,
            client_factory=lambda o: seen.append(o) or ScriptedClient(
                o, [[init_msg(), assistant(text="OK"), result()]]))
        self.assertEqual(rc, 0, line)
        settings = json.loads(seen[0].settings)
        self.assertIs(settings["includeCoAuthoredBy"], False)
        self.assertEqual(settings["attribution"], {"commit": "", "pr": ""})


class TestPolicyGuardrail(HermeticCase):
    def test_the_shipped_policy_denies_cousin_account(self):
        import pathlib
        repo = pathlib.Path(__file__).resolve().parents[2]
        self.assertIn("cousin-account", (repo / "templates" / "policy.toml.example").read_text())


if __name__ == "__main__":
    unittest.main()
