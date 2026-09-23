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

Raw files are named by local date (YYYY-MM-DD.jsonl); the window reads
every dated file from the day before the window's start onward, and
any file not named by a date, and decides by each entry's UTC
timestamp, so a file boundary never moves an entry in or out of it."""
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from cousin_lib import compact, memory, transcript_mine

WINDOW_CAP = 24                          # R8: new raw entries per rolling window
WINDOW_S = 86400
DEDUPE_DAYS = compact.DEFAULT_HOT_DAYS   # older raw files are archived, unread here
SOURCE = "turn-extract"
_CURSOR = ("data", "extract-cursor.json")


def _cursor_path(home):
    return Path(home).joinpath(*_CURSOR)


def _load(home):
    try:
        state = json.loads(_cursor_path(home).read_text())
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def _save(home, state):
    path = _cursor_path(home)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, sort_keys=True))
    tmp.replace(path)


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


def mine_turn(home, session_id, turn_no, *, store, now=None):
    """Mine the store's entries of `session_id` after its cursor into raw
    memory; write at most what the rolling window has left; advance the
    cursor even when nothing is written. Returns the number written, or
    -1 on any error: extraction never fails a turn."""
    try:
        home = Path(home)
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
