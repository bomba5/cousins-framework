"""console/secrets: the one writer a console route uses for a pasted
secret (a key, a token), and the one reader that says what may be shown
about it (set, last four at most). The writer is the accounts' private
writer and refuses what the runner's reader (agent_auth.read_private_file,
accounts._read_secret) would refuse at the next start; it never leaves the
directory it is given, a symlinked directory included."""
import os
import stat
import tempfile
import unittest
from pathlib import Path

from cousin_lib import agent_auth
from cousin_lib.console import secrets

GOOD = "sk-ant-api03-0123456789abcdefWXYZ"


class WriteSecretFile(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def write(self, path, value=GOOD):
        return secrets.write_secret_file(path, value, within=self.root)

    def test_the_file_is_0600_and_its_new_parent_0700_whatever_the_umask(self):
        path = self.root / "a" / "b" / "token"
        old = os.umask(0)
        try:
            out = self.write(path)
        finally:
            os.umask(old)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertEqual(out, {"set": True, "last4": "WXYZ", "error": None})
        self.assertNotIn("sk-ant", repr(out))

    def test_the_runners_reader_accepts_what_it_wrote(self):
        path = self.root / "s" / "key"
        self.write(path, "  %s \n" % GOOD)
        raw = agent_auth.read_private_file(path)
        self.assertEqual(raw.decode().strip(), GOOD)

    def test_an_open_existing_parent_is_tightened(self):
        parent = self.root / "open"
        parent.mkdir()
        parent.chmod(0o755)
        self.write(parent / "k")
        self.assertEqual(stat.S_IMODE(parent.stat().st_mode), 0o700)
        agent_auth.read_private_file(parent / "k")

    def test_what_the_reader_refuses_is_refused_before_writing(self):
        for bad in ("two\nlines", "has spaces", "x" * 513, "", None, 42, "a\x00b"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.write(self.root / "t", bad)
        self.assertFalse((self.root / "t").exists())

    def test_within_is_required(self):
        with self.assertRaises(TypeError):
            secrets.write_secret_file(self.root / "t", GOOD)

    def test_a_path_outside_the_root_is_refused(self):
        for path in (self.root / ".." / "escape", Path("/tmp/elsewhere")):
            with self.assertRaises(ValueError):
                self.write(path)

    def test_a_symlinked_directory_inside_the_root_does_not_escape(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        os.symlink(outside.name, self.root / "link")
        with self.assertRaises(ValueError):
            self.write(self.root / "link" / "k")
        with self.assertRaises(ValueError):
            self.write(self.root / "link" / "new" / "k")
        self.assertEqual(os.listdir(outside.name), [])

    def test_replacing_is_atomic_and_leaves_no_tmp(self):
        path = self.root / "token"
        self.write(path, "first-value-0000000000")
        self.write(path, "second-value-111111111")
        self.assertEqual(path.read_text().strip(), "second-value-111111111")
        self.assertEqual([p.name for p in self.root.iterdir()], ["token"])

    def test_a_symlink_planted_at_the_tmp_or_final_path_is_not_followed(self):
        path = self.root / "token"
        victim = self.root / "victim"
        victim.write_text("untouched")
        os.symlink(victim, self.root / ".token.tmp")
        self.write(path)
        self.assertEqual(victim.read_text(), "untouched")
        os.symlink(victim, self.root / "final")
        self.write(self.root / "final")
        self.assertEqual(victim.read_text(), "untouched")
        self.assertFalse((self.root / "final").is_symlink())


class SecretState(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_missing_is_not_set(self):
        self.assertEqual(secrets.secret_state(self.root / "none"),
                         {"set": False, "last4": None, "error": None})

    def test_a_short_value_shows_set_only(self):
        path = self.root / "short"
        secrets.write_secret_file(path, "abcd1234", within=self.root)
        self.assertEqual(secrets.secret_state(path), {"set": True, "last4": None, "error": None})

    def test_a_symlink_is_not_followed(self):
        target = self.root / "real"
        secrets.write_secret_file(target, GOOD, within=self.root)
        os.symlink(target, self.root / "link")
        st = secrets.secret_state(self.root / "link")
        self.assertFalse(st["set"])
        self.assertIsNone(st["last4"])
        self.assertTrue(st["error"])
        self.assertNotIn("WXYZ", str(st))


if __name__ == "__main__":
    unittest.main()
