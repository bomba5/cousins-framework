"""Memory primitives: decisions, activity checkpoints, recall,
consolidation.

The rule the whole module descends from: no cousin context, no
operation. A defaulted home silently reads and writes somebody else's
memory, so a missing COUSIN_HOME is a refusal with a message, never a
fallback.

Search and reindex dispatch to the keyword search module
(cousin_lib.memory_search): keyword always, semantic when configured.
"""
import argparse
import fcntl
import gzip
import hashlib
import json
import os
import re
import shlex
import sys
import zlib
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from cousin_lib import memory_lock, perimeter
from cousin_lib.trace import traced_cli

# decisions.jsonl grows monotonically; past the threshold the older
# entries move to a dated sibling archive and the newest tail stays
# live so recall keeps its recent-context behavior. Nothing is lost:
# every decision also feeds memory/raw via the producer bridge.
DECISIONS_ROTATE_BYTES = 1_000_000
DECISIONS_KEEP_TAIL = 200

# The durable layer: six files the distiller regenerates from raw. A
# file with nothing to say holds STUB_TEXT, and every reader treats the
# stub as empty.
DISTILLED_FILES = (
    "preferences.md", "project-facts.md", "decisions.md",
    "known-failures.md", "operator-calibration.md", "glossary.md",
)
STUB_TEXT = "_(empty - awaiting distillation)_"
DEFAULT_TRUTH_LEVEL = "L3_COUSIN_CONCLUSION"

# The truth-level taxonomy. Writers store the canonical name; older
# entries carry short forms, which normalize_level maps on read.
TRUTH_LEVELS = ("L0_OPERATOR", "L1_FRAMEWORK", "L2_TOOL",
                "L3_COUSIN_CONCLUSION", "L4_COUSIN_HYPOTHESIS",
                "L5_OBSOLETE")
OPERATOR_LEVEL = "L0_OPERATOR"
LEVEL_ALIASES = {
    "operator-stated": "L0_OPERATOR", "operator": "L0_OPERATOR",
    "framework": "L1_FRAMEWORK", "framework-observed": "L1_FRAMEWORK",
    "tool": "L2_TOOL", "tool-result": "L2_TOOL",
    "cousin-conclusion": "L3_COUSIN_CONCLUSION",
    "conclusion": "L3_COUSIN_CONCLUSION",
    "cousin-hypothesis": "L4_COUSIN_HYPOTHESIS",
    "hypothesis": "L4_COUSIN_HYPOTHESIS",
    "obsolete": "L5_OBSOLETE", "superseded": "L5_OBSOLETE",
}
# What a writer may pass: the short words (and the canonical names).
LEVEL_CHOICES = ("operator", "framework", "tool", "conclusion",
                 "hypothesis", "obsolete")


def normalize_level(value):
    """The canonical name for a stored or given truth level; the default
    (L3) for an empty one, 'other' for a value that is not a level."""
    if value is None or str(value).strip() == "":
        return DEFAULT_TRUTH_LEVEL
    text = str(value).strip()
    upper = text.upper()
    if upper in TRUTH_LEVELS:
        return upper
    if re.match(r"^L[0-5]_", upper):
        for level in TRUTH_LEVELS:
            if upper[:3] == level[:3]:
                return level
    return LEVEL_ALIASES.get(text.lower(), "other")


# Law 10: L0, L1 and L2 need a cited source. An uncited framework or tool
# level asked of `decide` or `remember` is written at the default (L3) and
# the line says so (demotion). An uncited operator level stays refused,
# stricter than the law: it is the strongest claim, and a cousin that
# meant it can say where at once. The framework's own entries
# (record_event: `framework:<kind>` state changes, job closes) never come
# through here, so they keep their level.
CITED_LEVELS = ("L1_FRAMEWORK", "L2_TOOL")


def resolve_level(level, cite):
    """(canonical level, error). An operator-stated entry must cite where
    the operator said it (a chat message id, a quote, a date): the level
    is the strongest claim a memory can make, so it carries its source.
    An uncited framework or tool level resolves to the default, L3
    (demotion says why)."""
    canonical = normalize_level(level)
    if canonical == "other":
        return None, "unknown truth level %r (use one of: %s)" % (
            level, ", ".join(LEVEL_CHOICES))
    if canonical == OPERATOR_LEVEL and not (cite or "").strip():
        return None, ("an operator-stated entry needs --cite (where the"
                      " operator said it: chat message id, quote, date)")
    if canonical in CITED_LEVELS and not (cite or "").strip():
        return DEFAULT_TRUTH_LEVEL, None
    return canonical, None


def demotion(level, cite):
    """The one line a write prints when resolve_level demoted its level,
    else None."""
    canonical = normalize_level(level)
    if canonical in CITED_LEVELS and not (cite or "").strip():
        return ("demoted: %s needs --cite (a cited source, law 10); written as %s"
                % (canonical, DEFAULT_TRUTH_LEVEL))
    return None


class _NoContext(Exception):
    pass


def raw_dir(home):
    return Path(home) / "memory" / "raw"


def distilled_dir(home):
    return Path(home) / "memory" / "distilled"


def ensure_layout(home):
    """memory/raw and memory/distilled with the six stubs. Never touches
    a file that exists."""
    raw_dir(home).mkdir(parents=True, exist_ok=True)
    ddir = distilled_dir(home)
    ddir.mkdir(parents=True, exist_ok=True)
    for fname in DISTILLED_FILES:
        path = ddir / fname
        if not path.exists():
            title = fname.replace(".md", "").replace("-", " ").title()
            path.write_text("# %s\n\n%s\n" % (title, STUB_TEXT))


def entry_timestamp(entry):
    """Epoch seconds of a raw entry, or None when it has no parsable
    stamp. _append_raw writes `timestamp`; `created_at` is accepted for
    entries other producers bring in."""
    stamp = entry.get("timestamp") or entry.get("created_at") or ""
    try:
        return datetime.fromisoformat(str(stamp)).timestamp()
    except ValueError:
        return None


def list_raw(home, since_days=30):
    """Raw entries from the last N days across every memory/raw/*.jsonl
    (daily files and monthly digests). Uncertainty keeps: an entry with
    no parsable stamp is never treated as old."""
    home = Path(home)
    ensure_layout(home)
    cutoff = datetime.now(timezone.utc).timestamp() - since_days * 86400
    out = []
    for path in sorted(raw_dir(home).glob("*.jsonl")):
        try:
            lines = path.read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            when = entry_timestamp(entry)
            if when is not None and when < cutoff:
                continue
            out.append(entry)
    return out


def _home(args):
    home = getattr(args, "home", None) or os.environ.get("COUSIN_HOME")
    if not home:
        raise _NoContext(
            "no cousin context - set COUSIN_HOME or pass --home"
            " (refusing to fall back into another cousin's memory)"
        )
    return Path(home).resolve()


