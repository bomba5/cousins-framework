"""Durable-layer producer: regenerate memory/distilled/*.md from
memory/raw.

Deterministic, no model in the loop, idempotent. Each run rebuilds every
distilled file from the raw candidates (hot daily files plus the monthly
digests raw_fold leaves behind):

- entries group by topic; the newest entry per topic is the line and
  the earlier ones are history ("N entries", "superseded N earlier");
- classify() picks the file: truth level first (operator-stated goes to
  operator-calibration), then whole-word topic and source keywords;
- each file is bounded to max_lines, ranked by entry count then
  recency;
- a file with nothing to say keeps the stub, so readers that test for
  the stub keep working.

Curated text survives. Everything above AUTO_MARKER in a distilled file
was written by a person or another tool and is copied through verbatim;
everything below is rewritten. A file without a marker is treated as
fully curated and gets the generated block appended under it - unless
its head already carries the generated block's own header, which means
an earlier run wrote it and keeping it would duplicate every line.

Consumers: boot.assemble (runs distill before reading the floor) and
`cousin-memory distill` / `consolidate`.
"""
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from cousin_lib import memory

DEFAULT_MAX_LINES = 40
DEFAULT_SINCE_DAYS = 3650
LINE_CONTENT_CHARS = 220
DEFAULT_TRUTH_LEVEL = "cousin-conclusion"
OPERATOR_TRUTH_LEVEL = "operator-stated"

AUTO_MARKER = ("<!-- distilled:auto - lines below are regenerated from"
               " memory/raw; edit above this line only -->")
# First line of the generated block. Its presence in a file's head is
# proof the head is generated, not curated.
AUTO_HEADER = "_Regenerated from memory/raw by the distiller"

_KEYWORDS = (
    ("known-failures.md", ("correction", "corrected", "wrong", "failed",
                           "failure", "bug", "broken", "mistake",
                           "regression", "lesson")),
    ("preferences.md", ("feedback", "preference", "prefers", "rule:",
                        "tone", "register", "style")),
    ("project-facts.md", ("reference", "host", "fact", "inventory", "api",
                          "network", "topology", "credential", "config")),
    ("glossary.md", ("glossary", "term:", "definition")),
)


def classify(entry):
    """Pick the distilled file for a raw entry."""
    if entry.get("truth_level") == OPERATOR_TRUTH_LEVEL:
        return "operator-calibration.md"
    hay = ("%s %s" % (entry.get("topic", ""), entry.get("source", ""))).lower()
    for fname, words in _KEYWORDS:
        for word in words:
            # Whole-word match: "api" must not fire inside "capitalise".
            if re.search(r"(?<![a-z0-9])" + re.escape(word) + r"(?![a-z0-9])",
                         hay):
                return fname
    return "decisions.md"


def _ts(entry):
    return memory.entry_timestamp(entry) or 0.0


def _when(entry):
    return str(entry.get("timestamp") or entry.get("created_at") or "")[:10]


def _title(fname):
    return fname.replace(".md", "").replace("-", " ").title()


def _stub(fname):
    return "# %s\n\n%s\n" % (_title(fname), memory.STUB_TEXT)


def _curated_block(path):
    """Text kept verbatim above the marker: the existing file up to the
    marker, or the whole file when it has no marker and is not the stub
    and does not carry a generated head."""
    if not path.exists():
        return ""
    text = path.read_text()
    if AUTO_MARKER in text:
        head = text.split(AUTO_MARKER, 1)[0]
        if AUTO_HEADER in head:
            return ""
    elif (not text.strip() or memory.STUB_TEXT in text
          or AUTO_HEADER in text):
        return ""
    else:
        head = text
    # A bare title is not curated content.
    body = "\n".join(l for l in head.splitlines()
                     if l.strip() and not l.startswith("# "))
    return head if body.strip() else ""


def _line(newest, count, superseded):
    level = newest.get("truth_level") or DEFAULT_TRUTH_LEVEL
    content = " ".join(str(newest.get("content", "")).split())
    content = content[:LINE_CONTENT_CHARS]
    tags = ["%d entries" % count if count != 1 else "1 entry"]
    if superseded:
        tags.append("superseded %d earlier" % superseded)
    when = _when(newest)
    if when:
        tags.append(when)
    return "- [%s] %s (%s; topic: %s)" % (
        level, content, ", ".join(tags), newest.get("topic", ""))


def strip_auto_marker(text):
    """The file as a reader should see it: marker and generated header
    removed, nothing else touched. For boot packets and any surface that
    quotes a distilled file."""
    kept = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == AUTO_MARKER or stripped.startswith(AUTO_HEADER):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def distill(home, *, max_lines=DEFAULT_MAX_LINES,
            since_days=DEFAULT_SINCE_DAYS):
    """Rebuild every distilled file from raw. Returns
    {"files": {fname: n_lines}, "topics": int, "entries": int}."""
    home = Path(home)
    memory.ensure_layout(home)
    groups = defaultdict(list)
    total = 0
    for entry in memory.list_raw(home, since_days=since_days):
        topic = str(entry.get("topic") or "").strip()
        if not topic or not entry.get("content"):
            continue
        groups[topic].append(entry)
        total += 1

    per_file = defaultdict(list)
    for topic, entries in groups.items():
        entries.sort(key=_ts)
        newest = entries[-1]
        # Digests carry their own entry count (folded history).
        count = sum(int(e.get("entries", 1) or 1) for e in entries)
        distinct = {" ".join(str(e.get("content", "")).split())
                    for e in entries}
        superseded = len(distinct) - 1
        per_file[classify(newest)].append(
            (count, _ts(newest), _line(newest, count, superseded)))

    report = {"files": {}, "topics": len(groups), "entries": total}
    ddir = memory.distilled_dir(home)
    for fname in memory.DISTILLED_FILES:
        ranked = sorted(per_file.get(fname, []),
                        key=lambda t: (-t[0], -t[1]))[:max_lines]
        lines = [t[2] for t in ranked]
        path = ddir / fname
        curated = _curated_block(path)
        if not lines and not curated:
            text = _stub(fname)
        else:
            head = curated if curated else "# %s\n\n" % _title(fname)
            body = ("\n".join(lines) + "\n" if lines
                    else "_(nothing distilled from raw yet)_\n")
            auto = ("%s: newest entry per topic, ranked by entry count"
                    " then recency; rewritten on every boot and"
                    " `cousin-memory distill`._\n\n%s" % (AUTO_HEADER, body))
            text = head.rstrip("\n") + "\n\n" + AUTO_MARKER + "\n" + auto
        if not path.exists() or path.read_text() != text:
            tmp = path.with_suffix(".md.tmp")
            tmp.write_text(text)
            tmp.replace(path)
        report["files"][fname] = len(lines)
    report["generated_at"] = datetime.now(timezone.utc).isoformat()
    return report
