"""Accounts: the model, fail-closed validation, the environment per kind."""
import json
import os
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

from cousin_lib import accounts
from tests._hermetic import HermeticCase

TOML = """
[accounts.fleet]
kind = "claude-login"

[accounts.nightly]
kind = "claude-token"

[accounts.metered]
kind = "anthropic-key"
"""


class AccountsCase(HermeticCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        (self.root / "config").mkdir()
        self.home = self.root / "cousins" / "wren"; (self.home / "data").mkdir(parents=True)
        self.cousin()

    def cousin(self, agent=""):
        (self.home / "cousin.toml").write_text('[cousin]\nslug = "wren"\nname = "Wren"\n\n[agent]\n'
                                               'runner = "sdk"\n' + agent)

    def write(self, text=TOML):
        (self.root / "config" / "accounts.toml").write_text(text)

    def secret(self, rel, value, mode=0o600, dir_mode=0o700):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True); os.chmod(path.parent, dir_mode)
        path.write_text(value + "\n"); os.chmod(path, mode)
        return path


class TestLoad(AccountsCase):
    def test_the_three_kinds_and_the_default_places(self):
        self.write()
        acc = accounts.load(self.root)
        self.assertEqual({n: a.kind for n, a in acc.items()},
                         {"fleet": "claude-login", "nightly": "claude-token",
                          "metered": "anthropic-key"})
        self.assertEqual(acc["fleet"].config_dir, self.root / "data" / "accounts" / "fleet")
        self.assertEqual(acc["nightly"].secret_file,               # git-ignored: .secrets/
                         self.root / ".secrets" / "accounts" / "nightly")

    def test_no_file_is_no_accounts(self):
        self.assertEqual(accounts.load(self.root), {})

    def test_fail_closed(self):
        bad = {
            '[accounts.x]\nkind = "gcp"\n': "kind",
            '[accounts.x]\nkind = "opencode"\n': "providers",
            '[accounts.x]\nkind = "claude-login"\ncolour = "red"\n': "colour",
            '[accounts.X]\nkind = "claude-login"\n': "name",
            '[accounts.x]\nkind = "claude-token"\nsecret_file = ""\n': "relative",
            '[accounts.x]\nkind = "claude-token"\nsecret_file = "s"\nconfig_dir = "d"\n': "config_dir",
            '[accounts.x]\nkind = "anthropic-key"\nsecret_file = "/etc/key"\n': "relative",
            '[accounts.x]\nkind = "claude-login"\nconfig_dir = "../outside"\n': "root",
        }
        for text, needle in bad.items():
            self.write(text)
            with self.assertRaises(accounts.AccountsError) as cm:
                accounts.load(self.root)
            self.assertIn(needle, str(cm.exception), text)


class TestForCousin(AccountsCase):
    def test_no_account_is_the_hosts_default_login(self):
        acc = accounts.for_cousin(self.home, self.root)
        self.assertEqual((acc.name, acc.kind, acc.config_dir), ("host", "claude-login", None))

    def test_api_key_file_is_an_implicit_key_account_named_after_the_cousin(self):
        self.cousin('api_key_file = "secrets/wren.key"\n')
        acc = accounts.for_cousin(self.home, self.root)
        self.assertEqual((acc.name, acc.kind, acc.implicit), ("wren", "anthropic-key", True))
        self.assertEqual(acc.secret_file, self.root / "secrets" / "wren.key")

    def test_a_named_account(self):
        self.write(); self.cousin('account = "nightly"\n')
        self.assertEqual(accounts.for_cousin(self.home, self.root).kind, "claude-token")

    def test_an_unknown_account_or_both_keys_refuse(self):
        self.write(); self.cousin('account = "nobody"\n')
        with self.assertRaises(accounts.AccountsError):
            accounts.for_cousin(self.home, self.root)
        self.cousin('account = "fleet"\napi_key_file = "k"\n')
        with self.assertRaises(accounts.AccountsError):
            accounts.for_cousin(self.home, self.root)


