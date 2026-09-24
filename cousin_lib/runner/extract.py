"""Continuous extraction (spec, "Continuous extraction and rollover").

After every completed turn the runner mines the transcript entries the
session store received since the last mine, with the flip miner's own
rules (transcript_mine): no model call, a keyword test per sentence,
hedged sentences at L4. Phase 0 finding 5: cost is nothing, VOLUME is
the constraint, so two rules bound it:
- dedupe: a sentence raw memory already holds (either miner, the hot
  raw files: DEDUPE_DAYS = compaction's hot window, since older files
  are folded into archives this does not read) is not written again;
- a rolling window per cousin: at most WINDOW_CAP new entries in any
  WINDOW_S, however often the cousin rolls over (R8).
A cursor per session makes a re-run a no-op; the dedupe makes it one
even when the cursor is lost.

Mining writes the turn's sentences as `episode:` entries, the log
(distill ranks them last). What the agent CLI's own memory had and this
lacks is the model being ASKED (spec, "One memory system"), so
propose_turn decides whether to ask: a turn whose text reached a
conclusion (the miner's own L3 rule) AND carries a decision word
(PROPOSAL_WORDS; R11, the operator's rule) and which recorded none
through the memory tool gets one proposal, queued by the runner as a
`propose` row, the lowest priority. Never about a proposal's own turn,
once per turn (a cursor of its own), at most PROPOSAL_CAP per rolling
WINDOW_S.

Raw files are named by local date (YYYY-MM-DD.jsonl); the window reads
every dated file from the day before the window's start onward, and
any file not named by a date, and decides by each entry's UTC
timestamp, so a file boundary never moves an entry in or out of it."""
import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from cousin_lib import compact, memory, memory_lock, transcript_mine

WINDOW_CAP = 24                          # R8: new raw entries per rolling window
WINDOW_S = 86400
DEDUPE_DAYS = compact.DEFAULT_HOT_DAYS   # older raw files are archived, unread here
SOURCE = "turn-extract"
_CURSOR = ("data", "extract-cursor.json")
PROPOSAL_SOURCE = "propose"             # delivery.SOURCES; base.SOURCE_PRIORITY: the lowest
PROPOSAL_MARK = "[memory proposal]"
PROPOSAL_CAP = 6                        # R11 (OPERATOR): proposals per cousin per rolling WINDOW_S
PROPOSAL_SENTENCES = 3
MEMORY_TOOL = "mcp__cousin__memory"     # the runner's tool server is registered as "cousin"
# R11 (OPERATOR): what makes a turn worth a proposal. The miner's own L3
# rule alone qualifies 59% of turns (157 of 265 real turns, measured over
# 12 transcripts), which spends the day's cap in the first hour; an L3
# sentence that also carries a decision word qualifies 2% (6 of 265).
PROPOSAL_WORDS = re.compile(r"\b(decided|decide to|decision|agreed|we chose|chose to|going with"
                            r"|from now on|the rule is|root cause|the fix is)\b", re.I)
_PROPOSE_CURSOR = ("data", "propose-cursor.json")
_PROPOSALS = ("data", "proposals.json")


def _cursor_path(home, parts=_CURSOR):
    return Path(home).joinpath(*parts)


def _load(home, parts=_CURSOR):
    try:
        state = json.loads(_cursor_path(home, parts).read_text())
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def _save(home, state, parts=_CURSOR):
    path = _cursor_path(home, parts)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, sort_keys=True))
    tmp.replace(path)


def set_cursor(home, session_id, cursor):
    """Move one session's mining cursor (R17: a kind switch keeps the
    session id and moves the cursor to the end, in the TARGET kind's unit:
    a byte offset in the CLI's transcript for tmux, a store row id for the
    SDK), so nothing is mined twice across the switch."""
    with memory_lock.write_lock(Path(home)):
        state = _load(home)
        state[session_id] = int(cursor)
        _save(home, state)


def _raw_entries(home, since_day):
    raw = Path(home) / "memory" / "raw"
    for path in sorted(raw.glob("*.jsonl")) if raw.is_dir() else []:
        if path.stem.startswith("20") and path.stem < since_day:
            continue        # a dated file older than the window cannot hold an entry in it
        for line in path.read_text().splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict):
                yield entry