def _append_raw(home, entry):
    """Durable-memory candidate into memory/raw/<date>.jsonl. This is
    the producer that keeps the extract_durable_memories audit honest -
    an audited surface nothing writes files noise forever."""
    raw_dir = home / "memory" / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **entry,
    }
    path = raw_dir / (datetime.now().strftime("%Y-%m-%d") + ".jsonl")
    # The perimeter is the framework's own check, on every raw write: no
    # dreamer, distiller or conscience keeps a private cousin's file out of
    # reach by convention. `mark_obsolete` and `record_event` land here, so
    # one check covers the three producers. See perimeter.py.
    perimeter.assert_writable(path, writer="memory._append_raw")
    with memory_lock.write_lock(home), open(path, "a") as fh:
        fh.write(json.dumps(entry) + "\n")
    return entry


# Automatic writers (the framework, the job tracker) are bounded so one
# noisy event cannot bloat raw memory.
EVENT_CONTENT_CHARS = 600
OBSOLETE_LEVEL = "L5_OBSOLETE"


def record_event(home, level, topic, content, source, **extra):
    """One raw entry from an automatic writer (the framework observing a
    state change, a job finishing). Best-effort by contract: it never
    raises into the caller, whose own work already happened, and it
    never creates a cousin home that is not there (a dismissed cousin
    must not be resurrected by a late event). Returns True when the
    entry was written. Extra keyword fields ride along on the entry;
    None values are dropped."""
    try:
        if not home:
            return False
        home = Path(home)
        if not home.is_dir():
            return False
        canonical = normalize_level(level)
        topic = str(topic or "").strip()
        content = " ".join(str(content or "").split())
        if canonical == "other" or not topic or not content:
            return False
        entry = {"topic": topic,
                 "content": content[:EVENT_CONTENT_CHARS],
                 "truth_level": canonical,
                 "source": str(source or "event")}
        entry.update({k: v for k, v in extra.items()
                      if v is not None and k not in entry
                      and k != "timestamp"})
        _append_raw(home, entry)
        return True
    except Exception as err:  # noqa: BLE001 - best-effort by contract
        try:
            print("warning: raw memory event not recorded (%s: %s)"
                  % (type(err).__name__, err), file=sys.stderr)
        except Exception:  # noqa: BLE001 - stderr itself may be gone
            pass
        return False


def topic_entries(home, topic):
    """The raw entries (daily files and digests) whose topic is exactly
    `topic`, oldest first."""
    topic = str(topic or "").strip()
    rows = [e for e in list_raw(home, since_days=36500)
            if str(e.get("topic") or "").strip() == topic]
    rows.sort(key=lambda e: entry_timestamp(e) or 0.0)
    return rows


class ObsoleteRefused(ValueError):
    pass


# ---- valid time ------------------------------------------------------
# raw is append-only; validity is derived from it, never written back.
# An entry is valid from its own `valid_from`, else when it was written;
# it is valid to its own `valid_to`, else the time of the first later
# L5_OBSOLETE mark that covers it: a topic-level mark (no `entry`) covers
# the topic's earlier entries, an entry-level mark (`entry: <id>`) covers
# that entry alone. An id hashes the entry's timestamp, topic and content,
# so it survives the monthly fold (raw_fold copies lines byte-identical).

def entry_id(entry):
    """A stable 12-hex id for a raw entry."""
    key = json.dumps([str(entry.get("timestamp") or entry.get("created_at") or ""),
                      str(entry.get("topic") or "").strip(),
                      str(entry.get("content") or "")], ensure_ascii=False)
    return hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()[:12]


def _all_raw(home):
    """Every raw entry, the hot days and the monthly archives, oldest
    first; a month's digest is a summary of entries already here, so it
    is left out."""
    from cousin_lib import memory_search
    out = []
    for path in memory_search._raw_files(home):
        opener = gzip.open if path.suffix == ".gz" else open
        try:
            with opener(path, "rt", errors="replace") as fh:
                lines = fh.readlines()
        except OSError:
            continue
        for line in lines:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict) and entry.get("source") != "digest" \
                    and str(entry.get("topic") or "").strip():
                out.append(entry)
    out.sort(key=lambda e: entry_timestamp(e) or 0.0)
    return out


def _is_mark(entry):
    return normalize_level(entry.get("truth_level")) == OBSOLETE_LEVEL


def is_entry_mark(entry):
    """An entry-level obsolete mark: an L5 line naming one entry's id."""
    return _is_mark(entry) and bool(entry.get("entry"))


def hidden_ids(entries):
    """The ids the memory views (distilled, the boot packet) leave out,
    from `entries` (the whole history, `_all_raw`): every entry an
    entry-level mark retired, and every entry the review gate holds."""
    from cousin_lib import review_gate
    entries = list(entries)
    return {e["entry"] for e in entries if is_entry_mark(e)} | review_gate.pending_ids(entries)


def digest_unsafe_ids(entries):
    """The ids whose topics the views must read from the whole history,
    never from a monthly digest: a hidden entry (a digest may carry its
    text) and any entry the review gate ever held (the fold left it out
    of its digest while it was held; kept later, only the history has it)."""
    from cousin_lib import review_gate
    entries = list(entries)
    return hidden_ids(entries) | review_gate.held_ever(entries)


def view_noise(entry):
    """A raw line that is bookkeeping, never a line of the views: an
    entry-level mark (a topic-level mark is the topic's newest word and
    stays) and the review gate's records."""
    from cousin_lib import review_gate
    return is_entry_mark(entry) or review_gate.is_record(entry)


def validity(home):
    """Every raw entry that is a claim (obsolete marks left out), oldest
    first, as a copy with `id`, `valid_from`, `valid_to` and `retired_by`
    (the covering mark's id, or None). The covering mark is the earliest
    of: an entry-level mark naming the entry (whenever it was written),
    and a topic-level mark on its topic written at or after it. Marks are
    indexed by entry and by topic, so this is one pass over the history."""
    import bisect
    entries = _all_raw(Path(home))
    by_entry, by_topic = {}, {}
    for m in entries:                       # oldest first: the first seen is the earliest
        if not _is_mark(m):
            continue
        if m.get("entry"):
            by_entry.setdefault(m["entry"], m)
        else:
            by_topic.setdefault(str(m.get("topic") or "").strip(), []).append(m)
    stamps = {t: [entry_timestamp(m) or 0.0 for m in ms] for t, ms in by_topic.items()}
    out = []
    for e in entries:
        if _is_mark(e):
            continue
        row = dict(e, id=entry_id(e))
        when = entry_timestamp(e) or 0.0
        topic = str(e.get("topic") or "").strip()
        candidates = []
        if row["id"] in by_entry:
            candidates.append(by_entry[row["id"]])
        if topic in by_topic:
            i = bisect.bisect_left(stamps[topic], when)
            if i < len(by_topic[topic]):
                candidates.append(by_topic[topic][i])
        cover = min(candidates, key=lambda m: entry_timestamp(m) or 0.0) if candidates else None
        row["valid_from"] = e.get("valid_from") or e.get("timestamp") or e.get("created_at")
        row["valid_to"] = e.get("valid_to") or (cover.get("timestamp") if cover else None)
        row["retired_by"] = entry_id(cover) if cover else None
        out.append(row)
    return out


