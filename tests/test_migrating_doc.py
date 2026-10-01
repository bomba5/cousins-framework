"""docs/migrating.md's "A cousin with no runner" (2.0.0): the
refusal it quotes is the one the code prints, and the way out is written
down both ways (the last 1.x release, and by hand)."""
import pathlib
import tempfile
import unittest

from cousin_lib import delivery

DOC = pathlib.Path(__file__).resolve().parent.parent / "docs" / "migrating.md"


def _section():
    text = DOC.read_text()
    start = text.index("## A cousin with no runner")
    return text[start:text.index("\n## ", start + 1)]


class TestTheUpgradeSection(unittest.TestCase):
    def test_the_way_out_is_written_down(self):
        sec = _section()
        for needle in ("[agent] runner", "cousin-memory import-auto", "cousin-supervisor reload",
                       "last 1.x release", "cousin-migrate tidy"):
            self.assertIn(needle, sec)

    def test_the_quoted_refusal_is_the_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp) / "wren"
            home.mkdir()
            (home / "cousin.toml").write_text('[cousin]\nslug = "wren"\nname = "Wren"\n')
            line = delivery.lane_refusal(home)
        quoted = " ".join(_section().split("```")[1].split())
        self.assertEqual(quoted, " ".join(line.split()))

    def test_the_old_section_is_gone(self):
        self.assertNotIn("## From the tmux lane to the SDK runner", DOC.read_text())

    def test_ascii_hyphens_only(self):
        self.assertNotRegex(_section(), "[–—]")


if __name__ == "__main__":
    unittest.main()
