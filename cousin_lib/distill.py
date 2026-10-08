"""Durable-layer producer: regenerate memory/distilled/*.md from
memory/raw.

Deterministic, no model in the loop, idempotent. Each run rebuilds every
distilled file from the raw candidates (hot daily files plus the monthly
digests raw_fold leaves behind):

- entries group by topic; the newest entry per topic is the line and
  the earlier ones are history ("N entries", "superseded N earlier");
- classify() picks the file: truth level first (operator-stated goes to
  operator-calibration), then whole-word topic and source keywords;
  an operator entry whose topic carries a preferences word is also a
  standing instruction (standing_instruction), which the runner's
  system prompt carries whole;
- each file is bounded to max_lines, ranked by entry count then
  recency, except that machine topics (MACHINE_PREFIXES: the transcript
  miners, the jobs ledger, framework state changes) rank after every
  authored topic, so the log fills only the lines memory leaves;
- a file with nothing to say keeps the stub, so readers that test for
  the stub keep working;
- a topic whose NEWEST entry is L5_OBSOLETE (`cousin-memory obsolete`,
  the console's "mark obsolete") is left out of every file entirely -
  no tombstone line, the boot packet should not spend lines on what
  was retired - and counted in the report; raw keeps its history and a
  later non-L5 entry on the topic revives it.

Curated text survives. Everything above AUTO_MARKER in a distilled file
was written by a person or another tool and is copied through verbatim;
everything below is rewritten. A file without a marker is treated as
fully curated and gets the generated block appended under it - unless
its head already carries the generated block's own header, which means
an earlier run wrote it and keeping it would duplicate every line.

Consumers: the runner's state digest (runner/prompt.py, runs distill
before reading the floor), its system prompt (operator_topics, read-only)
and `cousin-memory distill` / `consolidate`.
"""
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from cousin_lib import memory, memory_lock, perimeter

DEFAULT_MAX_LINES = 40
DEFAULT_SINCE_DAYS = 3650
LINE_CONTENT_CHARS = 220
DEFAULT_TRUTH_LEVEL = memory.DEFAULT_TRUTH_LEVEL
# Topics the framework's own machinery writes, not a cousin: the
# transcript miners (episode:), the jobs ledger (job:) and framework
# state changes (framework:). Indexed like every raw entry, but in a
# distilled file they rank after every authored topic, so they take
# only the lines authored memory leaves.
MACHINE_PREFIXES = ("episode:", "job:", "framework:")
OPERATOR_TRUTH_LEVEL = "operator-stated"

AUTO_MARKER = ("<!-- distilled:auto - lines below are regenerated from"
               " memory/raw; edit above this line only -->")
# First line of the generated block. Its presence in a file's head is
# proof the head is generated, not curated.
AUTO_HEADER = "_Regenerated from memory/raw by the distiller"

# The words that file an entry under preferences.md. They also split the
# operator's own entries (standing_instruction): an L0 entry whose topic
# or source carries one is a rule for how the cousin works.
PREFERENCE_WORDS = ("feedback", "preference", "prefers", "rule:",
                    "tone", "register", "style")

_KEYWORDS = (
    ("known-failures.md", ("correction", "corrected", "wrong", "failed",
                           "failure", "bug", "broken", "mistake",
                           "regression", "lesson")),
    ("preferences.md", PREFERENCE_WORDS),
    ("project-facts.md", ("reference", "host", "fact", "inventory", "api",
                          "network", "topology", "credential", "config")),
    ("glossary.md", ("glossary", "term:", "definition")),
)


def _has_word(entry, words):
    """True when the entry's topic or source carries one of `words`,
    whole-word: "api" must not fire inside "capitalise"."""
    hay = ("%s %s" % (entry.get("topic", ""), entry.get("source", ""))).lower()
    return any(re.search(r"(?<![a-z0-9])" + re.escape(word) + r"(?![a-z0-9])", hay)
               for word in words)