class TestEnvironment(AccountsCase):
    def test_claude_login_sets_its_config_dir_only(self):
        self.write()
        env = accounts.account_env(accounts.load(self.root)["fleet"], self.root)
        self.assertEqual(env, {"CLAUDE_CONFIG_DIR": str(self.root / "data" / "accounts" / "fleet")})

    def test_the_host_login_sets_nothing(self):
        self.assertEqual(accounts.account_env(accounts.for_cousin(self.home, self.root), self.root), {})

    def test_claude_token_sets_the_token_and_a_login_free_dir_and_no_scrub(self):
        self.write(); self.secret(".secrets/accounts/nightly", "tok-nightly")
        env = accounts.account_env(accounts.load(self.root)["nightly"], self.root)
        self.assertEqual(env, {"CLAUDE_CODE_OAUTH_TOKEN": "tok-nightly",
                               "CLAUDE_CONFIG_DIR": str(self.root / "data" / "accounts" / "nightly")})

    def test_anthropic_key_sets_the_key_and_a_login_free_dir_and_no_scrub(self):
        # The scrub forces the CLI's permission mode to `default` and the
        # sandbox on every Bash call: a runner could run no tool (1.18.2).
        self.write(); self.secret(".secrets/accounts/metered", "key-metered")
        env = accounts.account_env(accounts.load(self.root)["metered"], self.root)
        self.assertEqual(env, {"ANTHROPIC_API_KEY": "key-metered",
                               "CLAUDE_CONFIG_DIR": str(self.root / "data" / "accounts" / "metered")})

    def test_scrub_then_set(self):
        self.write(); self.secret(".secrets/accounts/nightly", "tok-nightly")
        base = {"PATH": "/bin", "ANTHROPIC_API_KEY": "stray", "ANTHROPIC_AUTH_TOKEN": "stray",
                "ANTHROPIC_BASE_URL": "http://stray", "CLAUDE_CODE_OAUTH_TOKEN": "stray",
                "CLAUDE_CONFIG_DIR": "/stray"}
        env = {**accounts.scrub(base),
               **accounts.account_env(accounts.load(self.root)["nightly"], self.root)}
        self.assertEqual(env["PATH"], "/bin")
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "tok-nightly")
        for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
            self.assertNotIn(var, env)
        self.assertNotEqual(env["CLAUDE_CONFIG_DIR"], "/stray")

    def test_a_readable_secret_or_directory_is_refused_without_its_content(self):
        self.write()
        for mode, dir_mode in ((0o644, 0o700), (0o600, 0o755)):
            self.secret(".secrets/accounts/metered", "key-metered", mode=mode, dir_mode=dir_mode)
            acc = accounts.load(self.root)["metered"]
            for call in (accounts.account_env, accounts.preflight):
                with self.assertRaises(accounts.AccountsError) as cm:
                    call(acc, self.root)
                self.assertNotIsInstance(cm.exception, accounts.SecretMissing)
                self.assertIn("chmod", str(cm.exception))
                self.assertNotIn("key-metered", str(cm.exception))

    def test_a_symlinked_secret_is_refused(self):
        self.write()
        real = self.secret("elsewhere/key", "key-metered")
        link = self.root / ".secrets" / "accounts" / "metered"
        link.parent.mkdir(parents=True); os.chmod(link.parent, 0o700)
        link.symlink_to(real)
        with self.assertRaises(accounts.AccountsError):
            accounts.account_env(accounts.load(self.root)["metered"], self.root)

    def test_a_missing_secret_is_secret_missing_not_a_config_error(self):
        self.write()
        acc = accounts.load(self.root)["nightly"]
        for call in (accounts.account_env, accounts.preflight):
            with self.assertRaises(accounts.SecretMissing):
                call(acc, self.root)

    def test_a_login_in_a_secret_kinds_dir_refuses_the_start(self):
        self.write(); self.secret(".secrets/accounts/metered", "key-metered")
        d = self.root / "data" / "accounts" / "metered"; d.mkdir(parents=True)
        (d / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "x"}}))
        with self.assertRaises(accounts.AccountsError) as cm:
            accounts.preflight(accounts.load(self.root)["metered"], self.root)
        self.assertIn("bill the login", str(cm.exception))

    def test_the_missing_secret_hint_is_worded_per_kind(self):
        self.write()
        acc = accounts.load(self.root)
        with self.assertRaises(accounts.SecretMissing) as cm:
            accounts.account_env(acc["nightly"], self.root)
        self.assertIn("cousin-account token nightly", str(cm.exception))
        with self.assertRaises(accounts.SecretMissing) as cm:
            accounts.account_env(acc["metered"], self.root)
        self.assertNotIn("cousin-account token", str(cm.exception))
        self.assertIn("mode 0600", str(cm.exception))

    def test_repr_never_shows_a_secret(self):
        acc = accounts.Account("x", "anthropic-key", None, None, True, secret_value="key-inline")
        self.assertNotIn("key-inline", repr(acc))


