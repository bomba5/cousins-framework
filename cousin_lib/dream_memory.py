"""The memory side of a dreaming pass.

`dreaming.py` runs the pass: it takes a bounded slice of this cousin's
memory, gives it to a short model session with no built-in tools, and
hands that session one MCP server whose tools are the four operations
below. This module owns everything those tools may do to memory, plus
the ledger that says how far the dreaming got.

Two rules shape all of it, and both are refusals in code rather than
sentences in a prompt: a pass may only touch the claims its slice
showed it, named by id, and it may never retire what the operator, the
framework or a tool said. The prompt asks for the rest.

A third rule is looser on purpose. What a pass writes may name the
claims it was built from (`derived_from`, one hop, written through
`memory.remember_entry` so there is one storage format), and a source
may be at any truth level - a pass concluding something out of what the
operator said is the most legitimate thing it can do. What is never
inherited along the hop: the claim keeps the level the pass wrote it at.

    slice_for(home, *, chars) -> Slice      # .text .through .empty
                                           # .coverage .tensions
    prompt(slice) -> str
    OPERATIONS -> [{"name", "description", "inputSchema", "fn"}]
        fn(home, pass_id, args) -> change record, or ValueError (the
        harness tells a ValueError to the model as a tool error)
    begin(home, pass_id) / commit(home, pass_id, through) /
    abandon(home, pass_id, reason)
    undo(home, pass_id, changes) -> [change record]
    journal(home, pass_id) -> [change record]

The cursor is `{"month": <raw file stem>, "lines": <lines of it taken>}`
and refuses to be anything else. It cannot be a timestamp: raw entries
are stamped when they are written, so a backfilled decision carries a
stamp older than entries written after it and an out-of-order stamp is
normal; a timestamp cursor would re-dream out-of-order forever. It
cannot be an entry id: `memory.entry_id` is a sha1 over the entry's
text, which carries no order at all. A line number carries order and a
file name carries position in the store.

The ledger is `memory/.dream-ledger.json` (memory/, not data/: a
transplant copies memory/, and a transplanted cousin must not re-dream
the memory it just inherited). It holds the committed cursor, the slice
handed out but not yet adopted, and the open attempt: the pass id, when
it started, the cursor it was given and the ids it was shown. That
attempt is the token every operation checks, so a tool call from a pass
that never began, or one that already committed, writes nothing.

Each change is appended to `data/dreams/journal/<pass_id>.jsonl` with
an fsync, after the write it describes actually landed (a write the
store refused is never reported as a write). The harness's pass log
(`data/dreams/YYYY-MM-DD.jsonl`) carries a `changes` list only when a
pass ended cleanly; the journal is what survives a pass that died, so
`journal(home, pass_id)` is how a lost or errored pass's changes are
read.

Every file written here goes through `perimeter.assert_writable` and the
home's memory write lock, so a pass writes `memory/` and `data/dreams/`
and nothing else, the way every other framework writer does.
"""
import gzip
import json
import os
import re
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from cousin_lib import memory, memory_lock, perimeter

# memory/.dream-ledger.json
LEDGER_PARTS = ("memory", ".dream-ledger.json")
# data/dreams/journal/<pass_id>.jsonl, in a subdirectory because
# dreaming.passes() globs data/dreams/*.jsonl and would merge journal
# lines into its pass records.
JOURNAL_PARTS = ("data", "dreams", "journal")
SCHEMA_VERSION = 1
HISTORY_KEEP = 20
# one claim's rendered line is capped, so one enormous claim cannot eat
# a whole slice
ENTRY_CHARS = 800
# the levels a pass may retire. L5 is a mark, not a claim, and L0-L2 is
# what the operator, the framework and the tools said: a background pass
# has no standing to retire any of it, whatever the model asks for.
RETIRABLE = ("L3_COUSIN_CONCLUSION", "L4_COUSIN_HYPOTHESIS")
PASS_LEVELS = ("conclusion", "hypothesis")
_STEM = re.compile(r"^\d{4}-\d{2}(-\d{2})?$")
_PASS_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,80}$")


# ---- the cursor --------------------------------------------------------------

def _key(month):
    """Sort key of a cursor's file name: the stem itself, which is
    chronological. A monthly archive (`YYYY-MM`) holds the days raw_fold
    folded out of that month and sorts before the month's remaining day
    files (`YYYY-MM-DD`), and after every earlier month's. `_STEM` has
    already refused anything else. Hot days first would strand the cursor:
    once a walk ended in an archive, every new day file sorted behind it."""
    return month


