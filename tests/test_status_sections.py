"""STATUS.md's open loops: one writer (the handoff) and every reader agree.

The bare `## Open loops` heading on a line of its own is the one live
section, the one the handoff replaces. A suffixed heading ("## Open loops
(current as of gen 4)"), a deeper "### Open loops archive" and prose that
quotes the heading are history or text, never the live section. Each
shape below states the live loops; every reader must read exactly those,
and the writer must replace the bare section they come from.

One compatibility rule reads homes the writer damaged before it normalised
its `status` (a status that began with its own suffixed heading): an empty
first bare section followed at once by a suffixed open-loops heading reads
that stranded section as live. The handoff then writes a proper bare
section above it, and the stranded one is history from then on.
"""
import pathlib
import re
import tempfile
import unittest

from cousin_lib import boot, status_sections
from cousin_lib.runner import checkpoints, prompt
from cousin_lib.runner.tools import _with_open_loops

_BULLET = re.compile(r"^[-*+] (?:\[.\] )?(.+)$")

# name -> (STATUS.md text, the live loops, loops a handoff keeps in the file)
SHAPES = {
    "stub-above-suffixed": (
        "# Status - Wren\n\n## Open loops\n\n"
        "## Open loops (reconciled gen 3)\n\n- reconciled loop\n",
        ["reconciled loop"], ["reconciled loop"]),
    "stub-above-suffixed-and-a-second-stub": (
        "# Status - Wren\n\nNotes.\n\n## Open loops\n\n"
        "## Open loops (gen handoff) - AUTHORITATIVE\n\n- one\n- two\n### detail\n- three\n\n"
        "## Open loops\n\n## Done\n\n- shipped\n",
        ["one", "two", "three"], ["one", "two", "three", "shipped"]),
    "whitespace-stub-above-suffixed": (
        "# Status - Wren\n\n## Open loops\n\n  \t\n\n"
        "## Open loops (reconciled gen 3)\n\n- reconciled loop\n",
        ["reconciled loop"], ["reconciled loop"]),
    "bullet-above-suffixed": (
        "# Status - Wren\n\n## Open loops\n-\n"
        "## Open loops (reconciled gen 3)\n\n- reconciled loop\n",
        [], ["reconciled loop"]),
    "subheading-above-suffixed": (
        "# Status - Wren\n\n## Open loops\n### parked\n"
        "## Open loops (reconciled gen 3)\n\n- reconciled loop\n",
        [], ["reconciled loop"]),
    "stub-above-another-heading": (
        "# Status - Wren\n\n## Open loops\n\n## Done\n\n- shipped\n",
        [], ["shipped"]),
    "suffixed-only": (
        "# Status - Wren\n\n## Open loops (current as of gen 4)\n\n- old loop\n\n"
        "## Done\n\n- shipped\n",
        [], ["old loop"]),
    "live-above-suffixed": (
        "# Status - Wren\n\n## Open loops\n\n- live loop\n\n"
        "## Open loops (current as of gen 2)\n\n- stale loop\n",
        ["live loop"], ["stale loop"]),
    "archive-heading": (
        "# Status - Wren\n\n### Open loops archive\n\n- archived loop\n\n"
        "## Open loops\n\n- live loop\n\n## Done\n\n- shipped\n",
        ["live loop"], ["archived loop"]),
    "prose-quoting": (
        "# Status - Wren\n\nThe handoff writes ## Open loops below.\n"
        "> ## Open loops\n> - quoted loop\n\n"
        "## Open loops\n\n- live loop\n",
        ["live loop"], ["quoted loop"]),
    "crlf": (
        "# Status - Wren\r\n\r\n## Open loops\r\n\r\n- crlf loop\r\n\r\n"
        "## Done\r\n\r\n- shipped\r\n",
        ["crlf loop"], []),
}


def _bullets(text):
    out = []
    for line in (text or "").splitlines():
        m = _BULLET.match(line.strip())
        if m:
            out.append(m.group(1).strip())
    return out


def _boot(text):
    return _bullets(boot._open_loops_section(text))


def _digest(text):
    return _bullets(prompt._open_loops(text))


def _checkpoints(text):
    with tempfile.TemporaryDirectory() as tmp:
        home = pathlib.Path(tmp)
        with open(home / "STATUS.md", "w", newline="") as fh:
            fh.write(text)
        return _bullets(checkpoints._open_work(home))


def _shared(text):
    return _bullets(status_sections.open_loops_body(text))


READERS = {"shared": _shared, "boot": _boot, "digest": _digest,
           "checkpoints": _checkpoints}


