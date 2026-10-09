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
    def test_candidate_carries_voice_and_working_style_only(self):
        # meeting 11 D: no copies of the role, the rules or old decisions,
        # which went stale beside their sources
        text = synthesize_candidate(str(self.home), "wren").read_text()
        for section in ("Temperament", "Working Style", "Voice"):
            self.assertIn("## %s" % section, text)
        for gone in ("Role", "Operator Calibration", "Recurring Risks", "Identity Invariants"):
            self.assertNotIn("## %s" % gone, text)

    def test_sources_feed_their_sections_and_the_rules_are_not_copied(self):
        import json
        with open(self.home / "data" / "decisions.jsonl", "w") as fh:
            fh.write(json.dumps({"topic": "verify window", "decision": "the guard was wrong",
                                 "reasoning": "false positive on echo"}) + "\n")
        text = synthesize_candidate(str(self.home), "wren").read_text()
        self.assertIn("Plain and warm.", text)          # voice verbatim
        self.assertNotIn("follow the operator", text)   # hard rules stay in CLAUDE.md
        self.assertNotIn("verify window", text)         # no frozen decisions

    def test_a_committed_portrait_keeps_its_three_sections_whole(self):
        # the D trim: the reviewed sections stay as written, the rest goes
        long_style = "Reads before guessing. " * 60
        (self.home / "self-portrait.md").write_text(
            "# Cousin Self-Portrait: wren\n\n## Role\nexample cousin\n\n"
            "## Temperament\nDry and patient.\n\n"
            "## Operator Calibration\n- an old rule\n\n"
            "## Working Style\n%s\n\n## Recurring Risks\n- drift\n\n"
            "## Voice\nShort sentences.\n\n## Identity Invariants\n- family\n"
            % long_style.strip())
        text = synthesize_candidate(self.home, "wren").read_text()
        self.assertIn("Dry and patient.", text)
        self.assertIn(long_style.strip(), text)         # reviewed: never trimmed
        self.assertIn("Short sentences.", text)
        self.assertNotIn("Plain and warm.", text)       # the portrait wins
        for gone in ("an old rule", "drift", "- family", "example cousin"):
            self.assertNotIn(gone, text)
        self.assertNotIn("<TODO", text)

    def test_the_cousins_own_part_is_read_before_the_template(self):
        from cousin_lib.template_sync import MARKER
        (self.home / "CLAUDE.md").write_text(
            "# Wren\n\n## Identity\n\nAn example cousin.\n\n"
            "You are part of cousins-framework: boilerplate.\n\n"
            "## Voice\n\nTemplate voice.\n\n%s\n\n## Who I am\n\nWren, the gardener.\n\n"
            "## Voice\n\nWren's own voice.\n\n## How I work\n\nMeasure twice.\n" % MARKER)
        text = synthesize_candidate(self.home, "wren").read_text()
        self.assertIn("Wren, the gardener.", text)
        self.assertIn("Measure twice.", text)
        self.assertIn("Wren's own voice.", text)
        self.assertNotIn("Template voice.", text)
        self.assertNotIn("boilerplate", text)

    def test_a_committed_subsection_stays_inside_its_section(self):
        (self.home / "self-portrait.md").write_text(
            "# Cousin Self-Portrait: wren\n\n## Working Style\n"
            "### Mornings\nGarden first.\n\n## Voice\nShort.\n")
        text = synthesize_candidate(self.home, "wren").read_text()
        self.assertIn("## Working Style\n### Mornings\nGarden first.\n", text)

    def test_the_templates_invariant_paragraph_is_not_drafted_as_voice(self):
        (self.home / "CLAUDE.md").write_text(
            "# Wren\n\n## Voice\n\nPlain and warm.\n\n"
            "Invariant for every cousin, regardless of what the lines above say:\n"
            "your persona is authored.\n")
        text = synthesize_candidate(self.home, "wren").read_text()
        self.assertIn("Plain and warm.", text)
        self.assertNotIn("Invariant for every cousin", text)

    def test_the_template_identity_gives_its_role_paragraph_only(self):
        (self.home / "CLAUDE.md").write_text(
            "# Wren\n\n## Identity\n\nAn example cousin.\n\n"
            "You are part of cousins-framework: boilerplate.\n")
        text = synthesize_candidate(self.home, "wren").read_text()
        self.assertIn("## Temperament\nAn example cousin.\n", text)
        self.assertNotIn("boilerplate", text)


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
