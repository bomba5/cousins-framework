"""accounts.write_entry: the one validated writer of config/accounts.toml
(the console's Accounts view adds, edits and removes entries through it).
Every other line of the file is kept; the result is validated with the
same rules accounts.load reads it by, before the atomic rename; a refusal
writes nothing."""
import os
import pathlib
import stat
import tempfile
import tomllib

from cousin_lib import accounts
from tests._hermetic import HermeticCase

START = """# the accounts this install runs on
[accounts.fleet]
kind = "claude-login"   # the shared login

# the nightly token
[accounts.nightly]
kind = "claude-token"
"""


class WriteCase(HermeticCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        (self.root / "config").mkdir()
        self.path = self.root / "config" / "accounts.toml"

    def seed(self, text=START, mode=0o640):
        self.path.write_text(text)
        os.chmod(self.path, mode)

    def text(self):
        return self.path.read_text()


class Add(WriteCase):
    def test_adds_an_entry_and_keeps_every_other_line(self):
        self.seed()
        acc = accounts.write_entry(self.root, "keyed", {"kind": "opencode",
                                                        "providers": ["openai", "mistral"]},
                                   expect="absent")
        self.assertEqual((acc.name, acc.kind, acc.providers),
                         ("keyed", "opencode", ("openai", "mistral")))
        text = self.text()
        self.assertTrue(text.startswith(START.rstrip("\n")))      # byte for byte
        self.assertIn('kind = "claude-login"   # the shared login', text)
        self.assertEqual(sorted(accounts.load(self.root)), ["fleet", "keyed", "nightly"])
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o640)   # mode kept

    def test_creates_the_file_when_there_is_none(self):
        accounts.write_entry(self.root, "fleet", {"kind": "claude-login"}, expect="absent")
        self.assertEqual(tomllib.loads(self.text()),
                         {"accounts": {"fleet": {"kind": "claude-login"}}})

    def test_an_existing_name_is_refused_when_absent_is_expected(self):
        self.seed()
        with self.assertRaisesRegex(accounts.AccountsError, "already"):
            accounts.write_entry(self.root, "fleet", {"kind": "claude-login"}, expect="absent")
        self.assertEqual(self.text(), START)

    def test_refusals_write_nothing(self):
        self.seed()
        bad = [
            ("host", {"kind": "claude-login"}, "name"),
            ("Bad Name", {"kind": "claude-login"}, "name"),
            ("x", {"kind": "nope"}, "kind"),
            ("x", {"kind": "claude-login", "secret_file": "a"}, "not allowed"),
            ("x", {"kind": "claude-token", "secret_file": "/etc/passwd"}, "relative"),
            ("x", {"kind": "claude-token", "secret_file": "../outside"}, "leaves the framework root"),
            ("x", {"kind": "opencode"}, "exactly one of"),
            ("x", {"kind": "opencode", "providers": ["anthropic"]}, "SDK and nowhere else"),
            ("x", {"kind": "opencode", "endpoint": "http://127.0.0.1/v1",
                   "endpoint_model": "claude-proxy"}, "SDK and nowhere else"),
            ("x", {"kind": "opencode", "endpoint": "http://u:p@127.0.0.1/v1",
                   "endpoint_model": "m"}, "credentials"),
            ("x", {"kind": "claude-login", "config_dir": True}, None),
            ("x", "not a table", "table"),
        ]
        for name, entry, words in bad:
            with self.subTest(name=name, entry=entry):
                try:
                    accounts.write_entry(self.root, name, entry)
                except accounts.AccountsError as err:
                    if words:
                        self.assertIn(words, str(err))
                else:
                    self.fail("accepted %r" % (entry,))
                self.assertEqual(self.text(), START)

    def test_an_endpoint_query_that_carries_a_secret_is_refused(self):
        for url in ("http://127.0.0.1:1/v1?api_key=s3cr3tvalue", "http://h/v1?token=s3cr3tvalue",
                    "http://h/v1#access_token=s3cr3tvalue"):
            with self.subTest(url=url):
                with self.assertRaisesRegex(accounts.AccountsError, "credential") as caught:
                    accounts.write_entry(self.root, "x", {"kind": "opencode", "endpoint": url,
                                                          "endpoint_model": "m"})
                self.assertNotIn("s3cr3tvalue", str(caught.exception))
        accounts.write_entry(self.root, "x", {"kind": "opencode",
                                              "endpoint": "http://h/v1?format=json",
                                              "endpoint_model": "m"})

    def test_the_error_never_repeats_an_endpoint(self):
        with self.assertRaises(accounts.AccountsError) as caught:
            accounts.write_entry(self.root, "x", {"kind": "opencode",
                                                  "endpoint": "http://u:hunter2@h/v1",
                                                  "endpoint_model": "m"})
        self.assertNotIn("hunter2", str(caught.exception))


