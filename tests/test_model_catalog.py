"""model_catalog: which models an account can run, asked of its own source
(the agent CLI's list, OpenRouter's, opencode's, an endpoint's /models),
cached per account, never fetched on a read, refreshed in a child. Fakes
only: no request leaves the test and no CLI runs."""
import io
import json
import os
import pathlib
import subprocess
import tempfile
import threading
from types import SimpleNamespace

from cousin_lib import accounts, agent_settings, model_catalog
from tests._hermetic import HermeticCase

# The agent CLI's list as its connect answer gives it (2026-10-23 shape),
# with ids that are not real ones: the code never names a model.
CLI_LIST = [
    {"value": "default", "resolvedModel": "m-one", "displayName": "Default"},
    {"value": "big", "resolvedModel": "m-one", "displayName": "Big"},
    {"value": "fast", "resolvedModel": "m-two", "displayName": "Fast"},
    {"value": "m-three", "resolvedModel": "m-three", "displayName": "Three"},
    {"value": "", "displayName": "nothing to run"},
]
OPENROUTER = {"data": [{"id": "anthropic/m-one"}, {"id": "anthropic/m-one:batch"},
                       {"id": "openai/gpt-x"}, {"id": "anthropic/m-two"}, {"name": "no id"}]}
OPENCODE_OUT = ("openrouter/~anthropic/m-latest\nopenrouter/openai/gpt-x\n"
                "a warning line\nopenrouter/google/g-1\nopenrouter/\nother/x\n")


def http(answers):
    """A urlopen stand-in: url -> a JSON answer or an exception."""
    def urlopen(req, timeout):
        answer = answers[req.full_url]
        if isinstance(answer, Exception):
            raise answer
        return io.BytesIO(json.dumps(answer).encode())
    return urlopen


def run(stdout="", returncode=0, stderr=""):
    calls = []

    def fake(argv, **kw):
        calls.append((argv, kw.get("env") or {}))
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)
    fake.calls = calls
    return fake


