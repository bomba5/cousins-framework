"""cousin-migrate's check and its CLI, and the refusal that stands where the
1.x migration was: `plan`, `apply` and `rollback` without `--to` exit 2
before doing anything (the kind switch they run with `--to` is
tests/test_kind_switch.py). `check` measures the exit criterion. Every
live action is injected here: this test touches no live home, no tmux,
no supervisor and no model."""
import contextlib
import importlib.util
import io
import json
import os
import pathlib
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

from cousin_lib import migrate
from cousin_lib.delivery import Item
from cousin_lib.runner.inbox import Inbox
from tests._hermetic import HermeticCase

TOML = ('[cousin]\r\nslug = "wren"\r\nname = "Wren"\r\nrole = "archivist"\r\n\r\n'
        '# the operator\'s own comment stays\r\n[runtime]\r\nmodel = "opus"\r\n')


def _root(case):
    tmp = tempfile.TemporaryDirectory(); case.addCleanup(tmp.cleanup)
    root = pathlib.Path(tmp.name)
    home = root / "cousins" / "wren"
    (home / "data").mkdir(parents=True)
    (root / "config").mkdir()
    (root / "config" / "accounts.toml").write_text(
        '[accounts.team]\nkind = "claude-login"\nconfig_dir = "accounts/team"\n')
    (home / "cousin.toml").write_bytes(TOML.encode())
    os.chmod(home / "cousin.toml", 0o640)
    p = mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": str(root)})
    p.start(); case.addCleanup(p.stop)
    return root, home


class Live:
    """The live actions check and the console's migrate view take, recorded."""

    def __init__(self, *, supervisor=True):
        self.calls = []
        self.supervisor = supervisor

    def kw(self):
        return dict(validator=self.validator,
                    cli_version=lambda: "Claude Code 9.9.9 (bundled with claude-agent-sdk 0.0.1)",
                    supervisor_up=lambda root: self.supervisor)

    def validator(self, account, root, *, model=None, effort=None):
        self.calls.append(("validate", model, effort, account.name))
        return 0, "validate: ok (one model turn answered)"


def _tree(path):
    return sorted(str(p.relative_to(path)) for p in path.rglob("*"))


def _inbox(home, rows):
    """(state, outcome, age_s) rows through the real inbox."""
    inbox = Inbox(home)
    for state, outcome, age in rows:
        rec = inbox.put(Item("operator:priya", "chat", "hello", sender="Priya"))
        with sqlite3.connect(home / "data" / "inbox.db") as db:
            db.execute("UPDATE inbox SET state = ?, outcome = ?, created_at = ? WHERE id = ?",
                       (state, outcome, time.time() - age, rec))


def _stream(home, events):
    (home / "data" / "stream").mkdir(parents=True, exist_ok=True)
    with open(home / "data" / "stream" / "sdk-00000001.jsonl", "w") as fh:
        for i, (kind, payload, age) in enumerate(events, 1):
            fh.write(json.dumps({"seq": i, "ts": time.time() - age, "kind": kind,
                                 "payload": payload}) + "\n")


class TestCheck(HermeticCase):
    def test_check_counts_lost_messages_unrecorded_calls_and_failed_recorders(self):
        root, home = _root(self)
        _inbox(home, [("done", "delivered", 7200), ("done", "failed", 7200),
                      ("queued", None, 7200), ("queued", None, 5)])
        _stream(home, [("tool", {"id": "tu-1", "name": "Bash"}, 7200),
                       ("tool_result", {"tool_use_id": "tu-1"}, 7200),
                       ("tool", {"id": "tu-2", "name": "Bash"}, 7200),
                       ("tool", {"id": "tu-3", "name": "Bash"}, 5),
                       ("hook", {"event": "PostToolUse", "error": "OSError: jobs.db locked"}, 60)])
        c = migrate.check(home, since=0.0, health=lambda h: (True, "answers"))
        self.assertEqual(c["inbox"], {"done": 2, "failed": 1, "open": 2, "stale": 1})
        self.assertEqual((c["tool_calls"], c["unrecorded"]), (3, ["tu-2"]))
        self.assertEqual(c["hook_errors"], ["PostToolUse: OSError: jobs.db locked"])
        self.assertFalse(c["ok"])

    def test_since_leaves_out_what_came_before_it(self):
        root, home = _root(self)
        _inbox(home, [("queued", None, 7200)])
        _stream(home, [("tool", {"id": "tu-1"}, 7200)])
        self.assertTrue(migrate.check(home, since=time.time() - 3600)["ok"])

    def test_an_unreadable_inbox_or_a_silent_chat_server_is_not_ok(self):
        root, home = _root(self)
        with mock.patch.object(migrate, "_inbox_rows", return_value=None):
            c = migrate.check(home, since=0.0)
        self.assertEqual((c["inbox_readable"], c["ok"]), (False, False))
        c = migrate.check(home, since=0.0, health=lambda h: (False, "refused"))
        self.assertEqual((c["chat_ok"], c["ok"]), (False, False))

    def test_a_clean_week_is_ok(self):
        root, home = _root(self)
        _inbox(home, [("done", "delivered", 7200)])
        _stream(home, [("tool", {"id": "tu-1"}, 7200), ("tool_result", {"tool_use_id": "tu-1"}, 7200)])
        self.assertTrue(migrate.check(home, since=0.0, health=lambda h: (True, "answers"))["ok"])


