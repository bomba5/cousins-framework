"""cousin-account login <name> --provider <id> for an opencode account (phase 9
R12'): an API key never travels through chat. It comes from stdin (hidden on a
terminal) or a strict key file and is written straight into the account's
opencode auth.json, merged, 0600 in a 0700 dir. An OAuth method goes through
the pty driver: opencode 1.18.31's every OAuth method is "auto" (a URL and an
instruction line, then it waits; nothing is pasted back), so the URL and the
instructions are relayed and the account's auth.json decides."""
import contextlib
import io
import json
import os
import pathlib
import stat
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

from cousin_lib import accounts
from tests.test_accounts_login import FakePty, LoginCase

TOML = """
[accounts.keyed]
kind = "opencode"
providers = ["openai", "mistral"]

[accounts.metered]
kind = "opencode"
providers = ["anthropic"]

[accounts.local]
kind = "opencode"
endpoint = "http://127.0.0.1:11434/v1"
endpoint_model = "qwen3-coder"

[accounts.fleet]
kind = "claude-login"
"""

KEY = "sk-fake-openai-0123456789abcdef"
URL = "https://auth.example.test/codex/device"
HEADLESS = "ChatGPT Pro/Plus (headless)"
# The screens measured from opencode 1.18.31 `auth login --provider openai
# --method <label>` in a pty (clean()ed): the "auto" OAuth shape, the API-key
# prompt, an unknown method, and a prompt the relay cannot answer.
GO = ("\n\u250c  Add credential\n\u2502\n\u25cf  Go to: %s\n\u2502\n\u25cf  Enter code: WXYZ-1234\n"
      "\u2502\n\u25d2  Waiting for authorization..." % URL)
DONE = "\u25c7  Login successful\n\u2502\n\u2514  Done\n"
FAILED = "\u25c7  Failed to authorize\n"
KEY_PROMPT = "\n\u250c  Add credential\n\u2502\n\u25c6  Enter your API key\n\u2502  _\n\u2514\n"
UNKNOWN = ('\n\u250c  Add credential\nError: Unknown method "nope" for openai. Available:'
           ' ChatGPT Pro/Plus (browser), ChatGPT Pro/Plus (headless), Manually enter API Key\n')
SELECT = ("\n\u250c  Add credential\n\u2502\n\u25c6  Select GitHub deployment type\n"
          "\u2502  \u25cf GitHub.com (Public)\n\u2502  \u25cb GitHub Enterprise\n\u2514\n")
OAUTH_ENTRY = {"type": "oauth", "refresh": "fake-refresh-token", "access": "fake-access-token",
               "expires": 1}


class OcLoginCase(LoginCase):
    def setUp(self):
        super().setUp()
        (self.root / "config" / "accounts.toml").write_text(TOML)
        self.acc = accounts.load(self.root)
        # never the operator's own: a cousin-account run under test sees none of these
        patcher = mock.patch.dict(os.environ, {
            "OPENAI_API_KEY": "sk-from-the-operators-shell", "ANTHROPIC_API_KEY": "sk-ant-shell",
            "OPENCODE_CONFIG_CONTENT": '{"plugin":["something"]}', "DISPLAY": ":0"})
        patcher.start(); self.addCleanup(patcher.stop)

    def auth_path(self, name="keyed"):
        return self.acc[name].data_dir.joinpath(*accounts.AUTH_JSON)

    def auth(self, name="keyed"):
        return json.loads(self.auth_path(name).read_text())

    def seed(self, entries, name="keyed", mode=0o600, dir_mode=0o755):
        path = self.auth_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.acc[name].data_dir, 0o700)
        os.chmod(path.parent, dir_mode)                  # opencode makes it 0755 (measured)
        path.write_text(json.dumps(entries, indent=2)); os.chmod(path, mode)
        return path

    def cli(self, *argv, stdin=None, tty=False, getpass_value=None):
        out, err = io.StringIO(), io.StringIO()
        if stdin is None:
            stdin = io.StringIO("")
        stdin.isatty = lambda: tty
        gp = mock.Mock(return_value=getpass_value)
        with mock.patch.object(sys, "stdin", stdin), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err), \
                mock.patch.object(accounts, "_inside_cousin_ancestry", return_value=None), \
                mock.patch("getpass.getpass", gp):
            rc = accounts.account_main([*argv, "--root", str(self.root)])
        self.getpass = gp
        return rc, out.getvalue(), err.getvalue()

    def files_holding(self, needle):
        """Every file under the root whose bytes hold `needle`."""
        hits = []
        for path in self.root.rglob("*"):
            if path.is_file() and not path.is_symlink() and needle.encode() in path.read_bytes():
                hits.append(path)
        return hits

    def key_file(self, value, mode=0o600, dir_mode=0o700):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        d = pathlib.Path(tmp.name) / "keys"; d.mkdir(); os.chmod(d, dir_mode)
        path = d / "openai.key"; path.write_text(value); os.chmod(path, mode)
        return path