def cursor(value, *, what="through"):
    """A validated `{"month", "lines"}`, or ValueError. The refusal is
    the mechanism: a timestamp, an entry id or a bare count is not a
    position in a file, and accepting one would re-dream silently."""
    if not isinstance(value, dict):
        raise ValueError("%s must be {'month': <raw file stem>, 'lines':"
                         " <lines of it taken>}" % what)
    extra = sorted(set(value) - {"month", "lines"})
    if extra:
        # a cursor carrying anything else is a cursor somebody else
        # designed; refuse it rather than read the two keys and hope
        raise ValueError("%s has keys this module does not write (%s): a"
                         " cursor is exactly month and lines"
                         % (what, ", ".join(extra)))
    month, lines = value.get("month"), value.get("lines")
    if not isinstance(month, str) or not _STEM.match(month):
        raise ValueError("%s.month must be a raw file name: YYYY-MM-DD for a"
                         " day file or YYYY-MM for a monthly archive (%r). A"
                         " timestamp or an entry id is not a position in"
                         " memory" % (what, month))
    if isinstance(lines, bool) or not isinstance(lines, int) or lines < 0:
        raise ValueError("%s.lines must be the number of lines taken from"
                         " that file (%r)" % (what, lines))
    return {"month": month, "lines": lines}


def _stem(path):
    return path.name.split(".")[0]


def _rel(home, path):
    return os.path.relpath(str(path), str(home)).replace(os.sep, "/")


def _files(home):
    """The raw files a pass reads, oldest first (`_key`), minus the
    digests: a digest summarises claims that already live in an archive,
    is not a claim, and carries no id a claim could be named by.
    [(stem, home-relative path, path)]"""
    from cousin_lib import memory_search
    out = []
    for path in memory_search._raw_files(Path(home)):
        if path.name.endswith("-digest.jsonl"):
            continue
        out.append((_stem(path), _rel(home, path), path))
    return sorted(out, key=lambda f: _key(f[0]))


def _read_lines(path):
    opener = gzip.open if path.suffix == ".gz" else open
    try:
        with opener(path, "rt", errors="replace") as fh:
            return fh.read().splitlines()
    except (OSError, EOFError, zlib.error) as err:
        raise ValueError("cannot read %s: %s: %s" % (path.name,
                                                     type(err).__name__, err))


def _index_of(files, month):
    """Where in `files` a cursor's stem points: the file itself; else,
    for a day raw_fold folded away, the start of its month's archive (it
    sorts before the day, and where the day's lines sit inside it is not
    known: re-dreaming the month costs tokens, skipping it would lose the
    day's lines past the cursor); else the first file past it; else
    len(files): the cursor is past the end of the store."""
    for i, (stem, _rel, _path) in enumerate(files):
        if stem == month:
            return i
    for i, (stem, _rel, _path) in enumerate(files):
        if len(month) == 10 and stem == month[:7]:
            return i
    wanted = _key(month)
    for i, (stem, _rel, _path) in enumerate(files):
        if _key(stem) > wanted:
            return i
    return len(files)


def _resume(files, through):
    """(file index, first line to take) for a cursor: 0, 0 when nothing
    has been dreamed yet."""
    if not through:
        return 0, 0
    index = _index_of(files, through["month"])
    if index >= len(files) or files[index][0] != through["month"]:
        return index, 0            # the file it named is folded away
    return index, through["lines"]


# ---- the ledger --------------------------------------------------------------

def ledger_path(home):
    return Path(home).joinpath(*LEDGER_PARTS)


def _load(home):
    try:
        data = json.loads(ledger_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"schema_version": SCHEMA_VERSION}
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        # no migration path yet: a ledger this module cannot read is
        # treated as empty, which re-dreams from the start rather than
        # guessing at a shape from another version
        return {"schema_version": SCHEMA_VERSION}
    return data


def _save(home, data):
    path = perimeter.assert_writable(ledger_path(home),
                                     writer="dream_memory._save")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)
    return data


def _history(led, record):
    led["history"] = (led.get("history") or [])[-HISTORY_KEEP:] + [record]


def committed(home):
    """The committed cursor, or None: nothing has been dreamed yet."""
    led = _load(home)
    if not led.get("through"):
        return None
    try:
        return cursor(led["through"])
    except ValueError:
        return None


# ---- the journal -------------------------------------------------------------

def _pass_id(pass_id):
    text = str(pass_id or "").strip()
    if not _PASS_ID.match(text):
        raise ValueError("not a pass id (%r): letters, digits, dot, dash"
                         " or underscore" % pass_id)
    return text


def journal_path(home, pass_id):
    return Path(home).joinpath(*JOURNAL_PARTS, ("%s.jsonl" % _pass_id(pass_id)))


def journal(home, pass_id):
    """The change records a pass wrote, oldest first: what to read for a
    pass whose end record never landed. A missing journal is an empty one,
    not an error (a pass that changed nothing wrote none)."""
    try:
        lines = journal_path(home, pass_id).read_text(encoding="utf-8").splitlines()
    except (OSError, ValueError):
        return []
    out = []
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            out.append(record)
    return out


def _note(home, pass_id, record):
    """One journal line, fsynced before the tool call returns: the
    changes of a pass that dies are only on the record if they were
    durable first."""
    path = perimeter.assert_writable(journal_path(home, pass_id),
                                     writer="dream_memory._note")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


# ---- the attempt token -------------------------------------------------------