# ------------------------------------------------------------ [runtime] carried
KEY = "sk-ant-fixture-quokka-0123456789"
CARRY_TOML = ('[cousin]\nslug = "wren"\nname = "Wren"\n\n'
              '[runtime]\nmodel = "opus"\neffort = "high"\nauth = "api_key"\n')


def _key_cousin(case, *, key_line="ANTHROPIC_API_KEY=%s\n" % KEY, toml=CARRY_TOML):
    """wren on the tmux lane with [runtime] auth = "api_key" and its key
    in <home>/.secrets/api-key.env (0600 in a 0700 directory), as
    agent_auth.write_key leaves it; key_line None writes no key file."""
    root, home = _root(case)
    (home / "cousin.toml").write_text(toml)
    if key_line is not None:
        (home / ".secrets").mkdir(mode=0o700)
        os.chmod(home / ".secrets", 0o700)
        (home / ".secrets" / "api-key.env").write_text(key_line)
        os.chmod(home / ".secrets" / "api-key.env", 0o600)
    return root, home


def _files_holding(root, needle):
    out = []
    for path in root.rglob("*"):
        if path.is_file() and needle.encode() in path.read_bytes():
            out.append(str(path.relative_to(root)))
    return sorted(out)


def _runner_cousin(case):
    """wren as a 1.x migration left it: the tmux lane's [runtime] (model,
    effort, the key mode) and an [agent] that runs the same on the key
    account the migration made from the cousin's key file."""
    root, home = _key_cousin(case)
    with open(home / "cousin.toml", "a") as fh:
        fh.write('\n[agent]\nrunner = "sdk"\nmodel = "opus"\neffort = "high"\n'
                 'account = "wren-key"\n')
    secret = root / ".secrets" / "accounts" / "wren-key"
    secret.parent.mkdir(parents=True, mode=0o700)
    secret.write_text(KEY + "\n")
    os.chmod(secret, 0o600)
    with open(root / "config" / "accounts.toml", "a") as fh:
        fh.write('\n[accounts.wren-key]\nkind = "anthropic-key"\n'
                 'secret_file = ".secrets/accounts/wren-key"\n')
    return root, home


class TestCheckConfig(HermeticCase):
    """check says loudly when the runner would run another model,
    effort or billing than the cousin's [runtime]."""

    def _migrated(self, case_toml):
        root, home = _root(self)
        (home / "cousin.toml").write_text(case_toml)
        return root, home

    def test_a_runner_on_the_cli_default_model_and_the_host_login_is_a_mismatch(self):
        root, home = self._migrated(CARRY_TOML + '\n[agent]\nrunner = "sdk"\n')
        c = migrate.check(home, root=root, since=0.0)
        self.assertFalse(c["ok"])
        text = " ".join(c["mismatches"])
        self.assertIn("model", text)
        self.assertIn("opus", text)
        self.assertIn("effort", text)
        self.assertIn("host", text)
        self.assertIn("api_key", text)

    def test_a_carried_config_matches(self):
        root, home = _runner_cousin(self)
        c = migrate.check(home, root=root, since=0.0)
        self.assertEqual(c["mismatches"], [])
        self.assertTrue(c["ok"], c)
        self.assertEqual(c["config"]["runner"],
                         {"model": "opus", "effort": "high", "account": "wren-key",
                          "kind": "anthropic-key"})

    def test_a_key_account_under_a_login_runtime_is_a_mismatch(self):
        root, home = _root(self)
        (root / "config" / "accounts.toml").write_text(
            '[accounts.paid]\nkind = "anthropic-key"\n')
        (home / "cousin.toml").write_text('[cousin]\nslug = "wren"\n\n[agent]\n'
                                          'runner = "sdk"\naccount = "paid"\n')
        c = migrate.check(home, root=root, since=0.0)
        self.assertFalse(c["ok"])
        self.assertIn("paid", " ".join(c["mismatches"]))

    def test_the_cli_says_mismatch_loudly(self):
        root, home = self._migrated(CARRY_TOML + '\n[agent]\nrunner = "sdk"\n')
        out = io.StringIO()
        with mock.patch.object(migrate, "chat_health", return_value=(True, "answers")), \
                contextlib.redirect_stdout(out):
            rc = migrate.migrate_main(["check", "wren", "--since", "2000-01-01T00:00:00+00:00"])
        self.assertEqual(rc, 1)
        self.assertIn("MISMATCH", out.getvalue())