class TestApiKey(OcLoginCase):
    def test_a_key_from_stdin_is_written_as_opencode_writes_it(self):
        rc, out, err = self.cli("login", "keyed", "--provider", "openai",
                                stdin=io.StringIO(KEY + "\nignored second line\n"))
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.auth(), {"openai": {"type": "api", "key": KEY}})
        path = self.auth_path()
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.acc["keyed"].data_dir.stat().st_mode), 0o700)
        self.assertEqual(list(path.parent.iterdir()), [path])          # no tmp left behind
        self.assertIn("login keyed: ok", out)
        self.assertIn("mistral", out)                  # what is still missing, named
        self.getpass.assert_not_called()

    def test_a_terminal_reads_the_key_hidden(self):
        rc, out, err = self.cli("login", "keyed", "--provider", "openai", tty=True,
                                getpass_value="  " + KEY + "  ")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.auth()["openai"]["key"], KEY)
        self.getpass.assert_called_once()
        self.assertIn("openai", self.getpass.call_args[0][0])

    def test_a_strict_key_file(self):
        rc, out, err = self.cli("login", "keyed", "--provider", "openai",
                                "--key-file", str(self.key_file(KEY + "\n")))
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.auth()["openai"], {"type": "api", "key": KEY})

    def test_a_loose_or_odd_key_file_is_refused_and_nothing_written(self):
        link_target = self.key_file(KEY)
        link = link_target.parent / "link.key"; link.symlink_to(link_target)
        cases = {
            "chmod 600": self.key_file(KEY, mode=0o644),
            "chmod 700": self.key_file(KEY, dir_mode=0o755),
            "symlink": link,
            "one line": self.key_file(KEY + "\n" + KEY + "x\n"),
            "does not exist": link_target.parent / "absent.key",
        }
        for needle, path in cases.items():
            rc, out, err = self.cli("login", "keyed", "--provider", "openai",
                                    "--key-file", str(path))
            self.assertEqual(rc, 2, needle)
            self.assertIn(needle, err.replace("Too many levels of symbolic links", "symlink"),
                          needle)
            self.assertNotIn(KEY, out + err)
            self.assertFalse(self.auth_path().exists(), needle)

    def test_the_merge_keeps_every_other_provider_and_replaces_its_own(self):
        others = {"mistral": {"type": "api", "key": "fake-mistral-old"},
                  "github-copilot": OAUTH_ENTRY}
        self.seed({**others, "openai": {"type": "api", "key": "sk-fake-old"}})
        rc, out, err = self.cli("login", "keyed", "--provider", "openai",
                                stdin=io.StringIO(KEY + "\n"))
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.auth(), {**others, "openai": {"type": "api", "key": KEY}})
        self.assertEqual(stat.S_IMODE(self.auth_path().parent.stat().st_mode), 0o700)  # tightened

    def test_a_loose_or_unreadable_existing_auth_json_is_refused_untouched(self):
        for mode, content, needle in ((0o644, None, "chmod 600"),
                                      (0o600, "not json", "not JSON"),
                                      (0o600, "[1, 2]", "JSON object")):
            path = self.seed({"mistral": {"type": "api", "key": "fake-mistral"}}, mode=mode)
            if content is not None:
                path.write_text(content)
            before = path.read_bytes()
            rc, out, err = self.cli("login", "keyed", "--provider", "openai",
                                    stdin=io.StringIO(KEY + "\n"))
            self.assertEqual(rc, 2, needle); self.assertIn(needle, err)
            self.assertEqual(path.read_bytes(), before)
            self.assertNotIn(KEY, out + err)

    def test_anthropic_takes_an_api_key_and_replaces_an_oauth_login(self):
        self.seed({"anthropic": OAUTH_ENTRY}, name="metered")
        rc, out, err = self.cli("login", "metered", "--provider", "anthropic",
                                stdin=io.StringIO("sk-ant-api03-fake\n"))
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.auth("metered"),
                         {"anthropic": {"type": "api", "key": "sk-ant-api03-fake"}})
        self.assertTrue(accounts.status(self.acc["metered"], self.root)["loggedIn"])

    def test_a_file_holding_a_claude_subscription_is_never_written_into(self):
        self.seed({"anthropic": OAUTH_ENTRY})
        before = self.auth_path().read_bytes()
        rc, out, err = self.cli("login", "keyed", "--provider", "openai",
                                stdin=io.StringIO(KEY + "\n"))
        self.assertEqual(rc, 2); self.assertIn("Anthropic OAuth", err)
        self.assertEqual(self.auth_path().read_bytes(), before)

    def test_an_empty_or_malformed_key_is_refused_without_repeating_it(self):
        for text, needle in (("\n", "empty"), ("two words-secret\n", "one word"),
                             ("x" * 600 + "\n", "one word")):
            rc, out, err = self.cli("login", "keyed", "--provider", "openai",
                                    stdin=io.StringIO(text))
            self.assertEqual(rc, 2, text); self.assertIn(needle, err)
            self.assertNotIn("words-secret", out + err)
            self.assertFalse(self.auth_path().exists())

    def test_the_key_is_in_auth_json_and_nowhere_else(self):
        rc, out, err = self.cli("login", "keyed", "--provider", "openai",
                                stdin=io.StringIO(KEY + "\n"))
        self.assertEqual(rc, 0, err)
        self.assertNotIn(KEY, out + err)
        self.assertEqual(self.files_holding(KEY), [self.auth_path()])
        rc, out, err = self.cli("status", "keyed")
        self.assertNotIn(KEY, out + err)
        self.assertFalse((self.root / "run").exists())          # no capture, no relay
        self.assertEqual(self.chat_texts(), [])

    def test_no_terminal_is_needed_for_a_key(self):
        rc, out, err = self.cli("login", "keyed", "--provider", "openai",
                                stdin=io.StringIO(KEY + "\n"), tty=False)
        self.assertEqual(rc, 0, err)


