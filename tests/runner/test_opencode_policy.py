"""The plugin pack and the SSE-side recording on the opencode lane (phase 9
Task 6; R9, R10; Review Focus 3): the policy the runner renders for
plugins/opencode/cousin-policy.js, the refusal to start when the plugin is
not in force, one tool-name form (the SDK lane's) for the policy and the
recorder, recording with arguments, `task` parts as subagent jobs, the
checkpoints, and an endpoint account's context limit. The default suite
drives the fake `opencode serve`; the plugin itself runs under node when a
`node` binary is on PATH (skipped otherwise); the live proof runs the real
binary against a loopback fake provider (opt-in: COUSIN_LIVE_OPENCODE=1 and
OPENCODE_BIN)."""
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path

from cousin_lib import accounts
from cousin_lib.runner import opencode
from cousin_lib.runner import policy as policy_mod
from cousin_lib.runner.opencode import OpencodeRunner
from cousin_lib.runner.policy import Policy
from tests._hermetic import HermeticCase
from tests.runner._home import temp_home
from tests.runner.test_opencode import Factory, OpencodeCase, _op, _wait
from tests.test_accounts import AccountsCase

NODE = shutil.which("node")
REPO = Path(__file__).resolve().parents[2]
POLICY_TOML = ('deny_tools = ["WebFetch", "mcp__cousin__send*"]\n'
               'deny_bash_patterns = ["rm\\\\s+-rf", "^sudo "]\n'
               'ask = ["Edit"]\n')
RM_REASON = "policy.toml: deny_bash_patterns 'rm\\\\s+-rf' matches"


def _policy_home(case, text=POLICY_TOML, **kw):
    home = case.home(**kw)
    (home / "policy.toml").write_text(text)
    return home


class TestNames(unittest.TestCase):
    def test_opencode_names_map_to_the_sdk_form(self):
        """One form for the policy and the recorder: the SDK lane's."""
        for oc, sdk in (("bash", "Bash"), ("read", "Read"), ("edit", "Edit"),
                        ("write", "Write"), ("glob", "Glob"), ("grep", "Grep"),
                        ("task", "Agent"), ("webfetch", "WebFetch"),
                        ("websearch", "WebSearch"), ("todowrite", "TodoWrite"),
                        ("skill", "Skill"), ("cousin_reply", "mcp__cousin__reply"),
                        ("cousin_memory", "mcp__cousin__memory"),
                        ("apply_patch", "apply_patch"), ("question", "question"),
                        ("", ""), (None, "")):
            with self.subTest(oc=oc):
                self.assertEqual(opencode.sdk_tool_name(oc), sdk)
        self.assertEqual(opencode.tool_name("reply"), "cousin_reply")
        self.assertEqual(opencode.sdk_tool_name(opencode.tool_name("handoff")),
                         policy_mod.OWN_TOOL_PREFIX + "handoff")


