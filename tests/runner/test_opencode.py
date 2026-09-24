"""OpencodeRunner (phase 9 Task 5) against the fake `opencode serve` (R15):
the rendered config (R5, R6, Review Focus 2), the server's environment
(an allowlist), the bridge guard (R13), the MCP check, and turns (R2',
R14'): events, fold, a peer held, interrupt with requeue, the doubled
idle, auth, a reconnect's gap, stop. Never the opencode binary."""
import http.client
import json
import os
import secrets
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.parse import urlsplit

from cousin_lib import accounts
from cousin_lib.delivery import Item
from cousin_lib.runner import opencode
from cousin_lib.runner.base import RunnerError
from cousin_lib.runner.opencode import OpencodeRunner
from cousin_lib.runner.opencode_http import OpencodeServer
from tests._hermetic import HermeticCase
from tests.runner._fake_opencode import FakeOpencode
from tests.runner._home import temp_home

FAKE_BIN = Path(__file__).with_name("_fake_opencode.py")
ENDPOINT = "http://127.0.0.1:11434/v1"


def _wait(pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


def _probe_mcp(config):
    """What opencode's GET /mcp would say: an MCP `initialize` against the
    URL and headers the rendered config names."""
    entry = (config.get("mcp") or {}).get("cousin") or {}
    try:
        parts = urlsplit(entry["url"])
        conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=5)
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                           "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                                      "clientInfo": {"name": "fake", "version": "0"}}})
        conn.request("POST", parts.path, body=body,
                     headers=dict(entry.get("headers") or {}, **{
                         "Content-Type": "application/json",
                         "Accept": "application/json, text/event-stream"}))
        status = conn.getresponse().status
        conn.close()
    except (KeyError, OSError, http.client.HTTPException, TypeError):
        return {"cousin": {"status": "failed", "error": "unreachable"}}
    return {"cousin": {"status": "connected" if status == 200 else
                       "needs_auth" if status == 401 else "failed"}}


class FakeServer:
    """What server_factory returns: the fake on a fresh password, serving
    the config the runner rendered, its /mcp the result of a real MCP
    handshake with the runner's server (unless `mcp` is forced)."""

    def __init__(self, factory, kwargs):
        self.factory, self.kwargs = factory, kwargs
        self.fake = None
        self.url = self.password = None
        self.stopped = False

    def start(self):
        config = json.loads(Path(self.kwargs["config_path"]).read_text())
        mcp = self.factory.mcp if self.factory.mcp is not None else _probe_mcp(config)
        if self.factory.shared is not None and self.factory.shared.fake is not None:
            self.fake = self.factory.shared.fake        # a restart on the same opencode store
            self.fake.mcp = mcp
        else:
            self.fake = FakeOpencode(self.factory.scripts, password=secrets.token_urlsafe(8),
                                     config=config, mcp=mcp, providers=self.factory.providers,
                                     directory=str(self.kwargs["cwd"])).start()
            if self.factory.shared is not None:
                self.factory.shared.fake = self.fake
        self.url, self.password = self.fake.url, self.fake.password
        return self

    def alive(self):
        return self.fake is not None and not self.stopped

    def output(self):
        return ""

    def stop(self, timeout=5):
        self.stopped = True
        if self.fake is not None and self.factory.shared is None:
            self.fake.close()


class Factory:
    def __init__(self, scripts=(), *, mcp=None, shared=None, providers=None):
        self.scripts, self.mcp, self.shared = list(scripts), mcp, shared
        self.providers = providers
        self.calls, self.servers = [], []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        self.servers.append(FakeServer(self, kwargs))
        return self.servers[-1]

    @property
    def fake(self):
        return self.servers[-1].fake


class Shared:
    """One fake kept across two runners (a restart that finds its session)."""
    fake = None


def _op(body, who="priya"):
    return Item("operator:%s" % who, "chat", body, sender=who.capitalize())