class CatalogCase(HermeticCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        (self.root / "config").mkdir()
        (self.root / "config" / "accounts.toml").write_text(
            '[accounts.work]\nkind = "openrouter"\n\n'
            '[accounts.keyed]\nkind = "opencode"\nproviders = ["openai", "mistral"]\n\n'
            '[accounts.box]\nkind = "opencode"\nendpoint = "http://127.0.0.1:11434/v1"\n'
            'endpoint_model = "qwen3"\n')
        self.host = model_catalog.account_named(self.root, accounts.HOST)
        self.opencode_bin = os.environ.get("COUSIN_OPENCODE_BIN")
        os.environ["COUSIN_OPENCODE_BIN"] = "/opt/fake/opencode"
        self.addCleanup(self._restore_bin)

    def _restore_bin(self):
        if self.opencode_bin is None:
            os.environ.pop("COUSIN_OPENCODE_BIN", None)
        else:
            os.environ["COUSIN_OPENCODE_BIN"] = self.opencode_bin

    def account(self, name):
        return model_catalog.account_named(self.root, name)


class Sources(CatalogCase):
    def test_a_claude_account_reads_the_clis_list_resolved_and_once_each(self):
        lanes, err = model_catalog.fetch(self.root, self.host, cli_models=lambda a, r: CLI_LIST)
        self.assertEqual(lanes, {"sdk": ["m-one", "m-two", "m-three"]})
        self.assertIsNone(err)

    def test_openrouter_gives_the_sdk_its_claude_models_and_opencode_its_own_list(self):
        fake_run = run(OPENCODE_OUT)
        lanes, err = model_catalog.fetch(
            self.root, self.account("work"),
            http=http({model_catalog.OPENROUTER_MODELS_URL: OPENROUTER}), run=fake_run)
        self.assertEqual(lanes["sdk"], ["anthropic/m-one", "anthropic/m-two"])
        self.assertEqual(lanes["opencode"], ["openrouter/openai/gpt-x", "openrouter/google/g-1"])
        self.assertIsNone(err)
        argv, env = fake_run.calls[0]
        self.assertEqual(argv, ["/opt/fake/opencode", "models", "openrouter"])
        # a throwaway HOME, never the data dir a running cousin holds; no
        # Claude variable reaches it
        self.assertNotIn("work.opencode", env["HOME"])
        self.assertEqual(env["XDG_DATA_HOME"], env["HOME"] + "/data")
        self.assertFalse((self.root / ".secrets" / "accounts" / "work.opencode").exists())
        self.assertFalse(set(env) & set(accounts.AUTH_VARS))

    def test_a_lane_that_fails_is_left_out_and_said_without_colour_codes(self):
        lanes, err = model_catalog.fetch(
            self.root, self.account("work"),
            http=http({model_catalog.OPENROUTER_MODELS_URL: OPENROUTER}),
            run=run("", returncode=1, stderr="\x1b[91m\x1b[1mError: \x1b[0mfetch failed"))
        self.assertEqual(list(lanes), ["sdk"])
        self.assertIn("exited 1: Error: fetch failed", err)
        self.assertNotIn("\x1b", err)

    def test_openrouter_is_listed_under_a_stand_in_never_its_key(self):
        key = self.root / ".secrets" / "accounts" / "work"
        key.parent.mkdir(parents=True, exist_ok=True); os.chmod(key.parent, 0o700)
        key.write_text("sk-or-v1-FAKEKEYVALUE0123456789abcdef\n"); os.chmod(key, 0o600)
        seen = {}

        def fake(argv, **kw):
            home = pathlib.Path(kw["env"]["HOME"])
            seen["auth"] = home.joinpath(*accounts.AUTH_JSON).exists()
            seen["key"] = kw["env"].get("OPENROUTER_API_KEY")
            return SimpleNamespace(returncode=0, stdout=OPENCODE_OUT, stderr="")
        model_catalog.fetch(self.root, self.account("work"),
                            http=http({model_catalog.OPENROUTER_MODELS_URL: OPENROUTER}), run=fake)
        self.assertEqual(seen, {"auth": False, "key": model_catalog.LIST_ONLY_KEY})

    def test_an_opencode_account_is_listed_with_a_copy_of_its_auth_json(self):
        account = self.account("keyed")
        accounts.store_api_key(account, "openai", "sk-FAKEKEYVALUE0123456789")
        seen = {}

        def fake(argv, **kw):
            home = pathlib.Path(kw["env"]["HOME"])
            path = home.joinpath(*accounts.AUTH_JSON)
            seen["copy"] = json.loads(path.read_text())
            seen["mode"] = os.stat(path).st_mode & 0o777
            seen["home"] = home
            return SimpleNamespace(returncode=0, stdout="openai/gpt-x\n", stderr="")
        model_catalog.fetch(self.root, account, run=fake)
        self.assertEqual(seen["copy"], {"openai": {"type": "api", "key": "sk-FAKEKEYVALUE0123456789"}})
        self.assertEqual(seen["mode"], 0o600)
        self.assertFalse(seen["home"].exists())          # the throwaway HOME is gone

    def test_an_opencode_account_lists_each_provider_and_never_a_claude_model(self):
        out = "openai/gpt-x\nopenai/claude-proxy\nmistral/m-large\n"
        lanes, _ = model_catalog.fetch(self.root, self.account("keyed"), run=run(out))
        self.assertEqual(lanes, {"opencode": ["openai/gpt-x", "mistral/m-large"]})

    def test_an_endpoint_lists_its_models_its_own_first(self):
        url = "http://127.0.0.1:11434/v1/models"
        lanes, _ = model_catalog.fetch(self.root, self.account("box"), http=http(
            {url: {"data": [{"id": "llama-9"}, {"id": "qwen3"}, {"id": "claude-ish"}]}}))
        self.assertEqual(lanes, {"opencode": ["local/qwen3", "local/llama-9"]})

    def test_nothing_fetched_is_an_error(self):
        with self.assertRaises(model_catalog.CatalogError):
            model_catalog.fetch(self.root, self.account("box"), http=http(
                {"http://127.0.0.1:11434/v1/models": OSError("refused")}))


class Cache(CatalogCase):
    def test_a_fetch_is_cached_private_and_a_read_never_fetches(self):
        model_catalog.update(self.root, self.host, now=1000.0, cli_models=lambda a, r: CLI_LIST)
        path = model_catalog.cache_path(self.root, "host")
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertEqual(model_catalog.models_for(self.root, "host", "sdk"),
                         ["m-one", "m-two", "m-three"])
        self.assertEqual(model_catalog.models_for(self.root, "host", "opencode"), [])
        self.assertEqual(model_catalog.summary(self.root, "host"),
                         {"lanes": {"sdk": 3}, "fetched_at": 1000.0, "checked_at": 1000.0,
                          "error": None})

    def test_a_failed_fetch_keeps_the_last_lists_and_says_why(self):
        model_catalog.update(self.root, self.host, now=1000.0, cli_models=lambda a, r: CLI_LIST)

        def broken(account, root):
            raise RuntimeError("the CLI exited 1")
        out = model_catalog.update(self.root, self.host, now=2000.0, cli_models=broken)
        self.assertEqual(out["lanes"], {"sdk": 3})
        self.assertEqual((out["fetched_at"], out["checked_at"]), (1000.0, 2000.0))
        self.assertIn("the CLI exited 1", out["error"])

    def test_a_try_that_got_nothing_is_retried_after_an_hour(self):
        def broken(account, root):
            raise RuntimeError("no network")
        model_catalog.update(self.root, self.host, now=1000.0, cli_models=broken)
        self.assertFalse(model_catalog.stale(self.root, "host", now=1000.0 + 1800))
        self.assertTrue(model_catalog.stale(self.root, "host", now=1000.0 + 3700))

    def test_a_saved_key_makes_the_list_stale(self):
        model_catalog.update(self.root, self.host, now=1000.0, cli_models=lambda a, r: CLI_LIST)
        model_catalog.mark_stale(self.root, "host")
        self.assertTrue(model_catalog.stale(self.root, "host", now=1001.0))
        self.assertEqual(model_catalog.models_for(self.root, "host", "sdk"),
                         ["m-one", "m-two", "m-three"])

    def test_stale_after_a_day_since_the_last_try(self):
        self.assertTrue(model_catalog.stale(self.root, "host"))
        model_catalog.update(self.root, self.host, now=1000.0, cli_models=lambda a, r: CLI_LIST)
        self.assertFalse(model_catalog.stale(self.root, "host", now=1000.0 + 3600))
        self.assertTrue(model_catalog.stale(self.root, "host", now=1000.0 + 25 * 3600))


class Refresh(CatalogCase):
    def test_the_fetch_runs_in_a_child_with_no_claude_variable(self):
        seen = {}

        def fake(argv, **kw):
            seen["argv"], seen["env"] = argv, kw["env"]
            return SimpleNamespace(returncode=0, stdout='noise\n{"lanes": {"sdk": 3}}\n', stderr="")
        os.environ["ANTHROPIC_API_KEY"] = "sk-must-not-pass"
        self.addCleanup(os.environ.pop, "ANTHROPIC_API_KEY", None)
        out = model_catalog.refresh(self.root, "host", run=fake)
        self.assertEqual(out, {"lanes": {"sdk": 3}})
        self.assertEqual(seen["argv"][1:], ["-m", "cousin_lib.model_catalog", "refresh",
                                            "--root", str(self.root), "host"])
        self.assertNotIn("ANTHROPIC_API_KEY", seen["env"])

    def test_a_child_that_says_nothing_is_an_error(self):
        out = model_catalog.refresh(self.root, "host", run=lambda argv, **kw: SimpleNamespace(
            returncode=1, stdout="", stderr="Traceback\nImportError: boom"))
        self.assertIn("ImportError: boom", out["error"])

    def test_one_registry_for_a_claimed_refresh_and_a_background_one(self):
        running = {}
        self.assertTrue(model_catalog.begin(running, "work"))
        self.assertFalse(model_catalog.begin(running, "work"))      # a second refresh: refused
        self.assertEqual(model_catalog.refresh_stale(self.root, ["work"], running,
                                                     start=lambda t: None), [])
        model_catalog.end(running, "work")
        started = model_catalog.refresh_stale(self.root, ["work"], running, start=lambda t: None)
        self.assertEqual(started, ["work"])

    def test_only_stale_accounts_with_no_fetch_running_start(self):
        model_catalog.update(self.root, self.host, cli_models=lambda a, r: CLI_LIST)
        started, running = [], {}
        names = model_catalog.refresh_stale(self.root, ["host", "work", "work"], running,
                                            start=lambda t: started.append(t.name))
        self.assertEqual(names, ["work"])         # host is fresh; work once
        self.assertEqual(started, ["models-work"])

    def test_the_child_entry_refuses_an_account_it_does_not_know(self):
        out = io.StringIO()
        import contextlib
        with contextlib.redirect_stdout(out):
            rc = model_catalog.main(["refresh", "--root", str(self.root), "nobody"])
        self.assertEqual(rc, 2)
        self.assertIn("no account 'nobody'", json.loads(out.getvalue())["error"])


class Suggestions(CatalogCase):
    def home(self, agent):
        home = self.root / "cousins" / "wren"
        (home / "data").mkdir(parents=True, exist_ok=True)
        (home / "cousin.toml").write_text('[cousin]\nslug = "wren"\nname = "Wren"\n\n[agent]\n'
                                          + agent)
        return home

    def test_the_settings_offer_the_accounts_own_list_once_one_is_fetched(self):
        home = self.home('runner = "sdk"\n')
        row = agent_settings.describe(home, self.root)["settings"]["model"]
        self.assertEqual(row["suggestions_from"], "catalogue")
        model_catalog.update(self.root, self.host, cli_models=lambda a, r: CLI_LIST)
        row = agent_settings.describe(home, self.root)["settings"]["model"]
        self.assertEqual((row["suggestions"], row["suggestions_from"]),
                         (["m-one", "m-two", "m-three"], "account"))

    def test_the_spawn_dialog_gets_each_lanes_list_per_account(self):
        from cousin_lib.console.routes_fleet import spawn_lane_options
        model_catalog.update(self.root, self.host, cli_models=lambda a, r: CLI_LIST)
        rows = {r["name"]: r for r in spawn_lane_options(self.root)["accounts"]}
        self.assertEqual(rows["host"]["models_by_lane"],
                         {"sdk": ["m-one", "m-two", "m-three"],
                          "tmux": ["m-one", "m-two", "m-three"]})
        # nothing fetched yet: the opencode lane falls back to "openrouter/";
        # the sdk lane has none, so the dialog offers the harness catalogue
        self.assertEqual(rows["work"]["models_by_lane"], {"opencode": ["openrouter/"]})
        self.assertNotIn("models", rows["work"])


class AccountCli(CatalogCase):
    def main(self, *argv):
        import contextlib
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = accounts.account_main([*argv, "--root", str(self.root)])
        return rc, out.getvalue(), err.getvalue()

    def test_models_prints_each_lanes_list_and_says_how_old_it_is(self):
        model_catalog.update(self.root, self.host, now=0.0, cli_models=lambda a, r: CLI_LIST)
        rc, out, _ = self.main("models", "host")
        self.assertEqual(rc, 0)
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith("# account host (claude-login), fetched 1970-01-01"))
        self.assertIn("older than a day", lines[0])
        self.assertEqual([ln.split() for ln in lines[1:]],
                         [["sdk", "m-one"], ["sdk", "m-two"], ["sdk", "m-three"]])
        self.assertEqual(self.main("models", "host", "--lane", "opencode")[1].splitlines()[1:], [])

    def test_no_list_yet_says_how_to_fetch_one(self):
        rc, _, err = self.main("models", "work")
        self.assertEqual(rc, 1)
        self.assertIn("models work --refresh", err)


class CliProbe(HermeticCase):
    def test_the_list_is_read_off_a_bare_client_before_any_prompt(self):
        from cousin_lib.runner import sdk
        seen = {}

        class Client:
            def __init__(self, options):
                seen["options"] = options

            async def connect(self):
                seen["connected"] = True

            async def get_server_info(self):
                return {"models": CLI_LIST}

            async def query(self, prompt):          # pragma: no cover - never called
                raise AssertionError("a prompt was sent")

            async def disconnect(self):
                seen["disconnected"] = True
        host = accounts.Account(accounts.HOST, "claude-login", None, None, implicit=True)
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        out = sdk.cli_models(host, pathlib.Path(tmp.name), client_factory=Client)
        self.assertEqual(out, CLI_LIST)
        self.assertTrue(seen["connected"] and seen["disconnected"])
        self.assertEqual(seen["options"].setting_sources, [])
        self.assertIn("no-session-persistence", seen["options"].extra_args)