class TestRefusals(OcLoginCase):
    def refused(self, *argv, needle, name="keyed"):
        stdin = io.StringIO(KEY + "\n")
        rc, out, err = self.cli("login", name, *argv, stdin=stdin, tty=True, getpass_value=KEY)
        self.assertEqual(rc, 2, (argv, err)); self.assertIn(needle, err, argv)
        self.getpass.assert_not_called()                      # refused before the key is asked
        self.assertEqual(stdin.tell(), 0, argv)
        self.assertNotIn(KEY, out + err)
        self.assertFalse(self.acc["keyed"].data_dir.joinpath(*accounts.AUTH_JSON).exists())

    def test_anthropic_by_oauth_is_a_claude_subscription(self):
        self.refused("--provider", "anthropic", "--method", "Claude Pro/Max", name="metered",
                     needle="Claude subscription")
        self.refused("--provider", "openai", "--method", "Claude Pro/Max", needle="Claude")

    def test_opencodes_own_hosted_service(self):
        self.refused("--provider", "opencode", needle="hosted")
        self.refused("--provider", "opencode", "--method", "OpenCode Console account",
                     needle="hosted")

    def test_every_bridge_marker(self):
        self.refused("--provider", "claude-max-proxy", needle="bridge")
        self.refused("--provider", "openai", "--method", "opencode-with-claude", needle="bridge")

    def test_a_provider_the_account_does_not_name(self):
        self.refused("--provider", "groq", needle="providers")

    def test_an_endpoint_account_or_a_claude_account(self):
        self.refused("--provider", "openai", name="local", needle="endpoint")
        self.refused("--provider", "openai", name="fleet", needle="opencode")
        self.refused("--key-file", "/nonexistent", name="fleet", needle="opencode")

    def test_the_flags_that_do_not_go_together(self):
        self.refused(needle="--provider")                     # an opencode account names one
        self.refused("--provider", "openai", "--via", "wren", needle="never travels through chat")
        self.refused("--provider", "openai", "--method", HEADLESS, "--key-file", "/k",
                     needle="--key-file")

    def test_a_malformed_provider_id(self):
        self.refused("--provider", "Open AI", needle="provider")

    def test_an_oauth_method_wants_a_terminal(self):
        rc, out, err = self.cli("login", "keyed", "--provider", "openai", "--method", HEADLESS,
                                tty=False)
        self.assertEqual(rc, 2); self.assertIn("terminal", err)