class OpencodeCase(HermeticCase):
    def home(self, *, model="local/m1", extra=""):
        home = temp_home(self, runner="opencode")
        self.root = home.parent.parent
        toml = (home / "cousin.toml").read_text()
        if model:
            toml += 'model = "%s"\n' % model
        (home / "cousin.toml").write_text(toml + extra)
        return home

    def account(self, **kw):
        fields = dict(endpoint=ENDPOINT, endpoint_model="m1")
        fields.update(kw)
        return accounts.Account("lab", "opencode", None, None,
                                data_dir=self.root / ".secrets" / "accounts" / "lab.opencode",
                                **fields)

    def runner(self, scripts=(), *, factory=None, home=None, account=None, **kw):
        home = home or self.home()
        self.factory = factory or Factory(scripts)
        r = OpencodeRunner(home, server_factory=self.factory,
                           account=account or self.account(), **kw)
        self.addCleanup(lambda: r.stop(timeout=5))
        return r

    def started(self, r):
        r.start()
        self.assertTrue(_wait(lambda: r.opencode_session is not None or r.fatal, 10), "no session")
        self.assertIsNone(r.fatal)
        return r

    def kinds(self, r):
        return [e["kind"] for e in r.events()]

    def payloads(self, r, kind):
        return [e["payload"] for e in r.events() if e["kind"] == kind]

    def outcome(self, r, receipt):
        row = r.inbox.get(receipt.inbox_id)
        return row["outcome"] if row and row["state"] == "done" else None

    def prompts(self):
        return [q for q in self.factory.fake.requests if q["path"].endswith("/prompt_async")]


class TestConfig(OpencodeCase):
    def test_the_rendered_config_disables_opencodes_own_provider_and_names_the_model(self):
        """Review Focus 2: opencode's hosted provider off, the model named,
        the runner's MCP server the only one, on the rendered file only."""
        r = self.started(self.runner(home=self.home(extra='small_model = "local/m1-mini"\n')))
        path = Path(self.factory.calls[0]["config_path"])
        self.assertEqual(path, r.account.data_dir / "opencode.runner.json")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        config = json.loads(path.read_text())
        self.assertEqual(config, self.factory.fake.config)       # what the server was given
        self.assertEqual(config["disabled_providers"], ["opencode"])
        self.assertEqual((config["model"], config["small_model"]), ("local/m1", "local/m1-mini"))
        self.assertIs(config["autoupdate"], False)
        self.assertEqual(config["share"], "disabled")
        self.assertEqual(config["permission"], {"*": "allow"})
        self.assertEqual(config["plugin"], [opencode.PLUGIN.as_uri()])
        self.assertTrue(config["plugin"][0].endswith("/plugins/opencode/cousin-policy.js"))
        self.assertEqual(config["mcp"], {"cousin": {
            "type": "remote", "url": r._mcp.url,
            "headers": {"Authorization": "Bearer %s" % r._mcp.token}}})
        self.assertNotIn(":3456", r._mcp.url)
        self.assertEqual(config["provider"], {"local": {
            "npm": "@ai-sdk/openai-compatible", "name": "local endpoint (lab)",
            "options": {"baseURL": ENDPOINT}, "models": {"m1": {"name": "m1"}}}})
        self.assertNotIn("enabled_providers", config)
        self.assertEqual(sorted(config), sorted(["$schema", "model", "small_model",
                                                 "disabled_providers", "autoupdate", "share",
                                                 "permission", "plugin", "mcp", "provider"]))
        self.assertEqual(self.factory.fake.mcp, {"cousin": {"status": "connected"}})

    def test_a_providers_account_is_enabled_by_name_with_no_provider_block(self):
        self.home()
        config = opencode.render_config(
            self.account(endpoint=None, endpoint_model=None, providers=("openai", "mistral")),
            model="openai/gpt-x", small_model="openai/gpt-x", mcp_url="http://127.0.0.1:9/mcp",
            mcp_token="t")
        self.assertEqual(config["enabled_providers"], ["openai", "mistral"])
        self.assertEqual(config["disabled_providers"], ["opencode"])
        self.assertNotIn("provider", config)

    def test_the_model_is_required(self):
        with self.assertRaises(RunnerError) as err:
            self.runner(home=self.home(model=None))
        self.assertIn("[agent] model is required", str(err.exception))
        for model, needle in (("m1", "<provider>/<model>"),
                              ("openai/gpt-x", "rendered as provider 'local'")):
            with self.subTest(model=model), self.assertRaises(RunnerError) as err:
                self.runner(home=self.home(model=model))
            self.assertIn(needle, str(err.exception))
        with self.assertRaises(RunnerError) as err:
            self.runner(home=self.home(model="mistral/big"),
                        account=self.account(endpoint=None, endpoint_model=None,
                                             providers=("openai",)))
        self.assertIn("holds keys for openai only", str(err.exception))

    def test_a_claude_account_never_reaches_the_opencode_lane(self):
        home = self.home()
        with self.assertRaises(RunnerError) as err:
            OpencodeRunner(home, server_factory=Factory(),
                           account=accounts.Account(accounts.HOST, "claude-login", None, None,
                                                    implicit=True))
        self.assertIn('runs on a kind = "opencode" account only', str(err.exception))

    def test_the_binary_is_named_by_the_cousin_then_the_image_then_path(self):
        self.assertEqual(opencode.opencode_bin({"opencode_bin": "/opt/a"},
                                               {"COUSIN_OPENCODE_BIN": "/opt/b"}), "/opt/a")
        self.assertEqual(opencode.opencode_bin({}, {"COUSIN_OPENCODE_BIN": "/opt/b"}), "/opt/b")
        self.assertEqual(opencode.opencode_bin({}, {}), "opencode")