class TestPerKind(AccountsCase):
    def test_resume_and_the_expected_source(self):
        self.write()
        acc = accounts.load(self.root)
        self.assertTrue(accounts.resume_via_cli(acc["fleet"]))
        self.assertFalse(accounts.resume_via_cli(acc["nightly"]))
        self.assertFalse(accounts.resume_via_cli(acc["metered"]))
        self.assertEqual(accounts.expected_source(acc["fleet"]), "none")
        self.assertEqual(accounts.expected_source(acc["nightly"]), "none")
        self.assertEqual(accounts.expected_source(acc["metered"]), "ANTHROPIC_API_KEY")

    def test_the_cli_is_an_absolute_path_or_a_refusal(self):
        with mock.patch("importlib.util.find_spec", return_value=None), \
                mock.patch("shutil.which", return_value=None):
            with self.assertRaises(accounts.AccountsError):
                accounts._cli()
        with mock.patch("importlib.util.find_spec", return_value=None), \
                mock.patch("shutil.which", return_value="/usr/local/bin/claude"):
            self.assertTrue(os.path.isabs(accounts._cli()))


class TestStatusAndCheck(AccountsCase):
    def run_with(self, payload):
        def run(argv, **kw):
            self.argv, self.env = argv, kw.get("env")
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(payload), stderr="")
        return run

    def test_status_runs_the_cli_under_the_accounts_config_dir(self):
        self.write()
        accounts.status(accounts.load(self.root)["fleet"], self.root,
                        run=self.run_with({"loggedIn": True, "authMethod": "claude.ai"}))
        self.assertEqual(self.argv[-3:], ["auth", "status", "--json"])
        self.assertEqual(self.env["CLAUDE_CONFIG_DIR"], str(self.root / "data" / "accounts" / "fleet"))

    def test_check_exit_codes_and_the_forward_reference_to_login(self):
        self.write(); self.cousin('account = "fleet"\n')
        rc, line = accounts.check(self.home, self.root,
                                  run=self.run_with({"loggedIn": True, "authMethod": "claude.ai"}))
        self.assertEqual(rc, 0); self.assertIn("account=fleet kind=claude-login", line)
        rc, line = accounts.check(self.home, self.root,
                                  run=self.run_with({"loggedIn": False, "authMethod": "none"}))
        self.assertEqual(rc, 4); self.assertIn("cousin-account login fleet", line)

    def test_a_token_account_wants_the_oauth_token_method(self):
        self.write(); self.secret(".secrets/accounts/nightly", "tok-nightly")
        self.cousin('account = "nightly"\n')
        rc, _ = accounts.check(self.home, self.root,
                               run=self.run_with({"loggedIn": True, "authMethod": "oauth_token"}))
        self.assertEqual(rc, 0)
        rc, line = accounts.check(self.home, self.root,
                                  run=self.run_with({"loggedIn": True, "authMethod": "claude.ai"}))
        self.assertEqual(rc, 4); self.assertIn("cousin-account token nightly", line)


class TestWritePrivate(AccountsCase):
    def test_a_stale_readable_tmp_ends_0600(self):
        path = self.root / "data" / "accounts" / "fleet" / "login.json"
        path.parent.mkdir(parents=True)
        tmp = path.with_name(".%s.tmp" % path.name)
        tmp.write_text("stale"); os.chmod(tmp, 0o644)
        accounts._write_private(path, {"t": "fake"})
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(path.read_text()), {"t": "fake"})
        self.assertFalse(tmp.exists())

    def test_a_symlink_at_the_tmp_path_is_refused(self):
        path = self.root / "data" / "accounts" / "fleet" / "login.json"
        path.parent.mkdir(parents=True)
        target = self.root / "elsewhere"
        target.write_text("untouched")
        tmp = path.with_name(".%s.tmp" % path.name)
        tmp.symlink_to(target)
        orig_unlink = os.unlink

        def keep_the_link(p, *a, **kw):   # a racer re-plants it after the stale unlink
            orig_unlink(p, *a, **kw)
            if pathlib.Path(p) == tmp and not os.path.lexists(tmp):
                tmp.symlink_to(target)
        with mock.patch("os.unlink", keep_the_link):
            with self.assertRaises(OSError):
                accounts._write_private(path, {"t": "fake"})
        self.assertEqual(target.read_text(), "untouched")
        self.assertFalse(path.exists())

    def test_the_parent_is_created_0700(self):
        path = self.root / "data" / "accounts" / "newone" / "login.json"
        accounts._write_private(path, {"t": "fake"})
        self.assertEqual(os.stat(path.parent).st_mode & 0o777, 0o700)


class TestCli(AccountsCase):
    def test_list_names_kinds_and_places_never_secrets(self):
        import io
        from contextlib import redirect_stdout
        self.write(); self.secret(".secrets/accounts/nightly", "tok-nightly")
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(accounts.account_main(["list", "--root", str(self.root)]), 0)
        out = buf.getvalue()
        self.assertIn("nightly", out); self.assertIn("claude-token", out)
        self.assertNotIn("tok-nightly", out)


if __name__ == "__main__":
    unittest.main()
