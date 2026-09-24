"""The reviewed self-portrait: synthesize -> review -> commit.

The review gate is the point of the design: a cousin's identity file
is never written directly, and the boot packet reads only what an
operator committed.
"""
import pathlib
import tempfile
import unittest

from cousin_lib.self_portrait import (
    commit_candidate,
    for_boot_packet,
    synthesize_candidate,
)


class PortraitCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = pathlib.Path(tmp.name) / "cousins" / "wren"
        (self.home / "data").mkdir(parents=True)
        (self.home / "cousin.toml").write_text(
            '[cousin]\nslug = "wren"\nname = "Wren"\n'
            'role = "example cousin"\n[chat]\nport = 8100\n'
        )
        (self.home / "CLAUDE.md").write_text(
            "# Wren - example\n\n## Identity\n\nYou are Wren.\n\n"
            "## Hard rules\n\n- follow the operator\n\n"
            "## Voice\n\nPlain and warm.\n"
        )


class TestSynthesize(PortraitCase):
    def test_candidate_carries_every_section(self):
        path = synthesize_candidate(str(self.home), "wren")
        text = path.read_text()
        for section in ("Role", "Temperament", "Operator Calibration",
                        "Working Style", "Recurring Risks", "Voice",
                        "Identity Invariants"):
            self.assertIn("## %s" % section, text)

    def test_sources_feed_their_sections(self):
        text = synthesize_candidate(str(self.home), "wren").read_text()
        self.assertIn("example cousin", text)      # role from cousin.toml
        self.assertIn("Plain and warm.", text)     # voice verbatim
        self.assertIn("follow the operator", text)  # hard rules

    def test_risks_mine_failure_language_not_prohibitions(self):
        import json
        with open(self.home / "data" / "decisions.jsonl", "w") as fh:
            fh.write(json.dumps({
                "topic": "verify window", "decision": "the guard was wrong",
                "reasoning": "false positive on echo"}) + "\n")
            fh.write(json.dumps({
                "topic": "style", "decision": "do not use tabs",
                "reasoning": "convention"}) + "\n")
        text = synthesize_candidate(str(self.home), "wren").read_text()
        self.assertIn("verify window", text)
        risks = text.split("## Recurring Risks", 1)[1].split("## ", 1)[0]
        self.assertNotIn("style", risks)


class TestSynthesizeNeverWritesThroughALink(PortraitCase):
    """The candidate is written to a fresh temp file in the home and
    renamed over the candidate path: a link planted there is replaced,
    never written through."""

    def test_a_dangling_link_is_replaced_not_followed(self):
        outside = pathlib.Path(tempfile.mkdtemp()) / "victim.md"
        self.addCleanup(lambda: outside.parent.rmdir()
                        if not outside.exists() else None)
        cand = self.home / ".self-portrait-candidate.md"
        cand.symlink_to(outside)
        synthesize_candidate(self.home, "wren")
        self.assertFalse(outside.exists())
        self.assertFalse(cand.is_symlink())
        self.assertIn("# Cousin Self-Portrait: wren", cand.read_text())

    def test_a_live_link_is_replaced_and_its_target_untouched(self):
        target = self.home / "data" / "keep.md"
        target.write_text("mine")
        cand = self.home / ".self-portrait-candidate.md"
        cand.symlink_to(target)
        synthesize_candidate(self.home, "wren")
        self.assertEqual(target.read_text(), "mine")
        self.assertFalse(cand.is_symlink())

    def test_no_temp_file_is_left_behind(self):
        synthesize_candidate(self.home, "wren")
        self.assertEqual([p.name for p in self.home.iterdir()
                          if p.name.startswith(".self-portrait-candidate")],
                         [".self-portrait-candidate.md"])


class TestCommitAndBoot(PortraitCase):
    def test_boot_reads_only_committed_never_candidate(self):
        synthesize_candidate(str(self.home), "wren")
        view = for_boot_packet(str(self.home))
        self.assertIn("(no committed self-portrait yet", view)
        commit_candidate(str(self.home))
        view = for_boot_packet(str(self.home))
        self.assertIn("Plain and warm.", view)

    def test_commit_without_candidate_fails_loud(self):
        with self.assertRaises(FileNotFoundError):
            commit_candidate(str(self.home))

    def test_commit_backs_up_the_previous_portrait(self):
        synthesize_candidate(str(self.home), "wren")
        commit_candidate(str(self.home))
        (self.home / ".self-portrait-candidate.md").write_text(
            "# Cousin Self-Portrait: wren\n## Voice\nRevised.\n"
        )
        commit_candidate(str(self.home))
        backup = self.home / ".self-portrait.md.bak"
        self.assertTrue(backup.exists())
        self.assertIn("Plain and warm.", backup.read_text())


class TestCli(PortraitCase):
    def _main(self, argv):
        import contextlib
        import io
        import os
        from unittest import mock

        from cousin_lib.self_portrait import portrait_main
        out, err = io.StringIO(), io.StringIO()
        env = {"COUSIN_HOME": str(self.home), "COUSIN_SLUG": "wren"}
        with mock.patch.dict(os.environ, env), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            rc = portrait_main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_synthesize_then_commit_then_show(self):
        rc, out, _ = self._main(["synthesize"])
        self.assertEqual(rc, 0)
        rc, _, _ = self._main(["commit"])
        self.assertEqual(rc, 0)
        rc, out, _ = self._main(["show"])
        self.assertEqual(rc, 0)
        self.assertIn("Plain and warm.", out)

    def test_commit_without_candidate_exits_one(self):
        rc, _, err = self._main(["commit"])
        self.assertEqual(rc, 1)
        self.assertIn("synthesize", err)


if __name__ == "__main__":
    unittest.main()
