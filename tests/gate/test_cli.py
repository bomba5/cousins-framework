"""CLI contract: exit codes are the gate's interface to CI."""
import contextlib
import io
import pathlib
import tempfile
import unittest

from cousin_lib.gate.cli import main


class TestCli(unittest.TestCase):
    @unittest.skipUnless(__import__("shutil").which("git"), "needs git")
    def test_git_visible_skips_ignored_paths(self):
        import subprocess
        root = self._tree({".gitignore": "cousins/\n",
                           "cousins/testa/x.txt": "who = 'zorblatt'\n",
                           "a.py": "x = 1\n"})
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        deny = self._denylist(["zorblatt"])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--root", str(root), "--denylist",
                                   str(deny), "--git-visible"]), 0)
            self.assertEqual(main(["--root", str(root), "--denylist",
                                   str(deny)]), 1)

    def _tree(self, files):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = pathlib.Path(tmp.name)
        for rel, content in files.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        return root

    @unittest.skipUnless(__import__("shutil").which("git"), "needs git")
    def test_commits_mode_scans_messages_trailers_and_identity(self):
        import subprocess
        root = self._tree({"a.py": "x = 1\n"})
        me = "Testa <testa@example.invalid>"

        def git(*a, who=me):
            name, email = who[:-1].split(" <")
            subprocess.run(["git", "-C", str(root), "-c", "user.name=" + name,
                            "-c", "user.email=" + email, *a], check=True,
                           capture_output=True)
        git("init", "-q")
        git("add", "-A"); git("commit", "-qm", "base")
        git("commit", "-q", "--allow-empty", "-m", "clean subject")
        clean = ["--root", str(root), "--commits", "HEAD~1..HEAD", "--expect-author", me]
        deny = self._denylist(["zorblatt"])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(clean + ["--denylist", str(deny)]), 0)
        for msg, who in (("fix: as zorblatt asked", me),
                         ("fix: x\n\nCo-authored-by: Sam <sam@example.invalid>", me),
                         ("fix: y", "Mallory <mallory@example.invalid>")):
            git("commit", "-q", "--allow-empty", "-m", msg, who=who)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = main(["--root", str(root), "--commits", "HEAD~1..HEAD",
                           "--expect-author", me, "--denylist", str(deny)])
            self.assertEqual(rc, 1, msg)
            self.assertIn("commit:", out.getvalue())
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as bad:
            main(["--root", str(root), "--commits", "no-such-ref..HEAD"])
        self.assertEqual(bad.exception.code, 2)

    def test_gate_mode_exits_zero_on_clean_tree(self):
        root = self._tree({"a.py": "x = 1\n"})
        self.assertEqual(main(["--root", str(root)]), 0)

    def _denylist(self, terms):
        # The list lives OUTSIDE the scanned root, as it does in real use.
        d = self._tree({"terms.txt": "\n".join(terms) + "\n"})
        return d / "terms.txt"

    def test_gate_mode_exits_one_and_reports_on_hits(self):
        root = self._tree({"a.py": "who = 'zorblatt'\n"})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(
                ["--root", str(root), "--denylist", str(self._denylist(["zorblatt"]))]
            )
        self.assertEqual(code, 1)
        self.assertIn("a.py:1", out.getvalue())

    def test_triage_mode_prints_manifest_and_exits_zero(self):
        root = self._tree({"a.py": "# zorblatt\n"})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(
                [
                    "--root",
                    str(root),
                    "--denylist",
                    str(self._denylist(["zorblatt"])),
                    "--mode",
                    "triage",
                ]
            )
        self.assertEqual(code, 0)
        self.assertIn('"position": "comment"', out.getvalue())


if __name__ == "__main__":
    unittest.main()