class TestRenderedPolicy(OpencodeCase):
    def test_the_policy_is_rendered_for_the_plugin_at_every_start(self):
        r = self.started(self.runner(home=_policy_home(self)))
        data = r.account.data_dir
        path = data / "cousin-policy.json"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        rendered = json.loads(path.read_text())
        self.assertEqual(rendered["version"], 1)
        self.assertEqual(rendered["file"], policy_mod.FILE)
        self.assertEqual(rendered["deny_tools"], ["WebFetch", "mcp__cousin__send*"])
        self.assertEqual(rendered["deny_bash_patterns"], [
            {"source": "rm\\s+-rf", "reason": RM_REASON},
            {"source": "^sudo ", "reason": "policy.toml: deny_bash_patterns '^sudo ' matches"}])
        self.assertEqual(rendered["ask"], ["Edit"])
        self.assertEqual(rendered["own_tool_prefix"], policy_mod.OWN_TOOL_PREFIX)
        self.assertEqual(rendered["names"], opencode.SDK_NAMES)
        self.assertEqual(rendered["prefixes"], {"cousin_": "mcp__cousin__"})
        self.assertEqual(rendered["ack"], str(data / "cousin-policy.ack.json"))
        self.assertEqual(self.factory.calls[0]["env"]["COUSIN_POLICY_FILE"], str(path))
        ack = json.loads((data / "cousin-policy.ack.json").read_text())
        self.assertEqual(ack["nonce"], rendered["nonce"])
        loaded = [p for p in self.payloads(r, "system") if p.get("subtype") == "policy_plugin"]
        self.assertEqual(loaded, [{"subtype": "policy_plugin", "plugin": opencode.PLUGIN.as_uri(),
                                   "deny_tools": 2, "deny_bash_patterns": 2, "ask": 1,
                                   "errors": []}])
        first = rendered["nonce"]
        r.stop(timeout=5)
        r2 = self.started(self.runner(home=r.home, factory=Factory()))
        self.assertNotEqual(json.loads(path.read_text())["nonce"], first)   # a fresh one
        self.assertIsNone(r2.fatal)

    def test_the_models_shell_gets_the_cousins_home_and_not_the_servers_secrets(self):
        """Review Important 4: opencode's bash inherits the server's
        environment (HOME and XDG in the account's data dir, where auth.json
        and the rendered config with the MCP token live; the server's
        password). opencode merges the plugin's `shell.env` answer over that
        environment, so the runner renders what to set: HOME to the cousin's
        home, the XDG variables, the password and the config path empty, and
        USER, LOGNAME and the variables `[agent] shell_env` names, from the
        runner's own environment."""
        os.environ.update(SSH_AUTH_SOCK="/run/user/1000/agent.sock", USER="sam",
                          LOGNAME="sam")
        os.environ.pop("GPG_AGENT_INFO", None)
        home = self.home(extra='shell_env = ["SSH_AUTH_SOCK", "GPG_AGENT_INFO"]\n')
        r = self.started(self.runner(home=home))
        rendered = json.loads((r.account.data_dir / "cousin-policy.json").read_text())
        self.assertEqual(rendered["shell_env"], {
            "HOME": str(home), "USER": "sam", "LOGNAME": "sam",
            "SSH_AUTH_SOCK": "/run/user/1000/agent.sock",           # GPG_AGENT_INFO is unset
            "XDG_CONFIG_HOME": "", "XDG_DATA_HOME": "", "XDG_CACHE_HOME": "",
            "XDG_STATE_HOME": "", "OPENCODE_SERVER_PASSWORD": "", "OPENCODE_CONFIG": "",
            "COUSIN_POLICY_FILE": ""})

    def test_shell_env_names_what_the_runner_owns_or_a_secret_is_refused(self):
        for bad in ('"HOME"', '"XDG_DATA_HOME"', '"OPENCODE_SERVER_PASSWORD"', '"OPENCODE_X"',
                    '"ANTHROPIC_API_KEY"', '"bad-name"', "3",
                    # review round 2, minor 6: a secret by its name's shape
                    '"DB_PASSWORD"', '"AWS_SECRET_ACCESS_KEY"', '"CLIENT_SECRET"',
                    '"MY_SECRET_THING"', '"SIGNING_KEY"', '"GH_TOKEN"'):
            with self.subTest(bad=bad):
                with self.assertRaises(opencode.RunnerError) as err:
                    self.runner(home=self.home(extra="shell_env = [%s]\n" % bad))
                self.assertIn("shell_env", str(err.exception))
        with self.assertRaises(opencode.RunnerError):
            self.runner(home=self.home(extra='shell_env = "SSH_AUTH_SOCK"\n'))
        self.runner(home=self.home(extra='shell_env = ["SSH_AUTH_SOCK", "KEYBOARD_LAYOUT"]\n'))

    def test_no_policy_file_renders_an_empty_policy(self):
        r = self.started(self.runner())
        rendered = json.loads((r.account.data_dir / "cousin-policy.json").read_text())
        self.assertEqual((rendered["deny_tools"], rendered["deny_bash_patterns"],
                          rendered["ask"], rendered["source"]), ([], [], [], "none"))

    def test_the_runner_refuses_to_start_when_the_plugin_is_not_loaded(self):
        """Review Focus 3. opencode lists a configured plugin in GET /config
        whether or not it loaded (measured: a missing file, a syntax error
        and a throwing init all listed, and a denied command ran), so the
        runner needs both the listing and the plugin's acknowledgement of
        THIS start's policy file."""
        cases = (("unlisted", "does not list the policy plugin"),
                 ("absent", "the policy plugin did not load"),
                 ("fatal", "the policy plugin cannot apply the policy"))
        for mode, needle in cases:
            with self.subTest(mode=mode):
                home = _policy_home(self)
                r = self.runner(home=home, factory=Factory(plugin=mode))
                r.plugin_timeout_s = 0.5
                a = r.enqueue(_op("hello"))
                r.start()
                self.assertTrue(_wait(lambda: r.fatal is not None, 10))
                self.assertIn(needle, r.fatal)
                self.assertIn("no turn runs", r.fatal)
                self.assertTrue(_wait(lambda: not r.worker_alive()))
                self.assertEqual(r.state(), "errored")
                self.assertEqual(r.inbox.get(a.inbox_id)["state"], "queued")
                self.assertEqual(self.prompts(), [])
                self.assertTrue(self.factory.servers[0].stopped)
                self.assertTrue(self.payloads(r, "error")[-1]["fatal"])

    def test_an_acknowledgement_from_an_earlier_start_does_not_count(self):
        home = _policy_home(self)
        r = self.runner(home=home, factory=Factory(plugin="absent"))
        r.plugin_timeout_s = 0.5
        stale = r.account.data_dir / "cousin-policy.ack.json"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text(json.dumps({"nonce": "an-earlier-start", "fatal": None, "errors": []}))
        r.start()
        self.assertTrue(_wait(lambda: r.fatal is not None, 10))
        self.assertIn("the policy plugin did not load", r.fatal)

    def test_a_pattern_the_plugin_cannot_compile_is_said(self):
        """The plugin reports the patterns JavaScript refused (it then denies
        every command); the runner says so as an error, and runs."""
        home = _policy_home(self)
        r = self.runner(home=home, factory=Factory(plugin="absent"))

        def plugin():
            self.assertTrue(_wait(lambda: self.factory.calls, 10))
            rendered = json.loads((r.account.data_dir / "cousin-policy.json").read_text())
            Path(rendered["ack"]).write_text(json.dumps({
                "nonce": rendered["nonce"], "fatal": None, "deny_tools": 2,
                "deny_bash_patterns": 2, "ask": 1,
                "errors": [{"source": "^sudo ", "error": "planted"}]}))
        t = threading.Thread(target=plugin)
        t.start()
        self.started(r)
        t.join(10)
        self.assertIsNone(r.fatal)
        errors = [e["error"] for e in self.payloads(r, "error")]
        self.assertTrue(any("'^sudo '" in e and "every command" in e for e in errors), errors)