def begin(home, pass_id):
    """Take the attempt token: adopt the slice waiting in the ledger as
    this pass's bound and stamp the start. Every operation refuses
    without one. ValueError when no slice was taken (the pass has nothing
    it is allowed to touch) or when another pass already holds the token
    (two passes would fight over one pending slice)."""
    pass_id = _pass_id(pass_id)
    with memory_lock.write_lock(home):
        led = _load(home)
        held = led.get("attempt") or {}
        if held.get("pass_id"):
            raise ValueError("pass %s has been dreaming since %.0f; one pass"
                             " at a time" % (held["pass_id"],
                                             held.get("started") or 0.0))
        pending = led.get("pending") or {}
        if not pending.get("through"):
            raise ValueError("begin: no slice was taken for this pass; call"
                             " slice_for first")
        attempt = {"pass_id": pass_id, "started": time.time(),
                   "through": cursor(pending["through"]),
                   "seen": list(pending.get("seen") or [])}
        led["attempt"] = attempt
        led["pending"] = None
        _save(home, led)
    return {"pass_id": attempt["pass_id"], "started": attempt["started"],
            "through": attempt["through"], "shown": len(attempt["seen"])}


def commit(home, pass_id, through):
    """Close the attempt and move the committed cursor to `through`. The
    cursor never moves backwards: a commit behind the ledger would
    re-dream what is already dreamed, and every pass is handed `through`
    by the slice it was given."""
    pass_id = _pass_id(pass_id)
    through = cursor(through)
    with memory_lock.write_lock(home):
        led = _load(home)
        attempt = led.get("attempt") or {}
        if attempt.get("pass_id") != pass_id:
            raise ValueError("commit: pass %s has no open attempt" % pass_id)
        old = led.get("through")
        if old and _behind(home, through, old):
            raise ValueError("commit: through (%s, %d lines) is behind the"
                             " ledger (%s, %d lines)"
                             % (through["month"], through["lines"],
                                old.get("month"), old.get("lines") or 0))
        led["through"] = through
        led["attempt"] = None
        _history(led, {"pass_id": pass_id, "started": attempt.get("started"),
                       "ended": time.time(), "result": "committed",
                       "through": through})
        _save(home, led)
    return {"through": through, "shown": len(attempt.get("seen") or [])}


def _behind(home, new, old):
    """True when `new` is behind `old` in the store's own file order."""
    try:
        old = cursor(old)
    except ValueError:
        return False
    files = _files(home)
    a, b = _index_of(files, new["month"]), _index_of(files, old["month"])
    return (a, new["lines"]) < (b, old["lines"]) if a == b else a < b


def abandon(home, pass_id, reason):
    """Give the attempt back without moving the cursor: nothing was
    committed, so the next pass takes the same slice. Tolerates a pass
    that never held one (the harness abandons after a failure that
    happened before begin)."""
    pass_id = _pass_id(pass_id)
    reason = " ".join(str(reason or "").split())[:400]
    with memory_lock.write_lock(home):
        led = _load(home)
        attempt = led.get("attempt") or {}
        if attempt.get("pass_id") != pass_id:
            if attempt:
                # somebody else's attempt is open: this pass never had
                # one and must not clear that one
                return {"abandoned": False, "reason": reason}
            attempt = {}
        led["attempt"] = None
        _history(led, {"pass_id": pass_id, "started": attempt.get("started"),
                       "ended": time.time(), "result": "abandoned",
                       "reason": reason})
        _save(home, led)
    return {"abandoned": True, "reason": reason}


def release_stale(home, max_age):
    """Abandon an attempt held longer than `max_age` seconds: its pass
    died without abandoning (killed at the harness's timeout, a loops
    restart, a reboot), and every later slice_for and begin would refuse
    behind it. What it changed stays, on its journal. None when nothing
    was held, or what was held is not stale yet; else the released
    attempt's pass id."""
    with memory_lock.write_lock(home):
        led = _load(home)
        attempt = led.get("attempt") or {}
        if not attempt.get("pass_id"):
            return None
        if time.time() - float(attempt.get("started") or 0.0) <= max_age:
            return None
        led["attempt"] = None
        _history(led, {"pass_id": attempt["pass_id"], "started": attempt.get("started"),
                       "ended": time.time(), "result": "abandoned",
                       "reason": "lost: held past %ds with no end" % max_age})
        _save(home, led)
    return attempt["pass_id"]


def _attempt(home, pass_id):
    """The open attempt for this pass, or ValueError. The first thing
    every operation asks: a call from a dead pass, or from an operator
    running an operation by hand, writes nothing."""
    led = _load(home)
    attempt = led.get("attempt") or {}
    if attempt.get("pass_id") != _pass_id(pass_id):
        raise ValueError("pass %s has no open attempt: this call is from a"
                         " pass that never began, or one that already"
                         " committed or was abandoned" % pass_id)
    return attempt


# ---- the slice ---------------------------------------------------------------

