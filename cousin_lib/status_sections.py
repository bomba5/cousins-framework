"""STATUS.md's open-loops section: the one definition its writer (the
handoff tool, runner/tools.py) and every reader (the digest, the boot
packet, data/state.json, the session-end baseline, the checkpoints) use,
so they cannot disagree on where the section is.

The live section is the FIRST bare `## Open loops` heading on a line of
its own, up to the next level-1 or level-2 heading. A suffixed heading
("## Open loops (current as of gen 4)") is history the cousin kept, never
the live section; so is "### Open loops archive", and so is prose that
quotes the heading. The handoff replaces the live section and inserts one
after the title line when there is none.

One compatibility rule reads homes written before the handoff normalised
its `status`: a `status` that began with its own "## Open loops (...)"
heading left the bare section empty and the loops under that suffixed
heading right below it. When the first bare section is empty and the very
next heading is such a suffixed open-loops heading, the readers take that
section's body as the live one. The file is not edited; the next handoff
writes a proper bare section and the rule stops applying by itself.
"""
import re

OPEN_LOOPS = "## Open loops"
# The section is the heading on a line of its own: a substring search would
# take "### Open loops archive" or prose that quotes the heading, and the
# writer would overwrite the cousin's own text there (STATUS.md is the
# cousin's file). A CRLF file ends the heading with "\r", which "$" alone
# does not consume.
OPEN_LOOPS_LINE = re.compile(r"^## Open loops[ \t]*\r?$", re.M)
# It ends at the next heading of level 1 or 2; a "###" inside it is its own.
NEXT_SECTION = re.compile(r"^#{1,2} ", re.M)


def open_loops_span(text):
    """(start, end) of the bare section in `text`, heading included, or
    None when there is no bare heading: what the handoff replaces. (The
    readers apply the compatibility rule on top: open_loops_body.)"""
    found = OPEN_LOOPS_LINE.search(text)
    if found is None:
        return None
    after = NEXT_SECTION.search(text, found.end())
    return found.start(), after.start() if after else len(text)


# A level-1 or level-2 open-loops heading with a suffix: history, except as
# the stranded section of the compatibility rule above.
_STRANDED = re.compile(r"#{1,2} +open loops\b[^\r\n]*", re.I)


def _live_body_span(text):
    """(heading_end, body_end) of the live body, the compatibility rule
    applied, or None when there is no bare heading."""
    found = OPEN_LOOPS_LINE.search(text)
    if found is None:
        return None
    after = NEXT_SECTION.search(text, found.end())
    end = after.start() if after else len(text)
    if after and not text[found.end():end].strip():
        stranded = _STRANDED.match(text, end)
        if stranded and not OPEN_LOOPS_LINE.match(text, end):
            nxt = NEXT_SECTION.search(text, stranded.end())
            return stranded.end(), nxt.start() if nxt else len(text)
    return found.end(), end


def open_loops_section(text):
    """The live section's text, headed by the bare heading, or None."""
    found = OPEN_LOOPS_LINE.search(text)
    if found is None:
        return None
    return text[found.start():found.end()] + open_loops_body(text)


def open_loops_body(text):
    """The live section's body without its heading line; "" when there is
    no live section."""
    span = _live_body_span(text)
    return text[span[0]:span[1]] if span else ""
