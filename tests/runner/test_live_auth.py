"""--check-auth, --validate and the initialize answer on the real host.
Opt in: COUSIN_LIVE_SDK=1. One run."""
import os
import subprocess
import sys
import unittest

from tests._hermetic import HermeticCase
from tests.runner._home import temp_home


@unittest.skipUnless(os.environ.get("COUSIN_LIVE_SDK") == "1", "set COUSIN_LIVE_SDK=1")
class TestLiveAuth(HermeticCase):
    def _env(self):
        return {k: v for k, v in os.environ.items()
                if not k.startswith(("ANTHROPIC_", "CLAUDE_CODE_OAUTH", "CLAUDE_CONFIG_DIR"))}

    def test_check_auth_and_validate_on_this_host(self):
        home = temp_home(self, runner="sdk")
        for extra in ([], ["--validate"]):
            proc = subprocess.run([sys.executable, "-m", "cousin_lib.runner.main", "--home",
                                   str(home), "--check-auth", *extra], capture_output=True,
                                  text=True, timeout=180, env=self._env())
            print("\nREPORT check-auth %s rc=%d: %s" % (" ".join(extra), proc.returncode,
                                                        proc.stdout.strip()))
            self.assertIn(proc.returncode, (0, 4))
            self.assertIn("account=host kind=claude-login", proc.stdout)
        self.assertFalse((home / "data" / "sessions.db").exists())   # --validate left no trace

    def test_report_what_the_initialize_answer_carries(self):
        """Does get_server_info() carry account.tokenSource / apiKeySource
        after connect, with no model turn? Reported, not asserted."""
        import asyncio
        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

        async def go():
            client = ClaudeSDKClient(options=ClaudeAgentOptions(setting_sources=[]))
            await client.connect()
            try:
                info = await client.get_server_info() or {}
            finally:
                await client.disconnect()
            account = info.get("account") or {}
            return {k: account.get(k) for k in ("tokenSource", "apiKeySource", "subscriptionType")}
        print("\nREPORT initialize account: %s" % asyncio.run(go()))


if __name__ == "__main__":
    unittest.main()