@dataclass
class Slice:
    """What a pass is shown: `text` to read, `through` the cursor it got
    as far as, `empty` when there was nothing new, `coverage` what it
    covered and what it left out, `tensions` the open disagreements among
    the topics in it (memory.tensions is the detector the framework has
    and nothing calls)."""
    text: str = ""
    through: dict = None
    empty: bool = True
    coverage: dict = field(default_factory=dict)
    tensions: list = field(default_factory=list)


def _parse(raw):
    """A raw entry, or None for a line that is not one: not JSON, not a
    dict, no topic, or a digest (a summary of claims already archived).
    A line that cannot be read is consumed, never shown, and counted as
    unread in the coverage."""
    try:
        entry = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(entry, dict) or not str(entry.get("topic") or "").strip():
        return None
    if str(entry.get("source") or "") == "digest":
        return None
    return entry


def _render(entry, entry_id):
    """One claim as one line: when, its truth level, its id (the
    operations are named by id, so the id has to be on the page), its
    topic and its content, whitespace-collapsed."""
    when = str(entry.get("timestamp") or "?")[:16]
    level = memory.normalize_level(entry.get("truth_level"))
    content = " ".join(str(entry.get("content") or "").split())
    if len(content) > ENTRY_CHARS:
        content = content[:ENTRY_CHARS] + "... (truncated)"
    return "[%s %s] (%s) %s: %s" % (when, level, entry_id,
                                    str(entry.get("topic") or "").strip(), content)


def slice_for(home, *, chars=40000):
    """The next bounded slice of raw memory: the lines no pass has taken
    yet, oldest first, until `chars` of rendered claims are in hand. The
    slice is recorded in the ledger as pending; `begin` freezes it into
    the attempt that bounds the pass. ValueError when a pass is already
    open, or when the budget is not positive.

    A claim longer than the whole budget is taken alone and the coverage
    says `over-budget`: making no progress would leave the cursor stuck,
    and showing half a claim would invite a change to it.
    """
    from cousin_lib import distill
    home = Path(home)
    chars = int(chars or 0)
    if chars < 1:
        raise ValueError("slice_for needs a positive character budget")
    memory.ensure_layout(home)
    with memory_lock.write_lock(home):
        led = _load(home)
        held = led.get("attempt") or {}
        if held.get("pass_id"):
            raise ValueError("pass %s has been dreaming since %.0f; one pass"
                             " at a time" % (held["pass_id"],
                                             held.get("started") or 0.0))
        files = _files(home)
        through = committed(home)
        index, first = _resume(files, through)
        rendered, ids, topics = [], set(), set()
        stems, last, machine, unread = [], None, 0, 0
        used, shown, cut, consumed = 0, 0, "end", 0
        stop = False
        for pos in range(index, len(files)):
            stem, _rel, path = files[pos]
            lines = _read_lines(path)
            if stem not in stems:
                stems.append(stem)
            for number in range(first if pos == index else 0, len(lines)):
                raw = lines[number]
                if not raw.strip():
                    continue
                # every non-blank line from here on is consumed, whether
                # or not it is shown, so the cursor advances past a claim
                # the pass may not touch
                entry = _parse(raw)
                consumed += 1
                if entry is None:
                    last, unread = (stem, number + 1), unread + 1
                    continue
                if distill.is_machine_topic(entry.get("topic")):
                    last, machine = (stem, number + 1), machine + 1
                    continue
                eid = memory.entry_id(entry)
                line = _render(entry, eid)
                if shown and used + len(line) + 1 > chars:
                    cut, stop = "budget", True
                    break            # this line is not consumed: it is the
                    last = last      # next slice's first line
                if shown == 0 and used + len(line) + 1 > chars:
                    cut = "over-budget"
                last = (stem, number + 1)
                rendered.append(line)
                used += len(line) + 1
                shown += 1
                ids.add(eid)
                topics.add(str(entry.get("topic") or "").strip())
            if stop:
                break
        final = {"month": last[0], "lines": last[1]} if last else through
        pending = {"taken": time.time(), "through": final, "seen": sorted(ids),
                   "chars": used, "entries": shown, "cut": cut,
                   "coverage": {"files": stems, "entries": shown,
                                "lines": consumed,
                                "chars": used, "budget": chars, "cut": cut,
                                "from": through, "to": final,
                                "topics": len(topics), "machine": machine,
                                "unread": unread}}
        _save(home, dict(led, pending=pending))
    piece = Slice(text="\n".join(rendered), through=final, empty=not shown,
                  coverage=pending["coverage"])
    if shown:
        piece.tensions = [t for t in memory.tensions(home)
                          if str(t.get("topic") or "") in topics]
    return piece


# ---- what an operation may do ------------------------------------------------

def _why(args, what="why"):
    why = " ".join(str((args or {}).get(what) or "").split())
    if not why:
        raise ValueError("%s is required and non-empty: a change to memory"
                         " has to say what superseded what, in a sentence"
                         " worth reading later" % what)
    return why


