"""Token usage from the harness transcripts, through the seam
config/harness.toml defines (docs/reference/console-api.md, "Tokens"). For each
cousin every transcript under its <transcripts_dir> (its sessions and
their subagents) touched inside the series window is scanned
incrementally (a byte offset per file, remembered per server) and each
message's usage summed per UTC calendar day, once per message id: the
harness writes one line per content block, each repeating the usage.
Absent seam: unavailable, with the reason.

Only the session in use was read before 2026-09-19, and a cousin that
flips daily starts a new session every day: the 14-day series held
today alone."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from cousin_lib.config import (MissingConfigError, expand_harness_path,
                               harness_config)

SERIES_DAYS = 14


def _seam(root):
    """(available, reason): the harness seam only."""
    try:
        cfg = harness_config(root)
    except MissingConfigError as err:
        return False, str(err)
    if cfg is None:
        return False, "config/harness.toml absent"
    if not cfg.get("transcripts_dir"):
        return False, "config/harness.toml has no transcripts_dir"
    return True, ""


def _any_sdk(root):
    from cousin_lib.config import FrameworkConfig
    from cousin_lib.delivery import _runner_kind
    try:
        return any(_runner_kind(c.home) == "sdk" for c in FrameworkConfig(root).list_cousins())
    except Exception:  # noqa: BLE001 - an unreadable fleet is "no sdk cousin"
        return False


def availability(root):
    """(available, reason): the harness seam, or any cousin on the SDK
    lane (its usage is in its own usage.db, no seam needed)."""
    ok, reason = _seam(root)
    if ok or _any_sdk(root):
        return True, ""
    return False, reason


def _usage_total(usage):
    total = 0
    output = 0
    for key in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                "cache_creation_input_tokens"):
        value = usage.get(key) or 0
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            total += int(value)
            if key == "output_tokens":
                output += int(value)
    # usage["cache_creation"] only splits cache_creation_input_tokens by
    # TTL; adding it too counted every cache write twice.
    return total, output


def _add_line(days, line, seen=None):
    if b'"usage"' not in line:
        return
    try:
        entry = json.loads(line)
    except ValueError:
        return
    if not isinstance(entry, dict):
        return
    ts = entry.get("timestamp") or ""
    day = ts[:10] if isinstance(ts, str) else ""
    if len(day) != 10:
        return
    message = entry.get("message")
    usage = message.get("usage") if isinstance(message, dict) else None
    if not isinstance(usage, dict):
        return
    if seen is not None:
        mid = message.get("id") or entry.get("requestId")
        if mid:
            if mid in seen:
                return
            seen.add(mid)
    total, output = _usage_total(usage)
    bucket = days.setdefault(day, {"total": 0, "output": 0})
    bucket["total"] += total
    bucket["output"] += output


def _transcripts(root, home, since):
    cfg = harness_config(root)
    base = expand_harness_path(cfg["transcripts_dir"], home)
    try:
        return [p for p in base.rglob("*.jsonl")
                if p.stat().st_mtime >= since]
    except OSError:
        return []


def _scan(entry, path):
    try:
        size = path.stat().st_size
    except OSError:
        return
    if size < entry["offset"]:
        entry.update(offset=0, days={}, seen=set())
    if size <= entry["offset"]:
        return
    try:
        with open(path, "rb") as fh:
            fh.seek(entry["offset"])
            chunk = fh.read(size - entry["offset"])
    except OSError:
        return
    cut = chunk.rfind(b"\n")
    if cut < 0:
        return
    data = chunk[:cut + 1]
    for line in data.splitlines():
        _add_line(entry["days"], line, entry["seen"])
    entry["offset"] += len(data)


def day_totals(server, home, *, days=SERIES_DAYS):
    """{day: {"total", "output"}} for one cousin. A cousin on runner =
    "sdk" is read from its usage.db ONLY: the SDK also writes the
    harness's local transcript for it (phase 0 finding 2), and scanning
    that too would count its turns twice. Every other cousin: every
    transcript touched in the last `days` days, scanning only the bytes
    appended since the last call on this server."""
    from cousin_lib import usage
    from cousin_lib.delivery import _runner_kind
    if _runner_kind(home) == "sdk":
        return {day: {"total": b["total"], "output": b["output"]}
                for day, b in usage.day_totals(home, days=days).items()}
    state = server.state.setdefault("tokens", {})
    files = state.setdefault(str(home), {})
    try:
        ok, _reason = _seam(server.root)
    except Exception:
        ok = False
    if not ok:
        return {}
    since = (datetime.now(timezone.utc) - timedelta(days=days + 1)).timestamp()
    out = {}
    for path in _transcripts(server.root, home, since):
        entry = files.setdefault(str(path), {"offset": 0, "days": {},
                                             "seen": set()})
        _scan(entry, path)
        for day, bucket in entry["days"].items():
            agg = out.setdefault(day, {"total": 0, "output": 0})
            agg["total"] += bucket["total"]
            agg["output"] += bucket["output"]
    return out


def _today():
    return datetime.now(timezone.utc).date()


def series(server, home, *, days=SERIES_DAYS):
    totals = day_totals(server, home)
    today = _today()
    out = []
    for back in range(days - 1, -1, -1):
        day = (today - timedelta(days=back)).isoformat()
        bucket = totals.get(day, {"total": 0, "output": 0})
        out.append({"day": day, "total": bucket["total"],
                    "output": bucket["output"]})
    return out


def today_total(server, home):
    ok, _reason = availability(server.root)
    if not ok:
        return 0
    return day_totals(server, home).get(_today().isoformat(),
                                        {"total": 0})["total"]
