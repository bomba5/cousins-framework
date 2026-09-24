"""Accounts, kind = "opencode" (phase 9 R12): a data dir, provider keys
opencode keeps in its own auth.json or a local endpoint; the env it gets;
presence-only status; the lane check (R12, R13's account half)."""
import io
import json
import os
import shutil
import unittest
from contextlib import redirect_stdout

from cousin_lib import accounts
from tests.test_accounts import AccountsCase

TOML = """
[accounts.keyed]
kind = "opencode"
providers = ["openai", "mistral"]

[accounts.local]
kind = "opencode"
endpoint = "http://127.0.0.1:11434/v1"
endpoint_model = "qwen3-coder"
data_dir = "data/opencode/local"

[accounts.fleet]
kind = "claude-login"

[accounts.nightly]
kind = "claude-token"

[accounts.metered]
kind = "anthropic-key"
"""

XDG = ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME")


def _never_run(argv, **kw):
    raise AssertionError("status ran a process for an opencode account: %r" % (argv,))


class OpencodeCase(AccountsCase):
    def auth_json(self, name, entries, mode=0o600):
        path = self.root / ".secrets" / "accounts" / ("%s.opencode" % name) / "data" \
            / "opencode" / "auth.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent.parent.parent, 0o700)
        path.write_text(json.dumps(entries)); os.chmod(path, mode)
        return path


class TestLoad(OpencodeCase):
    def test_a_providers_account_and_its_default_data_dir(self):
        self.write(TOML)
        acc = accounts.load(self.root)["keyed"]
        self.assertEqual(acc.kind, "opencode")
        self.assertEqual(acc.providers, ("openai", "mistral"))
        self.assertEqual(acc.data_dir, self.root / ".secrets" / "accounts" / "keyed.opencode")
        self.assertIsNone(acc.endpoint); self.assertIsNone(acc.endpoint_model)
        self.assertIsNone(acc.secret_file); self.assertIsNone(acc.config_dir)
        self.assertFalse(acc.data_dir.exists())           # created when used, not when read

    def test_an_endpoint_account_with_its_own_data_dir(self):
        self.write(TOML)
        acc = accounts.load(self.root)["local"]
        self.assertEqual((acc.endpoint, acc.endpoint_model),
                         ("http://127.0.0.1:11434/v1", "qwen3-coder"))
        self.assertEqual(acc.providers, ())
        self.assertEqual(acc.data_dir, self.root / "data" / "opencode" / "local")

    def test_opencode_is_a_kind_not_a_reserved_name(self):
        self.assertIn("opencode", accounts.KINDS)
        self.assertNotIn("opencode", accounts.RESERVED_KINDS)

    def test_fail_closed_naming_the_key(self):
        head = '[accounts.oc]\nkind = "opencode"\n'
        ep = 'endpoint = "http://127.0.0.1:8080/v1"\n'
        bad = {
            "": "providers",
            'providers = ["openai"]\n' + ep + 'endpoint_model = "m"\n': "exactly one",
            'providers = []\n': "providers",
            'providers = "openai"\n': "providers",
            'providers = ["Open AI"]\n': "providers",
            'providers = ["openai", "openai"]\n': "providers",
            'providers = ["opencode"]\n': "providers",
            ep: "endpoint_model",
            'providers = ["openai"]\nendpoint_model = "m"\n': "endpoint_model",
            ep + 'endpoint_model = ""\n': "endpoint_model",
            'endpoint = "ftp://127.0.0.1/v1"\nendpoint_model = "m"\n': "endpoint",
            'endpoint = "http://"\nendpoint_model = "m"\n': "endpoint",
            'endpoint = 8080\nendpoint_model = "m"\n': "endpoint",
            'endpoint = "http://127.0.0.1:3456/v1"\nendpoint_model = "m"\n': "3456",
            'endpoint = "http://127.0.0.1:8080/meridian"\nendpoint_model = "m"\n': "bridge",
            'providers = ["openai"]\ndata_dir = "/var/oc"\n': "relative",
            'providers = ["openai"]\ndata_dir = "../outside"\n': "root",
            'providers = ["openai"]\nsecret_file = "s"\n': "secret_file",
            'providers = ["openai"]\nconfig_dir = "d"\n': "config_dir",
        }
        for body, needle in bad.items():
            self.write(head + body)
            with self.assertRaises(accounts.AccountsError, msg=body) as cm:
                accounts.load(self.root)
            self.assertIn(needle, str(cm.exception), body)
            self.assertIn("[accounts.oc]", str(cm.exception), body)

    def test_the_opencode_keys_are_refused_on_the_other_kinds(self):
        for kind in ("claude-login", "claude-token", "anthropic-key"):
            for key in ('providers = ["openai"]', 'endpoint = "http://127.0.0.1:1/v1"',
                        'endpoint_model = "m"', 'data_dir = "d"'):
                self.write('[accounts.x]\nkind = "%s"\n%s\n' % (kind, key))
                with self.assertRaises(accounts.AccountsError) as cm:
                    accounts.load(self.root)
                self.assertIn(key.split(" ")[0], str(cm.exception))

    def test_an_endpoint_with_credentials_is_refused_without_them(self):
        self.write('[accounts.oc]\nkind = "opencode"\n'
                   'endpoint = "http://wren:hunter22@127.0.0.1:8080/v1"\nendpoint_model = "m"\n')
        with self.assertRaises(accounts.AccountsError) as cm:
            accounts.load(self.root)
        self.assertIn("endpoint", str(cm.exception))
        self.assertNotIn("hunter22", str(cm.exception))

    def test_a_cousin_names_an_opencode_account(self):
        self.write(TOML); self.cousin('account = "keyed"\n')
        self.assertEqual(accounts.for_cousin(self.home, self.root).kind, "opencode")


