"""Memory written under a live session by someone else: say so, once.

The boot digest is built once per generation, and the system prompt is
frozen for it (the cache). A claim the operator writes from the console,
a review-gate verdict, or a dreaming pass landed in raw memory and stayed
invisible to the running session until the next generation (or a lucky
recall hit). So the runner keeps how far each raw day file had grown when
the session started, and at each submitted prompt (hooks.on_prompt) reads
only what was appended since: the entries written from OUTSIDE the
session reach the model as one runner note in that prompt's context,
keyed by entry id, each said once. "Newer wins" over the digest.

What counts as written from outside: a console write (its cite starts
"console"), a review-gate verdict (source `review_gate`), anything a
dreaming pass wrote (source or cite starting "dream"), and an obsolete
mark made from the console (source `console`). The session's own
remember/decide/obsolete, its job events and the turn extractor are its
own and are never echoed back to it.

Bounded: at most MAX_ENTRIES entries and MAX_CHARS characters a note;
what is left out is counted, and stays readable through recall. A day
file that shrank or vanished (raw_fold folded it) is re-baselined, never
re-read."""
import json
from pathlib import Path

from cousin_lib.memory import entry_id

MAX_ENTRIES = 12
MAX_CHARS = 2500
CONTENT_CHARS = 280
NOTE_HEAD = ("[runner] memory written since this session started, not by you"
             " (it is not in your digest; where it disagrees with the digest,"
             " this is newer):")


def _raw_dir(home):
    return Path(home) / "memory" / "raw"


def _sizes(home):
    out = {}
    for path in _raw_dir(home).glob("*.jsonl"):
        try:
            out[path.name] = path.stat().st_size
        except OSError:
            continue
    return out


def foreign(entry):
    """Written from outside the live session (see the module docstring)."""
    source = str(entry.get("source") or "")
    cite = str(entry.get("cite") or "")
    return (cite.startswith("console") or source in ("console", "review_gate")
            or source.startswith("dream") or cite.startswith("dream"))


class MemoryWatch:
    """Raw day files' sizes as the session started, and what was appended
    since the last `check`."""

    def __init__(self, home):
        self.home = Path(home)
        self.sizes = _sizes(home)

    def check(self):
        """The foreign entries appended since the last check, oldest first;
        the new sizes become the baseline."""
        now = _sizes(self.home)
        found = []
        for name, size in sorted(now.items()):
            start = self.sizes.get(name, 0)
            if size <= start:
                continue
            try:
                with open(_raw_dir(self.home) / name, "rb") as f:
                    f.seek(start)
                    chunk = f.read(size - start)
            except OSError:
                continue
            # a line still being written is left for the next check
            complete = chunk[:chunk.rfind(b"\n") + 1]
            now[name] = start + len(complete)
            for line in complete.decode("utf-8", "replace").splitlines():
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict) and foreign(entry):
                    found.append(entry)
        self.sizes = now
        return found


def _line(entry):
    level = str(entry.get("truth_level") or "?").split("_", 1)[0]
    topic = str(entry.get("topic") or "?")
    eid = entry_id(entry)
    who = "the console" if str(entry.get("cite") or "").startswith("console") else \
        "the review gate" if entry.get("source") == "review_gate" else \
        "dreaming" if "dream" in str(entry.get("source") or entry.get("cite") or "") else "?"
    if str(entry.get("truth_level") or "").startswith("L5"):
        why = " ".join(str(entry.get("why") or entry.get("content") or "").split())
        what = "retired%s" % (": " + why[:CONTENT_CHARS] if why else "")
    else:
        what = " ".join(str(entry.get("content") or "").split())
        if len(what) > CONTENT_CHARS:
            what = what[:CONTENT_CHARS - 3] + "..."
    return "- [%s] %s: %s (by %s, id %s)" % (level, topic, what, who, eid)


def note(entries):
    """The runner note for `entries`, or "" when there are none."""
    if not entries:
        return ""
    lines, size = [], len(NOTE_HEAD)
    for entry in reversed(entries):              # newest first within the budget
        line = _line(entry)
        if len(lines) >= MAX_ENTRIES or size + len(line) + 1 > MAX_CHARS:
            break
        lines.append(line)
        size += len(line) + 1
    lines.reverse()
    text = NOTE_HEAD + "\n" + "\n".join(lines)
    left = len(entries) - len(lines)
    if left:
        text += "\n(%d older entr%s left out; recall finds them.)" % (left, "y" if left == 1 else "ies")
    return text