class TestEveryReaderAgrees(unittest.TestCase):
    def test_each_reader_reads_the_live_loops_of_each_shape(self):
        for shape, (text, live, _history) in SHAPES.items():
            for name, read in READERS.items():
                with self.subTest(shape=shape, reader=name):
                    self.assertEqual(read(text), live)

    def test_the_writer_replaces_exactly_what_the_readers_read(self):
        for shape, (text, live, history) in SHAPES.items():
            out = _with_open_loops(text, "Wren", "- handed over")
            with self.subTest(shape=shape):
                for loop in set(live) - set(history):
                    self.assertNotIn(loop, out)
                for loop in history:
                    self.assertIn(loop, out)
            for name, read in READERS.items():
                with self.subTest(shape=shape, reader=name, after="handoff"):
                    self.assertEqual(read(out), ["handed over"])

    def test_suffixed_only_gets_one_bare_heading_and_keeps_it(self):
        text = SHAPES["suffixed-only"][0]
        once = _with_open_loops(text, "Wren", "- first")
        twice = _with_open_loops(once, "Wren", "- second")
        for out in (once, twice):
            self.assertEqual(len(re.findall(r"^## Open loops", out, re.M)), 2)
            self.assertEqual(len(status_sections.OPEN_LOOPS_LINE.findall(out)), 1)
        self.assertNotIn("first", twice)
        self.assertIn("old loop", twice)

    def test_a_status_carrying_its_own_headings_stays_one_live_section(self):
        status = ("## Open loops (reconciled gen 6)\n\n> Gen 6 notes\n\n- loop one\n\n"
                  "## Notes\n\n- loop two\n# Appendix\n- loop three\n### kept as is\n")
        text = SHAPES["live-above-suffixed"][0]
        out = _with_open_loops(text, "Wren", status)
        self.assertEqual(len(status_sections.OPEN_LOOPS_LINE.findall(out)), 1)
        self.assertNotIn("reconciled gen 6", out)
        self.assertIn("### Notes", out)
        self.assertIn("### Appendix", out)
        self.assertIn("### kept as is", out)
        self.assertNotIn("#### kept", out)
        for name, read in READERS.items():
            with self.subTest(reader=name):
                self.assertEqual(read(out), ["loop one", "loop two", "loop three"])
        again = _with_open_loops(out, "Wren", status)
        self.assertEqual(again, out)
        self.assertEqual(len(re.findall(r"^#{1,6} Open loops", again, re.M)), 2)

    def test_a_status_heading_at_any_level_is_dropped(self):
        for heading in ("# Open loops", "## Open loops", "### Open loops (gen 6)",
                        "###### open loops - current"):
            with self.subTest(heading=heading):
                out = _with_open_loops("# Status - Wren\n", "Wren", heading + "\n- a loop\n")
                self.assertEqual(_shared(out), ["a loop"])
                self.assertEqual(out.lower().count("open loops"), 1)

    def test_an_all_whitespace_status_writes_an_empty_section(self):
        out = _with_open_loops("# Status - Wren\n", "Wren", "  \n\n")
        self.assertEqual(out, "# Status - Wren\n\n## Open loops\n\n\n")

    def test_an_empty_live_section_is_no_section_for_the_boot_packet(self):
        home = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        (home / "STATUS.md").write_text(SHAPES["stub-above-another-heading"][0])
        state = boot._active_state(home)
        self.assertNotIn("### STATUS.md (open loops)", state)

    def test_a_stranded_section_reaches_the_boot_packet_under_the_bare_heading(self):
        home = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        (home / "STATUS.md").write_text(SHAPES["stub-above-suffixed"][0])
        self.assertEqual(boot._active_state(home),
                         "### STATUS.md (open loops)\n\n## Open loops\n\n- reconciled loop")

    def test_only_a_whitespace_body_counts_as_empty(self):
        tail = "## Open loops (gen 3)\n- stranded\n"
        for body, stranded in (("", True), ("\n\n  \t\n", True), ("-\n", False),
                               ("### parked\n", False), ("prose\n", False)):
            with self.subTest(body=body):
                text = "# Status\n## Open loops\n" + body + tail
                self.assertEqual("stranded" in status_sections.open_loops_body(text), stranded)

    def test_the_writer_and_digest_share_the_patterns(self):
        from cousin_lib.runner import tools
        self.assertIs(tools.OPEN_LOOPS_LINE, status_sections.OPEN_LOOPS_LINE)
        self.assertIs(tools.NEXT_SECTION, status_sections.NEXT_SECTION)
        self.assertIs(tools._OPEN_LOOPS_LINE, status_sections.OPEN_LOOPS_LINE)


if __name__ == "__main__":
    unittest.main()