def _ids(value, field="entry_ids"):
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not value:
        raise ValueError("%s must be a list of claim ids (non-empty)" % field)
    out = [str(v or "").strip() for v in value]
    if not all(out):
        raise ValueError("%s must hold claim ids, not blanks" % field)
    return out


def _claims(home, attempt, ids, *, for_derivation=False):
    """The live claims for `ids`, or the first refusal as ValueError.
    One `validity()` scan per call: it is a full pass over the history,
    and MAX_TURNS bounds how often a pass pays for it.

    `for_derivation=True` relaxes the truth-level rule only. A pass
    never retires what the operator, the framework or a tool stated,
    but deriving a conclusion *from* such a claim is the most legitimate
    thing it can do: L0 material is the best material to reason from.
    The claim must still be in the slice, live, and stamped before the
    pass began.
    """
    rows = {r["id"]: r for r in memory.validity(home)}
    seen = set(attempt.get("seen") or ())
    started = float(attempt.get("started") or 0.0)
    out = []
    for eid in ids:
        row = rows.get(eid)
        if row is None:
            raise ValueError("no claim %r in this cousin's memory" % eid)
        if not for_derivation:
            level = memory.normalize_level(row.get("truth_level"))
            if level not in RETIRABLE:
                raise ValueError("claim %s is %s and a pass never retires it:"
                                 " what the operator, the framework or a tool"
                                 " stated (L0-L2) is not a pass's to"
                                 " consolidate" % (eid, level))
        if row.get("valid_to"):
            raise ValueError("claim %s is already retired, by %s"
                             % (eid, row.get("retired_by") or "an earlier mark"))
        if eid not in seen:
            raise ValueError("claim %s was not in this pass's slice: a pass"
                             " may only act on what it was shown, by id"
                             % eid)
        when = memory.entry_timestamp(row) or 0.0
        if when > started:
            raise ValueError("claim %s is stamped %.0f, after this pass"
                             " started at %.0f: a pass never touches what"
                             " it did not see" % (eid, when, started))
        out.append(row)
    return out


def _same_topic(rows, topic):
    """Every named claim has to be on the topic the model named: one
    operation works on one topic, and a model that mixes two is mixing
    two histories."""
    for row in rows:
        if str(row.get("topic") or "").strip() != topic:
            raise ValueError("claim %s is on topic %r, not %r: one operation"
                             " works on one topic" % (row["id"],
                                                      str(row.get("topic") or "").strip(),
                                                      topic))


def _live_topic(home, topic):
    """The live claims on `topic`."""
    return [r for r in memory.validity(home)
            if str(r.get("topic") or "").strip() == topic and not r.get("valid_to")]


def _level(args, what="level"):
    """A pass writes at L3 (a conclusion) or L4 (a hypothesis). It never
    writes as the operator, the framework or a tool: `source` says a pass
    wrote it, so a claim that reads as the operator's word would be a
    lie with a provenance line on it."""
    given = str((args or {}).get(what) or PASS_LEVELS[0]).strip() or PASS_LEVELS[0]
    level = memory.normalize_level(given)
    if level not in RETIRABLE:
        raise ValueError("a pass writes at L3 (conclusion) or L4"
                         " (hypothesis) only; %r is not a level a background"
                         " pass may write" % given)
    return level


def _topic(args):
    topic = str((args or {}).get("topic") or "").strip()
    if not topic:
        raise ValueError("topic is required: a change to memory is about a"
                         " topic, and a topicless claim can never be found"
                         " again")
    return topic


def _by(pass_id):
    return "dream:%s" % pass_id


def _remember(home, topic, fact, level, cite, derived_from=None):
    """Write one claim at the pass's own level, source `dream`, and
    return the entry as written.

    `memory.remember_entry`, not a hand-built `memory._append_raw`: one
    storage format for every claim, so `why` and the reverse hop read a
    dream-written claim without needing to know a pass wrote it. The
    entry comes back rather than the printed line because the journal
    needs to name its id (the id is a sha1 over the entry's own stamp,
    topic and content, so it cannot be derived from the text asked for).

    `derived_from` is recorded only when the pass named its sources and
    every one of them was in the slice, live, and stamped before the
    pass began. Truth is never inherited along the hop: the claim keeps
    the level the pass wrote it at.
    """
    return memory.remember_entry(home, topic, fact, level=level, cite=cite,
                                 derived_from=derived_from, source="dream")


def _op_retire(home, pass_id, args):
    """Retire one claim of this pass's own: a duplicate, or a claim a
    newer one supersedes. `why` is the sentence that goes on the mark."""
    eid = str((args or {}).get("entry_id") or "").strip()
    if not eid:
        raise ValueError("entry_id is required: name the claim to retire")
    why = _why(args)
    with memory_lock.write_lock(home):
        attempt = _attempt(home, pass_id)
        row = _claims(home, attempt, [eid])[0]
        mark = memory.mark_obsolete(home, str(row.get("topic") or ""), why,
                                    by=_by(pass_id), entry=eid, source="dream")
    record = {"op": "retire", "topic": str(row.get("topic") or "").strip(),
              "entry_ids": [eid], "mark_id": memory.entry_id(mark),
              "marks": [memory.entry_id(mark)], "created": None, "why": why}
    _note(home, pass_id, record)
    return record


