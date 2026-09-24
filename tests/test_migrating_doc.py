"""The runner-lane runbook (docs/migrating.md, master plan phase 7 task 7)
is held to the command it documents: every step cousin-migrate runs is in
its table, every `cousin-migrate` subcommand it names exists, the fleet's
order is written down, and the rollback is there."""
import contextlib
import io
import pathlib
import re
import unittest

from cousin_lib import migrate

DOC = pathlib.Path(__file__).resolve().parent.parent / "docs" / "migrating.md"


def _section():
    text = DOC.read_text()
    start = text.index("## From the tmux lane to the SDK runner")
    return text[start:text.index("\n## ", start + 1)]


class TestRunbook(unittest.TestCase):
    def test_every_step_is_in_the_table(self):
        sec = _section()
        for step in migrate.STEPS:
            self.assertIn("| `%s` |" % step, sec)

    def test_every_subcommand_it_names_exists(self):
        named = set(re.findall(r"cousin-migrate (\w+)", _section()))
        self.assertTrue({"plan", "apply", "rollback", "check"} <= named, named)
        for sub in named:
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as cm:
                migrate.migrate_main([sub, "--help"])
            self.assertEqual(cm.exception.code, 0, sub)

    def test_the_order_and_the_rollback_are_written_down(self):
        sec = _section()
        order = [sec.index(s) for s in ("**A test cousin.**", "**One low-stakes cousin.**",
                                        "**Observe two days.**", "**The rest, one at a time.**",
                                        "**The engineer cousin last.**")]
        self.assertEqual(order, sorted(order))
        self.assertIn("### Rolling back", sec)
        self.assertIn('runner = "tmux"', sec)

    def test_what_round_1_missed_is_written_down(self):
        """Review C2, M11, I7 and M12: a peer's send is checked, the new
        command needs a reinstall, a by-hand rollback drops the stale
        packet, and held entries after a rollback wait for a person."""
        sec = _section()
        self.assertIn("cousin-chat send wren", sec)
        self.assertIn("pip install -e", sec)
        self.assertIn("data/pending-boot.json", sec)
        self.assertIn("cousin-memory review", sec)

    def test_ascii_hyphens_only(self):
        self.assertNotRegex(_section(), "[\u2013\u2014]")


if __name__ == "__main__":
    unittest.main()
