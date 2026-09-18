"""The gate gates its own repository.

This is the "runs from commit #1" rule made literal: the unit suite fails
if this tree ever contains an address literal, a home path, a secret shape,
or an opaque binary. Name terms come from the out-of-tree denylist at run
time; here the generic classes alone must hold the line.
"""
import pathlib
import shutil
import subprocess
import tempfile
import unittest

from cousin_lib.gate.scanner import Scanner, run_gate

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


class TestGateOnOwnTree(unittest.TestCase):
    def test_repository_passes_its_own_gate(self):
        # Only what git would publish: tracked files and new files not
        # ignored. A documented install keeps its live cousins/ and
        # config/ under the checkout, gitignored, full of this
        # machine's absolute paths; they are never tree content.
        result = run_gate(_REPO_ROOT, git_visible=True)
        details = "\n".join(
            "%s:%s %s %r" % (h.file, h.line, h.kind, h.context)
            for h in result.hits
        )
        self.assertTrue(result.passed, "gate hits in own tree:\n" + details)

    def test_dot_directories_are_not_a_blind_spot(self):
        # CI workflows and other dotfiles can carry secrets; a future
        # _SKIP_DIRS change that excluded .github would silently stop
        # gating them. Plant a term under a dot-directory and require
        # the scan to find it.
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            planted = root / ".github" / "workflows"
            planted.mkdir(parents=True)
            (planted / "ci.yml").write_text("# zqxjkbanned marker\n")
            hits = Scanner(name_terms=["zqxjkbanned"]).scan_tree(root)
            self.assertTrue(
                any(".github" in h.file for h in hits),
                "the scanner does not reach dot-directories")



@unittest.skipUnless(shutil.which("git"), "git is a documented prerequisite")
class TestGitVisibleScan(unittest.TestCase):
    """git_visible scans what git would publish - tracked files and
    untracked ones .gitignore does not exclude - and nothing ignored."""

    def _repo(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = pathlib.Path(tmp.name)
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        (root / ".gitignore").write_text("cousins/\n")
        (root / "cousins" / "testa").mkdir(parents=True)
        (root / "cousins" / "testa" / "live.json").write_text(
            "zqxjkbanned ignored\n")
        (root / "tracked.txt").write_text("zqxjkbanned tracked\n")
        subprocess.run(["git", "-C", str(root), "add", "tracked.txt",
                        ".gitignore"], check=True)
        (root / "new.txt").write_text("zqxjkbanned new\n")
        return root

    def test_ignored_paths_are_not_scanned(self):
        root = self._repo()
        result = run_gate(root, name_terms=["zqxjkbanned"], git_visible=True)
        files = sorted({h.file for h in result.hits})
        self.assertEqual(files, ["new.txt", "tracked.txt"])

    def test_the_default_still_scans_everything(self):
        root = self._repo()
        result = run_gate(root, name_terms=["zqxjkbanned"])
        self.assertIn("cousins/testa/live.json",
                      {h.file for h in result.hits})

    def test_outside_a_repository_it_falls_back_to_the_whole_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "a.txt").write_text("zqxjkbanned\n")
            result = run_gate(root, name_terms=["zqxjkbanned"],
                              git_visible=True)
        self.assertEqual([h.file for h in result.hits], ["a.txt"])


if __name__ == "__main__":
    unittest.main()