class TestRecording(OpencodeCase):
    def test_a_tool_call_is_recorded_with_its_arguments(self):
        """R10: a part's `running` state carries the arguments; the recorder
        gets the SDK lane's hook payloads, the tool in the SDK form."""
        seen = []
        args = {"command": "ls -la", "description": "list the home"}
        r = self.started(self.runner([[("tool", "bash", args, "a\nb"), ("text", "done")]],
                                     recorder=seen.append))
        a = r.enqueue(_op("look around"))
        self.assertTrue(_wait(lambda: self.outcome(r, a) is not None))
        call = self.payloads(r, "tool")[0]["id"]
        self.assertEqual([(p["hook_event_name"], p["tool_name"], p["tool_input"],
                           p["tool_use_id"]) for p in seen],
                         [("PreToolUse", "Bash", args, call), ("PostToolUse", "Bash", args, call)])
        self.assertEqual(seen[1]["tool_response"], "a\nb")
        self.assertEqual({p["session_id"] for p in seen}, {r.opencode_session})
        self.assertEqual({p["opencode_tool"] for p in seen}, {"bash"})

    def test_the_default_recorder_writes_the_activity_line_with_the_arguments(self):
        r = self.started(self.runner([[("tool", "bash", {"command": "git status --short",
                                                         "description": "state"}, "M x"),
                                       ("tool", "read", {"filePath": "/srv/notes.md"}, "text"),
                                       ("text", "done")]]))
        a = r.enqueue(_op("status?"))
        self.assertTrue(_wait(lambda: self.outcome(r, a) is not None))
        log = r.home / "data" / "activity" / ("%s.log" % datetime.now().strftime("%Y-%m-%d"))
        self.assertTrue(_wait(log.exists))
        lines = log.read_text().splitlines()
        self.assertEqual(len(lines), 2, lines)
        self.assertRegex(lines[0], r"Bash\s+ok\s+git status --short # state$")
        self.assertRegex(lines[1], r"Read\s+ok\s+/srv/notes.md$")

    def test_a_denied_call_is_a_policy_event_and_leaves_no_pre_record(self):
        """The plugin threw: the part went running -> error. The runner says
        it as the SDK lane's `policy` event and records the failure (with
        the arguments), but no PreToolUse: a call the policy denies never
        ran (the SDK lane's recorder_for rule)."""
        seen = []
        args = {"command": "rm -rf /srv/x", "description": "clean"}
        r = self.started(self.runner(
            [[("tool_error", "bash", args, opencode.DENIED + RM_REASON), ("text", "ok, not that")]],
            home=_policy_home(self), recorder=seen.append))
        a = r.enqueue(_op("clean up"))
        self.assertTrue(_wait(lambda: self.outcome(r, a) is not None))
        self.assertEqual(self.outcome(r, a), "delivered")
        self.assertEqual([p for p in self.payloads(r, "policy") if "decision" in p], [
            {"tool": "Bash", "decision": "deny", "reason": RM_REASON, "opencode_tool": "bash"}])
        self.assertEqual([p["hook_event_name"] for p in seen], ["PostToolUseFailure"])
        self.assertEqual((seen[0]["tool_input"], seen[0]["error"]), (args, opencode.DENIED + RM_REASON))
        self.assertFalse(seen[0]["is_interrupt"])

    def test_a_task_tool_part_is_a_subagent_job(self):
        """R10: opencode's subagent tool is `task`; its part is a jobs row,
        started on running, done or failed on the outcome."""
        from cousin_lib import jobs
        r = self.started(self.runner([[
            ("tool", "task", {"description": "survey the logs", "prompt": "look at data/",
                              "subagent_type": "general"}, "found two warnings"),
            ("tool_error", "task", {"description": "second look", "prompt": "again",
                                    "subagent_type": "general"}, "the subagent failed"),
            ("text", "done")]]))
        a = r.enqueue(_op("survey"))
        self.assertTrue(_wait(lambda: self.outcome(r, a) is not None))
        rows = {j["title"]: j for j in jobs.list_jobs()}
        self.assertEqual((rows["survey the logs"]["kind"], rows["survey the logs"]["status"],
                          rows["survey the logs"]["result_summary"],
                          rows["survey the logs"]["spawned_by"]),
                         ("subagent", "done", "found two warnings", "wren"))
        self.assertEqual((rows["second look"]["status"], rows["second look"]["result_summary"]),
                         ("failed", "the subagent failed"))
        log = Path(rows["survey the logs"]["log_path"]).read_text()
        self.assertIn("look at data/", log)
        self.assertIn("found two warnings", log)

    def test_a_recorder_failure_is_a_hook_event_not_a_dead_turn(self):
        def broken(payload):
            raise OSError("jobs.db is locked")
        r = self.started(self.runner([[("tool", "bash", {"command": "true"}, ""), ("text", "ok")]],
                                     recorder=broken))
        a = r.enqueue(_op("go"))
        self.assertTrue(_wait(lambda: self.outcome(r, a) is not None))
        self.assertEqual(self.outcome(r, a), "delivered")
        hooks = self.payloads(r, "hook")
        self.assertEqual([h["event"] for h in hooks], ["PreToolUse", "PostToolUse"])
        self.assertIn("OSError: jobs.db is locked", hooks[0]["error"])