def live_entries(home, *, at=None):
    """The claims valid at `at` (an ISO time; now when None)."""
    moment = datetime.fromisoformat(at).timestamp() if at else \
        datetime.now(timezone.utc).timestamp()

    def ts(value):
        try:
            return datetime.fromisoformat(str(value)).timestamp()
        except ValueError:
            return None
    out = []
    for row in validity(home):
        start, end = ts(row["valid_from"]), ts(row["valid_to"]) if row["valid_to"] else None
        if (start is None or start <= moment) and (end is None or moment < end):
            out.append(row)
    return out


def tensions(home):
    """Topics whose live claims disagree, for the operator to settle:
    an authored topic (not the framework's own log, distill.MACHINE_PREFIXES)
    with two or more live claims of different content (whitespace aside).
    Whether two claims are opposite is not judged here: a correction or a
    "RESOLVED" beside the claim it resolves is the usual case, and settling
    one (mark_obsolete with its `entry`) clears the tension. Newest first:
    [{"topic", "claims": [row, ...]}], each claim a `validity` row."""
    from cousin_lib import distill
    by_topic = {}
    for row in live_entries(home):
        topic = str(row.get("topic") or "").strip()
        if distill.is_machine_topic(topic):
            continue
        by_topic.setdefault(topic, []).append(row)
    out = []
    for topic, claims in by_topic.items():
        words = {" ".join(str(c.get("content") or "").split()) for c in claims}
        if len(claims) >= 2 and len(words) >= 2:
            out.append({"topic": topic, "claims": claims})
    out.sort(key=lambda t: max(entry_timestamp(c) or 0.0 for c in t["claims"]), reverse=True)
    return out


def mark_obsolete(home, topic, why, *, by=None, force=False,
                  source="obsolete", entry=None):
    """Append an L5_OBSOLETE entry for `topic`: the distiller leaves a
    topic whose newest entry is L5 out of the distilled views, and a
    later entry of any other level revives it. The history stays in
    raw. Refuses an empty reason, and a topic with no raw entries
    unless force (a typo would otherwise retire nothing, silently).
    With `entry` (an id from `validity`/`cousin-memory history`) the
    mark retires that one entry and leaves the topic in the views.
    Returns the entry written."""
    topic = str(topic or "").strip()
    why = " ".join(str(why or "").split())
    if not topic:
        raise ObsoleteRefused("a topic is required")
    if not why:
        raise ObsoleteRefused("a reason is required (--why): an obsolete"
                              " mark says what superseded the topic")
    home = Path(home)
    if not force and not topic_entries(home, topic):
        near = sorted({str(e.get("topic") or "").strip()
                       for e in list_raw(home, since_days=36500)
                       if topic.lower() in str(e.get("topic") or "").lower()})
        hint = (" (similar: %s)" % ", ".join(near[:5])) if near else ""
        raise ObsoleteRefused("no raw entries for topic %r%s; pass --force"
                              " to mark it anyway" % (topic, hint))
    target = None
    if entry:
        target = next((r for r in validity(home)
                       if r["id"] == entry and str(r.get("topic") or "").strip() == topic), None)
        if target is None:
            raise ObsoleteRefused("no entry %r on topic %r (cousin-memory history %s lists"
                                  " them)" % (entry, topic, topic))
    mark = {"topic": topic, "content": "obsolete: %s" % why,
            "truth_level": OBSOLETE_LEVEL, "source": source, "why": why}
    if target is not None:
        mark["entry"] = target["id"]
    if by:
        mark["by"] = str(by)
    return _append_raw(home, mark)


def why(home, eid):
    """One entry and one hop around it: what it was built from (its
    `derived_from`, each resolved or marked missing), what was built from
    it (entries naming it), and the mark that retired it, if any. KeyError
    for an id no raw entry has. Nothing is inherited along the hop: each
    entry keeps its own truth level."""
    entries = _all_raw(home)
    by_id = {entry_id(e): e for e in entries}
    if eid not in by_id:
        raise KeyError(eid)
    entry = by_id[eid]
    built_from = [dict(by_id[d], id=d) if d in by_id else {"id": d, "missing": True}
                  for d in entry.get("derived_from") or []]
    used_by = [dict(e, id=entry_id(e)) for e in entries
               if eid in (e.get("derived_from") or [])]
    info = next((row for row in validity(home) if row["id"] == eid), {})
    return {"entry": dict(entry, id=eid), "derived_from": built_from, "used_by": used_by,
            "retired_by": info.get("retired_by"), "valid_to": info.get("valid_to")}


def format_why(out):
    def line(e):
        if e.get("missing"):
            return "  %s (not in raw memory)" % e["id"]
        text = " ".join(str(e.get("content") or "").split())
        return "  %s [%s] %s: %s" % (e["id"], e.get("truth_level", "?"), e.get("topic", "?"),
                                    text[:200])
    entry = out["entry"]
    lines = ["%s [%s] %s: %s" % (entry["id"], entry.get("truth_level", "?"),
                                 entry.get("topic", "?"),
                                 " ".join(str(entry.get("content") or "").split()))]
    if out.get("retired_by"):
        lines.append("retired by %s (%s)" % (out["retired_by"], out.get("valid_to") or "?"))
    lines.append("built from:" if out["derived_from"] else "built from: nothing recorded")
    lines += [line(e) for e in out["derived_from"]]
    lines.append("built on by:" if out["used_by"] else "built on by: nothing")
    lines += [line(e) for e in out["used_by"]]
    return "\n".join(lines)


def _rotate_decisions_if_needed(path):
    """Rotate an oversized decisions log; returns the archive path if
    rotation happened. Append-mode archive: same-day re-rotation is
    safe."""
    try:
        if path.stat().st_size < DECISIONS_ROTATE_BYTES:
            return None
        lines = path.read_text().splitlines(keepends=True)
    except OSError:
        return None
    if len(lines) <= DECISIONS_KEEP_TAIL:
        return None
    older, tail = lines[:-DECISIONS_KEEP_TAIL], lines[-DECISIONS_KEEP_TAIL:]
    stamp = datetime.now().strftime("%Y%m%d")
    archive = path.with_name(
        path.name.replace(".jsonl", "-archive-%s.jsonl" % stamp)
    )
    try:
        with open(archive, "a") as fh:
            fh.writelines(older)
        tmp = path.with_suffix(".jsonl.tmp")
        tmp.write_text("".join(tail))
        os.replace(tmp, path)
    except OSError:
        return None
    return archive


def parse_decide_stdin(text):
    """Split a decide body into (topic, decision, reasoning).

    Chunks are separated by a line that is exactly `---`. Nothing inside
    a chunk is interpreted: a quoted heredoc into stdin cannot be
    expanded by any shell, which is the point - backticks and $() in
    decision prose get executed by the CALLER's shell on the argv path.
    """
    chunks, current = [], []
    for line in text.split("\n"):
        if line.strip() == "---":
            chunks.append("\n".join(current).strip())
            current = []
        else:
            current.append(line)
    chunks.append("\n".join(current).strip())
    if len(chunks) != 3:
        raise ValueError(
            "decide --stdin needs 3 chunks separated by a line of exactly"
            " '---' (topic, decision, reasoning); got %d" % len(chunks))
    if not all(chunks):
        raise ValueError("decide --stdin: no chunk may be empty")
    return chunks[0], chunks[1], chunks[2]


