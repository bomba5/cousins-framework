"""The durable-layer producer: memory/distilled/*.md from memory/raw.

Raw had a producer (decide -> _append_raw) but nothing wrote the six
distilled files, so every boot packet read six stubs. The distiller
regenerates them deterministically, no model in the loop: newest entry
per topic wins, each file is bounded and ranked, runs are idempotent,
and anything an operator wrote above the auto marker survives.
"""
import json
import pathlib
import tempfile
import unittest

from cousin_lib import distill, memory


def _raw(home, topic, content, *, timestamp,
         truth_level="cousin-conclusion", source="decision", **extra):
    """Plant a raw entry in the exact shape memory._append_raw writes:
    `timestamp` (aware ISO), topic, content, truth_level, source."""
    entry = {"timestamp": timestamp, "topic": topic, "content": content,
             "truth_level": truth_level, "source": source}
    entry.update(extra)
    memory.ensure_layout(home)
    with open(memory.raw_dir(home) / (timestamp[:10] + ".jsonl"), "a") as fh:
        fh.write(json.dumps(entry) + "\n")


class DistillCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = pathlib.Path(tmp.name) / "cousins" / "testa"
        self.home.mkdir(parents=True)

    def _distilled(self, name):
        return (self.home / "memory" / "distilled" / name).read_text()


class TestLayout(DistillCase):
    def test_ensure_layout_creates_the_six_stubs_once(self):
        memory.ensure_layout(self.home)
        ddir = self.home / "memory" / "distilled"
        self.assertEqual(
            sorted(p.name for p in ddir.glob("*.md")),
            sorted(memory.DISTILLED_FILES))
        self.assertEqual(len(memory.DISTILLED_FILES), 6)
        for name in memory.DISTILLED_FILES:
            self.assertIn(memory.STUB_TEXT, (ddir / name).read_text())
        # A second call never touches an existing file.
        (ddir / "glossary.md").write_text("# Glossary\n\n- term: x\n")
        memory.ensure_layout(self.home)
        self.assertIn("term: x", (ddir / "glossary.md").read_text())

    def test_list_raw_reads_append_raw_shape_and_filters_by_age(self):
        memory._append_raw(self.home, {"topic": "fresh", "content": "now",
                                       "truth_level": "cousin-conclusion",
                                       "source": "decision"})
        _raw(self.home, "ancient", "then",
             timestamp="2001-01-01T10:00:00+00:00")
        # Uncertainty keeps: an entry with no parsable date is not old.
        memory.ensure_layout(self.home)
        with open(memory.raw_dir(self.home) / "undated.jsonl", "a") as fh:
            fh.write(json.dumps({"topic": "undated", "content": "?"}) + "\n")
        topics = {e["topic"] for e in memory.list_raw(self.home,
                                                      since_days=30)}
        self.assertEqual(topics, {"fresh", "undated"})
        everything = {e["topic"] for e in memory.list_raw(self.home,
                                                          since_days=36500)}
        self.assertEqual(everything, {"fresh", "ancient", "undated"})