def classify(entry):
    """Pick the distilled file for a raw entry."""
    if memory.normalize_level(entry.get("truth_level")) == memory.OPERATOR_LEVEL:
        return "operator-calibration.md"
    for fname, words in _KEYWORDS:
        if _has_word(entry, words):
            return fname
    return "decisions.md"


def standing_instruction(entry):
    """True for an operator-level entry that is a rule for how the cousin
    works rather than a fact about the world: its topic or source carries
    a PREFERENCE_WORDS word (`rule: no em dashes`, `feedback: short
    statuses`), the words that would have filed it under preferences.md
    had the operator not said it. The runner puts these in the system
    prompt, whole, and leaves them out of the digest."""
    return (memory.normalize_level(entry.get("truth_level")) == memory.OPERATOR_LEVEL
            and _has_word(entry, PREFERENCE_WORDS))


def is_machine_topic(topic):
    """True for a topic a framework writer produced (MACHINE_PREFIXES),
    matched at the start of the stripped topic, never inside it."""
    return str(topic or "").strip().startswith(MACHINE_PREFIXES)


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
    held = memory.qualifiers(newest)
    if held:
        tags.insert(0, held)
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
    {"files": {fname: n_lines}, "topics": int, "entries": int}.
    The read of raw and the writes of the views and the stamp are one
    critical section per home (memory_lock): a distill that read raw
    before another session's entry must not write its views, and a later
    stamp, after the distill that saw it."""
    home = Path(home)
    with memory_lock.write_lock(home):
        return _distill(home, max_lines=max_lines, since_days=since_days)


def _groups(home, since_days):
    """(groups, total): the raw candidates by topic, the entries the
    views leave out already left out, and how many were kept."""
    groups = defaultdict(list)
    total = 0
    raw = memory.list_raw(home, since_days=since_days)
    # What the views leave out (memory.hidden_ids: an entry an entry-level
    # mark retired) is read from the whole history, archives included: the
    # monthly fold keeps one digest line per topic, and a digest neither
    # carries a mark's `entry` nor knows its month's lines were retired
    # (it copies its newest line's text, so its id can equal a retired
    # one), and the fold leaves a held entry out of its digest. A topic
    # with such entries (memory.digest_unsafe_ids) is therefore built from
    # the whole history instead of its digests.
    truth = memory._all_raw(home)
    hidden = memory.hidden_ids(truth)
    unsafe = memory.digest_unsafe_ids(truth)
    rebuild = {str(e.get("topic") or "").strip() for e in truth
               if memory.view_noise(e) or memory.entry_id(e) in unsafe}
    cutoff = datetime.now(timezone.utc).timestamp() - since_days * 86400
    source = [e for e in raw if str(e.get("topic") or "").strip() not in rebuild]
    source += [e for e in truth if str(e.get("topic") or "").strip() in rebuild
               and (memory.entry_timestamp(e) or cutoff) >= cutoff]
    for entry in source:
        topic = str(entry.get("topic") or "").strip()
        if not topic or not entry.get("content"):
            continue
        # an entry-level mark is not the topic's newest word (a topic-level
        # mark still retires the whole topic, below)
        if memory.view_noise(entry) or memory.entry_id(entry) in hidden:
            continue
        groups[topic].append(entry)
        total += 1
    return groups, total


def _history(entries):
    """(count, superseded) of a topic's entries: digests carry their own
    entry count (folded history)."""
    count = sum(int(e.get("entries", 1) or 1) for e in entries)
    distinct = {" ".join(str(e.get("content", "")).split()) for e in entries}
    return count, len(distinct) - 1


def operator_topics(home, *, since_days=DEFAULT_SINCE_DAYS):
    """The topics operator-calibration.md is built from, newest first
    (ties by topic): one dict per topic whose newest entry is operator
    level, {"topic", "entry" (the newest, whole), "ts", "line" (the
    view's line for it), "rule" (standing_instruction)}. Read-only: no
    view is written and no lock is taken; a home with no raw memory has
    none."""
    home = Path(home)
    if not memory.raw_dir(home).is_dir():
        return []
    groups, _total = _groups(home, since_days)
    out = []
    for topic, entries in groups.items():
        entries.sort(key=_ts)
        newest = entries[-1]
        if classify(newest) != "operator-calibration.md":
            continue
        count, superseded = _history(entries)
        out.append({"topic": topic, "entry": newest, "ts": _ts(newest),
                    "line": _line(newest, count, superseded),
                    "rule": standing_instruction(newest)})
    out.sort(key=lambda t: (-t["ts"], t["topic"]))
    return out


def _distill(home, *, max_lines, since_days):
    memory.ensure_layout(home)
    groups, total = _groups(home, since_days)
    per_file = defaultdict(list)
    obsolete = 0
    for topic, entries in groups.items():
        entries.sort(key=_ts)
        newest = entries[-1]
        # A topic whose newest word is "obsolete" is retired from the
        # durable views; its history stays in raw, and any later entry
        # (of another level) brings it back.
        if memory.normalize_level(newest.get("truth_level")) \
                == memory.OBSOLETE_LEVEL:
            obsolete += 1
            continue
        count, superseded = _history(entries)
        per_file[classify(newest)].append(
            (is_machine_topic(topic), count, _ts(newest),
             _line(newest, count, superseded)))

    report = {"files": {}, "topics": len(groups), "entries": total,
              "obsolete": obsolete}
    ddir = memory.distilled_dir(home)
    for fname in memory.DISTILLED_FILES:
        # Before the first byte: the distiller runs on every boot and on
        # every dreamer pass, and it is the writer with the most reach.
        perimeter.assert_writable(ddir / fname, writer="distill._distill")
        ranked = sorted(per_file.get(fname, []),
                        key=lambda t: (t[0], -t[1], -t[2]))[:max_lines]
        lines = [t[3] for t in ranked]
        path = ddir / fname
        curated = _curated_block(path)
        if not lines and not curated:
            text = _stub(fname)
        else:
            head = curated if curated else "# %s\n\n" % _title(fname)
            body = ("\n".join(lines) + "\n" if lines
                    else "_(nothing distilled from raw yet)_\n")
            auto = ("%s: newest entry per topic, ranked by entry count"
                    " then recency, the framework's own log (episode:,"
                    " job:, framework:) after every authored topic;"
                    " rewritten on every boot and `cousin-memory"
                    " distill`._\n\n%s" % (AUTO_HEADER, body))
            text = head.rstrip("\n") + "\n\n" + AUTO_MARKER + "\n" + auto
        if not path.exists() or path.read_text() != text:
            tmp = path.with_suffix(".md.tmp")
            tmp.write_text(text)
            tmp.replace(path)
        report["files"][fname] = len(lines)
    report["generated_at"] = datetime.now(timezone.utc).isoformat()
    # A view is rewritten only when its text changes, so the files'
    # mtimes say when the floor last CHANGED, not when it was last
    # brought up to date. The stamp records the run itself: "is the
    # floor behind raw?" compares against it.
    try:
        last_run_path(home).write_text(report["generated_at"] + "\n")
    except OSError:
        pass
    return report


def last_run_path(home):
    """memory/.last-distill, touched by every distill. Outside
    memory/distilled/ on purpose: that directory holds exactly the
    views (and the capsules mirror), nothing else."""
    return Path(home) / "memory" / ".last-distill"


def last_run(home):
    """Epoch seconds of the last distill run, or None."""
    try:
        return last_run_path(home).stat().st_mtime
    except OSError:
        return None


def raw_newest_mtime(home):
    """Newest mtime among memory/raw/*.jsonl (daily files and digests),
    or None. Cheap: a stat per file, nothing parsed."""
    rdir = memory.raw_dir(Path(home))
    try:
        return max((p.stat().st_mtime for p in rdir.glob("*.jsonl")),
                   default=None)
    except OSError:
        return None


def distill_if_behind(home):
    """Distill when raw was written after the last run (or there never
    was one). Returns True when it ran."""
    newest = raw_newest_mtime(home)
    if newest is None:
        return False
    ran = last_run(home)
    if ran is not None and ran >= newest:
        return False
    distill(home)
    return True