def _op_merge(home, pass_id, args):
    """Consolidate a topic's claims into one: keep the claim that already
    says it (`keep`), or write the consolidated claim (`fact`), and retire
    the rest. Exactly one of `keep` and `fact`, so a merge always leaves
    a single live claim: a topic left with two is a tension the pass just
    created."""
    args = args or {}
    topic = _topic(args)
    retire = _ids(args.get("retire"), "retire")
    keep = str(args.get("keep") or "").strip()
    fact = " ".join(str(args.get("fact") or "").split())
    why = _why(args)
    if bool(keep) == bool(fact):
        raise ValueError("merge needs exactly one of keep (the claim that"
                         " already says it) or fact (the consolidated"
                         " claim), not both and not neither")
    overlap = sorted(set(retire) & ({keep} if keep else set()))
    if overlap:
        raise ValueError("keep and retire must be disjoint: %s is in both"
                         % ", ".join(overlap))
    level = _level(args)
    cite = args.get("cite")
    with memory_lock.write_lock(home):
        attempt = _attempt(home, pass_id)
        rows = _claims(home, attempt, retire + ([keep] if keep else []))
        _same_topic(rows, topic)
        created = None
        if fact:
            # the consolidated claim is built from the claims it retires,
            # so it names them: `why` then walks from it back to the
            # material. No extra validation is needed here - `retire` has
            # already been checked against the slice and the level rule.
            entry = _remember(home, topic, fact, level, cite,
                              derived_from=retire)
            created = memory.entry_id(entry)
        marks = []
        for eid in retire:
            mark = memory.mark_obsolete(home, topic, why, by=_by(pass_id),
                                        entry=eid, source="dream")
            marks.append(memory.entry_id(mark))
    record = {"op": "merge", "topic": topic, "entry_ids": retire,
              "kept": [keep] if keep else [], "created": created,
              "derived_from": list(retire) if created else [],
              "mark_id": marks[0], "marks": marks, "why": why}
    _note(home, pass_id, record)
    return record


def _op_settle(home, pass_id, args):
    """Settle a disagreement: retire the losing claims of a topic whose
    live claims contradict each other. `evidence` must be a quote from
    one of the claims being retired, so a settlement carries what it was
    settled from; the topic keeps at least one live claim, or this is the
    topic-level obsolete mark's job."""
    args = args or {}
    topic = _topic(args)
    retire = _ids(args.get("entry_ids"))
    why = _why(args)
    evidence = " ".join(str(args.get("evidence") or "").split())
    if not evidence:
        raise ValueError("settle needs evidence: a quote from the material"
                         " that shows the claim is superseded. Empty"
                         " evidence settles nothing")
    with memory_lock.write_lock(home):
        attempt = _attempt(home, pass_id)
        rows = _claims(home, attempt, retire)
        _same_topic(rows, topic)
        if not any(evidence in " ".join(str(r.get("content") or "").split())
                   for r in rows):
            raise ValueError("evidence %r is not in the content of any claim"
                             " being settled: evidence is a quote from what"
                             " the slice showed you. A claim longer than %d"
                             " chars is shown truncated, so quote from what"
                             " you were shown" % (evidence[:80], ENTRY_CHARS))
        live = len(_live_topic(home, topic))
        if len(retire) >= live:
            raise ValueError("settling %d of %d live claims would leave the"
                             " topic with none: to retire a whole topic, mark"
                             " it obsolete instead" % (len(retire), live))
        marks = []
        for eid in retire:
            mark = memory.mark_obsolete(home, topic, why, by=_by(pass_id),
                                        entry=eid, source="dream")
            marks.append(memory.entry_id(mark))
    record = {"op": "settle", "topic": topic, "entry_ids": retire,
              "evidence": evidence, "created": None,
              "mark_id": marks[0], "marks": marks, "why": why}
    _note(home, pass_id, record)
    return record


def _op_remember(home, pass_id, args):
    """Write one claim the slice showed you and no claim already says.
    A duplicate is refused rather than appended: raw memory keeps every
    line forever, so a second wording of the same fact is only noise the
    next pass has to read again.

    `derived_from` is optional: name the claims this one was read out of
    and the entry records them, so `why` can walk back from it. Omit it
    and the claim stands alone, which is what a genuinely new observation
    is. Sources are held to the slice, to being live, and to being
    stamped before the pass began - but not to a truth level: a pass may
    derive from what the operator said.
    """
    args = args or {}
    topic = _topic(args)
    fact = " ".join(str(args.get("fact") or "").split())
    if not fact:
        raise ValueError("fact is required and non-empty")
    level = _level(args)
    sources = None
    if args.get("derived_from"):
        sources = _ids(args.get("derived_from"), "derived_from")
    with memory_lock.write_lock(home):
        attempt = _attempt(home, pass_id)
        if sources:
            _claims(home, attempt, sources, for_derivation=True)
        live = _live_topic(home, topic)
        for row in live:
            if " ".join(str(row.get("content") or "").split()) == fact:
                raise ValueError("topic %r already says exactly that"
                                 " (claim %s): nothing to add"
                                 % (topic, row["id"]))
        entry = _remember(home, topic, fact, level, args.get("cite"),
                          derived_from=sources)
        created = memory.entry_id(entry)
    record = {"op": "remember", "topic": topic, "entry_ids": [created],
              "created": created, "mode": "append" if live else "create",
              "derived_from": list(sources or ()),
              "why": "a fact this slice showed and no claim says"}
    _note(home, pass_id, record)
    return record


