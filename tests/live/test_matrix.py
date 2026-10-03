"""The behaviour matrix of the locked harness: what the Agent SDK, its
CLI and opencode DO, not the arguments the framework passes them.

Opt in: COUSIN_LIVE=1. Each test runs real model turns on this host's
default account (its login; the shell's auth variables are put aside
while a turn runs), so it needs a login and spends a few small turns.
Never in public CI (tests/test_harness_lock.py checks no workflow sets
the variable). Run it before any bump of config/harness.lock.toml, on
the versions the bump names, with `COUSIN_LIVE=1 python -m tests.live`:
that prints the "tested with" block for the pull request.

0. what is installed is what the lock names (else the rest tests
   something else);
1. a session started with tools=[] and no MCP server reports exactly
   EXPECTED_INIT_TOOLS in its init message (a connector or a tool that
   attaches anyway is red; COUSIN_LIVE_INJECT_TOOL=<tool> starts it
   with that tool, which must turn this item red);
2. a turn with thinking on: the usage keys usage.py reads are there,
   and thinking is inside output_tokens (no separate key usage.py would
   miss);
3. a session's transcript file grows with each turn and holds the
   prompt;
4. every model in [models] claude answers one smallest turn on the
   locked CLI (a CLI too old for a model gives an API 400);
5. opencode: `--version` is the lock's, and one turn on
   [models] opencode_default returns text (OPENCODE_BIN, else
   COUSIN_OPENCODE_BIN, else `opencode` on PATH).

Item 6, the README's bare-host quick start on a clean venv, is a manual
step (docs/development.md, "The harness lock").

COUSIN_LIVE_MODEL picks the model of items 1-3 (default: the lock's
first, the catalogue's default)."""
import asyncio
import os
import shutil
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from cousin_lib import accounts, harness_lock
from tests._hermetic import HermeticCase

LIVE = os.environ.get("COUSIN_LIVE") == "1"
SKIP = ("live harness matrix: set COUSIN_LIVE=1 (a login and a few model turns;"
        " python -m tests.live)")
# What the init message lists for a session with tools=[] and no MCP
# server, on the locked CLI. A bump whose CLI adds one on purpose
# changes it here, in the same pull request.
EXPECTED_INIT_TOOLS = frozenset()
TURN_S = 180.0
# the item each test is, for the "tested with" block (tests/live/__main__.py)
ITEMS = {
    "test_0_installed_is_the_lock": "0 installed = lock",
    "test_1_init_tool_list": "1 init tool list",
    "test_2_usage_shape_with_thinking": "2 usage shape (thinking on)",
    "test_3_transcript_grows_with_the_prompt": "3 submit / transcript",
    "test_4_every_locked_model_answers": "4 every locked model answers",
    "test_5_opencode_version_and_one_turn": "5 opencode version + one turn",
}


def _sdk():
    try:
        import claude_agent_sdk
    except ImportError:
        raise unittest.SkipTest("claude-agent-sdk is not installed (pip install -e '.[sdk]')")
    return claude_agent_sdk


