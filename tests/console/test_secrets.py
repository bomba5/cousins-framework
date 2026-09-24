"""console/secrets: the one writer a console route uses for a pasted
secret (a key, a token): 0600 from the first byte through os.open, in a
0700 parent, atomic, never following a planted symlink; and the one
reader that says what may be shown about it (set, last four at most)."""
import os
import stat
import tempfile
import unittest
from pathlib import Path

from cousin_lib.console import secrets


class WriteSecretFile(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_the_file_is_0600_and_its_new_parent_0700_whatever_the_umask(self):
        path = self.root / "a" / "b" / "token"
        old = os.umask(0)
        try:
            out = secrets.write_secret_file(path, "s3cret-value-long-enough")
        finally:
            os.umask(old)
        self.assertEqual(path.read_text(), "s3cret-value-long-enough")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertEqual(out, {"set": True, "last4": "ough"})
        self.assertNotIn("s3cret", repr(out))

    def test_replacing_is_atomic_and_leaves_no_tmp(self):
        path = self.root / "token"
        secrets.write_secret_file(path, "first-value-0000000000")
        secrets.write_secret_file(path, "second-value-111111111")
        self.assertEqual(path.read_text(), "second-value-111111111")
        self.assertEqual([p.name for p in self.root.iterdir()], ["token"])

    def test_a_symlink_planted_at_the_tmp_path_is_not_followed(self):
        path = self.root / "token"
        victim = self.root / "victim"
        victim.write_text("untouched")
        os.symlink(victim, self.root / ".token.tmp")
        secrets.write_secret_file(path, "value-xxxxxxxxxxxxxxxx")
        self.assertEqual(victim.read_text(), "untouched")
        self.assertFalse(path.is_symlink())
        self.assertEqual(path.read_text(), "value-xxxxxxxxxxxxxxxx")

    def test_a_path_outside_the_given_root_is_refused(self):
        with self.assertRaises(ValueError):
            secrets.write_secret_file(self.root / ".." / "escape", "v", within=self.root)
        with self.assertRaises(ValueError):
            secrets.write_secret_file("/tmp/elsewhere", "v", within=self.root)
        secrets.write_secret_file(self.root / "in" / "ok", "v", within=self.root)

    def test_a_non_string_or_empty_value_is_refused(self):
        for bad in (None, "", 42, "a\x00b"):
            with self.assertRaises(ValueError):
                secrets.write_secret_file(self.root / "t", bad)
        self.assertFalse((self.root / "t").exists())

    def test_a_failed_write_leaves_the_old_file_and_no_tmp(self):
        path = self.root / "token"
        secrets.write_secret_file(path, "keep-this-value-0000")
        real_replace = os.replace

        def boom(*a, **k):
            raise OSError("disk full")
        os.replace = boom
        try:
            with self.assertRaises(OSError):
                secrets.write_secret_file(path, "new-value-1111111111")
        finally:
            os.replace = real_replace
        self.assertEqual(path.read_text(), "keep-this-value-0000")
        self.assertEqual([p.name for p in self.root.iterdir()], ["token"])


class SecretState(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_missing_is_not_set(self):
        self.assertEqual(secrets.secret_state(self.root / "none"), {"set": False, "last4": None})

    def test_a_short_value_shows_set_only(self):
        path = self.root / "short"
        secrets.write_secret_file(path, "abcd1234")
        self.assertEqual(secrets.secret_state(path), {"set": True, "last4": None})

    def test_trailing_whitespace_is_not_part_of_the_last_four(self):
        path = self.root / "t"
        secrets.write_secret_file(path, "0123456789abcdefWXYZ\n")
        self.assertEqual(secrets.secret_state(path), {"set": True, "last4": "WXYZ"})


if __name__ == "__main__":
    unittest.main()