# ---- undo --------------------------------------------------------------------

def undo(home, pass_id, changes):
    """Reverse one pass's changes. `memory_trash` moves the lines the pass
    wrote into a trash batch, so a reversal is auditable and restorable
    rather than a silent edit: removing an entry-level mark is what
    actually un-retires a claim, because `memory.validity` derives
    validity from the marks and no counter-mark appended later could
    out-rank the first one. The committed cursor is NOT rewound: the
    operator's verdict is that the material was already dreamed, and the
    next pass must not do it again.

    ValueError when the pass's changes are all gone already.
    """
    from cousin_lib import memory_trash
    pass_id = _pass_id(pass_id)
    by_change = []
    for change in changes or ():
        if not isinstance(change, dict):
            continue
        ids = [str(i) for i in (change.get("marks") or ()) if i]
        if change.get("mark_id") and str(change["mark_id"]) not in ids:
            ids.append(str(change["mark_id"]))
        if change.get("created"):
            ids.append(str(change["created"]))
        if ids:
            by_change.append((change, ids))
    if not by_change:
        raise ValueError("pass %s changed nothing to undo" % pass_id)
    found, blocked = _locate(home, [i for _c, ids in by_change for i in ids])
    gone = sorted({i for _c, ids in by_change for i in ids
                   if i not in found and i not in blocked})
    if not found:
        raise ValueError("nothing to undo for pass %s: none of its lines are"
                         " in a trashed file any more (%s)"
                         % (pass_id, ", ".join(gone[:5]) or "no ids"))
    refs = [(rel, line_no, memory_trash.line_sha(text))
            for rel, line_no, text in sorted(found.values())]
    manifest = memory_trash.trash_lines(home, refs, by=_by("undo " + pass_id))
    memory_trash.after_change(home, manifest)
    out = []
    for change, ids in by_change:
        out.append({"op": "undo", "reversed": change.get("op"),
                    "removed": [i for i in ids if i in found],
                    "missing": [i for i in ids if i not in found],
                    "blocked": {i: blocked[i] for i in ids if i in blocked},
                    "trash": manifest["id"]})
    _note(home, pass_id, {"op": "undone", "trash": manifest["id"],
                          "removed": sorted(found), "missing": gone})
    return out


def _locate(home, ids):
    """({id: (home-relative path, line number, line)}, {id: why}) for every
    id in raw. One walk; undo is rare and correctness beats a clever
    index. An id in the gzip archive lands in `blocked`: the forensic tier
    is never rewritten, so that line is not reversible by trashing it."""
    from cousin_lib import distill, memory_trash
    wanted = set(ids)
    found, blocked = {}, {}
    for _stem, rel, path in _files(home):
        if not wanted:
            break
        trashable = memory_trash.can_trash_line(rel)
        for number, raw in enumerate(_read_lines(path), start=1):
            if not raw.strip():
                continue
            entry = _parse(raw)
            if entry is None or distill.is_machine_topic(entry.get("topic")):
                continue
            eid = memory.entry_id(entry)
            if eid in wanted:
                if trashable:
                    found[eid] = (rel, number, raw)
                else:
                    blocked[eid] = ("in %s, the forensic archive: not"
                                    " reversible by trashing" % rel)
                wanted.discard(eid)
    return found, blocked


# ---- the prompt --------------------------------------------------------------

