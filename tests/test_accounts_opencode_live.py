"""cousin-account login --provider against the real opencode binary (phase 9
R12'). Opt in: COUSIN_LIVE_OPENCODE=1 and OPENCODE_BIN=<absolute path>. No
credentials and no login completes: a dummy key is stored and read back by
`opencode auth list`, and the OAuth flow stops once the URL is relayed (the
browser method builds its URL locally; nothing is sent to the provider)."""
import json
import os
import pathlib
import re
import subprocess
import tempfile
import unittest

from cousin_lib import accounts
from tests._hermetic import HermeticCase

LIVE = os.environ.get("COUSIN_LIVE_OPENCODE") == "1" and os.path.isabs(
    os.environ.get("OPENCODE_BIN", ""))
DUMMY = "sk-DUMMY-not-a-key-0000"


@unittest.skipUnless(LIVE, "set COUSIN_LIVE_OPENCODE=1 and OPENCODE_BIN")
class TestLiveOpencodeLogin(HermeticCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        (self.root / "config").mkdir()
        (self.root / "config" / "accounts.toml").write_text(
            '[accounts.keyed]\nkind = "opencode"\nproviders = ["openai", "mistral"]\n')
        self.acc = accounts.load(self.root)["keyed"]
        self.bin = os.environ["OPENCODE_BIN"]

    def test_a_stored_key_is_what_opencode_reads(self):
        accounts.store_api_key(self.acc, "mistral", DUMMY)
        env = {"PATH": "/usr/bin:/bin", "TERM": "dumb", "OPENCODE_DISABLE_AUTOUPDATE": "1",
               **accounts.account_env(self.acc, self.root)}
        proc = subprocess.run([self.bin, "auth", "list"], env=env, capture_output=True,
                              text=True, timeout=60)
        out = re.sub(r"\x1b\[[0-9;]*m", "", proc.stdout + proc.stderr)
        print("\nREPORT opencode auth list: rc=%d %s" % (proc.returncode, " ".join(out.split())))
        self.assertEqual(proc.returncode, 0)
        self.assertIn("Mistral", out); self.assertIn("1 credential", out)
        self.assertNotIn(DUMMY, out)

    def test_the_oauth_screen_is_the_one_the_flow_reads(self):
        relayed = []
        out = accounts.opencode_login_flow(
            self.acc, self.root, provider="openai", method="ChatGPT Pro/Plus (browser)",
            relay=lambda u, i: relayed.append((u, i)), binary=self.bin, timeout=2)
        print("\nREPORT oauth: %s relayed=%s" % (json.dumps(out), relayed))
        self.assertEqual(len(relayed), 1)
        url, instructions = relayed[0]
        self.assertTrue(url.startswith("https://auth.openai.com/oauth/authorize?"), url)
        self.assertIn("Complete authorization in your browser", instructions)
        self.assertEqual(out, {"ok": False, "reason": "no login within 2s"})
        self.assertFalse(self.acc.data_dir.joinpath(*accounts.AUTH_JSON).exists())

    def test_an_api_key_method_and_an_unknown_one_end_before_any_relay(self):
        for method, needle in (("Manually enter API Key", "asks for an API key"),
                               ("nope", 'Unknown method "nope"')):
            relayed = []
            out = accounts.opencode_login_flow(
                self.acc, self.root, provider="openai", method=method,
                relay=lambda u, i: relayed.append(u), binary=self.bin, timeout=2)
            print("\nREPORT %s: %s" % (method, out["reason"]))
            self.assertFalse(out["ok"]); self.assertIn(needle, out["reason"])
            self.assertEqual(relayed, [])


if __name__ == "__main__":
    unittest.main()