class TestOAuthFlow(OcLoginCase):
    def flow(self, fake, relay=None, provider="openai", method=HEADLESS, name="keyed"):
        self.relayed = []

        def record(url, instructions):
            self.relayed.append((url, instructions))
            if relay:
                relay()
        return accounts.opencode_login_flow(self.acc[name], self.root, provider=provider,
                                            method=method, relay=record, spawn=fake,
                                            binary="/opt/fake/opencode")

    def complete(self, entry=OAUTH_ENTRY, provider="openai"):
        def write():                     # what opencode does once the operator signs in
            path = self.auth_path(); path.parent.mkdir(parents=True, exist_ok=True)
            data = json.loads(path.read_text()) if path.exists() else {}
            data[provider] = entry
            path.write_text(json.dumps(data)); os.chmod(path, 0o600)
        return write

    def test_the_url_and_the_instructions_are_relayed_and_the_file_decides(self):
        fake = FakePty([GO + DONE])
        out = self.flow(fake, relay=self.complete())
        self.assertTrue(out["ok"], out)
        self.assertEqual(self.relayed, [(URL, "Enter code: WXYZ-1234")])
        self.assertEqual(fake.argv, ["/opt/fake/opencode", "auth", "login", "--pure",
                                     "--provider", "openai", "--method", HEADLESS])
        d = self.acc["keyed"].data_dir
        self.assertEqual(fake.env["HOME"], str(d))
        self.assertEqual(fake.env["XDG_DATA_HOME"], str(d / "data"))
        self.assertEqual(fake.env["BROWSER"], "/bin/false")
        for var in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENCODE_CONFIG_CONTENT", "DISPLAY"):
            self.assertNotIn(var, fake.env)
        self.assertEqual(fake.written, [])               # nothing is pasted back in 1.18.31
        self.assertTrue(fake.closed)
        self.assertNotIn("fake-access-token", json.dumps(out))
        self.assertEqual(stat.S_IMODE(self.auth_path().parent.stat().st_mode), 0o700)

    def test_login_successful_on_screen_without_the_file_is_a_failure(self):
        out = self.flow(FakePty([GO + DONE]))
        self.assertFalse(out["ok"]); self.assertIn("auth.json", out["reason"])

    def test_a_failure_in_the_clis_own_words(self):
        out = self.flow(FakePty([GO + FAILED]))
        self.assertFalse(out["ok"]); self.assertIn("Failed to authorize", out["reason"])

    def test_no_login_within_the_window(self):
        fake = FakePty([GO])
        out = self.flow(fake)
        self.assertFalse(out["ok"]); self.assertIn("no login", out["reason"])
        self.assertEqual(len(self.relayed), 1); self.assertTrue(fake.closed)

    def test_an_api_key_prompt_is_never_answered_through_the_pty(self):
        fake = FakePty([KEY_PROMPT])
        out = self.flow(fake, method="Manually enter API Key")
        self.assertFalse(out["ok"]); self.assertIn("without --method", out["reason"])
        self.assertEqual((self.relayed, fake.written), ([], []))

    def test_an_unknown_method_in_the_clis_words(self):
        out = self.flow(FakePty([UNKNOWN]), method="nope")
        self.assertFalse(out["ok"]); self.assertIn('Unknown method "nope"', out["reason"])
        self.assertEqual(self.relayed, [])

    def test_a_prompt_the_relay_cannot_answer_is_named(self):
        out = self.flow(FakePty([SELECT]), method="Login with GitHub Copilot")
        self.assertFalse(out["ok"])
        self.assertIn("Select GitHub deployment type", out["reason"])
        self.assertEqual(self.relayed, [])

    def test_a_subscription_login_the_cli_stored_is_reported(self):
        # the CLI may store the result under another provider id (opencode's
        # `result.provider ?? provider`): an Anthropic OAuth entry is refused
        out = self.flow(FakePty([GO + DONE]), relay=self.complete(provider="anthropic"))
        self.assertFalse(out["ok"]); self.assertIn("Anthropic OAuth", out["reason"])

    def test_the_refusals_come_before_any_process(self):
        spawn = mock.Mock(side_effect=AssertionError("spawned"))
        for provider, method, name in (("anthropic", "Claude Pro/Max", "metered"),
                                       ("opencode", "OpenCode Console account", "keyed"),
                                       ("openai", "via meridian", "keyed")):
            with self.assertRaises(accounts.AccountsError):
                accounts.opencode_login_flow(self.acc[name], self.root, provider=provider,
                                             method=method, relay=print, spawn=spawn,
                                             binary="/opt/fake/opencode")
        spawn.assert_not_called()

    def test_the_binary_is_an_absolute_path_from_the_env_or_path(self):
        with mock.patch.dict(os.environ, {"COUSIN_OPENCODE_BIN": "opencode"}):
            with self.assertRaises(accounts.AccountsError) as cm:
                accounts.opencode_bin()
            self.assertIn("absolute", str(cm.exception))
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        with mock.patch.dict(os.environ, {"PATH": tmp.name}):
            os.environ.pop("COUSIN_OPENCODE_BIN", None)
            with self.assertRaises(accounts.AccountsError) as cm:
                accounts.opencode_bin()
            self.assertIn("COUSIN_OPENCODE_BIN", str(cm.exception))
            exe = pathlib.Path(tmp.name) / "opencode"; exe.write_text("#!/bin/sh\n")
            os.chmod(exe, 0o700)
            self.assertEqual(accounts.opencode_bin(), str(exe.resolve()))