_DOCTRINE = """\
You are dreaming: one short pass over your own memory, alone, with no
tools except the four memory operations below. Nothing here is urgent,
and you see only the slice printed under this text.

A pass exists to consolidate. Fewer claims saying the same thing, and
claims that contradict each other settled with evidence. That is all it
is for.

- Prefer nothing. If this slice needs no change, change nothing and say
  so in one line. A pass that invents structure is a defect, not work.
- Prefer modify over create. `merge` and `retire` are almost always the
  right tools. `remember` is for a fact this slice shows you and no claim
  says yet, never a second wording of something already there.
- Act only on claims shown below, by the ids on this page. The
  operations refuse anything else, and so should you.
- Never retire an L0, L1 or L2 claim. What the operator, the framework
  or a tool stated is not yours to consolidate. Only your own L3 and L4
  conclusions are.
- Name what you built from. A claim you consolidate out of others is
  written with those ids on it, and `remember` takes `derived_from` when
  you read a new claim out of the slice. Deriving from what the operator
  said is allowed and welcome; only retiring it is not. Nothing about
  the source's certainty carries over: your claim is written at the
  level you chose.
- Settle a disagreement with evidence: `evidence` must be a quote from
  the claim being retired. If you cannot quote what shows it superseded,
  leave the disagreement standing and name it in your summary.
- Leave one live claim per topic. A merge that ends with two live claims
  on a topic has created the tension it was sent to settle.
- When two claims disagree and you cannot tell which stands, do nothing
  and report it. An unresolved state is reported, never resolved by
  picking a side.
- A tool error is a refusal, not a hiccup to retry: a write the store
  turned down was not written. Do not get it another way.

Every change is recorded with its ids before your session ends, and the
operator can undo the whole pass. Write `why` as the sentence you would
want to read later: what superseded what, and why."""

_TENSION_HEAD = """\
Open disagreements among the topics in this slice (memory.tensions found
them; settling one means retiring the losing claims with evidence, not
arguing):"""


def prompt(slice):
    """The doctrine and the slice, as one prompt. The tensions come
    first: a pass that only reads what is in front of it never notices a
    contradiction nobody prompted it for."""
    parts = [_DOCTRINE]
    for tension in getattr(slice, "tensions", None) or ():
        claims = tension.get("claims") or []
        lines = ["- %s:" % tension.get("topic")]
        for claim in claims:
            lines.append("  (%s) %s" % (claim.get("id"),
                                       " ".join(str(claim.get("content") or "").split())[:200]))
        parts.append("\n".join(lines))
    if len(parts) > 1:
        parts.insert(1, _TENSION_HEAD)
    parts.append("Your slice:\n\n%s" % (getattr(slice, "text", "") or "(empty)"))
    return "\n\n".join(parts)


# ---- the operations ----------------------------------------------------------

_WHY = {"type": "string", "minLength": 1,
        "description": "the sentence that goes on the mark: what superseded"
                       " what, and why"}

OPERATIONS = [
    {"name": "merge",
     "description": "Consolidate a topic's claims into one live claim: "
                    "give either keep (the claim that already says it) or "
                    "fact (the consolidated claim), and retire the claims "
                    "it supersedes.",
     "inputSchema": {"type": "object",
                     "properties": {
                         "topic": {"type": "string", "minLength": 1},
                         "keep": {"type": "string",
                                  "description": "the claim id that stays "
                                                 "live (omit when giving fact)"},
                         "fact": {"type": "string", "minLength": 1,
                                  "description": "the consolidated claim "
                                                 "(omit when giving keep)"},
                         "retire": {"type": "array", "minItems": 1,
                                    "items": {"type": "string"},
                                    "description": "claim ids to retire"},
                         "level": {"type": "string",
                                   "enum": list(PASS_LEVELS),
                                   "description": "conclusion (L3, default) or"
                                                  " hypothesis (L4)"},
                         "why": _WHY},
                     "required": ["topic", "retire", "why"]},
     "fn": _op_merge},
    {"name": "retire",
     "description": "Retire one claim of your own: a duplicate, or one a "
                    "newer claim supersedes.",
     "inputSchema": {"type": "object",
                     "properties": {
                         "entry_id": {"type": "string", "minLength": 1},
                         "why": _WHY},
                     "required": ["entry_id", "why"]},
     "fn": _op_retire},
    {"name": "settle",
     "description": "Settle a disagreement: retire the losing claims of a "
                    "topic whose live claims contradict each other, with a "
                    "quote from the material as evidence. The topic keeps "
                    "at least one live claim.",
     "inputSchema": {"type": "object",
                     "properties": {
                         "topic": {"type": "string", "minLength": 1},
                         "entry_ids": {"type": "array", "minItems": 1,
                                       "items": {"type": "string"}},
                         "evidence": {"type": "string", "minLength": 1,
                                      "description": "a quote from one of the"
                                                     " claims being settled"},
                         "why": _WHY},
                     "required": ["topic", "entry_ids", "evidence", "why"]},
     "fn": _op_settle},
    {"name": "remember",
     "description": "Write one claim this slice shows you and no claim "
                    "already says. A duplicate is refused.",
     "inputSchema": {"type": "object",
                     "properties": {
                         "topic": {"type": "string", "minLength": 1},
                         "fact": {"type": "string", "minLength": 1},
                          "level": {"type": "string",
                                    "enum": list(PASS_LEVELS)},
                          "cite": {"type": "string"},
                          "derived_from": {"type": "array", "minItems": 1,
                                           "items": {"type": "string"},
                                           "description": "the claim ids this one"
                                                          " was read out of,"
                                                          " so `why` can walk back"
                                                          " from it; any truth"
                                                          " level, including what"
                                                          " the operator said"}},
                      "required": ["topic", "fact"]},
     "fn": _op_remember},
]