def _when(entry):
    try:
        when = datetime.fromisoformat(str(entry.get("timestamp", "")).replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def written_in_window(home, now=None):
    """Raw entries this module wrote in the last WINDOW_S."""
    now = now or datetime.now(timezone.utc)
    start = now - timedelta(seconds=WINDOW_S)
    count = 0
    for entry in _raw_entries(home, (start.date() - timedelta(days=1)).isoformat()):
        when = _when(entry)
        if entry.get("source") == SOURCE and when is not None and when >= start:
            count += 1
    return count


def _recent(home):
    """Normalised sentences either miner wrote in the last DEDUPE_DAYS."""
    oldest = (date.today() - timedelta(days=DEDUPE_DAYS)).isoformat()
    return {transcript_mine.normalize(e.get("content", ""))
            for e in _raw_entries(home, oldest)
            if e.get("source") in (SOURCE, transcript_mine.SOURCE)}


def _recorded_memory(entries):
    """True when the turn called the memory tool's decide or remember."""
    for record in entries:
        if not isinstance(record, dict) or record.get("type") != "assistant":
            continue
        for block in (record.get("message") or {}).get("content") or []:
            if (isinstance(block, dict) and block.get("type") == "tool_use"
                    and block.get("name") == MEMORY_TOOL
                    and (block.get("input") or {}).get("command") in ("decide", "remember")):
                return True
    return False


def proposal_text(sentences):
    """The row's body: the sentences, and the ask."""
    lines = [PROPOSAL_MARK + " Your last turn reached conclusions that are not in memory yet:"]
    lines += ["- %s" % s for s in sentences]
    lines.append("If one is worth keeping, record it now with the memory tool (remember, or"
                 " decide with its reasoning) under a topic you would search for later. If"
                 " none is, end the turn without a reply.")
    return "\n".join(lines)


def propose_turn(home, session_id, *, store, turn_bodies=(), now=None):
    """The body of a memory proposal for the turn just finished, or None:
    None for a proposal's own turn, a turn with no conclusion-level
    sentence, a turn that already called memory decide or remember, and
    past PROPOSAL_CAP in the rolling window. Reads the store's entries
    after its own cursor and advances it on every path, a proposal's own
    turn included, so no turn is read twice and a re-run proposes once.
    Raises on a broken store or corrupt state (a bad cursor, a
    proposals.json whose shape is not the one this module writes): None
    already means "nothing to propose", so folding an error into it
    would switch proposals off without a trace. The caller (SdkRunner
    ._propose) is the one that must never fail a turn; it catches this
    and records the error on the `propose` event."""
    home = Path(home)
    # the cursor and the window's file are read-modify-write: one writer at
    # a time per home (memory_lock), whichever session asks
    with memory_lock.write_lock(home):
        cursors = _load(home, _PROPOSE_CURSOR)
        entries, cursor = store.entries_after(session_id, int(cursors.get(session_id, 0)))
        cursors[session_id] = cursor
        _save(home, cursors, _PROPOSE_CURSOR)
        if any(str(b).startswith(PROPOSAL_MARK) for b in turn_bodies):
            return None                  # a proposal's own turn: consumed, never proposed about
        if not entries or _recorded_memory(entries):
            return None
        texts = list(transcript_mine.texts_from_entries(entries))
        picked = [s for s in transcript_mine.candidates(texts, max_entries=10 ** 6)
                  if transcript_mine.level_for(s) == transcript_mine.TRUTH_LEVEL
                  and PROPOSAL_WORDS.search(s)]
        if not picked:
            return None
        now = now or datetime.now(timezone.utc)
        start = now - timedelta(seconds=WINDOW_S)
        sent = [s for s in _load(home, _PROPOSALS).get("sent", [])
                if (_when({"timestamp": s}) or start) > start]
        if len(sent) >= PROPOSAL_CAP:
            return None
        _save(home, {"sent": sent + [now.isoformat()]}, _PROPOSALS)
        return proposal_text(picked[:PROPOSAL_SENTENCES])


def mine_turn(home, session_id, turn_no, *, store, now=None):
    """Mine the store's entries of `session_id` after its cursor into raw
    memory; write at most what the rolling window has left; advance the
    cursor even when nothing is written. Returns the number written, or
    -1 on any error: extraction never fails a turn."""
    try:
        home = Path(home)
        # one critical section per home (memory_lock): the cursor file holds
        # every session's position and the window's cap is the cousin's, so a
        # second session mining at the same moment must wait, not overwrite
        with memory_lock.write_lock(home):
            state = _load(home)
            entries, cursor = store.entries_after(session_id, int(state.get(session_id, 0)))
            left = max(0, WINDOW_CAP - written_in_window(home, now))
            kept = []
            if entries and left:
                seen = _recent(home)
                texts = list(transcript_mine.texts_from_entries(entries))
                for sentence in transcript_mine.candidates(texts, max_entries=10 ** 6):
                    key = transcript_mine.normalize(sentence)
                    if key in seen:
                        continue
                    seen.add(key)
                    kept.append(sentence)
                    if len(kept) >= left:
                        break
            topic = "episode:%s" % session_id[:8]
            for sentence in kept:
                level = transcript_mine.level_for(sentence)
                memory._append_raw(home, {
                    "topic": topic if level == transcript_mine.TRUTH_LEVEL else topic + ":hypothesis",
                    "content": sentence, "truth_level": level, "source": SOURCE, "turn": turn_no})
            state[session_id] = cursor
            _save(home, state)
            return len(kept)
    except Exception:  # noqa: BLE001 - extraction never fails a turn
        return -1