TOO_NEW = ("API Error: 400 Claude Code 2.1.277 does not support this model; version 2.1.280"
           " or newer is required")


class TestValidateTheModel(HermeticCase):
    """check --validate runs one turn on what the runner would run, and
    names the runner's CLI: a model the bundled CLI is too old for fails
    every turn."""

    def test_check_validate_runs_the_runners_config_and_fails_loudly(self):
        root, home = _runner_cousin(self)
        calls = []

        def failing(account, root_, *, model=None, effort=None):
            calls.append((model, effort, account.name))
            return 4, "validate: " + TOO_NEW
        c = migrate.check(home, root=root, since=0.0, validate=True, validator=failing,
                          cli_version=lambda: "Claude Code 2.1.277 (bundled)")
        self.assertEqual(calls, [("opus", "high", "wren-key")])
        self.assertFalse(c["ok"])
        self.assertFalse(c["validate_ok"])
        self.assertIn("2.1.280", c["validate"])
        self.assertEqual(c["cli"], "Claude Code 2.1.277 (bundled)")

    def test_check_prints_the_runners_cli(self):
        root, home = _key_cousin(self)
        out = io.StringIO()
        with mock.patch.object(migrate, "chat_health", return_value=(True, "answers")), \
                mock.patch.object(migrate, "runner_cli", return_value="Claude Code 2.1.277 (b)"), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            migrate.migrate_main(["check", "wren"])
        self.assertIn("runner CLI: Claude Code 2.1.277 (b)", out.getvalue())
        # no per-cousin chat server to report on in 2.0.0
        self.assertNotIn("chat server", out.getvalue())

    @unittest.skipUnless(importlib.util.find_spec("claude_agent_sdk"), "needs the sdk extra")
    def test_the_runner_cli_is_the_sdks_bundled_version(self):
        from claude_agent_sdk._cli_version import __cli_version__
        self.assertIn("Claude Code %s" % __cli_version__, migrate.runner_cli())


class TestNoLegacyPath(HermeticCase):
    """`plan`, `apply` and `rollback` without `--to` exit 2 before
    doing anything: 2.0.0 keeps no conversion from the legacy lane. A
    cousin with no runner gets delivery.lane_refusal; a runner
    cousin is told to name a kind."""

    def _main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(migrate, "_live", side_effect=AssertionError("live")), \
                mock.patch.object(migrate, "_switch_live", side_effect=AssertionError("live")):
            rc = migrate.migrate_main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_plan_without_to_on_a_cousin_with_no_runner_prints_the_refusal_and_exits_2(self):
        from cousin_lib.delivery import lane_refusal
        root, home = _root(self)
        before = _tree(root)
        for argv in (("plan", "wren"), ("apply", "wren", "--yes"), ("apply", "wren"),
                     ("rollback", "wren", "--yes")):
            rc, out, err = self._main(*argv)
            self.assertEqual(rc, 2, argv)
            self.assertIn(lane_refusal(home), err, argv)
            self.assertEqual(out, "", argv)
        self.assertEqual((home / "cousin.toml").read_bytes(), TOML.encode())
        self.assertEqual(_tree(root), before)

    def test_the_legacy_migrations_flags_are_gone(self):
        # plan/apply --account and --validate, rollback --force: read only by
        # the legacy migration, which no argv reaches; refused, not ignored
        root, home = _root(self)
        for argv in (("plan", "wren", "--to", "sdk", "--account", "team"),
                     ("plan", "wren", "--to", "sdk", "--validate"),
                     ("apply", "wren", "--to", "sdk", "--yes", "--account", "team"),
                     ("apply", "wren", "--to", "sdk", "--yes", "--validate"),
                     ("rollback", "wren", "--to", "sdk", "--yes", "--force")):
            with self.assertRaises(SystemExit) as caught:
                self._main(*argv)
            self.assertEqual(caught.exception.code, 2, argv)

    def test_without_to_on_a_runner_cousin_it_names_the_kinds(self):
        root, home = _root(self)
        text = '[cousin]\nslug = "wren"\n\n[agent]\nrunner = "sdk"\n'
        (home / "cousin.toml").write_text(text)
        for argv in (("plan", "wren"), ("apply", "wren", "--yes"), ("rollback", "wren", "--yes")):
            rc, out, err = self._main(*argv)
            self.assertEqual(rc, 2, argv)
            self.assertIn("name a kind with --to (sdk, tmux)", err, argv)
        self.assertEqual((home / "cousin.toml").read_text(), text)


if __name__ == "__main__":
    unittest.main()