class TestEnvironment(OpencodeCase):
    def test_account_env_is_home_and_the_four_xdg_dirs_inside_the_data_dir_only(self):
        self.write(TOML)
        for name in ("keyed", "local"):
            acc = accounts.load(self.root)[name]
            env = accounts.account_env(acc, self.root)
            self.assertEqual(set(env), {"HOME", *XDG})
            d = acc.data_dir
            self.assertEqual(env, {"HOME": str(d), "XDG_CONFIG_HOME": str(d / "config"),
                                   "XDG_DATA_HOME": str(d / "data"),
                                   "XDG_CACHE_HOME": str(d / "cache"),
                                   "XDG_STATE_HOME": str(d / "state")})
            for sub in ("", "config", "data", "cache", "state"):
                self.assertEqual(os.stat(d / sub).st_mode & 0o777, 0o700, (name, sub))

    def test_no_key_reaches_the_environment_even_when_one_is_stored(self):
        self.write(TOML)
        self.auth_json("keyed", {"openai": {"type": "api", "key": "sk-fake-openai"},
                                 "mistral": {"type": "api", "key": "fake-mistral"}})
        env = accounts.account_env(accounts.load(self.root)["keyed"], self.root)
        self.assertEqual(set(env), {"HOME", *XDG})
        self.assertNotIn("sk-fake-openai", json.dumps(env))

    def test_a_loose_data_dir_is_tightened_and_a_symlinked_one_refused(self):
        self.write(TOML)
        acc = accounts.load(self.root)["keyed"]
        acc.data_dir.mkdir(parents=True); os.chmod(acc.data_dir, 0o755)
        accounts.preflight(acc, self.root)
        self.assertEqual(os.stat(acc.data_dir).st_mode & 0o777, 0o700)
        real = self.root / "elsewhere"; real.mkdir(mode=0o700)
        shutil.rmtree(acc.data_dir); acc.data_dir.symlink_to(real)
        for call in (accounts.account_env, accounts.preflight):
            with self.assertRaises(accounts.AccountsError) as cm:
                call(acc, self.root)
            self.assertIn("symlink", str(cm.exception))

    def test_a_readable_auth_json_refuses_the_start_without_its_content(self):
        self.write(TOML)
        self.auth_json("keyed", {"openai": {"type": "api", "key": "sk-fake-openai"}}, mode=0o644)
        with self.assertRaises(accounts.AccountsError) as cm:
            accounts.preflight(accounts.load(self.root)["keyed"], self.root)
        self.assertIn("chmod 600", str(cm.exception))
        self.assertNotIn("sk-fake-openai", str(cm.exception))

    def test_a_missing_auth_json_is_not_a_start_error(self):
        self.write(TOML)
        accounts.preflight(accounts.load(self.root)["keyed"], self.root)   # a login to do

    def test_an_anthropic_oauth_login_in_auth_json_refuses_the_start(self):
        self.write('[accounts.oc]\nkind = "opencode"\nproviders = ["anthropic"]\n')
        self.auth_json("oc", {"anthropic": {"type": "oauth", "refresh": "fake-r",
                                            "access": "fake-a", "expires": 0}})
        acc = accounts.load(self.root)["oc"]
        with self.assertRaises(accounts.AccountsError) as cm:
            accounts.preflight(acc, self.root)
        self.assertIn("subscription", str(cm.exception))
        self.assertNotIn("fake-a", str(cm.exception))

    def test_an_auth_json_entry_named_for_claude_refuses_the_start_whatever_its_type(self):
        """Review round 2, minor 3: ruling P9-1 reaches auth.json. opencode
        reads every entry live, so an Anthropic key (or any entry whose id
        says claude or anthropic) on this lane is refused, not only an OAuth
        login."""
        self.write('[accounts.oc]\nkind = "opencode"\nproviders = ["openai"]\n')
        acc = accounts.load(self.root)["oc"]
        for pid, entry in (("anthropic", {"type": "api", "key": "fake-anthropic-key"}),
                           ("my-claude-proxy", {"type": "api", "key": "fake-k"}),
                           ("Anthropic-Vertex", {"type": "oauth", "refresh": "fake-r"})):
            with self.subTest(provider=pid):
                self.auth_json("oc", {"openai": {"type": "api", "key": "fake-openai"}, pid: entry})
                with self.assertRaises(accounts.AccountsError) as cm:
                    accounts.preflight(acc, self.root)
                self.assertIn(pid, str(cm.exception))
                self.assertIn("Agent SDK", str(cm.exception))
                self.assertNotIn("fake-", str(cm.exception))

    def test_auth_json_holds_api_keys_and_non_anthropic_oauth_only(self):
        """Review Critical 1: opencode reads every auth.json entry live, and
        a `wellknown` entry (or any type it may add) brings in a config or a
        token source the guard never sees. Only `api`, and `oauth` on a
        provider other than Anthropic, start; the refusal names the provider
        and the type, never a value."""
        self.write('[accounts.oc]\nkind = "opencode"\nproviders = ["openai"]\n')
        acc = accounts.load(self.root)["oc"]
        for entry in ({"type": "wellknown", "key": "fake-wk", "token": "fake-wt"},
                      {"key": "fake-untyped"}, "fake-not-an-object"):
            with self.subTest(entry=entry):
                self.auth_json("oc", {"openai": {"type": "api", "key": "fake-openai"},
                                      "https://corp.example": entry})
                with self.assertRaises(accounts.AccountsError) as cm:
                    accounts.preflight(acc, self.root)
                self.assertIn("https://corp.example", str(cm.exception))
                self.assertNotIn("fake-", str(cm.exception))
        self.auth_json("oc", {"openai": {"type": "api", "key": "fake-openai"},
                              "github-copilot": {"type": "oauth", "refresh": "fake-r",
                                                 "access": "fake-a", "expires": 0}})
        accounts.preflight(acc, self.root)                    # another vendor's OAuth: P9-2

    def test_scrub_then_set(self):
        self.write(TOML)
        base = {"PATH": "/bin", "HOME": "/srv/elsewhere", "ANTHROPIC_API_KEY": "stray",
                "CLAUDE_CODE_OAUTH_TOKEN": "stray", "XDG_DATA_HOME": "/stray"}
        env = {**accounts.scrub(base),
               **accounts.account_env(accounts.load(self.root)["local"], self.root)}
        self.assertEqual(env["PATH"], "/bin")
        self.assertNotIn("ANTHROPIC_API_KEY", env); self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)
        self.assertTrue(env["HOME"].startswith(str(self.root)))
        self.assertTrue(env["XDG_DATA_HOME"].startswith(str(self.root)))