class _Live(HermeticCase):
    def setUp(self):
        self.lock = harness_lock.load()
        tmp = tempfile.TemporaryDirectory(prefix="cousin-live-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / "cousins" / "wren"
        for sub in ("data", "run", "memory"):
            (self.home / sub).mkdir(parents=True)
        (self.root / "config").mkdir()
        (self.home / "cousin.toml").write_text(
            '[cousin]\nslug = "wren"\nname = "Wren"\n\n[agent]\nrunner = "sdk"\n')
        self.cwd = Path(tempfile.mkdtemp(prefix="cousin-live-cwd-"))
        self.addCleanup(shutil.rmtree, self.cwd, True)

    def model(self):
        return os.environ.get("COUSIN_LIVE_MODEL") or self.lock["models"]["claude"][0]

    def account(self):
        return accounts.for_cousin(self.home, self.root)

    def options(self, **kw):
        sdk = _sdk()
        base = dict(cwd=str(self.cwd), model=self.model(),
                    env=accounts.account_env(self.account(), self.root),
                    setting_sources=[], tools=[], mcp_servers={}, max_turns=1,
                    extra_args={"strict-mcp-config": None})
        base.update(kw)
        return sdk.ClaudeAgentOptions(**base)

    def session(self, options, prompts, after_each=None):
        """Every message of each prompt's turn, one client for all of
        them; after_each(i, messages) runs once each turn's result is in."""
        from cousin_lib.runner.sdk import _ScrubbedAuthEnv
        sdk = _sdk()

        async def run():
            client = sdk.ClaudeSDKClient(options=options)
            await client.connect()
            turns = []
            try:
                for i, prompt in enumerate(prompts):
                    await client.query(prompt)
                    got = [m async for m in client.receive_response()]
                    turns.append(got)
                    if after_each:
                        after_each(i, got)
            finally:
                await client.disconnect()
            return turns

        with _ScrubbedAuthEnv():
            return asyncio.run(asyncio.wait_for(run(), TURN_S * len(prompts)))

    def result_of(self, messages):
        sdk = _sdk()
        results = [m for m in messages if isinstance(m, sdk.ResultMessage)]
        self.assertEqual(len(results), 1, messages)
        self.assertFalse(results[0].is_error, results[0])
        return results[0]


@unittest.skipUnless(LIVE, SKIP)
class TestClaudeMatrix(_Live):
    def test_0_installed_is_the_lock(self):
        _sdk()
        got = harness_lock.check("sdk")
        self.assertTrue(got["ok"], got["message"])

    def test_1_init_tool_list(self):
        sdk = _sdk()
        inject = os.environ.get("COUSIN_LIVE_INJECT_TOOL")
        options = self.options(tools=[inject] if inject else [],
                               extra_args={"strict-mcp-config": None,
                                           "no-session-persistence": None})
        [messages] = self.session(options, ["Reply only OK."])
        inits = [m.data for m in messages
                 if isinstance(m, sdk.SystemMessage) and m.subtype == "init"]
        self.assertEqual(len(inits), 1, messages)
        tools = set(inits[0].get("tools") or ())
        self.assertEqual(tools, set(EXPECTED_INIT_TOOLS),
                         "init tools beyond the expected set: %s"
                         % sorted(tools - set(EXPECTED_INIT_TOOLS)))
        self.assertEqual(list(inits[0].get("mcp_servers") or ()), [], inits[0])
        self.result_of(messages)

    def test_2_usage_shape_with_thinking(self):
        sdk = _sdk()
        from cousin_lib import usage
        options = self.options(thinking={"type": "enabled", "budget_tokens": 1024},
                               extra_args={"strict-mcp-config": None,
                                           "no-session-persistence": None})
        [messages] = self.session(options, [
            "Think it through step by step, then reply with only the number: 17 * 23 + 9"])
        result = self.result_of(messages)
        used = dict(result.usage or {})
        for key in usage.USAGE_KEYS:
            self.assertIn(key, used, used)
            self.assertIsInstance(used[key], int, (key, used))
        apart = sorted(k for k in used if "thinking" in k or "reasoning" in k)
        self.assertEqual(apart, [], "a thinking count of its own, which usage._totals does"
                                    " not add: decide how the accounting reads it (%r)" % used)
        self.assertGreater(used["output_tokens"], 0, used)
        row = usage.record(self.home, client_id="live", session_id=result.session_id,
                           result={"usage": used, "total_cost_usd": result.total_cost_usd},
                           lane="login")
        self.assertNotIn("error", row, row)
        self.assertEqual(row["total"], sum(used[k] for k in usage.USAGE_KEYS))

    def test_3_transcript_grows_with_the_prompt(self):
        sdk = _sdk()
        env = accounts.account_env(self.account(), self.root)
        config_dir = Path(env.get("CLAUDE_CONFIG_DIR") or os.environ.get("CLAUDE_CONFIG_DIR")
                          or Path.home() / ".claude")
        project = config_dir / "projects" / sdk.project_key_for_directory(self.cwd)
        self.addCleanup(shutil.rmtree, project, True)      # the turns' transcript, nothing else
        markers = ["kestrel-%s" % uuid.uuid4().hex[:8], "testa-%s" % uuid.uuid4().hex[:8]]
        sizes = []

        def measure(i, messages):
            sid = self.result_of(messages).session_id
            path = project / ("%s.jsonl" % sid)
            deadline = time.monotonic() + 10      # the CLI may append just after the result
            while time.monotonic() < deadline and not (
                    path.is_file() and markers[i] in path.read_text(errors="replace")):
                time.sleep(0.2)
            self.assertTrue(path.is_file(), "no transcript at %s" % path)
            self.assertIn(markers[i], path.read_text(encoding="utf-8", errors="replace"))
            sizes.append(path.stat().st_size)

        self.session(self.options(), ["Reply only OK. Marker: %s" % m for m in markers],
                     after_each=measure)
        self.assertEqual(len(sizes), 2)
        self.assertGreater(sizes[1], sizes[0])

    def test_4_every_locked_model_answers(self):
        _sdk()
        from cousin_lib.runner import sdk as runner_sdk
        for model in self.lock["models"]["claude"]:
            with self.subTest(model=model):
                rc, line = runner_sdk.validate_account(self.account(), self.root, model=model,
                                                       timeout=TURN_S)
                self.assertEqual(rc, 0, "%s: %s" % (model, line))


def _opencode_bin():
    named = os.environ.get("OPENCODE_BIN") or os.environ.get("COUSIN_OPENCODE_BIN")
    return named or shutil.which("opencode")


@unittest.skipUnless(LIVE, SKIP)
class TestOpencodeMatrix(_Live):
    def test_5_opencode_version_and_one_turn(self):
        binary = _opencode_bin()
        if not binary:
            self.skipTest("no opencode binary (OPENCODE_BIN, COUSIN_OPENCODE_BIN or PATH)")
        got = harness_lock.check("opencode", {"opencode_bin": binary})
        self.assertTrue(got["ok"], got["message"])
        from cousin_lib import delivery
        from cousin_lib.delivery import Item
        from cousin_lib.runner.main import runner_for
        (self.root / "config" / "accounts.toml").write_text(
            '[accounts.zen]\nkind = "opencode"\nproviders = ["opencode"]\n')
        (self.home / "cousin.toml").write_text(
            '[cousin]\nslug = "wren"\nname = "Wren"\n\n[operator]\nname = "Priya"\n\n'
            '[agent]\nrunner = "opencode"\naccount = "zen"\nmodel = "%s"\nopencode_bin = "%s"\n'
            % (self.lock["models"]["opencode_default"], binary))
        r = runner_for(self.home)
        self.addCleanup(r.stop, timeout=30)
        r.start()
        out = delivery.deliver(self.home, Item("operator:priya", "chat",
                                               "Reply with the single word: pomegranate",
                                               sender="Priya"), wait=True, timeout=TURN_S)
        texts = [e["payload"].get("text") or "" for e in r.events() if e["kind"] == "text"]
        self.assertEqual(out, delivery.DELIVERED, [e["kind"] for e in r.events()])
        self.assertTrue(any(t.strip() for t in texts), texts)


if __name__ == "__main__":
    unittest.main()
