"""Per-cousin auth mode: the harness's own login (the default) or an
API key from the cousin's key file.

The properties pinned here are the ones a billing mistake hides:
the key reaches the agent only through its environment (a fake agent
prints its argv and environment; tmux's argv is logged), the default
mode strips the key and config-dir variables, a bad key file refuses
the mode, the isolated harness config holds no login and no account
block, a login found there refuses the launch, a mid-turn agent is not
restarted, and the mode survives a start and a flip.

The harness here is invented (kestrel): its variable names, config dir
and settings file come from a test harness.toml, as they do in
production from the shipped preset.
"""
import contextlib
import io
import json
import os
import pathlib
import stat
import sys
import tarfile
import tempfile
import tomllib
import unittest
from unittest import mock

from cousin_lib import agent_auth
from cousin_lib.agent_auth import (AUTH_MODES, DEFAULT_MODE, MODE_API_KEY,
                                   MODE_LOGIN, AgentBusy, AuthError)

KEY = "kst-test-0123456789abcdefWXYZ"

_FAKE_TMUX = """#!%s
import os, subprocess, sys
args = sys.argv[1:]
with open(os.environ["FAKE_TMUX_LOG"], "a") as fh:
    fh.write(" ".join(args) + "\\n")
sub = next((a for a in args if a in ("has-session", "kill-session",
            "capture-pane", "new-session", "send-keys", "list-panes")), "")
if sub == "has-session":
    sys.exit(int(os.environ.get("FAKE_TMUX_HAS", "1")))
if sub == "capture-pane":
    try:
        sys.stdout.write(open(os.environ["FAKE_TMUX_PANE"]).read())
    except OSError:
        pass
if sub == "new-session":
    # Run the session's command the way tmux would, synchronously, so
    # the fake agent has written its report when start_cousin returns.
    i = args.index("/usr/bin/env")
    subprocess.run(args[i:], check=False)
sys.exit(0)
""" % sys.executable

_FAKE_AGENT = """#!%s
import json, os, sys
with open(os.environ["FAKE_AGENT_OUT"], "w") as fh:
    json.dump({"argv": sys.argv, "env": dict(os.environ)}, fh)
""" % sys.executable

_HARNESS = """
busy_patterns = ["esc to interrupt"]

[agent]
default_model = "m1"

[agent.resume]
session_arg = "--session-id {session_id}"
resume_arg = "--resume {session_id}"

[auth.api_key]
key_env = "KESTREL_KEY"
config_dir_env = "KESTREL_CONFIG_DIR"
source_dir = "%(src)s"
settings_file = "%(settings)s"
settings_name = ".kestrel.json"
exclude = [".credentials.json", "stats-cache.json"]
strip_settings_keys = ["oauthAccount", "userID"]
preserve_settings_keys = ["keyApprovals"]
login_files = [".credentials.json"]
login_settings_keys = ["oauthAccount"]
"""


class AuthCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.home = self.root / "cousins" / "wren"
        (self.home / "data").mkdir(parents=True)
        (self.home / "memory").mkdir()
        (self.home / "cousin.toml").write_text(
            '[cousin]\nslug = "wren"\nname = "Wren"\n'
            '[chat]\nport = 8100\ntmux_session = "wren"\n')
        (self.home / "STATUS.md").write_text("# Wren\n\n## Open loops\n")
        (self.root / "config").mkdir()
        self.src = self.root / "kestrel-home" / ".kestrel"
        self.src.mkdir(parents=True)
        for name in (".credentials.json", "stats-cache.json",
                     "settings.json"):
            (self.src / name).write_text("{}")
        (self.src / "projects").mkdir()
        self.settings = self.root / "kestrel-home" / ".kestrel.json"
        self.settings.write_text(json.dumps({
            "oauthAccount": {"emailAddress": "someone@example.invalid"},
            "userID": "u-1", "projects": {"/x": {"trusted": True}},
            "theme": "dark"}))
        (self.root / "config" / "harness.toml").write_text(
            _HARNESS % {"src": self.src, "settings": self.settings})
        self.tmux = self.root / "tmux"
        self.tmux.write_text(_FAKE_TMUX)
        self.tmux.chmod(0o755)
        self.agent = self.root / "fake-agent"
        self.agent.write_text(_FAKE_AGENT)
        self.agent.chmod(0o755)
        self.log = self.root / "tmux.log"
        self.pane = self.root / "pane.txt"
        self.out = self.root / "agent-out.json"
        (self.root / "config" / "agent-cmd").write_text(
            "%s --model {model} --session-id {session_id}\n" % self.agent)
        patcher = mock.patch.dict(os.environ, {
            "FRAMEWORK_ROOT": str(self.root),
            "FAKE_TMUX_LOG": str(self.log),
            "FAKE_TMUX_PANE": str(self.pane),
            "FAKE_AGENT_OUT": str(self.out),
            "FAKE_TMUX_HAS": "1",
        })
        patcher.start()
        self.addCleanup(patcher.stop)

    def cfg(self):
        return agent_auth.api_key_config(self.root)

    def write_key(self, text=KEY):
        return agent_auth.write_key(self.home, text, "KESTREL_KEY")

    def start(self, **kw):
        from cousin_lib.spawn import start_cousin
        kw.setdefault("agent_cmd", (self.root / "config" / "agent-cmd")
                      .read_text().strip())
        start_cousin(self.home, tmux_bin=str(self.tmux), root=self.root,
                     start_chat_server=lambda home: None, **kw)
        return json.loads(self.out.read_text())


class TestModeNames(AuthCase):
    def test_the_two_modes_and_the_default(self):
        self.assertEqual(AUTH_MODES, ("claude", "api_key"))
        self.assertEqual(DEFAULT_MODE, MODE_LOGIN)
        self.assertEqual(agent_auth.read_mode(self.home), "claude")

    def test_an_unknown_mode_in_cousin_toml_is_loud(self):
        with open(self.home / "cousin.toml", "a") as fh:
            fh.write('\n[runtime]\nauth = "company"\n')
        with self.assertRaises(AuthError):
            agent_auth.read_mode(self.home)

    def test_persist_keeps_the_rest_of_runtime(self):
        with open(self.home / "cousin.toml", "a") as fh:
            fh.write('\n[runtime]\nmodel = "m2"\n')
        agent_auth.persist_mode(self.home, MODE_API_KEY)
        data = tomllib.loads((self.home / "cousin.toml").read_text())
        self.assertEqual(data["runtime"], {"model": "m2", "auth": "api_key"})

    def test_no_other_module_spells_the_mode_names(self):
        # Renaming a mode is a one-file change: the names are spelled in
        # agent_auth.py only (the console page reads them from the API).
        pkg = pathlib.Path(agent_auth.__file__).parent
        for path in list(pkg.rglob("*.py")) + list(pkg.rglob("*.jsx")):
            if path.name == "agent_auth.py":
                continue
            text = path.read_text(encoding="utf-8")
            self.assertNotIn('"api_key"', text, path)
            self.assertNotIn("'api_key'", text, path)