class TestStatusAndCheck(OpencodeCase):
    def test_status_is_presence_only_and_never_runs_a_process(self):
        self.write(TOML)
        acc = accounts.load(self.root)["keyed"]
        st = accounts.status(acc, self.root, run=_never_run)
        self.assertFalse(st["loggedIn"]); self.assertEqual(st["missing"], ["openai", "mistral"])
        self.auth_json("keyed", {"openai": {"type": "api", "key": "sk-fake-openai"}})
        st = accounts.status(acc, self.root, run=_never_run)
        self.assertFalse(st["loggedIn"]); self.assertEqual(st["missing"], ["mistral"])
        self.auth_json("keyed", {"openai": {"type": "api", "key": "sk-fake-openai"},
                                 "mistral": {"type": "api", "key": "fake-mistral"}})
        st = accounts.status(acc, self.root, run=_never_run)
        self.assertTrue(st["loggedIn"]); self.assertEqual(st["missing"], [])
        self.assertEqual(st["authMethod"], "opencode")
        self.assertNotIn("sk-fake-openai", json.dumps(st))

    def test_an_endpoint_account_is_logged_in_by_configuration(self):
        self.write(TOML)
        st = accounts.status(accounts.load(self.root)["local"], self.root, run=_never_run)
        self.assertTrue(st["loggedIn"]); self.assertEqual(st["endpoint"], "http://127.0.0.1:11434/v1")

    def test_a_readable_auth_json_is_an_error_in_status_without_its_content(self):
        self.write(TOML)
        self.auth_json("keyed", {"openai": {"type": "api", "key": "sk-fake-openai"}}, mode=0o640)
        st = accounts.status(accounts.load(self.root)["keyed"], self.root, run=_never_run)
        self.assertIn("chmod 600", st["error"]); self.assertNotIn("sk-fake-openai", json.dumps(st))

    def test_check_names_the_login_for_the_missing_provider(self):
        self.write(TOML); self.cousin('account = "keyed"\n')
        self.auth_json("keyed", {"openai": {"type": "api", "key": "sk-fake-openai"}})
        rc, line = accounts.check(self.home, self.root, run=_never_run)
        self.assertEqual(rc, 4)
        self.assertIn("account=keyed kind=opencode", line)
        # an API key never travels through chat: the action names the key's
        # way in, and --via only with an OAuth --method (phase 9 R12')
        self.assertIn("`cousin-account login keyed --provider mistral`", line)
        self.assertIn("--key-file", line)
        self.assertIn("--method <label> --via wren", line)
        self.cousin('account = "local"\n')
        rc, line = accounts.check(self.home, self.root, run=_never_run)
        self.assertEqual(rc, 0, line)

    def test_login_action_per_shape(self):
        self.write(TOML)
        acc = accounts.load(self.root)
        self.assertEqual(accounts.login_action(acc["keyed"]),
                         "`cousin-account login keyed --provider openai` (the API key on"
                         " stdin or with --key-file; an OAuth method: add --method <label>)")
        self.assertEqual(accounts.login_action(acc["keyed"], "wren", provider="mistral"),
                         "`cousin-account login keyed --provider mistral` (the API key on"
                         " stdin or with --key-file; an OAuth method: add --method <label>"
                         " --via wren)")
        self.assertIn("check the endpoint http://127.0.0.1:11434/v1",
                      accounts.login_action(acc["local"]))

    def test_list_shows_the_data_dir(self):
        self.write(TOML)
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(accounts.account_main(["list", "--root", str(self.root)]), 0)
        line = [ln for ln in buf.getvalue().splitlines() if ln.startswith("keyed")][0]
        self.assertIn("opencode", line)
        self.assertIn(str(self.root / ".secrets" / "accounts" / "keyed.opencode"), line)