class TestClassify(DistillCase):
    def test_operator_stated_entries_go_to_calibration(self):
        entry = {"topic": "anything", "content": "x",
                 "truth_level": "operator-stated"}
        self.assertEqual(distill.classify(entry), "operator-calibration.md")

    def test_by_topic_keywords(self):
        cases = (
            ("feedback: stay terse", "preferences.md"),
            ("preference for flat pricing", "preferences.md"),
            ("reference: router API", "project-facts.md"),
            ("host facts for the vpn box", "project-facts.md"),
            ("correction: my probe was wrong", "known-failures.md"),
            ("deploy failed on the first try", "known-failures.md"),
            ("glossary: RRF", "glossary.md"),
            ("pick the retention window", "decisions.md"),
        )
        for topic, expected in cases:
            with self.subTest(topic=topic):
                self.assertEqual(
                    distill.classify({"topic": topic, "content": "x"}),
                    expected)

    def test_keywords_match_whole_words_only(self):
        # "api" inside "capitalise" and "host" inside "ghost" must not
        # steer the file.
        self.assertEqual(
            distill.classify({"topic": "capitalise the heading"}),
            "decisions.md")
        self.assertEqual(
            distill.classify({"topic": "ghost writer plan"}),
            "decisions.md")
        self.assertEqual(
            distill.classify({"topic": "the api key rotation"}),
            "project-facts.md")

    def test_a_standing_instruction_is_an_operator_entry_with_a_preferences_word(self):
        cases = (
            ("rule: no em dashes", "L0_OPERATOR", True),
            ("feedback: short statuses", "operator-stated", True),
            ("tone with Priya", "L0_OPERATOR", True),
            ("rulebook for the deploy", "L0_OPERATOR", False),   # whole words only
            ("deploy window", "L0_OPERATOR", False),             # a fact, not a rule
            ("feedback: short statuses", "L3_COUSIN_CONCLUSION", False),
        )
        for topic, level, expected in cases:
            with self.subTest(topic=topic, level=level):
                self.assertIs(distill.standing_instruction(
                    {"topic": topic, "content": "x", "truth_level": level}), expected)

    def test_operator_topics_are_newest_first_and_read_only(self):
        _raw(self.home, "deploy window", "Tuesdays", timestamp="2026-09-01T10:00:00+00:00",
             truth_level="L0_OPERATOR")
        _raw(self.home, "rule: no em dashes", "ASCII hyphens only",
             timestamp="2026-09-02T10:00:00+00:00", truth_level="L0_OPERATOR")
        _raw(self.home, "retention window", "thirty days", timestamp="2026-09-03T10:00:00+00:00")
        before = {p: p.read_text() for p in memory.distilled_dir(self.home).iterdir()}
        topics = distill.operator_topics(self.home)
        self.assertEqual([(t["topic"], t["rule"]) for t in topics],
                         [("rule: no em dashes", True), ("deploy window", False)])
        self.assertEqual(topics[0]["entry"]["content"], "ASCII hyphens only")
        self.assertEqual({p: p.read_text() for p in memory.distilled_dir(self.home).iterdir()},
                         before)