FAKE_OPENCODE = textwrap.dedent(r'''
    #!%(python)s
    # opencode 1.18.31 `auth login --provider P --method M` as measured, "auto" OAuth:
    # the URL, the instruction line, a spinner; then (the operator signed in) the
    # credential lands in $XDG_DATA_HOME/opencode/auth.json and the CLI says so.
    import json, os, sys, time
    args = sys.argv[1:]
    provider = args[args.index("--provider") + 1]
    w = sys.stdout.write
    w("\x1b[0m\r\n\x1b[90m\u250c\x1b[0m  Add credential\r\n\x1b[90m\u2502\x1b[0m\r\n")
    w("\x1b[94m\u25cf\x1b[0m  Go to: %(url)s\r\n\x1b[90m\u2502\x1b[0m\r\n")
    w("\x1b[94m\u25cf\x1b[0m  Enter code: WXYZ-1234\r\n\x1b[90m\u2502\x1b[0m\r\n")
    for frame in "\u25d2\u25d0\u25d3\u25d1" * 3:
        w("\x1b[2K\x1b[1G\x1b[35m%%s\x1b[0m  Waiting for authorization..." %% frame)
        sys.stdout.flush(); time.sleep(0.05)
    path = os.path.join(os.environ["XDG_DATA_HOME"], "opencode", "auth.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = json.load(open(path)) if os.path.exists(path) else {}
    data[provider] = {"type": "oauth", "refresh": "fake-refresh-token",
                      "access": "fake-access-token", "expires": 1}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.write(fd, json.dumps(data, indent=2).encode()); os.close(fd)
    w("\x1b[2K\x1b[1G\x1b[32m\u25c7\x1b[0m  Login successful\r\n\x1b[90m\u2514\x1b[0m  Done\r\n")
    sys.stdout.flush()
''').lstrip()


class TestOAuthThroughARealPty(OcLoginCase):
    """The flow through the real pty driver against a scripted opencode."""

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.bin = pathlib.Path(tmp.name) / "opencode"
        self.bin.write_text(FAKE_OPENCODE % {"python": sys.executable, "url": URL})
        os.chmod(self.bin, 0o700)

    def test_the_flow_end_to_end(self):
        relayed = []
        out = accounts.opencode_login_flow(self.acc["keyed"], self.root, provider="openai",
                                           method=HEADLESS,
                                           relay=lambda u, i: relayed.append((u, i)),
                                           binary=str(self.bin), timeout=20)
        self.assertTrue(out["ok"], out)
        self.assertEqual(relayed, [(URL, "Enter code: WXYZ-1234")])
        self.assertEqual(self.auth()["openai"]["type"], "oauth")
        self.assertEqual(out["status"]["providers"], ["openai"])

    def test_via_relays_the_url_to_the_operator_and_arms_nothing(self):
        with mock.patch.dict(os.environ, {"COUSIN_OPENCODE_BIN": str(self.bin)}):
            rc, out, err = self.cli("login", "keyed", "--provider", "openai", "--method",
                                    HEADLESS, "--via", "wren", "--timeout", "20", tty=True)
        self.assertEqual(rc, 0, err)
        texts = self.chat_texts()
        self.assertEqual(len(texts), 1)
        self.assertIn(URL, texts[0]); self.assertIn("Enter code: WXYZ-1234", texts[0])
        self.assertIn("nothing to paste", texts[0].lower())
        self.assertIsNone(accounts.read_capture(self.root, "keyed"))    # no code is taken
        for secret in ("fake-access-token", "fake-refresh-token"):
            self.assertNotIn(secret, out + err + "".join(texts))
            self.assertEqual(self.files_holding(secret), [self.auth_path()])


if __name__ == "__main__":
    unittest.main()