class Edit(WriteCase):
    def test_replaces_the_entry_whole(self):
        self.seed(START + '\n[accounts.keyed]\nkind = "opencode"\nproviders = ["openai"]\n')
        accounts.write_entry(self.root, "keyed", {"kind": "opencode",
                                                  "endpoint": "http://127.0.0.1:11434/v1",
                                                  "endpoint_model": "qwen3-coder"},
                             expect="present")
        entry = tomllib.loads(self.text())["accounts"]["keyed"]
        self.assertEqual(entry, {"kind": "opencode", "endpoint": "http://127.0.0.1:11434/v1",
                                 "endpoint_model": "qwen3-coder"})   # providers gone
        self.assertTrue(self.text().startswith(START))

    def test_a_missing_entry_is_refused_when_present_is_expected(self):
        self.seed()
        with self.assertRaisesRegex(accounts.AccountsError, "no account"):
            accounts.write_entry(self.root, "ghost", {"kind": "claude-login"}, expect="present")

    def test_a_file_that_does_not_parse_is_left_alone(self):
        self.seed("[accounts.fleet\nkind = ")
        with self.assertRaisesRegex(accounts.AccountsError, "by hand"):
            accounts.write_entry(self.root, "x", {"kind": "claude-login"})
        self.assertEqual(self.text(), "[accounts.fleet\nkind = ")

    def test_an_edit_can_fix_the_one_broken_entry(self):
        self.seed(START + '\n[accounts.broken]\nkind = "nope"\n')
        with self.assertRaises(accounts.AccountsError):
            accounts.load(self.root)
        accounts.write_entry(self.root, "broken", {"kind": "claude-login"})
        self.assertEqual(accounts.load(self.root)["broken"].kind, "claude-login")


class Check(WriteCase):
    def test_the_check_sees_the_new_account_and_can_refuse(self):
        self.seed()
        seen = []

        def refuse(account):
            seen.append(account)
            raise accounts.AccountsError("a cousin runs on it")
        with self.assertRaisesRegex(accounts.AccountsError, "a cousin runs on it"):
            accounts.write_entry(self.root, "fleet", {"kind": "claude-token"}, check=refuse)
        self.assertEqual(seen[0].kind, "claude-token")
        with self.assertRaises(accounts.AccountsError):
            accounts.write_entry(self.root, "fleet", None, check=refuse)
        self.assertIsNone(seen[1])
        self.assertEqual(self.text(), START)


class Remove(WriteCase):
    def test_removes_the_table_and_nothing_else(self):
        self.seed()
        self.assertIsNone(accounts.write_entry(self.root, "fleet", None, expect="present"))
        parsed = tomllib.loads(self.text())
        self.assertEqual(parsed, {"accounts": {"nightly": {"kind": "claude-token"}}})
        self.assertIn("# the nightly token\n[accounts.nightly]", self.text())
        self.assertNotIn("claude-login", self.text())

    def test_removing_a_missing_entry_is_refused(self):
        self.seed()
        with self.assertRaisesRegex(accounts.AccountsError, "no account"):
            accounts.write_entry(self.root, "ghost", None)
        self.assertEqual(self.text(), START)

    def test_an_inline_entry_is_left_to_a_hand_edit(self):
        self.seed('[accounts]\nfleet = { kind = "claude-login" }\n')
        with self.assertRaisesRegex(accounts.AccountsError, "by hand"):
            accounts.write_entry(self.root, "fleet", None)


if __name__ == "__main__":
    import unittest
    unittest.main()
