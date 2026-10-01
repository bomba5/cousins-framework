"""The docs that say what the opencode lane is and is not, and how to run
it. Doc checks only: the lane's behaviour is pinned by
tests/runner/test_opencode*.py and the contract suite
(tests/runner/contract/test_opencode.py)."""
import pathlib
import re
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]


def _section(text, heading):
    start = text.index(heading)
    level = heading.split(" ", 1)[0]
    nxt = re.search(r"^%s " % re.escape(level), text[start + len(heading):], re.M)
    return text[start:start + len(heading) + nxt.start()] if nxt else text[start:]


class TestOpencodeLaneDocs(unittest.TestCase):
    def read(self, rel):
        return (_REPO / rel).read_text()

    def has(self, needle, text, where):
        self.assertIn(needle, text, "%s does not say %r" % (where, needle))

    def test_another_vendors_subscription_is_documented(self):
        """`--method` (another vendor's subscription login) is documented."""
        commands = self.read("docs/commands.md")
        self.has("--method", commands, "commands.md")
        runners = self.read("docs/reference/runners.md")
        self.has("--method", runners, "runners.md")

    def test_the_per_turn_check_promises_no_time_bound(self):
        """The check catches a change still in
        place when a turn starts; it bounds nothing the model does from its
        shell (a change made and undone inside a turn, a detached process
        prompting the server between turns)."""
        runners = self.read("docs/reference/runners.md")
        self.has("bounds nothing the model does from its shell", runners, "runners.md")
        self.assertNotIn("lasts at most", runners)
        from cousin_lib.runner import opencode
        doc = opencode.OpencodeRunner._guard_turn.__doc__
        self.assertIn("bounds nothing the model does from its shell", doc)
        self.assertNotIn("the window is one turn", doc)

    def test_the_docs_say_how_to_run_the_lane_and_what_it_does_not_do(self):
        # The default image carries the opencode binary since the lane
        # went into it; only compose.slim.yml's image lacks it.
        ops = _section(self.read("docs/operations.md"), "## The container")
        for needle in ("The default image carries the opencode binary", "compose.slim.yml",
                       "never carries a Claude subscription",
                       "reference/runners.md#known-gaps"):
            self.has(needle, ops, "operations.md, The container")
        self.assertNotIn("-f compose.opencode.yml", ops)
        sessions = _section(self.read("docs/configuration.md"), "### [agent.sessions]")
        self.has('`runner = "opencode"`', sessions, "configuration.md, [agent.sessions]")
        gaps = _section(self.read("docs/reference/runners.md"), "## Known gaps")
        for needle in ("apply_patch", "--check-auth --validate",
                       "Image attachments", "cousin-spawn --runner opencode"):
            self.has(needle, gaps, "runners.md, Known gaps")
        # usage is recorded on this lane since 2.5.0: no longer a known gap
        self.assertNotIn("usage records", gaps)


if __name__ == "__main__":
    unittest.main()