class TestCheckpoints(OpencodeCase):
    def test_every_turn_ends_with_a_session_checkpoint(self):
        """The SDK lane's Stop hook: data/session-checkpoint.md at a turn's end."""
        r = self.started(self.runner())
        a = r.enqueue(_op("hi"))
        self.assertTrue(_wait(lambda: self.outcome(r, a) is not None))
        path = r.home / "data" / "session-checkpoint.md"
        self.assertTrue(_wait(path.exists))
        self.assertTrue(path.read_text().startswith("# Session checkpoint - wren - "))
        self.assertTrue(_wait(lambda: {"kind": "session", "path": str(path)}
                              in self.payloads(r, "checkpoint")))     # after the file

    def test_a_compaction_writes_the_pre_compact_checkpoint_and_asks_for_a_rollover(self):
        """R10: `session.compacted` -> the pre-compact checkpoint, and (the SDK
        lane's PreCompact rule) a rollover at the next turn boundary."""
        r = self.started(self.runner([[("text", "long answer"), ("COMPACT",)]]))
        a = r.enqueue(_op("keep going"))
        self.assertTrue(_wait(lambda: self.outcome(r, a) is not None))
        path = r.home / "data" / "pre-compact-checkpoint.md"
        self.assertTrue(_wait(path.exists))
        self.assertIn("# Pre-compaction checkpoint - wren - ", path.read_text())
        self.assertTrue(_wait(lambda: {"kind": "pre_compact", "path": str(path),
                                       "trigger": "session.compacted"}
                              in self.payloads(r, "checkpoint")))
        requested = lambda: [p["reason"] for p in self.payloads(r, "rollover")
                             if p.get("phase") == "requested"]
        self.assertTrue(_wait(lambda: requested() == ["compacted"]), requested())


