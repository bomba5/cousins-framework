"""Usage-weighted recall: a cousin's memory strengthens by use.

Every search records which files it surfaced, as one event line in
<home>/memory/.recall-log.jsonl and a bump in the compact per-file
counts of <home>/memory/.recall-counts.json. Later searches lift a
file's fused score by (1 + bonus), where the bonus is

    MAX_BONUS * count / (count + 5) * 0.5 ** (days since last / 14)

Asymptotic in the count, so a much-used memory rises but can never
drown actual relevance (the cap is a 15% nudge on a score, not a new
rank). Halved every 14 days of disuse, so what was hot last quarter
stops riding on it, and one fresh recall restores it - use is what
counts, not history.

Files are keyed by absolute path, the same string a hit carries in
"path". A corrupt counts file resets to empty instead of raising:
losing the counts costs a little ranking memory, breaking every
search costs the whole memory. Recording never raises either; the
search that called it has already done its job.
"""
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from cousin_lib import memory_lock, perimeter

MAX_BONUS = 0.15
HALF_LIFE_DAYS = 14.0
LOG_ROTATE_BYTES = 1_000_000
_SATURATION = 5.0
_QUERY_CHARS = 120
_DAY = 86400.0


def _log_path(home):
    return Path(home) / "memory" / ".recall-log.jsonl"


def _counts_path(home):
    return Path(home) / "memory" / ".recall-counts.json"


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(
        timespec="seconds")


def _epoch(stamp):
    """The epoch seconds of a stored ISO timestamp, or None when the
    value is not one (a corrupt slot decays to nothing)."""
    try:
        parsed = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _boost(count):
    """Bounded and asymptotic: 0 recalls is 0.0; grows toward
    MAX_BONUS and never reaches it."""
    if count <= 0:
        return 0.0
    return MAX_BONUS * count / (count + _SATURATION)


def load_counts(home):
    """{absolute path: {"count": int, "last": iso timestamp}}. A
    missing, unreadable or malformed file is an empty mapping; a
    malformed slot is dropped. The reset is what the next record()
    writes back."""
    try:
        data = json.loads(_counts_path(home).read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    counts = {}
    for key, slot in data.items():
        if (isinstance(slot, dict)
                and isinstance(slot.get("count"), int)
                and slot["count"] > 0):
            counts[str(key)] = {"count": slot["count"],
                                "last": slot.get("last", "")}
    return counts


def bonus(home, path, *, now=None):
    """The score multiplier's excess for one file, in [0, MAX_BONUS]:
    zero for a file never recalled, growing with recalls, halving
    every HALF_LIFE_DAYS since the last one."""
    slot = load_counts(home).get(str(path))
    if not slot:
        return 0.0
    base = _boost(slot["count"])
    last = _epoch(slot.get("last"))
    if last is None:
        return 0.0
    now = time.time() if now is None else now
    age_days = max(0.0, (now - last) / _DAY)
    return base * 0.5 ** (age_days / HALF_LIFE_DAYS)


def _rotate(log):
    """Keep the live log under LOG_ROTATE_BYTES by appending it to the
    archive beside it; nothing is lost, the live file just stays
    small enough to tail."""
    try:
        if log.stat().st_size <= LOG_ROTATE_BYTES:
            return
        archive = log.with_name(".recall-log-archive.jsonl")
        with open(archive, "a") as out, open(log) as src:
            out.write(src.read())
        log.write_text("")
    except OSError:
        pass


def record(home, paths, *, query=None):
    """Append one recall event and bump the counts for every distinct
    path. An empty recall records nothing. Never raises: reinforcement
    is a side effect of a search that already succeeded."""
    try:
        paths = sorted({str(p) for p in paths if p})
        if not paths:
            return
        now = time.time()
        log = _log_path(home)
        log.parent.mkdir(parents=True, exist_ok=True)
        # Reinforcement records what was read, so its log is a memory
        # file by the same rule as any other. The refusal stops the write,
        # which is the property; `memory_search._record` is fail-open by
        # design and swallows the exception, so this surfaces as a missing
        # bonus on a mis-rooted home, never as a broken search.
        perimeter.assert_writable(log, writer="reinforce.record")
        perimeter.assert_writable(_counts_path(home), writer="reinforce.record")
        # the log's rotation and the counts are read-modify-write: one
        # writer at a time per home (memory_lock), or a session loses the
        # other's recall
        with memory_lock.write_lock(home):
            with open(log, "a") as out:
                out.write(json.dumps({
                    "ts": _iso(now),
                    "query": (query or "")[:_QUERY_CHARS],
                    "paths": paths,
                }) + "\n")
            _rotate(log)
            counts = load_counts(home)
            for path in paths:
                slot = counts.setdefault(path, {"count": 0, "last": ""})
                slot["count"] += 1
                slot["last"] = _iso(now)
            target = _counts_path(home)
            tmp = target.with_name(target.name + ".tmp")
            tmp.write_text(json.dumps(counts, indent=0))
            os.replace(tmp, target)
    except (OSError, TypeError, ValueError):
        return


def carry(home, moves):
    """Give each new path the usage history of its old one ({old: new}):
    a file that moved keeps its bonus. The new entry takes the larger count
    and the later timestamp of the two, so carrying twice changes nothing;
    the old entry is left as it is. Never raises, as record() does not."""
    try:
        with memory_lock.write_lock(home):
            counts = load_counts(home)
            changed = False
            for old, new in moves.items():
                slot = counts.get(str(old))
                if not slot:
                    continue
                have = counts.get(str(new)) or {"count": 0, "last": ""}
                counts[str(new)] = {"count": max(have["count"], slot["count"]),
                                    "last": max(str(have.get("last") or ""),
                                                str(slot.get("last") or ""))}
                changed = True
            if changed:
                target = _counts_path(home)
                tmp = target.with_name(target.name + ".tmp")
                tmp.write_text(json.dumps(counts, indent=0))
                os.replace(tmp, target)
    except (OSError, TypeError, ValueError):
        return


def top_used(home, n=10):
    """The n most-recalled files as (path, count, last), most used
    first, ties by path: consolidation's promotion candidates."""
    rows = [(path, slot["count"], slot.get("last", ""))
            for path, slot in load_counts(home).items()]
    rows.sort(key=lambda row: (-row[1], row[0]))
    return rows[:n]
