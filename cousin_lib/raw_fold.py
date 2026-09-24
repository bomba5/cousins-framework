"""Bound memory/raw losslessly.

Daily raw files older than keep_days are appended, byte-identical, to
memory/raw/archive/<YYYY-MM>.jsonl.gz (forensic tier) and removed. The
month keeps ONE digest entry per topic in
memory/raw/<YYYY-MM>-digest.jsonl (newest content, entry count, first
and last day, source "digest") so list_raw and the distiller still see
the topic. Idempotent: a second run folds nothing. Digest ids derive
from a stable hash of the topic, never from hash(), which is salted per
process and would make every index re-embed every digest on every boot.
"""
import gzip
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cousin_lib import memory, memory_lock

DEFAULT_KEEP_DAYS = 30
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.jsonl$")


def archive_dir(home):
    return memory.raw_dir(home) / "archive"


def digest_path(home, month):
    return memory.raw_dir(home) / ("%s-digest.jsonl" % month)


def topic_key(topic):
    """Stable 12-hex key for a topic."""
    return hashlib.sha1(topic.strip().lower().encode()).hexdigest()[:12]


def _ts(entry):
    return memory.entry_timestamp(entry) or 0.0


def _stamp(entry):
    return entry.get("timestamp") or entry.get("created_at")


def _load_digest(path):
    out = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("topic"):
            out[entry["topic"]] = entry
    return out


def _merge_digest(digest, entry, month, hidden=frozenset()):
    """Fold one entry into its topic's digest line. A line the memory
    views leave out (memory.view_noise, or an id in `hidden`) is archived
    but never becomes a digest's text: a digest stands for what the
    views may show."""
    topic = str(entry.get("topic") or "").strip()
    if not topic or not entry.get("content"):
        return
    if memory.view_noise(entry) or memory.entry_id(entry) in hidden:
        return
    day = str(_stamp(entry) or "")[:10]
    current = digest.get(topic)
    if current is None:
        digest[topic] = {
            "timestamp": _stamp(entry),
            "topic": topic,
            "content": entry.get("content"),
            "truth_level": entry.get("truth_level")
            or memory.DEFAULT_TRUTH_LEVEL,
            "source": "digest",
            "entries": 1,
            "first_at": day,
            "last_at": day,
            "id": "digest-%s-%s" % (month, topic_key(topic)),
        }
        return
    current["entries"] = int(current.get("entries", 1)) + 1
    current["first_at"] = min(current.get("first_at") or day, day)
    if _ts(entry) >= _ts(current):
        current["content"] = entry.get("content")
        current["truth_level"] = (entry.get("truth_level")
                                  or current.get("truth_level"))
        current["timestamp"] = _stamp(entry)
        current["last_at"] = day


def _fold_month(home, month, files, report, hidden=frozenset()):
    dpath = digest_path(home, month)
    digest = _load_digest(dpath)
    archive = archive_dir(home) / ("%s.jsonl.gz" % month)
    with gzip.open(archive, "at") as out:
        for path in files:
            raw = path.read_text()
            if raw and not raw.endswith("\n"):
                raw += "\n"
            out.write(raw)
            for line in raw.splitlines():
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                _merge_digest(digest, entry, month, hidden)
                report["folded_entries"] += 1
            report["folded_days"] += 1
    tmp = dpath.with_suffix(".jsonl.tmp")
    tmp.write_text("".join(json.dumps(d) + "\n"
                           for d in digest.values()))
    tmp.replace(dpath)
    for path in files:
        path.unlink()
    report["months"].append(month)


def fold_raw(home, *, keep_days=DEFAULT_KEEP_DAYS):
    """Fold daily raw files older than keep_days. Returns
    {"folded_days", "folded_entries", "months"}."""
    home = Path(home)
    memory.ensure_layout(home)
    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=keep_days)).date().isoformat()
    report = {"folded_days": 0, "folded_entries": 0, "months": []}
    by_month = {}
    for path in sorted(memory.raw_dir(home).iterdir()):
        if not _DAY_RE.match(path.name):
            continue
        day = path.name[:10]
        if day < cutoff:
            by_month.setdefault(day[:7], []).append(path)
    if not by_month:
        return report
    archive_dir(home).mkdir(parents=True, exist_ok=True)
    hidden = memory.hidden_ids(memory._all_raw(home))
    for month, files in by_month.items():
        # One month's read, archive, digest and unlink are one section under
        # the home's memory write lock: the decisions backfill appends to a
        # decision's own (often old) day file, and an append between this
        # read and the unlink would be lost for good (review P8-5).
        with memory_lock.write_lock(home):
            files = [p for p in files if p.exists()]   # a concurrent fold took it
            if not files:
                continue
            _fold_month(home, month, files, report, hidden)
    return report
