"""kind = "openrouter": one OpenRouter key for both lanes. On the sdk lane
the agent CLI goes to OpenRouter's Anthropic-compatible endpoint with the
key as its bearer token; on the opencode lane the account is an opencode
account with the one provider openrouter, its key copied into auth.json.
Fakes only: no request leaves the test."""
import io
import json
import os
import pathlib
import tempfile
import urllib.error
from unittest import mock

from cousin_lib import accounts, agent_settings
from cousin_lib.runner import opencode
from tests._hermetic import HermeticCase

TOML = """
[accounts.work]
kind = "openrouter"

[accounts.fleet]
kind = "claude-login"
"""
KEY = "sk-or-v1-FAKEKEYVALUE0123456789abcdef"


class OpenrouterCase(HermeticCase):
    def setUp(self):
        super().setUp()
        # CI's umask, not this host's 077: the dirs the code makes must be
        # 0700 whatever the umask
        self.addCleanup(os.umask, os.umask(0o022))
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        (self.root / "config").mkdir()
        (self.root / "config" / "accounts.toml").write_text(TOML)

    def account(self):
        return accounts.load(self.root)["work"]

    def key(self, value=KEY):
        path = self.root / ".secrets" / "accounts" / "work"
        path.parent.mkdir(parents=True, exist_ok=True); os.chmod(path.parent, 0o700)
        path.write_text(value + "\n"); os.chmod(path, 0o600)
        return path


class Load(OpenrouterCase):
    def test_its_key_and_its_opencode_dir_have_default_places(self):
        acc = self.account()
        self.assertEqual(acc.kind, "openrouter")
        self.assertEqual(acc.secret_file, self.root / ".secrets" / "accounts" / "work")
        self.assertEqual(acc.data_dir, self.root / ".secrets" / "accounts" / "work.opencode")

    def test_it_takes_a_secret_file_and_nothing_of_the_opencode_kind(self):
        (self.root / "config" / "accounts.toml").write_text(
            '[accounts.work]\nkind = "openrouter"\nproviders = ["openrouter"]\n')
        with self.assertRaisesRegex(accounts.AccountsError, "providers not allowed for kind openrouter"):
            accounts.load(self.root)


class SdkLane(OpenrouterCase):
    def test_the_cli_goes_to_openrouter_with_the_key_as_its_bearer_token(self):
        self.key()
        env = accounts.account_env(self.account(), self.root)
        self.assertEqual(env, {"ANTHROPIC_BASE_URL": "https://openrouter.ai/api",
                               "ANTHROPIC_AUTH_TOKEN": KEY, "ANTHROPIC_API_KEY": "",
                               "CLAUDE_CONFIG_DIR": str(self.root / "data" / "accounts" / "work")})

    def test_a_bearer_token_reads_source_none_and_its_lane_is_openrouter(self):
        self.assertEqual(accounts.expected_source(self.account()), "none")
        self.assertEqual(accounts.WANTED_METHOD["openrouter"], "openrouter")

    def test_it_runs_on_sdk_fake_and_opencode_never_on_tmux(self):
        acc = self.account()
        for lane in ("sdk", "fake", "opencode"):
            agent_settings.check_lane(acc, lane)
        with self.assertRaisesRegex(accounts.AccountsError, "subscription login"):
            agent_settings.check_lane(acc, "tmux")