class TestEndpointContext(AccountsCase):
    BASE = ('[accounts.local]\nkind = "opencode"\nendpoint = "http://127.0.0.1:11434/v1"\n'
            'endpoint_model = "qwen3-coder"\n')

    def render(self, account):
        return opencode.render_config(account, model="local/qwen3-coder",
                                      small_model="local/qwen3-coder",
                                      mcp_url="http://127.0.0.1:9/mcp", mcp_token="t")

    def test_endpoint_context_is_rendered_as_the_models_limit(self):
        """Task 5b: an endpoint model has no limit, so context pressure is off;
        `endpoint_context` names it (and opencode's own overflow check sees it)."""
        for extra, limit in (("endpoint_context = 65536\n", {"context": 65536, "output": 16384}),
                             ("endpoint_context = 262144\n", {"context": 262144, "output": 32000}),
                             ("endpoint_context = 8192\nendpoint_output = 2048\n",
                              {"context": 8192, "output": 2048})):
            with self.subTest(extra=extra):
                self.write(self.BASE + extra)
                acc = accounts.load(self.root)["local"]
                model = self.render(acc)["provider"]["local"]["models"]["qwen3-coder"]
                self.assertEqual(model, {"name": "qwen3-coder", "limit": limit})
        self.write(self.BASE)
        model = self.render(accounts.load(self.root)["local"])["provider"]["local"]["models"]
        self.assertEqual(model, {"qwen3-coder": {"name": "qwen3-coder"}})     # no limit, as before

    def test_endpoint_context_fail_closed_naming_the_key(self):
        for extra, needle in (("endpoint_context = 0\n", "endpoint_context"),
                              ("endpoint_context = -5\n", "endpoint_context"),
                              ('endpoint_context = "64k"\n', "endpoint_context"),
                              ("endpoint_context = 1.5\n", "endpoint_context"),
                              ("endpoint_context = true\n", "endpoint_context"),
                              ("endpoint_output = 2048\n", "endpoint_output"),
                              ("endpoint_context = 4096\nendpoint_output = 4096\n",
                               "endpoint_output")):
            with self.subTest(extra=extra):
                self.write(self.BASE + extra)
                with self.assertRaises(accounts.AccountsError) as err:
                    accounts.load(self.root)
                self.assertIn(needle, str(err.exception))
        self.write('[accounts.keyed]\nkind = "opencode"\nproviders = ["openai"]\n'
                   'endpoint_context = 4096\n')
        with self.assertRaises(accounts.AccountsError) as err:
            accounts.load(self.root)
        self.assertIn("endpoint_context goes with endpoint", str(err.exception))


class TestPluginFile(unittest.TestCase):
    def test_the_plugin_imports_nothing(self):
        """opencode must install nothing for the pack: no import, no require."""
        text = opencode.PLUGIN.read_text()
        code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("//"))
        self.assertIsNone(re.search(r"^\s*import\b|\bimport\s*\(|\brequire\s*\(", code, re.M))
        self.assertIn("export default {", code)
        self.assertEqual(len(re.findall(r"^export\b", code, re.M)), 1)   # the default only

    def test_the_image_carries_the_plugin_pack(self):
        from tests.test_docker_files import _DOCKERIGNORE, _dockerignored
        patterns = [l.strip() for l in _DOCKERIGNORE.read_text().splitlines()
                    if l.strip() and not l.strip().startswith("#")]
        for path in ("plugins/opencode/cousin-policy.js", "plugins/opencode/README.md"):
            self.assertFalse(_dockerignored(path, patterns), path)


DRIVER = r"""
const mod = (await import(process.argv[2])).default;
const hooks = await mod.server({}, undefined);
const out = [];
for (const [tool, args] of JSON.parse(process.argv[3])) {
  try {
    await hooks["tool.execute.before"]({tool, sessionID: "ses_x", callID: "call_x"}, {args});
    out.push(["allow", null]);
  } catch (err) {
    out.push(["threw", err.message]);
  }
}
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(NODE, "no node on PATH: the plugin's own test needs a JavaScript runtime")
class TestPluginUnderNode(HermeticCase):
    """The plugin itself, run by node (opencode runs it under Bun): its
    decisions against Policy.decide on the same policy, its acknowledgement,
    its failure modes. No opencode binary."""

    CASES = [["bash", {"command": "rm -rf /srv/x"}], ["bash", {"command": "sudo reboot"}],
             ["bash", {"command": "ls -la"}], ["bash", {"command": "echo sudo "}],
             ["cousin_reply", {"command": "rm -rf x", "text": "a"}],
             ["cousin_send", {"to": "sam"}], ["cousin_send_later", {}],
             ["cousin_memory", {"command": "search"}], ["webfetch", {"url": "https://x"}],
             ["edit", {"filePath": "/x"}], ["read", {"filePath": "/x"}],
             ["task", {"prompt": "p", "command": "rm -rf y"}], ["apply_patch", {"patchText": "x"}],
             ["WebFetch", {}], ["", {}], ["bash", None]]

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.driver = self.dir / "driver.mjs"
        self.driver.write_text(DRIVER)

    def run_plugin(self, policy_text, cases, *, env_file=True):
        home = self.dir / "home"
        home.mkdir(exist_ok=True)
        (home / "policy.toml").write_text(policy_text)
        rendered = opencode.render_policy(Policy.load(home), nonce="n-1",
                                          ack=self.dir / "ack.json")
        path = self.dir / "cousin-policy.json"
        path.write_text(json.dumps(rendered))
        env = {"PATH": os.environ.get("PATH", "")}
        if env_file:
            env["COUSIN_POLICY_FILE"] = str(path)
        done = subprocess.run([NODE, str(self.driver), opencode.PLUGIN.as_uri(), json.dumps(cases)],
                              env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        return home, json.loads(done.stdout)

    def test_the_plugin_decides_as_policy_decide(self):
        home, out = self.run_plugin(POLICY_TOML, self.CASES)
        policy = Policy.load(home)
        for (tool, args), got in zip(self.CASES, out):
            decision, reason = policy.decide(opencode.sdk_tool_name(tool), args or {})
            want = ["allow", None] if decision == "allow" else ["threw", opencode.DENIED + reason + (
                " (no operator approval surface yet: ask is enforced as deny)"
                if decision == "ask" else "")]
            with self.subTest(tool=tool, args=args):
                self.assertEqual(got, want)
        self.assertEqual([o[0] for o in out].count("threw"), 8)       # the table is not all-allow
        ack = json.loads((self.dir / "ack.json").read_text())
        self.assertEqual((ack["nonce"], ack["fatal"], ack["deny_tools"], ack["deny_bash_patterns"],
                          ack["ask"], ack["errors"]), ("n-1", None, 2, 2, 1, []))
        self.assertEqual(stat.S_IMODE((self.dir / "ack.json").stat().st_mode), 0o600)

    def test_a_pattern_javascript_cannot_compile_denies_every_command(self):
        """Python's `(?P<name>...)` is not JavaScript: fail closed on commands."""
        _home, out = self.run_plugin('deny_bash_patterns = ["(?P<verb>rm) "]\n',
                                     [["bash", {"command": "ls"}], ["read", {"filePath": "/x"}]])
        self.assertEqual(out[0][0], "threw")
        self.assertIn('deny_bash_patterns "(?P<verb>rm) " is not a valid JavaScript RegExp',
                      out[0][1])
        self.assertIn("every command is denied", out[0][1])
        self.assertEqual(out[1], ["allow", None])
        ack = json.loads((self.dir / "ack.json").read_text())
        self.assertEqual([e["source"] for e in ack["errors"]], ["(?P<verb>rm) "])

    def test_without_its_policy_file_the_plugin_denies_everything(self):
        _home, out = self.run_plugin(POLICY_TOML, [["read", {"filePath": "/x"}],
                                                   ["cousin_reply", {"text": "hi"}]],
                                     env_file=False)
        for got in out:
            self.assertEqual(got, ["threw", opencode.DENIED
                                   + "cousin-policy: COUSIN_POLICY_FILE is not set"])
        self.assertFalse((self.dir / "ack.json").exists())       # the runner then refuses