def _derived_args(p):
    p.add_argument("--derived-from", dest="derived_from", action="append", metavar="ID",
                   default=None, help="an entry id this was built from (repeat for"
                                      " more); `why ID` walks it")


def _level_args(p):
    p.add_argument("--level", default="conclusion",
                   help="truth level: %s (default conclusion); operator"
                        " = the operator stated it, needs --cite;"
                        " framework or tool without --cite is written as"
                        " conclusion" % ", ".join(LEVEL_CHOICES))
    p.add_argument("--cite", default=None,
                   help="where it comes from (chat message id, quote,"
                        " file, date); required for --level operator,"
                        " keeps --level framework or tool")


_DECIDE_USAGE = (
    "Usage: cousin-memory decide TOPIC DECISION REASONING\n"
    "   or: cousin-memory decide --stdin <<'EOF'\n"
    "       topic\n       ---\n       decision\n       ---\n"
    "       reasoning\n       EOF")


# ------------------------------------------------ library (the one implementation)

_ENTRY_ID = re.compile(r"^[0-9a-f]{12}$")


def check_derived(derived_from):
    """`derived_from` as a list of entry ids (12 hex characters, entry_id's
    shape), or ValueError. None and [] mean none. One hop only: what this
    entry was built from, never a chain or a confidence inherited along it."""
    if derived_from in (None, "", []):
        return []
    if isinstance(derived_from, str):
        derived_from = [derived_from]
    if not isinstance(derived_from, (list, tuple)):
        raise ValueError("derived_from must be a list of entry ids")
    out = []
    for value in derived_from:
        value = str(value or "").strip()
        if not _ENTRY_ID.match(value):
            raise ValueError("derived_from: %r is not an entry id (12 hex characters,"
                             " as `history` and `why` list them)" % value)
        if value not in out:
            out.append(value)
    return out


def decide(home, topic, decision, reasoning, *, level=None, cite=None, derived_from=None):
    """Log a decision with its reasoning. Raises ValueError for a missing
    part or a level error. Returns the line the CLI prints. Mirrors the
    old CLI's non-`--stdin` path exactly: no stripping here - padding
    in topic/decision/reasoning is kept verbatim in stdout,
    decisions.jsonl and the raw bridge. A caller that wants trimmed
    text strips it itself before calling (the `--stdin` parser already
    strips its own chunks, before this function ever sees them)."""
    home = Path(home)
    if not (topic and decision and reasoning):
        raise ValueError("decide needs topic, decision and reasoning")
    resolved, err = resolve_level(level, cite)
    if err:
        raise ValueError(err)
    derived = check_derived(derived_from)
    entry = {"timestamp": datetime.now().astimezone().isoformat(),
             "topic": topic, "decision": decision, "reasoning": reasoning}
    decisions = home / "data" / "decisions.jsonl"
    decisions.parent.mkdir(parents=True, exist_ok=True)
    # One critical section: the append, the rotation (a read, then a
    # replace) and the raw bridge. A decision appended by another session
    # between the rotation's read and its replace would be lost.
    with memory_lock.write_lock(home):
        with open(decisions, "a") as fh:
            fh.write(json.dumps(entry) + "\n")
        lines = ["Decision logged: [%s] %s" % (topic, decision)]
        note = demotion(level, cite)
        if note:
            lines.append(note)
        archive = _rotate_decisions_if_needed(decisions)
        if archive:
            lines.append("(decisions.jsonl rotated: older entries -> %s)" % archive.name)
        try:
            _append_raw(home, {"topic": topic,
                               "content": "%s - why: %s" % (decision, reasoning),
                               "truth_level": resolved, "source": "decision",
                               **({"cite": cite} if cite else {}),
                               **({"derived_from": derived} if derived else {})})
        except OSError as err:
            lines.append("warning: raw-memory bridge failed (%s); decision logged anyway" % err)
    return "\n".join(lines)


def remember_entry(home, topic, fact, *, level=None, cite=None, derived_from=None,
                   source="remember"):
    """The raw entry a `remember` writes, as written (timestamp stamped),
    or ValueError. Every writer that journals what it changed needs the
    entry, not the line: `entry_id` is a sha1 over the entry's own stamp,
    topic and content, so it cannot be recomputed from the text asked
    for. `source` says which door the claim came in by - a dreaming pass
    writes `dream`, so a claim that reads as the operator's word is not
    left carrying a line that says `remember`.

    `derived_from`: the entry ids this claim was built from (check_derived),
    one hop, nothing inherited along it. `why` walks it.
    """
    topic, fact = str(topic or "").strip(), str(fact or "").strip()
    if not (topic and fact):
        raise ValueError("remember needs a topic and a fact")
    resolved, err = resolve_level(level, cite)
    if err:
        raise ValueError(err)
    derived = check_derived(derived_from)
    entry = {"topic": topic, "content": fact, "truth_level": resolved, "source": source}
    if cite:
        entry["cite"] = cite
    if derived:
        entry["derived_from"] = derived
    return _append_raw(Path(home), entry)


def remember(home, topic, fact, *, level=None, cite=None, derived_from=None,
             source="remember"):
    """One durable fact into raw memory. Raises ValueError. Returns the line,
    and under it the demotion line when an uncited level was demoted.
    `derived_from`: the entry ids it was built from (check_derived), one hop;
    `why` walks it."""
    entry = remember_entry(home, topic, fact, level=level, cite=cite,
                           derived_from=derived_from, source=source)
    line = "Remembered [%s] (%s): %s" % (entry["topic"], entry["truth_level"],
                                         entry["content"])
    note = demotion(level, cite)
    return line + "\n" + note if note else line


RECALL_ALL = 50           # a keyword recall with last=0 ("all") still stops somewhere
_BACKFILL_MARK = ("data", ".decisions-backfilled")   # data/, not memory/: a transplant copies memory/


def _decision_rows(home):
    """Every decision row of data/decisions.jsonl and of its rotated
    archives (data/decisions-archive-*.jsonl), oldest file first."""
    data = Path(home) / "data"
    for path in sorted(data.glob("decisions-archive-*.jsonl")) + [data / "decisions.jsonl"]:
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("topic") and row.get("decision"):
                yield row


def _raw_memories(home):
    """{(topic, content)} over every raw entry, daily files, digests and
    archives alike, whitespace-normalised: what a twin is compared on."""
    from cousin_lib import memory_search
    seen = set()
    for path in memory_search._raw_files(home):
        opener = gzip.open if path.suffix == ".gz" else open
        try:
            with opener(path, "rt", errors="replace") as fh:
                lines = fh.readlines()
        except OSError:
            continue
        for line in lines:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict):
                seen.add((str(entry.get("topic") or "").strip(),
                          " ".join(str(entry.get("content") or "").split())))
    return seen


def _trashed_memories(home):
    """{(topic, content)} of every raw line still in the memory trash
    (memory/.trash/<id>/manifest.json, items of kind "line"), normalised
    as _raw_memories normalises: removed on purpose, so never a twin to
    bring back. A restored batch leaves the trash, so it is not here."""
    from cousin_lib import memory_trash
    seen = set()
    for manifest in memory_trash.list_trash(home):
        for item in manifest.get("items") or []:
            if not isinstance(item, dict) or item.get("kind") != "line":
                continue
            try:
                entry = json.loads(item.get("line") or "")
            except ValueError:
                continue
            if isinstance(entry, dict):
                seen.add((str(entry.get("topic") or "").strip(),
                          " ".join(str(entry.get("content") or "").split())))
    return seen


