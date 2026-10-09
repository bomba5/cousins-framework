"""STATUS.md to data/state.json: the open loops in machine-readable
form, for a cold session that wants instant context without parsing
markdown.

Deprecated (3.47.0, meeting 11 A): STATUS.md is the one copy of the open
loops, the handoff no longer writes state.json and nothing in the
framework reads it. The command still writes it on demand, and says it
is deprecated, until it is removed in 4.0.0.

The open loops are the live section the handoff writes, read with the
one shared definition (cousin_lib.status_sections): the first bare
`## Open loops` heading; a suffixed `## Open loops (...)` heading is
history. The other sections keep the source's rule: a STATUS.md
accumulates copies of a section and never deletes the old ones, so the
FIRST `## ` heading starting with the section's name is the current one
and every later copy is history. And bullets are plain `- **text**` far
more often than checkboxes; a parser that counts only checkboxes reports
a full section as empty, which is indistinguishable from "no open loops".
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from cousin_lib import status_sections
from cousin_lib.trace import traced_cli

TEXT_MAX = 200
# (heading prefix, lowercase, matched after "## ") -> state key. The open
# loops are not matched by prefix: status_sections reads the live section.
SECTIONS = (
    ("open loops", "open_loops"),
    ("parked", "parked"),
    ("recently closed", "recently_closed"),
)
_BY_PREFIX = SECTIONS[1:]
# `- [x] text`, `* [ ] text`, `+ [~] text`, or a plain bullet of any
# marker. Top level only: an indented bullet is a sub-point of a loop,
# not a loop.
_BULLET = re.compile(r"^[-*+] (?:\[(.)\] )?(.+)$")


class _NoContext(Exception):
    pass


def _home(args):
    home = getattr(args, "home", None) or os.environ.get("COUSIN_HOME")
    if not home:
        raise _NoContext(
            "no cousin context - set COUSIN_HOME or pass --home"
            " (refusing to sync another cousin's status)")
    return Path(home).resolve()


def _section_for(heading):
    """State key a `## ` heading opens, or None for any other heading."""
    title = heading[3:].strip().lower()
    for prefix, key in _BY_PREFIX:
        if title.startswith(prefix):
            return key
    return None


def _item(line):
    """A top-level bullet as a state item, or None."""
    m = _BULLET.match(line.rstrip("\r"))
    if not m:
        return None
    mark, body = m.group(1), m.group(2).strip()
    return {"text": body[:TEXT_MAX], "done": mark == "x", "partial": mark == "~"}


def parse_status(text):
    state = {key: [] for _, key in SECTIONS}
    state["open_loops"] = [item for item in map(
        _item, status_sections.open_loops_body(text).split("\n")) if item]
    section = None
    seen = set()
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("## "):
            key = _section_for(stripped)
            # Any other heading ends the section; a later copy of a
            # section already captured is history and ends it too.
            if key is None or key in seen:
                section = None
            else:
                section = key
                seen.add(key)
            continue
        if section is None:
            continue
        item = _item(line)
        if item:
            state[section].append(item)
    return state


def write_state(home):
    """Parse <home>/STATUS.md into <home>/data/state.json; a missing
    STATUS.md is an empty state, not an error. Returns the state."""
    home = Path(home)
    try:
        text = (home / "STATUS.md").read_text()
    except OSError:
        text = ""
    state = parse_status(text)
    state["generated_at"] = datetime.now().replace(
        microsecond=0).isoformat()
    path = home / "data" / "state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(path)
    return state


DEPRECATED = ("cousin-sync-state is deprecated (3.47.0): STATUS.md's open loops are the one"
              " copy, and nothing reads data/state.json. It is removed in 4.0.0.")


@traced_cli("cousin-sync-state")
def sync_state_main(argv=None):
    parser = argparse.ArgumentParser(
        prog="cousin-sync-state",
        description="render STATUS.md into data/state.json")
    parser.add_argument("--home")
    args = parser.parse_args(argv)
    print(DEPRECATED, file=sys.stderr)
    try:
        home = _home(args)
    except _NoContext as err:
        print("ERROR: %s" % err, file=sys.stderr)
        return 2
    state = write_state(home)
    print("state synced: %d open, %d parked, %d closed" % (
        len(state["open_loops"]), len(state["parked"]),
        len(state["recently_closed"])))
    return 0


if __name__ == "__main__":
    sys.exit(sync_state_main())