SHELL_DRIVER = r"""
const mod = (await import(process.argv[2])).default;
const hooks = await mod.server({}, undefined);
const output = {env: {}};
await hooks["shell.env"]({cwd: "/x", sessionID: "ses_x", callID: "call_x"}, output);
console.log(JSON.stringify(output.env));
"""


@unittest.skipUnless(NODE, "no node on PATH: the plugin's own test needs a JavaScript runtime")
class TestShellEnvUnderNode(HermeticCase):
    def test_the_plugin_answers_shell_env_with_the_rendered_variables(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        d = Path(tmp.name)
        (d / "driver.mjs").write_text(SHELL_DRIVER)
        home = d / "home"; home.mkdir()
        want = {"HOME": str(home), "SSH_AUTH_SOCK": "/run/a.sock", "XDG_DATA_HOME": "",
                "OPENCODE_SERVER_PASSWORD": ""}
        rendered = opencode.render_policy(Policy.load(home), nonce="n", ack=d / "ack.json",
                                          shell_env=want)
        (d / "p.json").write_text(json.dumps(rendered))
        done = subprocess.run([NODE, str(d / "driver.mjs"), opencode.PLUGIN.as_uri()],
                              env={"PATH": os.environ.get("PATH", ""),
                                   "COUSIN_POLICY_FILE": str(d / "p.json")},
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout), want)


@unittest.skipUnless(os.environ.get("COUSIN_LIVE_OPENCODE") == "1" and os.environ.get("OPENCODE_BIN"),
                     "live opencode: set COUSIN_LIVE_OPENCODE=1 and OPENCODE_BIN")
