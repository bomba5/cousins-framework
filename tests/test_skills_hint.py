"""The per-turn skills hint (cousin_lib/skills_hint.py; docs/reference/
skills.md, "The per-turn hint"): the skills exported for this start that
match a message, by embedding, with no model call. The embedding service
is stood in for: each text has a vector of our choosing. Invented cast."""
import pathlib
import tempfile
from unittest import mock

from cousin_lib import memory_search, skills, skills_hint
from tests._hermetic import HermeticCase

BOARD = "board-check: Check a regenerated board against its approved revision. Use after a regen."
DINNER = "dinner-plan: Plan the week's dinners from the household list. Use when asked to plan meals."
VECTORS = {
    BOARD: [1.0, 0.0, 0.0],
    DINNER: [0.0, 1.0, 0.0],
    "please check the regenerated board for wren": [0.9, 0.2, 0.0],
    "is the board fine, and what is for dinner?": [0.7, 0.7, 0.0],
    "what is the weather like on the coast today": [0.0, 0.0, 1.0],
}
CONFIG = {"url": "http://embed.test", "model": "m", "recall": {}}


class HintCase(HermeticCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.home = self.root / "cousins" / "wren"
        self.home.mkdir(parents=True)
        self.asked = []
        embed = mock.patch.object(memory_search, "_embed", side_effect=self.embed)
        seam = mock.patch.object(memory_search, "_embedding_config", return_value=CONFIG)
        for patch in (embed, seam):
            patch.start()
            self.addCleanup(patch.stop)

    def embed(self, text, config):
        self.asked.append(text)
        return VECTORS[text]

    def export(self, plugin, name, description):
        """A skill as the runner's start exports it."""
        folder = skills.export_dir(self.home) / plugin / "skills" / name
        folder.mkdir(parents=True)
        front = {"name": name, "description": description,
                 "metadata": {"context": "personal", "side-effect": "read-only"}}
        (folder / skills.SKILL_FILE).write_text(skills.render(front, "# Steps\n"))


class TestHint(HintCase):
    def setUp(self):
        super().setUp()
        self.export("personal", "board-check", BOARD.split(": ", 1)[1])
        self.export("shared", "dinner-plan", DINNER.split(": ", 1)[1])
        self.hinter = skills_hint.Hinter(self.home, self.root)

    def test_a_message_names_the_skill_it_matches(self):
        line, hits = self.hinter.hint("please check the regenerated board for wren")
        self.assertEqual([name for name, _ in hits], ["personal:board-check"])
        self.assertTrue(line.startswith(skills_hint.HINT_PREFIX + "personal:board-check."))
        self.assertIn("instead of working from memory", line)

    def test_two_that_match_come_best_first(self):
        _line, hits = self.hinter.hint("is the board fine, and what is for dinner?")
        self.assertEqual(sorted(name for name, _ in hits),
                         ["personal:board-check", "shared:dinner-plan"])
        self.assertGreaterEqual(hits[0][1], hits[1][1])

    def test_a_message_far_from_every_skill_gets_none(self):
        self.assertEqual(self.hinter.hint("what is the weather like on the coast today"), ("", []))

    def test_a_short_message_asks_nothing(self):
        self.assertEqual(self.hinter.hint("ok, go"), ("", []))
        self.assertEqual(self.asked, [])

    def test_the_skills_vectors_are_taken_once(self):
        self.hinter.hint("please check the regenerated board for wren")
        self.hinter.hint("what is the weather like on the coast today")
        self.assertEqual(self.asked.count(BOARD), 1)
        self.assertEqual(self.asked.count(DINNER), 1)

    def test_a_failing_service_is_asked_again_at_the_next_message(self):
        with mock.patch.object(memory_search, "_embed", side_effect=OSError("down")):
            with self.assertRaises(OSError):
                self.hinter.hint("please check the regenerated board for wren")
        self.assertEqual(self.hinter.hint("please check the regenerated board for wren")[1][0][0],
                         "personal:board-check")


class TestNothingToHint(HintCase):
    def test_no_export_asks_nothing(self):
        hinter = skills_hint.Hinter(self.home, self.root)
        self.assertEqual(hinter.hint("please check the regenerated board for wren"), ("", []))
        self.assertEqual(self.asked, [])

    def test_no_embedding_seam_no_hint(self):
        self.export("personal", "board-check", BOARD.split(": ", 1)[1])
        with mock.patch.object(memory_search, "_embedding_config", return_value=None):
            hinter = skills_hint.Hinter(self.home, self.root)
            self.assertEqual(hinter.hint("please check the regenerated board for wren"), ("", []))
        self.assertEqual(self.asked, [])

    def test_a_skill_that_does_not_parse_is_left_out(self):
        self.export("personal", "board-check", BOARD.split(": ", 1)[1])
        bad = skills.export_dir(self.home) / "personal" / "skills" / "broken"
        bad.mkdir(parents=True)
        (bad / skills.SKILL_FILE).write_text("no frontmatter here\n")
        self.assertEqual([name for name, _ in skills_hint.exported(self.home)],
                         ["personal:board-check"])
