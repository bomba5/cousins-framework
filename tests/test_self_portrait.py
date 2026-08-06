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


if __name__ == "__main__":
    unittest.main()