class TestLane(OpencodeCase):
    def test_an_opencode_runner_takes_only_an_opencode_account(self):
        self.write(TOML)
        acc = accounts.load(self.root)
        host = accounts.for_cousin(self.home, self.root)
        self.cousin('api_key_file = "secrets/wren.key"\n')
        implicit_key = accounts.for_cousin(self.home, self.root)
        for a in (host, implicit_key, acc["fleet"], acc["nightly"], acc["metered"]):
            with self.assertRaises(accounts.AccountsError, msg=a.name) as cm:
                accounts.check_lane(a, "opencode")
            self.assertIn(a.kind, str(cm.exception))
            self.assertIn("opencode", str(cm.exception))
        for name in ("keyed", "local"):
            accounts.check_lane(acc[name], "opencode")

    def test_claude_by_name_never_runs_on_the_opencode_lane(self):
        """Ruling P9-1 (review Important 5 and 6): "Claude cousins run on the
        Agent SDK and nowhere else", read literally. On the opencode lane an
        account that names the anthropic provider, or an endpoint model whose
        id says claude or anthropic, is refused: the latter is the shape of
        any OpenAI-compatible proxy in front of a Claude subscription."""
        self.write('[accounts.both]\nkind = "opencode"\nproviders = ["openai", "anthropic"]\n'
                   '[accounts.proxy]\nkind = "opencode"\nendpoint = "http://127.0.0.1:8080/v1"\n'
                   'endpoint_model = "Claude-Sonnet-local"\n'
                   '[accounts.proxy2]\nkind = "opencode"\nendpoint = "http://192.0.2.10:9/v1"\n'
                   'endpoint_model = "my-anthropic-mirror"\n'
                   '[accounts.qwen]\nkind = "opencode"\nendpoint = "http://127.0.0.1:8080/v1"\n'
                   'endpoint_model = "qwen3-coder"\n')
        acc = accounts.load(self.root)
        for name, needle in (("both", "anthropic"), ("proxy", "Claude-Sonnet-local"),
                             ("proxy2", "my-anthropic-mirror")):
            with self.subTest(account=name):
                with self.assertRaises(accounts.AccountsError) as cm:
                    accounts.check_lane(acc[name], "opencode")
                self.assertIn(needle, str(cm.exception))
                self.assertIn("Agent SDK", str(cm.exception))
        accounts.check_lane(acc["qwen"], "opencode")

    def test_an_sdk_or_fake_runner_refuses_an_opencode_account(self):
        self.write(TOML)
        acc = accounts.load(self.root)
        for kind in ("sdk", "fake"):
            for name in ("keyed", "local"):
                with self.assertRaises(accounts.AccountsError) as cm:
                    accounts.check_lane(acc[name], kind)
                self.assertIn(kind, str(cm.exception))
            for name in ("fleet", "nightly", "metered"):
                accounts.check_lane(acc[name], kind)
            accounts.check_lane(accounts.for_cousin(self.home, self.root), kind)


if __name__ == "__main__":
    unittest.main()