class TestKeyFile(AuthCase):
    def test_write_is_0600_in_a_0700_dir_with_its_own_gitignore(self):
        state = self.write_key("KESTREL_KEY=" + KEY + "\n")
        path = agent_auth.key_file(self.home)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertEqual((path.parent / ".gitignore").read_text(), "*\n")
        self.assertEqual(path.read_text(), "KESTREL_KEY=%s\n" % KEY)
        self.assertEqual(state, {"set": True, "last4": "WXYZ",
                                 "error": None})
        self.assertEqual(agent_auth.read_key(self.home, "KESTREL_KEY"), KEY)

    def test_missing_empty_open_and_foreign_files_refuse(self):
        with self.assertRaisesRegex(AuthError, "does not exist"):
            agent_auth.read_key(self.home, "KESTREL_KEY")
        self.write_key()
        path = agent_auth.key_file(self.home)
        path.write_text("KESTREL_KEY=\n")
        with self.assertRaisesRegex(AuthError, "empty"):
            agent_auth.read_key(self.home, "KESTREL_KEY")
        path.write_text("OTHER_KEY=%s\n" % KEY)
        with self.assertRaisesRegex(AuthError, "variable other than"):
            agent_auth.read_key(self.home, "KESTREL_KEY")
        path.write_text("KESTREL_KEY=%s\n" % KEY)
        path.chmod(0o640)
        with self.assertRaisesRegex(AuthError, "group or others") as ctx:
            agent_auth.read_key(self.home, "KESTREL_KEY")
        self.assertNotIn(KEY, str(ctx.exception))
        path.chmod(0o600)
        path.parent.chmod(0o755)
        with self.assertRaisesRegex(AuthError, "chmod 700"):
            agent_auth.read_key(self.home, "KESTREL_KEY")
        path.parent.chmod(0o700)
        path.unlink()
        other = self.root / "elsewhere.env"
        other.write_text("KESTREL_KEY=%s\n" % KEY)
        other.chmod(0o600)
        path.symlink_to(other)
        with self.assertRaises(AuthError):
            agent_auth.read_key(self.home, "KESTREL_KEY")

    def test_a_malformed_paste_is_refused_before_anything_is_written(self):
        for bad in ("", "   ", "two words", "a\nb"):
            with self.assertRaises(AuthError):
                self.write_key(bad)
        self.assertFalse(agent_auth.key_file(self.home).exists())

    def test_key_state_never_carries_the_key(self):
        self.write_key()
        state = agent_auth.key_state(self.home, "KESTREL_KEY")
        self.assertNotIn(KEY, json.dumps(state))
        self.write_key("short-key")
        self.assertIsNone(
            agent_auth.key_state(self.home, "KESTREL_KEY")["last4"])


class TestIsolatedDir(AuthCase):
    def test_links_all_but_the_login_and_strips_the_account_block(self):
        cfg = self.cfg()
        out = agent_auth.build_isolated_dir(cfg)
        iso = cfg["isolated_dir"]
        self.assertEqual(iso, self.root / "data"
                         / "harness-api-key-config")
        self.assertEqual(stat.S_IMODE(iso.stat().st_mode), 0o700)
        self.assertEqual(sorted(out["linked"]), ["projects", "settings.json"])
        self.assertFalse(os.path.lexists(iso / ".credentials.json"))
        self.assertFalse(os.path.lexists(iso / "stats-cache.json"))
        self.assertEqual(os.readlink(iso / "projects"),
                         str(self.src / "projects"))
        copy = json.loads((iso / ".kestrel.json").read_text())
        self.assertNotIn("oauthAccount", copy)
        self.assertNotIn("userID", copy)
        self.assertEqual(copy["theme"], "dark")
        self.assertEqual(stat.S_IMODE(
            (iso / ".kestrel.json").stat().st_mode), 0o600)
        agent_auth.check_isolated_dir(cfg)

    def test_a_rebuild_keeps_preserved_keys_and_drops_stale_links(self):
        cfg = self.cfg()
        agent_auth.build_isolated_dir(cfg)
        iso = cfg["isolated_dir"]
        copy = json.loads((iso / ".kestrel.json").read_text())
        copy["keyApprovals"] = ["accepted"]
        (iso / ".kestrel.json").write_text(json.dumps(copy))
        (self.src / "settings.json").unlink()
        agent_auth.build_isolated_dir(cfg)
        self.assertFalse(os.path.lexists(iso / "settings.json"))
        copy = json.loads((iso / ".kestrel.json").read_text())
        self.assertEqual(copy["keyApprovals"], ["accepted"])

    def test_a_login_file_without_the_login_key_is_not_a_login(self):
        # Canary (live, 2026-09-18): the harness itself wrote a
        # .credentials.json into the isolated dir holding only plugin
        # MCP sign-in state; treating mere existence as a login would
        # refuse the cousin's next start. With login_file_keys set, only
        # a file carrying one of those keys (or an unreadable one) counts.
        (self.root / "config" / "harness.toml").write_text(
            (self.root / "config" / "harness.toml").read_text()
            + 'login_file_keys = ["kestrelOauth"]\n')
        cfg = self.cfg()
        agent_auth.build_isolated_dir(cfg)
        iso = cfg["isolated_dir"]
        (iso / ".credentials.json").write_text(json.dumps(
            {"mcpOAuth": {"plugin|1": {"accessToken": ""}}}))
        agent_auth.check_isolated_dir(cfg)
        (iso / ".credentials.json").write_text(json.dumps(
            {"kestrelOauth": {"accessToken": "t"}}))
        with self.assertRaisesRegex(AuthError, "holds a login"):
            agent_auth.check_isolated_dir(cfg)
        (iso / ".credentials.json").write_text("not json")
        with self.assertRaisesRegex(AuthError, "holds a login"):
            agent_auth.check_isolated_dir(cfg)

    def test_a_login_in_the_isolated_dir_refuses(self):
        cfg = self.cfg()
        agent_auth.build_isolated_dir(cfg)
        iso = cfg["isolated_dir"]
        (iso / ".credentials.json").write_text("{}")
        with self.assertRaisesRegex(AuthError, "holds a login"):
            agent_auth.check_isolated_dir(cfg)
        (iso / ".credentials.json").unlink()
        copy = json.loads((iso / ".kestrel.json").read_text())
        copy["oauthAccount"] = {}
        (iso / ".kestrel.json").write_text(json.dumps(copy))
        with self.assertRaisesRegex(AuthError, "account keys"):
            agent_auth.check_isolated_dir(cfg)