def backfill_decisions(home, *, dry_run=False):
    """Every decision in data/decisions.jsonl (and its rotated archives)
    with no raw twin becomes a raw entry: source "decision", its original
    timestamp kept, in the raw file of its own day, so the raw fold treats
    it like any entry of that day. Decisions logged before the raw store
    existed live only in that log, and recall reads raw: without this they
    become unreachable. A twin is the same topic and the same
    "<decision> - why: <reasoning>" content anywhere in raw. Idempotent.
    Returns the number written, or with dry_run the number that would be
    (dry_run writes nothing)."""
    home = Path(home)
    # the read of raw and the write are one section (memory_lock): a
    # decision another session logs in between would be written twice
    with memory_lock.write_lock(home):
        return _backfill(home, dry_run)


def _backfill(home, dry_run):
    # A raw line the cousin or its operator trashed is not an orphan: the
    # old log still holds it, and backfilling it would undo the removal.
    have = _raw_memories(home) | _trashed_memories(home)
    todo = []
    for row in _decision_rows(home):
        content = "%s - why: %s" % (row["decision"], row.get("reasoning", ""))
        key = (str(row["topic"]).strip(), " ".join(content.split()))
        if key in have:
            continue
        have.add(key)
        todo.append({"timestamp": str(row.get("timestamp") or ""), "topic": row["topic"],
                     "content": content, "truth_level": DEFAULT_TRUTH_LEVEL,
                     "source": "decision", "backfilled": True})
    if dry_run or not todo:
        return len(todo)
    rdir = raw_dir(home)
    rdir.mkdir(parents=True, exist_ok=True)
    by_day = {}
    for entry in todo:
        day = entry["timestamp"][:10]
        by_day.setdefault(day if re.match(r"\d{4}-\d{2}-\d{2}$", day) else "undated",
                          []).append(entry)
    for day, entries in sorted(by_day.items()):
        with open(rdir / ("%s.jsonl" % day), "a") as fh:
            fh.writelines(json.dumps(e) + "\n" for e in entries)
    return len(todo)