class TestDistill(DistillCase):
    def test_newest_entry_per_topic_wins_and_history_is_counted(self):
        _raw(self.home, "retention window", "keep 7 days",
             timestamp="2026-09-01T10:00:00+00:00")
        _raw(self.home, "retention window", "keep 30 days",
             timestamp="2026-09-05T10:00:00+00:00")
        report = distill.distill(self.home)
        text = self._distilled("decisions.md")
        self.assertIn("keep 30 days", text)
        self.assertNotIn("keep 7 days", text)
        self.assertIn("2 entries", text)
        self.assertIn("[cousin-conclusion]", text)
        self.assertEqual(report["files"]["decisions.md"], 1)
        self.assertEqual(report["topics"], 1)
        self.assertEqual(report["entries"], 2)

    def test_regenerates_instead_of_appending(self):
        _raw(self.home, "t1", "first", timestamp="2026-09-01T10:00:00+00:00")
        distill.distill(self.home)
        path = self.home / "memory" / "distilled" / "decisions.md"
        first = path.read_bytes()
        distill.distill(self.home)
        self.assertEqual(path.read_bytes(), first)
        # A raw file removed: the line disappears on the next run and
        # the stub returns, so readers that test for the stub still work.
        for raw in memory.raw_dir(self.home).glob("*.jsonl"):
            raw.unlink()
        distill.distill(self.home)
        text = path.read_text()
        self.assertNotIn("first", text)
        self.assertIn(memory.STUB_TEXT, text)

    def test_bounds_each_file_and_ranks_by_count_then_recency(self):
        for i in range(30):
            _raw(self.home, "topic-%02d" % i, "content %d" % i,
                 timestamp="2026-08-%02dT10:00:00+00:00" % ((i % 28) + 1))
        # One topic with three entries on OLD dates must still lead.
        for day in ("2026-07-01", "2026-07-02", "2026-07-03"):
            _raw(self.home, "hot-topic", "hot %s" % day,
                 timestamp="%sT10:00:00+00:00" % day)
        distill.distill(self.home, max_lines=10)
        lines = [l for l in self._distilled("decisions.md").splitlines()
                 if l.startswith("- ")]
        self.assertEqual(len(lines), 10)
        self.assertTrue(lines[0].startswith(
            "- [cousin-conclusion] hot 2026-07-03"), lines[0])
        self.assertIn("3 entries", lines[0])

    def test_marks_superseded_conclusions(self):
        _raw(self.home, "cause of the outage", "the disk",
             timestamp="2026-09-01T10:00:00+00:00")
        _raw(self.home, "cause of the outage", "the network, not the disk",
             timestamp="2026-09-02T10:00:00+00:00")
        distill.distill(self.home)
        self.assertIn("superseded 1 earlier", self._distilled("decisions.md"))

    def test_never_writes_outside_the_distilled_dir(self):
        _raw(self.home, "t", "c", timestamp="2026-09-01T10:00:00+00:00")
        before = sorted(str(p) for p in memory.raw_dir(self.home).rglob("*"))
        distill.distill(self.home)
        after = sorted(str(p) for p in memory.raw_dir(self.home).rglob("*"))
        self.assertEqual(before, after)
        written = {p.relative_to(self.home).parts[:2]
                   for p in self.home.rglob("*.md")}
        self.assertEqual(written, {("memory", "distilled")})

    def test_curated_text_above_the_marker_survives(self):
        # Operators plant facts straight into distilled files. Anything
        # above the marker is curated and must survive regeneration; a
        # file without a marker is fully curated and gets the auto block
        # appended below it.
        memory.ensure_layout(self.home)
        path = self.home / "memory" / "distilled" / "project-facts.md"
        path.write_text("# Project Facts\n\n- code-phrase: needle-7755\n")
        _raw(self.home, "reference: router api", "port 80",
             timestamp="2026-09-01T10:00:00+00:00")
        distill.distill(self.home)
        text = path.read_text()
        self.assertIn("needle-7755", text)
        self.assertIn("port 80", text)
        self.assertLess(text.index("needle-7755"),
                        text.index(distill.AUTO_MARKER))
        self.assertLess(text.index(distill.AUTO_MARKER),
                        text.index("port 80"))
        distill.distill(self.home)
        text2 = path.read_text()
        self.assertEqual(text2.count(distill.AUTO_MARKER), 1)
        self.assertEqual(text2, text)

    def test_a_head_carrying_the_auto_header_is_not_curated(self):
        # A file whose head already holds a generated block (written by
        # an earlier run without a marker, or by hand-pasting one) would
        # otherwise keep that block as "curated" and duplicate every
        # line on each run.
        memory.ensure_layout(self.home)
        path = self.home / "memory" / "distilled" / "decisions.md"
        path.write_text("# Decisions\n\n" + distill.AUTO_HEADER
                        + " ..._\n\n- old line\n\n"
                        + distill.AUTO_MARKER + "\n- old line\n")
        _raw(self.home, "t", "fresh", timestamp="2026-09-01T10:00:00+00:00")
        distill.distill(self.home)
        text = path.read_text()
        self.assertNotIn("old line", text)
        self.assertEqual(text.count("fresh"), 1)

    def test_strip_auto_marker_leaves_only_the_lines(self):
        _raw(self.home, "t", "the fact", timestamp="2026-09-01T10:00:00+00:00")
        distill.distill(self.home)
        clean = distill.strip_auto_marker(self._distilled("decisions.md"))
        self.assertNotIn(distill.AUTO_MARKER, clean)
        self.assertNotIn(distill.AUTO_HEADER, clean)
        self.assertIn("the fact", clean)


if __name__ == "__main__":
    unittest.main()