class OpencodeLane(OpenrouterCase):
    def test_on_opencode_it_is_an_opencode_account_with_the_one_provider(self):
        view = accounts.for_lane(self.account(), "opencode")
        self.assertEqual((view.kind, view.providers, view.via),
                         ("opencode", ("openrouter",), "openrouter"))
        self.assertEqual(view.data_dir, self.account().data_dir)
        self.assertEqual(accounts.for_lane(self.account(), "sdk"), self.account())

    def test_preflight_copies_the_key_into_auth_json_once(self):
        self.key()
        view = accounts.for_lane(self.account(), "opencode")
        accounts.preflight(view, self.root)
        path = view.data_dir.joinpath(*accounts.AUTH_JSON)
        self.assertEqual(json.loads(path.read_text()), {"openrouter": {"type": "api", "key": KEY}})
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        before = os.stat(path).st_mtime_ns
        os.utime(path, ns=(before - 10**9, before - 10**9))
        accounts.preflight(view, self.root)          # the same key: left as it is
        self.assertEqual(os.stat(path).st_mtime_ns, before - 10**9)
        self.key("sk-or-v1-ANOTHERKEYVALUE0123456789")
        accounts.preflight(view, self.root)          # a new key: copied in
        self.assertEqual(json.loads(path.read_text())["openrouter"]["key"],
                         "sk-or-v1-ANOTHERKEYVALUE0123456789")

    def test_with_no_key_yet_the_login_check_finds_the_provider_missing(self):
        view = accounts.for_lane(self.account(), "opencode")
        accounts.preflight(view, self.root)
        st = accounts._opencode_status(view)
        self.assertEqual((st["loggedIn"], st["missing"]), (False, ["openrouter"]))

    def test_the_dirs_it_makes_are_0700_parents_too(self):
        view = accounts.for_lane(self.account(), "opencode")
        accounts.preflight(view, self.root)
        for d in (self.root / ".secrets", self.root / ".secrets" / "accounts", view.data_dir):
            self.assertEqual(os.stat(d).st_mode & 0o777, 0o700, d)

    def test_its_models_are_openrouter_ones_and_never_claude(self):
        view = accounts.for_lane(self.account(), "opencode")
        opencode.check_model(view, "openrouter/openai/gpt-5")
        with self.assertRaisesRegex(opencode.RunnerError, "Claude or Anthropic"):
            opencode.check_model(view, "openrouter/anthropic/claude-x")
        with self.assertRaisesRegex(opencode.RunnerError, "holds keys for openrouter only"):
            opencode.check_model(view, "openai/gpt-5")
        self.assertEqual(agent_settings.account_models(self.account()), ["openrouter/"])


class Status(OpenrouterCase):
    def fake(self, answer):
        calls = []

        def urlopen(req, timeout):
            calls.append((req.full_url, req.get_header("Authorization")))
            if isinstance(answer, Exception):
                raise answer
            return io.BytesIO(json.dumps(answer).encode())
        return urlopen, calls

    def test_openrouter_is_asked_about_the_key_and_nothing_repeats_it(self):
        self.key()
        urlopen, calls = self.fake({"data": {"label": "sk-or-v1-FAKE...cdef"}})
        st = accounts._openrouter_status(self.account(), urlopen=urlopen)
        self.assertEqual(st, {"loggedIn": True, "authMethod": "openrouter"})
        self.assertEqual(calls, [("https://openrouter.ai/api/v1/key", "Bearer " + KEY)])

    def test_a_refused_key_is_not_logged_in_and_says_where_to_write_one(self):
        self.key()
        err = urllib.error.HTTPError("u", 401, "no", {}, None)
        urlopen, _ = self.fake(err)
        st = accounts._openrouter_status(self.account(), urlopen=urlopen)
        self.assertFalse(st["loggedIn"])
        self.assertIn("refused the key (HTTP 401)", st["error"])
        self.assertNotIn(KEY, json.dumps(st))

    def test_no_key_yet_is_a_login_to_do(self):
        urlopen, calls = self.fake({})
        st = accounts._openrouter_status(self.account(), urlopen=urlopen)
        self.assertEqual(st, {"loggedIn": False, "authMethod": "openrouter"})
        self.assertEqual(calls, [])
        with mock.patch.object(accounts, "_openrouter_status", return_value=st):
            rc, line = accounts._check_account(self.account(), self.root)
        self.assertEqual(rc, 4)
        self.assertIn("write the key to %s" % (self.root / ".secrets" / "accounts" / "work"), line)
