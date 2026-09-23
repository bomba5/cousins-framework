"""cousin-account status on this host's default account. Opt in: COUSIN_LIVE_SDK=1. One run."""
import os
import subprocess
import sys
import unittest

from tests._hermetic import HermeticCase


@unittest.skipUnless(os.environ.get("COUSIN_LIVE_SDK") == "1", "set COUSIN_LIVE_SDK=1")
class TestLiveAccountStatus(HermeticCase):
    def test_the_hosts_default_account_reads_logged_in(self):
        proc = subprocess.run([sys.executable, "-m", "cousin_lib.accounts", "status", "host"],
                              capture_output=True, text=True, timeout=60)
        print("\nREPORT cousin-account status host: rc=%d %s" % (proc.returncode, proc.stdout.strip()))
        self.assertIn("account=host kind=claude-login", proc.stdout)


if __name__ == "__main__":
    unittest.main()