class TestAgentEnvironment(AuthCase):
    def test_claude_mode_strips_the_key_and_the_config_dir(self):
        env = agent_auth.agent_env(self.home, self.root, {
            "KESTREL_KEY": "leaked", "KESTREL_CONFIG_DIR": "/x",
            "PATH": "/bin"})
        self.assertEqual(env, {"PATH": "/bin"})

    def test_api_key_mode_hands_over_key_and_isolated_dir(self):
        self.write_key()
        agent_auth.build_isolated_dir(self.cfg())
        agent_auth.persist_mode(self.home, MODE_API_KEY)
        env = agent_auth.agent_env(self.home, self.root, {"PATH": "/bin"})
        self.assertEqual(env["KESTREL_KEY"], KEY)
        self.assertEqual(env["KESTREL_CONFIG_DIR"],
                         str(self.cfg()["isolated_dir"]))

    def test_api_key_mode_without_configuration_refuses(self):
        (self.root / "config" / "harness.toml").write_text("")
        agent_auth.persist_mode(self.home, MODE_API_KEY)
        with self.assertRaisesRegex(AuthError, r"\[auth.api_key\]"):
            agent_auth.agent_env(self.home, self.root, {})


class TestLaunchThroughTmux(AuthCase):
    """start_cousin -> tmux -> launcher -> agent, with a fake tmux that
    runs the session command and a fake agent that reports its argv and
    environment."""

    def test_api_key_reaches_the_env_and_never_an_argv(self):
        self.write_key()
        agent_auth.build_isolated_dir(self.cfg())
        agent_auth.persist_mode(self.home, MODE_API_KEY)
        report = self.start()
        self.assertEqual(report["env"]["KESTREL_KEY"], KEY)
        self.assertEqual(report["env"]["KESTREL_CONFIG_DIR"],
                         str(self.cfg()["isolated_dir"]))
        self.assertNotIn(KEY, " ".join(report["argv"]))
        self.assertIn("--model", report["argv"])
        log = self.log.read_text()
        self.assertIn("new-session", log)
        self.assertIn("agent_launch.py", log)
        self.assertNotIn(KEY, log)

    def test_claude_mode_strips_an_inherited_key(self):
        with mock.patch.dict(os.environ, {"KESTREL_KEY": KEY,
                                          "KESTREL_CONFIG_DIR": "/tmp/x"}):
            report = self.start()
        self.assertNotIn("KESTREL_KEY", report["env"])
        self.assertNotIn("KESTREL_CONFIG_DIR", report["env"])

    def test_a_bad_key_file_refuses_the_start_before_tmux(self):
        from cousin_lib.spawn import SpawnError
        agent_auth.build_isolated_dir(self.cfg())
        agent_auth.persist_mode(self.home, MODE_API_KEY)
        with self.assertRaisesRegex(SpawnError, "auth: no key file"):
            self.start()
        self.assertFalse(self.log.exists())

    def test_the_launcher_refuses_on_its_own_too(self):
        # The check binds at exec time, not only in the preflight: a
        # key file removed between the two still stops the agent.
        agent_auth.persist_mode(self.home, MODE_API_KEY)
        import subprocess
        r = subprocess.run(
            [sys.executable, str(agent_auth.LAUNCHER), "--home",
             str(self.home), "--root", str(self.root), "--",
             str(self.agent)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 78)
        self.assertIn("refused", r.stderr)
        self.assertFalse(self.out.exists())


class TestResume(AuthCase):
    def test_the_session_arg_is_swapped_for_the_resume_arg(self):
        from cousin_lib.spawn import SpawnError, resume_agent_cmd
        self.assertEqual(
            resume_agent_cmd("a --model {model} --session-id {session_id}",
                             self.root, "abc-123"),
            "a --model {model} --resume abc-123")
        with self.assertRaises(SpawnError):
            resume_agent_cmd("a --sid {session_id}", self.root, "abc-123")
        with self.assertRaises(SpawnError):
            resume_agent_cmd("a --session-id {session_id}", self.root,
                             "x; rm")


class TestSwitch(AuthCase):
    def running(self, pane):
        os.environ["FAKE_TMUX_HAS"] = "0"
        self.pane.write_text(pane)
        with open(self.home / "cousin.toml", "a") as fh:
            fh.write('\n[runtime]\nsession_id = "11111111-2222"\n')

    def switch(self, mode, **kw):
        return agent_auth.switch(self.root, "wren", mode,
                                 tmux_bin=str(self.tmux),
                                 start_chat_server=lambda home: None, **kw)

    def test_a_stopped_cousin_just_changes_mode(self):
        self.write_key()
        out = self.switch(MODE_API_KEY)
        self.assertFalse(out["restarted"])
        self.assertEqual(agent_auth.read_mode(self.home), MODE_API_KEY)
        self.assertTrue(self.cfg()["isolated_dir"].is_dir())

    def test_switching_to_api_key_without_a_key_changes_nothing(self):
        with self.assertRaises(AuthError):
            self.switch(MODE_API_KEY)
        self.assertEqual(agent_auth.read_mode(self.home), MODE_LOGIN)

    def test_a_mid_turn_agent_is_not_restarted(self):
        self.write_key()
        self.running("* Cogitating... (12s . esc to interrupt)\n")
        before = (self.home / "cousin.toml").read_text()
        with self.assertRaises(AgentBusy):
            self.switch(MODE_API_KEY)
        self.assertEqual((self.home / "cousin.toml").read_text(), before)
        self.assertNotIn("kill-session", self.log.read_text())

    def test_force_restarts_a_busy_agent(self):
        self.write_key()
        self.running("* Cogitating... (12s . esc to interrupt)\n")
        out = self.switch(MODE_API_KEY, force=True)
        self.assertTrue(out["restarted"])

    def test_an_idle_agent_restarts_on_the_same_session(self):
        self.write_key()
        self.running("* Worked for 7m 28s\n> \n")
        out = self.switch(MODE_API_KEY)
        self.assertTrue(out["restarted"])
        self.assertEqual(out["session_id"], "11111111-2222")
        log = self.log.read_text()
        self.assertIn("kill-session", log)
        report = json.loads(self.out.read_text())
        self.assertIn("--resume", report["argv"])
        self.assertIn("11111111-2222", report["argv"])
        self.assertNotIn("--session-id", report["argv"])
        self.assertEqual(report["env"]["KESTREL_KEY"], KEY)
        self.assertNotIn(KEY, log)
        # And back: claude mode, same session, the key gone.
        self.switch(MODE_LOGIN)
        report = json.loads(self.out.read_text())
        self.assertNotIn("KESTREL_KEY", report["env"])
        self.assertIn("11111111-2222", report["argv"])

    def test_no_resume_rule_refuses_before_anything_changes(self):
        self.write_key()
        self.running("idle\n")
        text = (self.root / "config" / "harness.toml").read_text()
        (self.root / "config" / "harness.toml").write_text(
            text.replace("[agent.resume]", "[unused]"))
        with self.assertRaisesRegex(AuthError, "cannot resume"):
            self.switch(MODE_API_KEY)
        self.assertEqual(agent_auth.read_mode(self.home), MODE_LOGIN)
        out = self.switch(MODE_API_KEY, restart=False)
        self.assertFalse(out["restarted"])
        self.assertEqual(agent_auth.read_mode(self.home), MODE_API_KEY)


class TestModeSurvivesStartAndFlip(AuthCase):
    def test_a_plain_start_reads_the_persisted_mode(self):
        self.write_key()
        self.assertFalse(agent_auth.switch(
            self.root, "wren", MODE_API_KEY, tmux_bin=str(self.tmux))
            ["restarted"])
        report = self.start()
        self.assertEqual(report["env"]["KESTREL_KEY"], KEY)

    def test_a_flip_respawns_in_the_same_mode(self):
        from cousin_lib.flip import flip
        self.write_key()
        agent_auth.switch(self.root, "wren", MODE_API_KEY,
                          tmux_bin=str(self.tmux))
        from cousin_lib import spawn
        real = spawn.start_cousin

        def no_chat_server(home, **kw):
            kw["start_chat_server"] = lambda h: None
            return real(home, **kw)
        with mock.patch("cousin_lib.flip.start_cousin", no_chat_server):
            out = flip("wren", tmux_bin=str(self.tmux), handoff_deadline=1,
                       halfway=0.4, settle=0,
                       which=lambda name: "/bin/" + name)
        self.assertTrue(out["ok"], out)
        report = json.loads(self.out.read_text())
        self.assertEqual(report["env"]["KESTREL_KEY"], KEY)
        self.assertEqual(agent_auth.read_mode(self.home), MODE_API_KEY)

    def test_a_flip_refuses_before_the_kill_when_the_key_is_gone(self):
        from cousin_lib.flip import flip
        self.write_key()
        agent_auth.switch(self.root, "wren", MODE_API_KEY,
                          tmux_bin=str(self.tmux))
        agent_auth.key_file(self.home).unlink()
        out = flip("wren", tmux_bin=str(self.tmux), handoff_deadline=1,
                   halfway=0.4, settle=0, which=lambda name: "/bin/" + name)
        self.assertFalse(out["ok"])
        self.assertIn("auth:", out["error"])
        self.assertFalse(self.log.exists()
                         and "kill-session" in self.log.read_text())


class TestSecretsStayHome(AuthCase):
    def test_the_dismiss_archive_leaves_the_key_out(self):
        from cousin_lib.spawn import dismiss_cousin
        self.write_key()
        out = dismiss_cousin(self.root, slug="wren",
                             stop=lambda home, **kw: None)
        with tarfile.open(out["archive"]) as tf:
            names = tf.getnames()
        self.assertIn("wren/cousin.toml", names)
        self.assertFalse([n for n in names if ".secrets" in n], names)

    def test_git_ignores_a_home_secrets_dir_anywhere(self):
        repo = pathlib.Path(__file__).resolve().parents[1]
        lines = (repo / ".gitignore").read_text().splitlines()
        self.assertIn(".secrets/", lines)


class TestCli(AuthCase):
    def run_cli(self, argv, stdin_text=None):
        out, err = io.StringIO(), io.StringIO()
        stdin = io.StringIO(stdin_text or "")
        with contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err), \
                mock.patch("sys.stdin", stdin):
            rc = agent_auth.auth_main(argv + ["--root", str(self.root)])
        return rc, out.getvalue(), err.getvalue()

    def test_show_key_stdin_and_switch(self):
        rc, out, _ = self.run_cli(["wren"])
        self.assertEqual(rc, 0)
        self.assertIn("auth claude", out)
        self.assertIn("key: not set", out)
        rc, out, _ = self.run_cli(["wren", "--key-stdin"], KEY + "\n")
        self.assertEqual(rc, 0, out)
        self.assertIn("ends WXYZ", out)
        self.assertNotIn(KEY, out)
        rc, out, _ = self.run_cli(["wren", "api_key", "--no-restart"])
        self.assertEqual(rc, 0)
        self.assertIn("claude -> api_key", out)
        rc, out, _ = self.run_cli(["wren"])
        self.assertIn("auth api_key", out)
        self.assertNotIn(KEY, out)

    def test_a_refusal_is_rc_1(self):
        rc, _out, err = self.run_cli(["wren", "api_key"])
        self.assertEqual(rc, 1)
        self.assertIn("no key file", err)


if __name__ == "__main__":
    unittest.main()
