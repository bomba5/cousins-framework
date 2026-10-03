"""The state digest inherits the boot packet's budget, order and degraded rules."""
import json
import os
import pathlib
import tempfile
import time
import unittest
from unittest import mock

from cousin_lib import boot
from cousin_lib.runner import prompt
from tests._hermetic import HermeticCase

NOWHERE = {"FRAMEWORK_ROOT": "/nonexistent/framework-root"}


class DigestCase(HermeticCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.home = self.root / "cousins" / "wren"
        (self.home / "data").mkdir(parents=True); (self.home / "memory").mkdir()
        (self.home / "cousin.toml").write_text('[cousin]\nslug = "wren"\nname = "Wren"\n')
        (self.root / "config").mkdir()
        (self.root / "config" / "law.md").write_text("1. Persona is authored.\n")
        p = mock.patch.dict(os.environ, NOWHERE); p.start(); self.addCleanup(p.stop)

    def healthy(self):
        (self.home / "STATUS.md").write_text("# Wren\n\n## Open loops\n\n- finish the report\n")
        (self.home / "data" / "handoff.md").write_text("# Handoff\nmid-report\n")
        (self.home / "data" / "active-threads.md").write_text("# Threads\n- report: drafting\n")
        (self.home / "self-portrait.md").write_text(
            "# Portrait\n## Voice\nPlain.\n## Operator Calibration\nShort statuses.\n")
        distilled = self.home / "memory" / "distilled"; distilled.mkdir()
        (distilled / "operator-calibration.md").write_text("Short statuses, always.\n")

    def digest(self, generation=3):
        return prompt.state_digest(self.home, root=self.root, slug="wren", generation=generation)


class TestInheritedFromBoot(DigestCase):
    def test_healthy_home_composes_every_layer_not_degraded(self):
        self.healthy()
        d = self.digest()
        self.assertEqual(d["degraded_sections"], [])
        for title in ("Operator Calibration", "Active State", "Task Packet",
                      "Recent Tool Trace", "Retrieved Memories"):
            self.assertIn(title, d["text"])
        self.assertIn("Generation: 3", d["text"])

    def test_fresh_home_names_its_real_gaps_and_only_those(self):
        d = self.digest(1)
        self.assertIn("active_state", d["degraded_sections"])
        self.assertIn("identity", d["degraded_sections"])
        self.assertNotIn("memories", d["degraded_sections"])
        self.assertNotIn("trace_summary", d["degraded_sections"])
        self.assertNotIn("calibration", d["degraded_sections"])

    def test_task_fallback_with_real_active_state_is_not_degraded(self):
        self.healthy(); (self.home / "data" / "active-threads.md").unlink()
        self.assertNotIn("task_packet", self.digest()["degraded_sections"])

    def test_the_ceiling_governs_over_per_layer_maxima(self):
        self.healthy()
        (self.home / "data" / "handoff.md").write_text("h " * 20000)
        (self.home / "data" / "active-threads.md").write_text("t " * 20000)
        raw = self.home / "memory" / "raw"; raw.mkdir()
        with open(raw / "2026-08-06.jsonl", "w") as fh:
            for i in range(400):
                fh.write(json.dumps({"topic": "t%d" % i, "content": "m" * 300}) + "\n")
        d = self.digest()
        self.assertLessEqual(d["chars"], prompt.DIGEST_MAX_CHARS + 600)

    def test_staleness_warning_when_decisions_outrun_status(self):
        self.healthy()
        old = time.time() - 7200
        os.utime(self.home / "STATUS.md", (old, old))
        with open(self.home / "data" / "decisions.jsonl", "w") as fh:
            fh.write(json.dumps({"timestamp": "2099-01-01T00:00:00+00:00", "topic": "t",
                                 "decision": "d", "reasoning": "r"}) + "\n")
        self.assertIn("STALE WARNING", self.digest()["text"])


class TestDigestOwnRules(DigestCase):
    def test_order_is_boots_order_filtered(self):
        self.assertEqual(prompt.DIGEST_ORDER, ["memories", "trace_summary", "calibration",
                                               "task_packet", "active_state", "shared_index"])

    def test_no_distilled_calibration_means_no_section_and_no_fallback(self):
        self.healthy()
        (self.home / "memory" / "distilled" / "operator-calibration.md").unlink()
        d = self.digest()
        self.assertNotIn("Operator Calibration", d["text"])
        self.assertNotIn("Short statuses.", d["text"])        # the portrait's, already in the prompt
        self.assertNotIn("calibration", d["degraded_sections"])

    def test_memories_give_before_active_state(self):
        self.healthy()
        (self.home / "data" / "handoff.md").write_text("keep-me " * 150)
        raw = self.home / "memory" / "raw"; raw.mkdir()
        with open(raw / "2026-08-06.jsonl", "w") as fh:
            for i in range(2000):
                fh.write(json.dumps({"topic": "t%d" % i, "content": "m" * 200}) + "\n")
        with mock.patch.object(prompt, "DIGEST_MAX_CHARS", 9000):
            d = self.digest()
        self.assertIn("memories (overflow)", d["text"])
        self.assertNotIn("active_state (overflow)", d["text"])

    def test_the_prompt_layers_are_not_in_the_digest(self):
        self.healthy()
        text = self.digest()["text"]
        self.assertNotIn("Persona is authored", text)     # the law
        self.assertNotIn("Framework contract", text)
        self.assertNotIn("Tool Surface", text)             # retired: the contract replaces it

    def test_the_shared_reference_index_rides_the_digest_with_the_real_command(self):
        shared = self.root / "shared"; shared.mkdir()
        (shared / "ref_map.md").write_text("---\ndescription: a map\n---\nx\n")
        (shared / "rule_x.md").write_text("---\nkind: rule\n---\nBe brief.\n")
        text = self.digest()["text"]
        self.assertIn("ref_map.md", text)
        self.assertIn("cousin-shared read <file>", text)
        self.assertNotIn("`shared` tool", text)
        self.assertNotIn("Be brief.", text)                # rules live in the prompt

    def test_generation_defaults_to_the_counter(self):
        boot.bump_generation(self.home); boot.bump_generation(self.home)
        self.assertEqual(prompt.state_digest(self.home, root=self.root, slug="wren")["generation"], 2)



class TestCalibrationGivesWayByWholeEntries(DigestCase):
    """The calibration layer: the operator's facts newest first, whole,
    then "N more not shown"; the operator's rules are in the prompt."""

    def plant(self, n, *, size=200):
        raw = self.home / "memory" / "raw"; raw.mkdir(parents=True, exist_ok=True)
        with open(raw / "2026-09-01.jsonl", "a") as fh:
            for i in range(n):
                fh.write(json.dumps({
                    "timestamp": "2026-09-01T%02d:%02d:00+00:00" % (i // 60, i % 60),
                    "topic": "fact %03d" % i, "content": ("Kestrel fact %03d " % i) + "x" * size,
                    "truth_level": "L0_OPERATOR", "source": "remember", "cite": "chat %d" % i}) + "\n")

    def section(self, text):
        return text[text.index("Operator Calibration"):text.index("Active State")]

    def shown(self, section):
        return [l for l in section.splitlines() if l.startswith("- [L0_OPERATOR]")]

    def test_a_long_calibration_is_never_cut_mid_entry(self):
        self.healthy(); self.plant(80)
        section = self.section(self.digest()["text"])
        self.assertNotIn("budget hit", section)
        shown = self.shown(section)
        self.assertTrue(shown)
        for line in shown:                          # every entry whole: its tags close it
            self.assertRegex(line, r"\(1 entry, 2026-09-01; topic: fact \d{3}\)$")
        self.assertIn("Kestrel fact 079", shown[0])  # newest first
        self.assertIn("- ... %d more not shown" % (80 - len(shown)), section)
        self.assertLessEqual(len(section), prompt.DIGEST_BUDGETS["calibration"][1] + 100)

    def test_the_cap_is_two_thousand_tokens_and_the_floor_the_old_cap(self):
        self.assertEqual(boot.LAYER_BUDGETS["calibration"],
                         (800 * boot.CHARS_PER_TOKEN, 2000 * boot.CHARS_PER_TOKEN))

    def test_an_overflow_pass_also_drops_whole_entries(self):
        self.healthy(); self.plant(80)
        raw = self.home / "memory" / "raw"
        with open(raw / "2026-08-06.jsonl", "w") as fh:
            for i in range(2000):
                fh.write(json.dumps({"topic": "t%d" % i, "content": "m" * 200}) + "\n")
        with mock.patch.object(prompt, "DIGEST_MAX_CHARS", 9000):
            text = self.digest()["text"]
        section = self.section(text)
        self.assertNotIn("calibration (overflow)", text)
        self.assertLessEqual(len(section), prompt.DIGEST_BUDGETS["calibration"][0] + 100)
        for line in self.shown(section):
            self.assertTrue(line.endswith(")"), line)
        self.assertIn("more not shown", section)

    def test_the_corrections_keep_their_heading_beside_a_long_calibration(self):
        blocks = [("## operator-calibration.md", ["- fact %d" % i + "y" * 90 for i in range(100)],
                   ""),
                  ("# Recent operator corrections (last 2)", ['- [halt] "stop"', '- [x] "no"'], "")]
        text = prompt.pack_calibration(blocks, 2000)
        self.assertLessEqual(len(text), 2000)
        self.assertIn("# Recent operator corrections", text)
        self.assertEqual(prompt.pack_calibration(blocks, 10), "")

    def test_an_operator_rule_is_not_duplicated_in_the_digest(self):
        self.healthy()
        raw = self.home / "memory" / "raw"; raw.mkdir(parents=True, exist_ok=True)
        with open(raw / "2026-09-02.jsonl", "w") as fh:
            fh.write(json.dumps({"timestamp": "2026-09-02T09:00:00+00:00",
                                 "topic": "rule: Toki's reviews", "content": "Review Toki's diffs line by line.",
                                 "truth_level": "L0_OPERATOR", "source": "remember",
                                 "cite": "chat 9"}) + "\n")
        self.assertNotIn("line by line", self.digest()["text"])
        self.assertIn("line by line", "\n".join(prompt.standing_instructions(self.home)))


class TestOpenLoopsReadAsTheWriterWritesThem(DigestCase):
    """The handoff finds STATUS.md's open loops as a whole heading line
    and ends the section at the next level-1 or level-2 heading; the
    digest's active-state layer reads it the same way. The boot packet's
    own reader is left as it is."""
    ARCHIVE = ("# Wren\n\n### Open loops archive\n\nold archived loop\n\n"
               "## Open loops\n\n- live loop\n")

    def test_a_deeper_heading_holding_the_words_is_not_the_section(self):
        (self.home / "STATUS.md").write_text(self.ARCHIVE)
        text = self.digest()["text"]
        self.assertIn("- live loop", text)
        self.assertNotIn("old archived loop", text)

    def test_the_words_in_prose_are_not_the_section(self):
        (self.home / "STATUS.md").write_text(
            "# Wren\n\nSee ## Open loops below.\n\n## Open loops\n\n- live loop\n")
        text = self.digest()["text"]
        self.assertIn("- live loop", text)
        self.assertNotIn("See ## Open loops below.", text)

    def test_the_section_ends_at_a_top_level_heading_as_the_writer_ends_it(self):
        (self.home / "STATUS.md").write_text(
            "# Wren\n\n## Open loops\n\n- live loop\n\n# Appendix\n\nnot a loop\n")
        text = self.digest()["text"]
        self.assertIn("- live loop", text)
        self.assertNotIn("not a loop", text)

    def test_what_the_handoff_writes_the_digest_reads(self):
        from cousin_lib.runner import tools
        from cousin_lib.runner.tools import _with_open_loops
        (self.home / "STATUS.md").write_text(_with_open_loops(self.ARCHIVE, "Wren", "- handed over"))
        text = self.digest()["text"]
        self.assertIn("- handed over", text)
        self.assertNotIn("- live loop", text)
        self.assertNotIn("old archived loop", text)

    def test_the_boot_packet_reads_the_same_section(self):
        (self.home / "STATUS.md").write_text("# Wren\n\n## Open loops\n\n- live loop\n\n## Done\n")
        self.assertEqual(boot._active_state(self.home),
                         "### STATUS.md (open loops)\n\n## Open loops\n\n- live loop")


if __name__ == "__main__":
    unittest.main()