def ensure_backfilled(home):
    """backfill_decisions once per home, from the first recall or search:
    the mark (data/.decisions-backfilled) records that it ran, and an
    exclusive lock keeps two processes from writing the same decisions. A
    home with no decisions log gets no mark and no write."""
    home = Path(home)
    mark = home.joinpath(*_BACKFILL_MARK)
    if mark.exists():
        return
    data = home / "data"
    if not (data / "decisions.jsonl").exists() and not any(data.glob("decisions-archive-*.jsonl")):
        return
    mark.parent.mkdir(parents=True, exist_ok=True)
    with open(mark.with_name(mark.name + ".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if mark.exists():
            return
        count = backfill_decisions(home)
        mark.write_text("%s backfilled %d decision(s) from data/decisions.jsonl\n"
                        % (datetime.now(timezone.utc).isoformat(), count))


def try_backfill(home):
    """ensure_backfilled for a reader (search, recall): a failure (a
    read-only or full data/, an unreadable log, or a truncated gzip
    archive raising EOFError or zlib.error out of _raw_memories) costs
    the backfill, never the read. It says so on stderr and writes no
    mark, so the next read tries again."""
    try:
        ensure_backfilled(home)
    except (OSError, ValueError, EOFError, zlib.error) as err:
        print("memory: decisions backfill skipped: %s: %s" % (type(err).__name__, err),
              file=sys.stderr)


def _relevant(home, keyword, hits, top, root):
    """The hits recall prints: those the keyword leg found, and those the
    semantic leg ranks at or above [recall] min_score. The semantic leg
    returns the nearest entries whatever they are, so without this floor
    a keyword that matches nothing still prints `top` unrelated entries."""
    from cousin_lib import memory_search
    if all(h.get("similarity") is None for h in hits):
        return hits                       # keyword leg only: every hit matched a word
    words = {h["path"] for h in memory_search._keyword_search(keyword, home, top, "raw", root)}
    floor = float(memory_search.recall_thresholds(root)[0]["min_score"])
    return [h for h in hits if h["path"] in words
            or (h.get("similarity") is not None and h["similarity"] >= floor)]


def recall_entries(home, keyword="", last=10, *, root=None):
    """Recall over the one store: raw
    memory, through the index `search` reads, so a fact written by
    `remember` is as recallable as a decision. With a keyword: the
    `last` best-ranked raw entries. Without one: the newest `last`
    authored entries (distill.MACHINE_PREFIXES topics are the log, not
    memory). Either way oldest first, so the reading order stays
    chronological. data/decisions.jsonl is not read here: the first recall
    in a home backfills the decisions only it holds into raw, and
    `decide` keeps appending it for its other readers.
    Recall is not a search the cousin made: it records nothing in the
    recall log. `root` as in memory_search.search: None discovers it, the
    runner passes its own."""
    from cousin_lib import distill, memory_search
    home = Path(home)
    try_backfill(home)
    last = int(last or 0)
    keyword = str(keyword or "").strip()
    if keyword:
        top = last or RECALL_ALL
        hits, _notice = memory_search.search(keyword, top=top, home=home,
                                             collection="raw", root=root, record=False)
        hits = _relevant(home, keyword, hits, top, root)
        entries = [e for e in (memory_search.raw_entry(h["path"]) for h in hits) if e]
    else:
        entries = [e for e in list_raw(home, since_days=36500)
                   if e.get("topic") and e.get("content")
                   and not distill.is_machine_topic(e.get("topic"))]
        entries.sort(key=lambda e: entry_timestamp(e) or 0.0)
        entries = entries[-last:] if last else entries
    return sorted(entries, key=lambda e: entry_timestamp(e) or 0.0)


def format_recall(entries, keyword=""):
    """Each entry as two lines plus a blank line AFTER IT (including the
    last), never trimmed. A decision prints as the old CLI printed it:
    its raw content splits at the first " - why: " (decide's own
    separator) into the decision and a `  Why:` line; so does a monthly
    digest (raw_fold), which carries a decision's content under source
    "digest". Any other entry prints its content and `  (level, source)`.
    Timestamps are the raw entry's own: UTC for what decide writes now,
    the original local stamp for a backfilled decision."""
    if not entries:
        return "No memories found matching '%s'" % (keyword or "(all)")
    out = []
    for entry in entries:
        when = str(entry.get("timestamp") or "?")[:16]
        topic, content = entry.get("topic", "?"), str(entry.get("content", ""))
        if entry.get("source") in ("decision", "digest") and " - why: " in content:
            decision, why = content.split(" - why: ", 1)
            out.append("[%s] %s: %s" % (when, topic, decision))
            out.append("  Why: %s" % why)
        else:
            out.append("[%s] %s: %s" % (when, topic, content))
            out.append("  (%s, %s)" % (entry.get("truth_level", "?"), entry.get("source", "?")))
        out.append("")
    return "\n".join(out)


def note_activity(home, text):
    text = text or "Idle"
    path = Path(home) / "data" / "last-activity.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    # tmp + replace under the lock: a reader in another session (a side
    # session's digest) never sees the file truncated mid-write
    with memory_lock.write_lock(home):
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text("%s: %s\n" % (datetime.now().isoformat(), text))
        os.replace(tmp, path)
    return "Activity saved: %s" % text[:80]


def search_text(home, query, *, top=5, collection=None, root=None):
    from cousin_lib import memory_search
    hits, notice = memory_search.search(query, top=top, home=Path(home),
                                        collection=collection, root=root)
    fmt = getattr(memory_search, "format_results", None)
    if fmt is not None:
        text = fmt(hits)
    else:
        text = "\n".join("%d. [%.3f] [%s] %s\n   %s" % (
            i + 1, h.get("score", 0.0), h.get("collection", ""), h.get("path", ""),
            (h.get("snippet") or "").strip()) for i, h in enumerate(hits)) or "no hits"
    if notice:
        text += "\nnotice: %s" % notice
    return text


def _cmd_decide(args):
    home = _home(args)
    topic, decision, reasoning = args.topic, args.decision, args.reasoning
    if getattr(args, "stdin", False):
        try:
            topic, decision, reasoning = parse_decide_stdin(sys.stdin.read())
        except ValueError as err:
            print("error: %s" % err, file=sys.stderr)
            return 2
    if not (topic and decision and reasoning):
        print(_DECIDE_USAGE, file=sys.stderr)
        return 2
    try:
        print(decide(home, topic, decision, reasoning,
                     level=getattr(args, "level", None), cite=getattr(args, "cite", None),
                     derived_from=getattr(args, "derived_from", None)))
    except ValueError as err:
        print("error: %s" % err, file=sys.stderr)
        return 2
    return 0


def _cmd_remember(args):
    """One durable fact straight into raw memory (no decision record):
    what the operator told you, what a tool measured, a hypothesis."""
    home = _home(args)
    if not (str(args.topic or "").strip() and str(args.fact or "").strip()):
        print("Usage: cousin-memory remember TOPIC FACT [--level operator"
              " --cite SOURCE]", file=sys.stderr)
        return 2
    try:
        print(remember(home, args.topic, args.fact, level=args.level, cite=args.cite,
                       derived_from=getattr(args, "derived_from", None)))
    except ValueError as err:
        print("error: %s" % err, file=sys.stderr)
        return 2
    return 0


def _cmd_why(args):
    """One entry, what it was built from and what was built from it."""
    home = _home(args)
    try:
        out = why(home, args.id)
    except KeyError:
        print("error: no raw entry with id %s (cousin-memory history TOPIC lists ids)"
              % args.id, file=sys.stderr)
        return 1
    print(json.dumps(out, default=str) if args.json else format_why(out))
    return 0


def _cmd_obsolete(args):
    """Mark a topic superseded (L5): out of the distilled views, kept in
    raw. A later entry on the topic brings it back."""
    home = _home(args)
    try:
        mark_obsolete(home, args.topic, args.why, force=args.force,
                      by=os.environ.get("COUSIN_SLUG") or home.name, entry=args.entry)
    except ObsoleteRefused as err:
        print("error: %s" % err, file=sys.stderr)
        return 2
    print("Marked obsolete [%s]: %s" % (args.topic.strip(),
                                        " ".join(args.why.split())))
    return _run_distill(home)


def _cmd_history(args):
    """A topic's claims, oldest first, each with its id and valid time."""
    home = _home(args)
    topic = args.topic.strip()
    rows = [r for r in validity(home) if str(r.get("topic") or "").strip() == topic]
    if not rows:
        print("no claims for topic %r" % topic)
        return 1
    for r in rows:
        state = ("valid to %s" % r["valid_to"]) if r["valid_to"] else "live"
        print("%s [%s] %s  (%s)" % (r["id"], str(r["valid_from"] or "?")[:16],
                                   " ".join(str(r.get("content", "")).split())[:200], state))
    return 0


def _cmd_tensions(args):
    """Topics whose live claims disagree, and how to settle one."""
    home = _home(args)
    found = tensions(home)
    if args.json:
        print(json.dumps(found, indent=1, default=str))
        return 0
    if not found:
        print("no tensions")
        return 0
    for t in found:
        print("%s (%d live claims)" % (t["topic"], len(t["claims"])))
        for c in t["claims"]:
            print("  %s [%s] %s" % (c["id"], str(c["valid_from"] or "?")[:16],
                                   " ".join(str(c.get("content", "")).split())[:200]))
        print("  settle: cousin-memory obsolete %s --why \"...\" --entry <id>"
              % shlex.quote(t["topic"]))
    return 0


def _cmd_review(args):
    """What the review gate holds, or the operator's verdict on some of it.
    A verdict from inside a cousin's own process tree is refused: the
    model under review must not release its own writes."""
    import getpass
    from cousin_lib import accounts, review_gate
    home = _home(args)
    ids, verdict = (args.keep, "keep") if args.keep else (args.drop, "drop")
    if not ids:
        rows = review_gate.pending(home)
        if not rows:
            print("nothing held for review")
            return 0
        for r in rows:
            print("%s [%s] %s: %s" % (r["id"], str(r["valid_from"] or "?")[:16], r["topic"],
                                      " ".join(str(r.get("content", "")).split())[:200]))
        print("settle: cousin-memory review --keep <id>... | --drop <id>... --why \"...\""
              " (from your own shell)")
        return 0
    ancestor = accounts._inside_cousin_ancestry()
    if ancestor:
        print("error: a review verdict is the operator's: this runs inside a cousin's"
              " process tree (pid %d). Run it from your own shell." % ancestor, file=sys.stderr)
        return 2
    by = "operator:%s" % getpass.getuser()
    code = 0
    for entry_id in ids:
        try:
            row = review_gate.release(home, entry_id, verdict, why=args.why, by=by)
        except ObsoleteRefused as err:
            print("error: %s" % err, file=sys.stderr)
            code = 2
            continue
        print("%s %s [%s] by %s" % ("Kept" if verdict == "keep" else "Dropped", entry_id,
                                    row["topic"], by))
    return _run_distill(home) or code


def _cmd_recall(args):
    home = _home(args)
    keyword = (args.keyword or "").lower()
    entries = recall_entries(home, args.keyword, args.last)
    print(format_recall(entries, keyword))
    return 0


def _cmd_activity(args):
    home = _home(args)
    text = " ".join(args.text) if args.text else "Idle"
    print(note_activity(home, text))
    return 0


def _run_distill(home, max_lines=None):
    from cousin_lib import distill

    kwargs = {}
    if max_lines:
        kwargs["max_lines"] = max_lines
    report = distill.distill(home, **kwargs)
    print("distilled %d topics from %d raw entries:"
          % (report["topics"], report["entries"]))
    if report.get("obsolete"):
        print("  (%d obsolete topic(s) left out; history kept in raw)"
              % report["obsolete"])
    for fname, count in report["files"].items():
        print("  %s: %d" % (fname, count))
    return 0


def _cmd_distill(args):
    """Regenerate memory/distilled/*.md from memory/raw. Deterministic
    and idempotent; the boot assembler runs it too, so a manual run is
    for inspection or after bulk raw edits."""
    home = _home(args)
    return _run_distill(home, max_lines=args.max_lines)


def _cmd_consolidate(args):
    """Topics with 3+ entries in memory/raw are promotion candidates.
    Raw holds every decision (decide writes it there too, and the
    backfill brings the ones only the old log held), so counting
    data/decisions.jsonl as well would count each decision twice.
    Consolidation is a mechanism, not a reminder: the distiller then
    rebuilds memory/distilled/ from raw right here, so consolidate
    promotes instead of only suggesting."""
    home = _home(args)
    ensure_backfilled(home)
    counts = Counter()
    latest = defaultdict(str)

    def feed(topic, sample):
        topic = (topic or "").strip().lower()
        if topic:
            counts[topic] += 1
            latest[topic] = sample[:90]

    raw_dir = home / "memory" / "raw"
    if raw_dir.is_dir():
        for path in sorted(raw_dir.glob("*.jsonl")):
            try:
                for line in path.read_text().splitlines():
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    feed(entry.get("topic"), entry.get("content", ""))
            except OSError:
                continue
    candidates = [(t, n) for t, n in counts.most_common() if n >= 3]
    if not candidates:
        print("no promotion candidates (topics with 3+ entries in"
              " memory/raw/)")
    else:
        print("promotion candidates (3+ entries; consider promoting to"
              " the memory index):")
        for topic, count in candidates[:15]:
            print("  %3dx  %s" % (count, topic))
            print("        latest: %s" % latest[topic])
    return _run_distill(home)


def _cmd_search(args):
    from cousin_lib import memory_search

    home = _home(args)
    hits, notice = memory_search.search(args.query, top=args.top,
                                        home=home,
                                        collection=args.collection)
    if args.json:
        print(json.dumps(hits))
    else:
        memory_search.print_results(hits)
    if notice:
        # The degrade contract: a promised-but-dead semantic leg is
        # never silent. stderr, so --json output stays parseable.
        print("notice: %s" % notice, file=sys.stderr)
    return 0


def _cmd_reindex(args):
    from cousin_lib import memory_search

    home = _home(args)
    count = memory_search.build_index(home)
    print("indexed %d file(s)" % count)
    config = memory_search._embedding_config()
    if config is None:
        return 0
    if config == "broken":
        print("notice: embedding config exists but is unusable; vector"
              " index not rebuilt", file=sys.stderr)
        return 0
    report = memory_search.ensure_index(home, config, force=True)
    print("embedded %d chunk(s), %d failed" % (report["embedded"],
                                                report["failed"]))
    if report["failed"]:
        print("notice: embedding service failed for %d chunk(s); prior"
              " vectors kept where available" % report["failed"],
              file=sys.stderr)
    return 0


def _cmd_compact(args):
    from cousin_lib import compact

    home = _home(args)
    if args.target == "raw":
        from cousin_lib import raw_fold

        if args.dry_run:
            print("raw: dry-run not supported; the fold is lossless"
                  " (archive/<month>.jsonl.gz keeps every byte)")
            return 0
        kwargs = {}
        if args.hot_days is not None:
            kwargs["keep_days"] = args.hot_days
        print("raw: %s" % json.dumps(raw_fold.fold_raw(home, **kwargs),
                                     sort_keys=True))
        return 0
    kwargs = {"dry_run": args.dry_run}
    if args.budget is not None:
        kwargs["budget"] = args.budget
    if args.hot_days is not None:
        kwargs["hot_days"] = args.hot_days
    report = compact.compact_index(home, **kwargs)
    print(json.dumps(report, sort_keys=True))
    return 0 if report.get("ok") else 1


def _cmd_propose_shared(args):
    from cousin_lib.config import CousinConfig
    from cousin_lib.shared_tier import commit_bulk_propose, plan_bulk_propose

    home = _home(args)
    slug = CousinConfig.load(home).slug
    plan = plan_bulk_propose(home, slug)
    if not plan["eligible"]:
        print("not eligible: [memory] scope is %r (need 'shared';"
              " private and unset are excluded by design)"
              % plan["scope"])
        return 0
    for item in plan["propose"]:
        print("  PROPOSE  %s -> proposed/%s"
              % (item["fname"], item["proposed_name"]))
    for fname, reason in plan["skipped"]:
        print("  skip     %s  (%s)" % (fname, reason))
    if not args.commit:
        print("[dry-run] %d would be proposed, %d skipped;"
              " re-run with --commit to write"
              % (len(plan["propose"]), len(plan["skipped"])))
        return 0
    count = commit_bulk_propose(plan, slug)
    for fname, reason in plan.get("refused", ()):
        print("  refused  %s  (%s)" % (fname, reason))
    print("proposed %d file(s) into the review queue; nothing landed"
          " in canonical" % count)
    return 0


def _cmd_import_auto(args):
    """Fold the agent CLI's own auto-memory into memory/imported/auto/
    (memory_import). A dry run unless --apply."""
    from cousin_lib import memory_import
    from cousin_lib.config import FrameworkConfig, MissingConfigError
    home = _home(args)
    try:
        root = FrameworkConfig.resolve().root
    except MissingConfigError as err:
        print("ERROR: import-auto reads config/harness.toml under the framework"
              " root: %s" % err, file=sys.stderr)
        return 2
    try:
        if args.apply:
            rows = memory_import.apply(home, root=root, sample=args.sample)
        else:
            rows = memory_import.plan(home, root=root)
        report = memory_import.verify(home, root=root) if args.verify else None
    except memory_import.ManifestError as err:
        print("ERROR: import-auto: %s" % err, file=sys.stderr)
        return 2
    if report is not None:
        print(json.dumps(report, indent=1) if args.json
              else memory_import.format_verify(report))
        if not report["baseline"] or not report["queries"]:
            return 2          # nothing was compared: the check proved nothing
        return 1 if report["lost"] else 0
    if args.json:
        print(json.dumps(rows, indent=1))
    else:
        print(memory_import.format_plan(rows, applied=args.apply))
    return 0


def _cmd_export(args):
    """The home's memory as one tar.gz, byte for byte (memory_export)."""
    from cousin_lib import memory_export
    home = _home(args)
    try:
        manifest = memory_export.export(home, args.out)
    except FileExistsError as err:
        print("error: %s" % err, file=sys.stderr)
        return 2
    size = sum(f["size"] for f in manifest["files"])
    print("exported %d file(s), %d bytes, of %s to %s (%d left out: rebuilt, not moved)"
          % (len(manifest["files"]), size, manifest["source"], args.out,
             len(manifest["excluded"])))
    return 0


def _cmd_import(args):
    """A memory export into this home: every file checked against the
    manifest first, a dry run unless --yes (memory_export)."""
    from cousin_lib import memory_export
    home = _home(args)
    try:
        report = memory_export.import_bundle(home, args.bundle, merge=args.merge,
                                             apply=args.yes)
    except (memory_export.BundleError, memory_export.ImportRefused) as err:
        print("error: %s" % err, file=sys.stderr)
        return 2
    print(json.dumps(report, indent=1) if args.json else memory_export.format_import(report))
    return 0


def _cmd_trash(args):
    from cousin_lib import memory_trash

    return memory_trash.cli(args.rest, _home(args))


@traced_cli("cousin-memory")
def memory_main(argv=None):
    parser = argparse.ArgumentParser(prog="cousin-memory")
    parser.add_argument("--home")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("search")
    p.add_argument("query")
    p.add_argument("--top", type=int, default=5)
    p.add_argument("--collection", default=None,
                   help="limit to one of memory, notes, harness")
    p.add_argument("--json", action="store_true",
                   help="print the hits as a JSON list")
    p.set_defaults(func=_cmd_search)
    p = sub.add_parser("reindex")
    p.set_defaults(func=_cmd_reindex)
    p = sub.add_parser(
        "compact",
        help="index: retire old reachable pointers from MEMORY.md until"
             " it fits the byte budget (hygiene, never deletion);"
             " raw: fold daily raw files older than the hot window into"
             " monthly gzip archives plus a per-topic digest (lossless)")
    p.add_argument("--target", choices=["index", "raw"], default="index")
    p.add_argument("--budget", type=int, default=None)
    p.add_argument("--hot-days", type=int, default=None,
                   help="index: pointers newer than this never move;"
                        " raw: days of daily files kept unfolded")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=_cmd_compact)
    p = sub.add_parser(
        "distill",
        help="rebuild memory/distilled/*.md from memory/raw (newest"
             " entry per topic, bounded, curated text above the marker"
             " kept); the boot assembler runs this too")
    p.add_argument("--max-lines", type=int, default=None,
                   help="lines per distilled file (default 40)")
    p.set_defaults(func=_cmd_distill)
    p = sub.add_parser(
        "propose-shared",
        help="nominate marked shareable memories into the shared"
             " review queue (dry-run unless --commit; nothing ever"
             " lands in canonical from here)")
    p.add_argument("--commit", action="store_true")
    p.set_defaults(func=_cmd_propose_shared)
    p = sub.add_parser("decide")
    p.add_argument("topic", nargs="?")
    p.add_argument("decision", nargs="?")
    p.add_argument("reasoning", nargs="?")
    p.add_argument("--stdin", action="store_true",
                   help="read topic, decision and reasoning from stdin,"
                        " separated by a line that is exactly '---'"
                        " (a quoted heredoc cannot be shell-expanded)")
    _level_args(p)
    _derived_args(p)
    p.set_defaults(func=_cmd_decide)
    p = sub.add_parser(
        "remember",
        help="record one fact in raw memory with its truth level;"
             " --level operator (what the operator said) needs --cite")
    p.add_argument("topic", nargs="?")
    p.add_argument("fact", nargs="?")
    _level_args(p)
    _derived_args(p)
    p.set_defaults(func=_cmd_remember)
    p = sub.add_parser(
        "why",
        help="one entry by its id, what it was built from (derived_from) and"
             " what was built from it: one hop each way, nothing inherited")
    p.add_argument("id")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_why)
    p = sub.add_parser(
        "obsolete",
        help="mark a topic superseded (L5_OBSOLETE): the distiller drops"
             " it from the distilled views, raw keeps the history; a"
             " later entry on the topic revives it")
    p.add_argument("topic")
    p.add_argument("--why", required=True,
                   help="what superseded it (required, non-empty)")
    p.add_argument("--force", action="store_true",
                   help="mark a topic that has no raw entries")
    p.add_argument("--entry", default=None,
                   help="retire one entry of the topic by its id (cousin-memory history"
                        " lists them); the topic stays in the views")
    p.set_defaults(func=_cmd_obsolete)
    p = sub.add_parser(
        "tensions",
        help="topics whose live claims disagree (two or more live claims of"
             " different content on an authored topic), each with how to settle it")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_tensions)
    p = sub.add_parser(
        "history",
        help="a topic's claims, oldest first, each with its id and valid time"
             " (live, or valid to when an obsolete mark retired it)")
    p.add_argument("topic")
    p.set_defaults(func=_cmd_history)
    p = sub.add_parser(
        "review",
        help="the entries the review gate holds (a turn wrote more than"
             " [memory] review_batch), or a verdict: --keep or --drop ids")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--keep", nargs="+", metavar="ID", default=None)
    g.add_argument("--drop", nargs="+", metavar="ID", default=None)
    p.add_argument("--why", default="", help="the reason, recorded with the verdict")
    p.set_defaults(func=_cmd_review)
    p = sub.add_parser("recall")
    p.add_argument("keyword", nargs="?", default="")
    p.add_argument("--last", type=int, default=20)
    p.set_defaults(func=_cmd_recall)
    p = sub.add_parser("activity")
    p.add_argument("text", nargs="*")
    p.set_defaults(func=_cmd_activity)
    p = sub.add_parser(
        "trash",
        help="removed memories: `trash` or `trash list` shows the"
             " batches the console moved to memory/.trash, `trash"
             " restore <id>` puts one back")
    p.add_argument("rest", nargs=argparse.REMAINDER)
    p.set_defaults(func=_cmd_trash)
    p = sub.add_parser(
        "import-auto",
        help="fold the agent CLI's own auto-memory into"
             " memory/imported/auto/ with provenance (dry run unless"
             " --apply; idempotent; an edited copy is never overwritten)")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--json", action="store_true", help="print the plan's rows as JSON")
    p.add_argument("--verify", action="store_true",
                   help="replay the queries --apply took as its baseline and report any"
                        " that lost a memory (exit 1 on a loss, 2 when nothing was"
                        " compared)")
    p.add_argument("--sample", type=int, default=50,
                   help="with --apply: how many of the newest logged queries the"
                        " baseline replays")
    p.set_defaults(func=_cmd_import_auto)
    p = sub.add_parser(
        "export",
        help="the home's memory (raw, archives, knowledge files, distilled,"
             " dreams, decisions) as one tar.gz with a MANIFEST.json of sha256"
             " sums; every file byte for byte, indexes left out")
    p.add_argument("--out", required=True, help="the tar.gz to write (never overwritten)")
    p.set_defaults(func=_cmd_export)
    p = sub.add_parser(
        "import",
        help="bring a memory export into a home: refused on any sha256 mismatch"
             " and into a home that has raw memory unless --merge; a dry run"
             " unless --yes; distills afterwards")
    p.add_argument("bundle", help="the tar.gz cousin-memory export wrote")
    p.add_argument("--home", default=argparse.SUPPRESS,
                   help="the home to import into (default: COUSIN_HOME)")
    p.add_argument("--merge", action="store_true",
                   help="into a home with raw memory: append, byte for byte, the"
                        " lines it does not hold yet")
    p.add_argument("--yes", action="store_true", help="really import")
    p.add_argument("--json", action="store_true", help="print the report as JSON")
    p.set_defaults(func=_cmd_import)
    p = sub.add_parser(
        "consolidate",
        help="list recurring topics, then rebuild memory/distilled/"
             " from raw (runs distill)")
    p.set_defaults(func=_cmd_consolidate)
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except _NoContext as err:
        print("ERROR: %s" % err, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(memory_main())