class TestEnvironment(OpencodeCase):
    PLANTED = {"OPENCODE_CONFIG_CONTENT": '{"plugin": ["x"]}', "OPENCODE_CONFIG_DIR": "/x",
               "OPENCODE_SERVER_USERNAME": "mallory", "OPENCODE_AUTH_CONTENT": "{}",
               "OPENCODE_PERMISSION": "{}", "ANTHROPIC_API_KEY": "sk-ant-wren",
               "OPENAI_API_KEY": "sk-wren", "CLAUDE_CODE_OAUTH_TOKEN": "tok",
               "HOME": "/srv/elsewhere", "XDG_DATA_HOME": "/elsewhere"}

    def test_the_server_environment_is_an_allowlist(self):
        for name, value in dict(self.PLANTED, LANG="C.UTF-8", LC_ALL="C.UTF-8",
                                TZ="UTC").items():
            os.environ[name] = value
        r = self.started(self.runner())
        env = self.factory.calls[0]["env"]
        data = r.account.data_dir
        self.assertEqual(env["HOME"], str(data))
        for var, sub in accounts.XDG_DIRS:
            self.assertEqual(env[var], str(data / sub))
        self.assertEqual((env["LANG"], env["LC_ALL"], env["TZ"]), ("C.UTF-8", "C.UTF-8", "UTC"))
        self.assertEqual(env["PATH"], os.environ["PATH"])
        self.assertEqual((env["COUSIN_HOME"], env["FRAMEWORK_ROOT"]), (str(r.home), str(r.root)))
        for name in self.PLANTED:
            if name not in ("HOME", "XDG_DATA_HOME"):
                self.assertNotIn(name, env)
        self.assertNotIn("OPENCODE_DISABLE_MODELS_FETCH", env)       # R21: allowed by default
        # what OpencodeServer then gives the child: the switches, nothing planted
        srv = OpencodeServer("opencode", **{k: v for k, v in self.factory.calls[0].items()
                                            if k != "argv0"})
        srv.password = "pw"
        child = srv.child_env()
        for name in ("OPENCODE_DISABLE_AUTOUPDATE", "OPENCODE_DISABLE_SHARE",
                     "OPENCODE_DISABLE_CLAUDE_CODE", "OPENCODE_DISABLE_PROJECT_CONFIG"):
            self.assertEqual(child[name], "1")
        self.assertEqual(sorted(set(child) - set(env)), sorted([
            "OPENCODE_CONFIG", "OPENCODE_DISABLE_AUTOUPDATE", "OPENCODE_DISABLE_CLAUDE_CODE",
            "OPENCODE_DISABLE_PROJECT_CONFIG", "OPENCODE_DISABLE_SHARE",
            "OPENCODE_SERVER_PASSWORD"]))

    def test_models_fetch_off_is_a_cousin_setting(self):
        r = self.runner(home=self.home(extra="opencode_models_fetch = false\n"))
        env = opencode.server_env(r.account, r.root, home=r.home, models_fetch=r.models_fetch)
        self.assertEqual(env["OPENCODE_DISABLE_MODELS_FETCH"], "1")

    def test_planted_variables_never_reach_the_child(self):
        """End to end through the real OpencodeServer and a fake `opencode`
        binary ([agent] opencode_bin) that records its own environment."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        dump = Path(tmp.name) / "env.json"
        binary = Path(tmp.name) / "opencode"
        binary.write_text('#!/bin/sh\n"%s" -c \'import json, os, sys; json.dump(dict(os.environ),'
                          ' open(sys.argv[1], "w"))\' "%s"\nexec "%s" "%s" "$@"\n'
                          % (sys.executable, dump, sys.executable, FAKE_BIN))
        binary.chmod(0o755)
        for name, value in self.PLANTED.items():
            os.environ[name] = value
        home = self.home(extra='opencode_bin = "%s"\n' % binary)
        r = OpencodeRunner(home, account=self.account(), health_timeout_s=20)
        r.mcp_timeout_s = 0.5
        self.addCleanup(lambda: r.stop(timeout=5))
        r.start()
        self.assertTrue(_wait(lambda: r.fatal is not None, 25))
        self.assertIn("cousin MCP server absent", r.fatal)   # the fake binary serves no MCP
        seen = json.loads(dump.read_text())
        for name in self.PLANTED:
            if name not in ("HOME", "XDG_DATA_HOME"):
                self.assertNotIn(name, seen)
        self.assertEqual(seen["HOME"], str(r.account.data_dir))
        self.assertEqual(seen["OPENCODE_CONFIG"], str(r.account.data_dir / "opencode.runner.json"))
        self.assertTrue(_wait(lambda: not r.worker_alive()))
        self.assertFalse(r._server.alive())                  # stopped when the start failed


class TestGuards(OpencodeCase):
    def test_a_bridge_marker_is_refused_before_the_runner_exists(self):
        with self.assertRaises(RunnerError) as err:
            self.runner(home=self.home(model="meridian/opus"))
        self.assertIn("config model names the Claude-subscription bridge", str(err.exception))
        os.environ["PATH"] = "/opt/meridian/bin" + os.pathsep + os.environ["PATH"]
        with self.assertRaises(RunnerError) as err:
            self.runner()
        self.assertIn("environment variable PATH", str(err.exception))

    def test_a_bridge_marker_at_start_refuses_the_start(self):
        r = self.runner()
        os.environ["LC_MERIDIAN"] = "1"
        r.start()
        self.assertTrue(_wait(lambda: r.fatal is not None))
        self.assertIn("names the Claude-subscription bridge", r.fatal)
        self.assertEqual(self.factory.calls, [])            # no server was started
        self.assertEqual(r.state(), "errored")
        self.assertTrue(_wait(lambda: not r.worker_alive()))

    def test_an_mcp_server_opencode_cannot_reach_refuses_every_turn(self):
        r = self.runner(factory=Factory(mcp={"cousin": {"status": "needs_auth"}}))
        a = r.enqueue(_op("hello"))
        r.start()
        self.assertTrue(_wait(lambda: r.fatal is not None))
        self.assertIn("cousin MCP server needs_auth", r.fatal)
        self.assertTrue(_wait(lambda: not r.worker_alive()))
        errors = self.payloads(r, "error")
        self.assertTrue(errors and errors[-1]["fatal"])
        self.assertEqual(r.state(), "errored")
        self.assertEqual(r.inbox.get(a.inbox_id)["state"], "queued")
        self.assertEqual(self.prompts(), [])
        self.assertTrue(self.factory.servers[0].stopped)


class TestTurns(OpencodeCase):
    def test_a_turn_emits_its_events_and_closes_its_row(self):
        r = self.started(self.runner([[("reasoning", "let me look"),
                                       ("tool", "cousin_memory", {"command": "search"}, "found"),
                                       ("text", "all done here")]]))
        a = r.enqueue(_op("look it up"))
        self.assertTrue(_wait(lambda: self.outcome(r, a) is not None))
        self.assertEqual(self.outcome(r, a), "delivered")
        kinds = [k for k in self.kinds(r) if k in ("turn_start", "thinking", "tool",
                                                   "tool_result", "text", "result")]
        self.assertEqual(kinds, ["turn_start", "thinking", "tool", "tool_result", "text",
                                 "result"])
        tool = self.payloads(r, "tool")[0]
        self.assertEqual((tool["name"], tool["input"]), ("cousin_memory", {"command": "search"}))
        self.assertEqual(self.payloads(r, "tool_result")[0],
                         {"tool_use_id": tool["id"], "is_error": False, "text": "found"})
        self.assertEqual(self.payloads(r, "text"), [{"text": "all done here"}])   # no deltas
        self.assertEqual(self.payloads(r, "thinking")[0]["text"], "let me look")
        result = self.payloads(r, "result")[-1]
        self.assertEqual((result["inbox_ids"], result["requeued"], result["interrupted"],
                          result["is_error"]), ([a.inbox_id], [], False, False))
        self.assertEqual(result["session_id"], r.opencode_session)
        self.assertEqual(result["usage"]["total"], 14)
        self.assertEqual(result["num_turns"], 2)          # the tool step, then the answer
        self.assertTrue(_wait(lambda: r.state() == "idle"))
        echo = self.payloads(r, "user")[0]
        self.assertEqual(echo["echo_of"], a.inbox_id)

    def test_the_prompt_body_is_exactly_parts_system_and_model(self):
        """R8, R22: the composed prompt (opencode's tool names) as `system`,
        the named model, the envelope as the only part."""
        r = self.started(self.runner())
        a = r.enqueue(_op("hi there"))
        self.assertTrue(_wait(lambda: self.outcome(r, a) is not None))
        body = self.prompts()[0]["body"]
        self.assertEqual(sorted(body), ["model", "parts", "system"])
        self.assertEqual(body["model"], {"providerID": "local", "modelID": "m1"})
        self.assertEqual(len(body["parts"]), 1)
        self.assertEqual(body["parts"][0]["type"], "text")
        self.assertTrue(body["parts"][0]["text"].startswith("[operator:priya] chat from Priya"))
        self.assertIn("hi there", body["parts"][0]["text"])
        self.assertIn("`cousin_reply`", body["system"])
        self.assertNotIn("mcp__cousin__", body["system"])

    def test_an_operator_message_mid_turn_is_folded_into_the_run(self):
        r = self.started(self.runner([[("SLOW", 1.0), ("text", "first")], [("text", "second")]]))
        a = r.enqueue(_op("first"))
        self.assertTrue(_wait(lambda: r.state() == "running"))
        b = r.enqueue(_op("second, mid-turn", "sam"))
        self.assertTrue(_wait(lambda: len(self.prompts()) == 2))
        self.assertTrue(_wait(lambda: self.outcome(r, b) is not None, 8))
        time.sleep(0.3)
        results = self.payloads(r, "result")
        self.assertEqual(len(results), 1, "one idle closes both (R14')")
        self.assertEqual(sorted(results[0]["inbox_ids"]), sorted([a.inbox_id, b.inbox_id]))
        self.assertEqual((self.outcome(r, a), self.outcome(r, b)), ("delivered", "delivered"))
        self.assertEqual(r.turn.active, False)

    def test_a_peer_message_mid_turn_waits_for_its_own_turn(self):
        r = self.started(self.runner([[("SLOW", 0.8)]]))
        a = r.enqueue(_op("first"))
        self.assertTrue(_wait(lambda: r.state() == "running"))
        peer = r.enqueue(Item("peer:testa", "chat", "from a peer", sender="Testa"))
        time.sleep(0.4)
        self.assertEqual(len(self.prompts()), 1, "held, not sent into the busy run")
        self.assertTrue(_wait(lambda: len(self.payloads(r, "result")) == 2, 8))
        self.assertEqual([x["inbox_ids"] for x in self.payloads(r, "result")],
                         [[a.inbox_id], [peer.inbox_id]])

    def test_an_interrupt_aborts_and_requeues_what_the_abort_dropped(self):
        """R14': an abort drops the prompt queued behind the running one;
        the runner sends it again in a turn of its own."""
        r = self.started(self.runner([[("HANG",)], [("text", "queued answer")]]))
        a = r.enqueue(_op("hang"))
        self.assertTrue(_wait(lambda: r.state() == "running"))
        b = r.enqueue(_op("queued behind", "sam"))
        self.assertTrue(_wait(lambda: len(self.prompts()) == 2))
        self.assertTrue(_wait(lambda: any(e.get("echo_of") == b.inbox_id
                                          for e in self.payloads(r, "user"))))
        stop = r.enqueue(Item("system", "interrupt", "stop it", sender="Priya"))
        self.assertTrue(_wait(lambda: self.outcome(r, stop) is not None))
        self.assertEqual(self.outcome(r, stop), "delivered")
        self.assertTrue(_wait(lambda: self.outcome(r, b) is not None, 8))
        results = self.payloads(r, "result")
        self.assertEqual(results[0]["inbox_ids"], [a.inbox_id])
        self.assertEqual(results[0]["requeued"], [b.inbox_id])
        self.assertTrue(results[0]["interrupted"])
        self.assertFalse(results[0]["is_error"])
        self.assertEqual(self.outcome(r, a), "delivered", "the model received it")
        self.assertEqual(results[1]["inbox_ids"], [b.inbox_id])
        self.assertFalse(results[1]["interrupted"])
        self.assertEqual(self.outcome(r, b), "delivered")
        self.assertEqual(len(self.prompts()), 3)            # b sent again
        self.assertEqual(self.factory.fake.aborts, [r.opencode_session])

    def test_interrupt_in_process_aborts_the_running_turn_only(self):
        r = self.started(self.runner([[("HANG",)]]))
        self.assertFalse(r.interrupt())
        a = r.enqueue(_op("hang"))
        self.assertTrue(_wait(lambda: r.state() == "running"))
        self.assertTrue(r.interrupt())
        self.assertTrue(_wait(lambda: self.outcome(r, a) is not None, 3))
        self.assertTrue(self.payloads(r, "result")[-1]["interrupted"])
        self.assertTrue(_wait(lambda: r.state() == "idle"))
        self.assertFalse(r.interrupt())

    def test_the_doubled_idle_after_a_failure_is_ignored(self):
        """A failure emits error, idle, idle, the message, idle, idle (R2'):
        one result for the failed turn, and the next turn is not closed by
        the leftover idles."""
        r = self.started(self.runner([[("FAIL", "UnknownError", "the provider broke")],
                                       [("SLOW", 0.3), ("text", "fine")]]))
        a = r.enqueue(_op("one"))
        self.assertTrue(_wait(lambda: self.outcome(r, a) is not None))
        b = r.enqueue(_op("two"))
        self.assertTrue(_wait(lambda: self.outcome(r, b) is not None, 8))
        self.assertEqual((self.outcome(r, a), self.outcome(r, b)), ("failed", "delivered"))
        results = self.payloads(r, "result")
        self.assertEqual([x["inbox_ids"] for x in results], [[a.inbox_id], [b.inbox_id]])
        self.assertTrue(results[0]["is_error"])
        self.assertIn("the provider broke", results[0]["error"])
        self.assertEqual(self.payloads(r, "text")[-1], {"text": "fine"})
        states = [s["to"] for s in self.payloads(r, "state")]
        self.assertEqual(states[:3], ["running", "errored", "idle"])
        self.assertEqual(len(self.factory.fake.events_since(0)) > 0, True)
        idles = [e for e in self.factory.fake.events if e["type"] == "session.idle"]
        self.assertGreaterEqual(len(idles), 3)             # two for the failure, one for b

    def test_an_auth_error_puts_the_row_back_and_waits_for_the_login(self):
        home = self.home()
        r = self.started(self.runner([[("AUTH_401",)], [("text", "back")]], home=home))
        a = r.enqueue(_op("one"))
        self.assertTrue(_wait(lambda: r.login_required()))
        self.assertEqual(r.state(), "errored")
        self.assertEqual(r.inbox.get(a.inbox_id)["state"], "queued")
        result = self.payloads(r, "result")[-1]
        self.assertEqual((result["inbox_ids"], result["requeued"], result["auth"]),
                         ([], [a.inbox_id], "login_required"))
        signal = self.payloads(r, "auth")[-1]
        self.assertEqual((signal["account"], signal["kind"], signal["reason"]),
                         ("lab", "opencode", "login_required"))
        self.assertIn("HTTP 401", signal["detail"])
        on_file = json.loads((home / "data" / "login-required.json").read_text())
        self.assertEqual(on_file["action"], signal["action"])
        self.assertIn(ENDPOINT, on_file["action"])
        time.sleep(0.5)
        self.assertEqual(len(self.prompts()), 1, "no turn while the login waits")
        (home / "data" / "login-required.json").unlink()   # the operator's manual retry
        self.assertTrue(_wait(lambda: self.outcome(r, a) is not None, 8))
        self.assertEqual(self.outcome(r, a), "delivered")
        self.assertFalse(r.login_required())
        self.assertEqual([p.get("retry") for p in self.payloads(r, "auth")][-2:],
                         ["manual retry", None])
        self.assertTrue(self.payloads(r, "auth")[-1]["restored"])

    def test_classify(self):
        self.assertEqual(opencode.classify({"name": "ProviderAuthError",
                                            "data": {"message": "no"}})[0], "auth")
        self.assertEqual(opencode.classify({"name": "APIError",
                                            "data": {"statusCode": 403}})[0], "auth")
        self.assertEqual(opencode.classify({"name": "APIError",
                                            "data": {"statusCode": 429}})[0], "failed")
        self.assertEqual(opencode.classify({"name": "MessageAbortedError"})[0], "aborted")
        self.assertEqual(opencode.classify(None)[0], "failed")

    def test_a_permission_ask_is_rejected_not_waited_on(self):
        r = self.started(self.runner([[("ASK", "bash", {"command": "rm -rf x"}, "ran")]]))
        a = r.enqueue(_op("go"))
        self.assertTrue(_wait(lambda: self.outcome(r, a) is not None))
        self.assertEqual(self.outcome(r, a), "delivered")
        self.assertIn("asked permission for bash", self.payloads(r, "error")[-1]["error"])
        self.assertTrue(self.payloads(r, "tool_result")[-1]["is_error"])

    def test_a_reconnected_stream_that_missed_the_idle_settles_the_turn(self):
        r = self.runner([[("SLOW", 0.2), ("text", "done in the gap")]])
        r.reader_backoff_s = 1.0
        self.started(r)
        a = r.enqueue(_op("go"))
        self.assertTrue(_wait(lambda: r.state() == "running"))
        self.factory.fake.drop_event_streams()
        self.assertTrue(_wait(lambda: self.outcome(r, a) is not None, 8))
        self.assertEqual(self.outcome(r, a), "delivered")
        gap = [p for p in self.payloads(r, "system") if p.get("subtype") == "event_gap"]
        self.assertEqual(gap, [{"subtype": "event_gap", "settled": True}])

    def test_stop_during_a_turn_is_bounded_and_stops_everything(self):
        r = self.started(self.runner([[("HANG",)]]))
        a = r.enqueue(_op("hang"))
        self.assertTrue(_wait(lambda: r.state() == "running"))
        mcp, reader = r._mcp, r._reader
        t = time.monotonic()
        r.stop(timeout=5)
        self.assertLess(time.monotonic() - t, 3.0)
        self.assertEqual(r.state(), "stopped")
        self.assertFalse(r.worker_alive())
        self.assertTrue(self.factory.servers[0].stopped)
        self.assertIsNone(mcp.url)
        self.assertFalse(reader.is_alive())
        self.assertEqual(self.outcome(r, a), "delivered")     # aborted: the model received it
        r.stop(timeout=5)
        self.assertEqual(r.state(), "stopped")


class TestSession(OpencodeCase):
    def test_a_fresh_session_with_state_starts_with_the_digest(self):
        home = self.home()
        (home / "STATUS.md").write_text("# Status\n\nworking on the widget\n")
        r = self.started(self.runner(home=home))
        self.assertTrue(_wait(lambda: len(self.payloads(r, "result")) == 1))
        first = self.prompts()[0]["body"]["parts"][0]["text"]
        self.assertIn("STATE DIGEST FOR COUSIN: wren", first)
        on_file = json.loads((home / "data" / "runner-session.json").read_text())
        self.assertEqual((on_file["session_id"], on_file["lane"]), (r.opencode_session, "opencode"))

    def test_a_restart_resumes_the_session_opencode_still_holds(self):
        home, shared = self.home(), Shared()
        one = self.started(self.runner(home=home, factory=Factory(shared=shared)))
        sid = one.opencode_session
        one.stop(timeout=5)
        self.addCleanup(shared.fake.close)
        two = self.started(self.runner(home=home, factory=Factory(shared=shared)))
        self.assertEqual(two.opencode_session, sid)
        self.assertTrue(_wait(lambda: {"subtype": "resumed", "session_id": sid}
                              in self.payloads(two, "system")))
        self.assertEqual([q for q in shared.fake.requests if q["path"] == "/session"
                          and q["method"] == "POST"].__len__(), 1)
        self.assertEqual(self.prompts(), [], "a resumed session gets no digest")

    def test_a_session_on_file_that_is_gone_or_not_opencodes_starts_fresh(self):
        home = self.home()
        for lane, subtype in (("opencode", "resume_failed"), ("sdk", None)):
            with self.subTest(lane=lane):
                (home / "data" / "runner-session.json").write_text(json.dumps(
                    {"session_id": "ses_gone", "lane": lane}))
                r = self.started(self.runner(home=home))
                self.assertNotEqual(r.opencode_session, "ses_gone")
                self.assertTrue(_wait(lambda: len(self.prompts()) == 1))
                subtypes = [p["subtype"] for p in self.payloads(r, "system")]
                self.assertEqual(subtype in subtypes, subtype is not None)
                self.assertIn("fresh", subtypes)
                # a session on file is state to carry: the digest goes first
                self.assertIn("STATE DIGEST", self.prompts()[0]["body"]["parts"][0]["text"])
                r.stop(timeout=5)


if __name__ == "__main__":
    unittest.main()