class TestLivePlugin(HermeticCase):
    """The real opencode binary through OpencodeRunner, a throwaway data
    dir for its HOME and XDG, and a loopback fake OpenAI-compatible provider
    (no credential, no network provider), the runner's shipped bounds: the
    plugin loads from its file:// entry, a denied bash call ends as a tool
    error the model sees, the turn ends at session.idle, the recorder saw the
    call with its arguments, and the account's endpoint_context reaches
    opencode as the model's limit (context pressure on, output bounded)."""

    def test_the_plugin_vetoes_a_call_on_the_real_binary(self):
        from tests.runner._fake_provider import FakeProvider
        provider = FakeProvider([("tool", "bash", {"command": "echo forbidden-thing",
                                                   "description": "live probe"}),
                                 ("text", "after the denial")]).start()
        self.addCleanup(provider.close)
        home = temp_home(self, runner="opencode")
        with open(home / "cousin.toml", "a") as f:
            f.write('model = "local/m1"\nopencode_bin = "%s"\n' % os.environ["OPENCODE_BIN"])
        (home / "policy.toml").write_text('deny_bash_patterns = ["forbidden"]\n')
        data = home.parent.parent / ".secrets" / "accounts" / "live.opencode"
        account = accounts.Account("live", "opencode", None, None, data_dir=data,
                                   endpoint=provider.url, endpoint_model="m1",
                                   endpoint_context=32768)
        r = OpencodeRunner(home, account=account,
                           environ={"PATH": os.defpath + ":/run/current-system/sw/bin"})
        self.addCleanup(lambda: r.stop(timeout=10))
        started = time.monotonic()
        a = r.enqueue(_op("run the probe"))
        r.start()
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline and r.fatal is None:
            row = r.inbox.get(a.inbox_id)
            if row and row["state"] == "done":
                break
            time.sleep(0.5)
        self.assertIsNone(r.fatal)
        events = list(r.events())
        kinds = lambda k: [e["payload"] for e in events if e["kind"] == k]
        self.assertEqual(r.inbox.get(a.inbox_id)["outcome"], "delivered")
        loaded = [p for p in kinds("system") if p.get("subtype") == "policy_plugin"]
        self.assertEqual(len(loaded), 1, kinds("system"))
        results = kinds("tool_result")
        seen = [(e["kind"], json.dumps(e["payload"])[:160]) for e in events]
        asked = [(q["roles"], q["tool_results"]) for q in provider.chats()]
        self.assertTrue(results and results[0]["is_error"], (seen, asked))
        self.assertTrue(results[0]["text"].startswith(opencode.DENIED), results)
        self.assertEqual([p["tool"] for p in kinds("policy") if "decision" in p], ["Bash"])
        tool_msgs = provider.chats()[1]["tool_results"]
        self.assertTrue(tool_msgs and tool_msgs[0]["content"].startswith(opencode.DENIED),
                        tool_msgs)
        result = kinds("result")[-1]
        self.assertFalse(result["is_error"])
        # the limit is read after the turn (after its row closed): wait for it
        self.assertTrue(_wait(lambda: r._limit is not None, 30), "no context limit read")
        self.assertEqual([p["payload"] for p in r.events() if p["kind"] == "system"
                          and p["payload"].get("subtype") == "pressure_off"], [])
        self.assertEqual(r._limit, ("local/m1", 32768))
        self.assertEqual(provider.chats()[0]["body"].get("max_tokens"), 8192)
        self.assertEqual(kinds("text")[-1]["text"], "after the denial")
        log = home / "data" / "activity" / ("%s.log" % datetime.now().strftime("%Y-%m-%d"))
        line = [l for l in log.read_text().splitlines() if "forbidden-thing" in l]
        self.assertEqual(len(line), 1, log.read_text())
        self.assertRegex(line[0], r"Bash\s+FAIL\s+echo forbidden-thing # live probe")
        print("\nLIVE: opencode %s loaded %s; denied call -> %r; model saw %r; turn %.1fs;"
              " activity: %s; provider max_tokens %s"
              % (os.environ["OPENCODE_BIN"], loaded[0]["plugin"], results[0]["text"][:80],
                 tool_msgs[0]["content"][:80], time.monotonic() - started, line[0],
                 provider.chats()[0]["body"].get("max_tokens")))

    def test_the_models_shell_on_the_real_binary(self):
        """Review Important 4, live: what a bash call sees on 1.18.31 with
        the plugin's shell.env answer merged over the server's environment."""
        from tests.runner._fake_provider import FakeProvider
        probe = ("env | grep -E '^(HOME|XDG_[A-Z]+_HOME|OPENCODE_SERVER_PASSWORD|"
                 "OPENCODE_CONFIG|USER|SSH_AUTH_SOCK)=' | sort")
        provider = FakeProvider([("tool", "bash", {"command": probe, "description": "env"}),
                                 ("text", "seen")]).start()
        self.addCleanup(provider.close)
        home = temp_home(self, runner="opencode")
        with open(home / "cousin.toml", "a") as f:
            f.write('model = "local/m1"\nopencode_bin = "%s"\nshell_env = ["SSH_AUTH_SOCK"]\n'
                    % os.environ["OPENCODE_BIN"])
        data = home.parent.parent / ".secrets" / "accounts" / "live.opencode"
        account = accounts.Account("live", "opencode", None, None, data_dir=data,
                                   endpoint=provider.url, endpoint_model="m1")
        r = OpencodeRunner(home, account=account,
                           environ={"PATH": os.defpath + ":/run/current-system/sw/bin",
                                    "USER": "live", "SSH_AUTH_SOCK": "/run/live/agent.sock"})
        self.addCleanup(lambda: r.stop(timeout=10))
        a = r.enqueue(_op("show the env"))
        r.start()
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline and r.fatal is None:
            row = r.inbox.get(a.inbox_id)
            if row and row["state"] == "done":
                break
            time.sleep(0.5)
        self.assertIsNone(r.fatal)
        results = [e["payload"] for e in r.events() if e["kind"] == "tool_result"]
        self.assertTrue(results and not results[0]["is_error"], results)
        seen = dict(line.split("=", 1) for line in results[0]["text"].splitlines() if "=" in line)
        self.assertEqual(seen.get("HOME"), str(home))
        self.assertEqual(seen.get("USER"), "live")
        self.assertEqual(seen.get("SSH_AUTH_SOCK"), "/run/live/agent.sock")
        for name in opencode.SHELL_BLANK:
            self.assertEqual(seen.get(name, ""), "", name)
        print("\nLIVE shell env: %s" % sorted(seen.items()))

    def test_a_detached_process_the_model_started_is_reaped_by_its_marker(self):
        """Review round 2, minor 1, live on 1.18.31. opencode starts the
        model's bash in a session of its own, out of the server's process
        group, and a server killed hard (its runner SIGKILLed, the death
        signal SIGKILLs it) leaves the model's command running: the bash, its
        children and anything it detached (`setsid`). The next start's
        reap_leftover kills them all by the start's marker, which they
        inherited."""
        from tests.runner._fake_provider import FakeProvider
        from cousin_lib.runner import opencode_http
        cmd = "setsid sleep 301 < /dev/null > /dev/null 2>&1 & sleep 300; echo done"
        provider = FakeProvider([("tool", "bash", {"command": cmd, "description": "detach"}),
                                 ("text", "never")]).start()
        self.addCleanup(provider.close)
        home = temp_home(self, runner="opencode")
        with open(home / "cousin.toml", "a") as f:
            f.write('model = "local/m1"\nopencode_bin = "%s"\n' % os.environ["OPENCODE_BIN"])
        data = home.parent.parent / ".secrets" / "accounts" / "live.opencode"
        account = accounts.Account("live", "opencode", None, None, data_dir=data,
                                   endpoint=provider.url, endpoint_model="m1")
        r = OpencodeRunner(home, account=account,
                           environ={"PATH": os.defpath + ":/run/current-system/sw/bin"})
        self.addCleanup(lambda: r.stop(timeout=10))
        r.enqueue(_op("run the detaching one"))
        r.start()

        def procs():
            """{pid: comm} of every live process carrying this start's marker."""
            marker = getattr(r._server, "marker", None)
            if not marker:
                return {}
            entry = ("%s=%s" % (opencode_http.MARKER_ENV, marker)).encode()
            found = {}
            for name in os.listdir("/proc"):
                if not name.isdigit() or int(name) == getattr(r._server, "pid", None):
                    continue
                try:
                    if entry not in Path("/proc/%s/environ" % name).read_bytes().split(b"\0"):
                        continue
                    st = Path("/proc/%s/stat" % name).read_text()
                    if st[st.rindex(")") + 2:].split()[0] != "Z":
                        found[int(name)] = Path("/proc/%s/cmdline" % name).read_bytes()
                except OSError:
                    pass
            return found
        self.assertTrue(_wait(lambda: r._server is not None and any(
            b"301" in c for c in procs().values()), 300), "the detached process never started")
        before = procs()
        pidfile = data / opencode_http.PIDFILE
        self.assertTrue(pidfile.exists())
        # A runner killed with its server never tears down: stand that in by
        # taking the sweep away while the live runner notices and tears down,
        # so what outlives the server is opencode's doing; then the next
        # start's reap, from the pidfile, with the sweep back.
        record = pidfile.read_text()          # a SIGKILLed runner never removes it
        sweep = opencode_http.kill_marked
        opencode_http.kill_marked = lambda *a, **k: []
        try:
            server = r._server
            os.kill(server.pid, signal.SIGKILL)           # the death signal's kill
            self.assertTrue(_wait(lambda: server.proc.poll() is not None, 10))
            time.sleep(2.0)                               # the runner's teardown, sweepless
            after = procs()
        finally:
            opencode_http.kill_marked = sweep
        print("\nLIVE after the server's SIGKILL: %s" % sorted(
            (p, b" ".join(c.split(b"\0")[:3]).decode()) for p, c in after.items()))
        self.assertTrue(after, "nothing outlived the server")
        pidfile.write_text(record)
        killed = opencode_http.reap_leftover(pidfile)
        self.assertTrue(set(after) <= set(killed), (after, killed))
        self.assertTrue(_wait(lambda: procs() == {}, 10), "the reap missed them")
        print("LIVE: %d marked under the turn; reap_leftover killed %s" % (len(before), killed))

if __name__ == "__main__":
    unittest.main()
