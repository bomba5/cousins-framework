"""Token usage from the harness transcripts, through the seam
config/harness.toml defines (docs/reference/console-api.md, "Tokens"). For each
cousin with a persisted session id the transcript
<transcripts_dir>/<session_id>.jsonl is scanned incrementally (byte
offset remembered per server) and each message's usage block summed
per UTC calendar day. Absent seam: unavailable, with the reason."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from cousin_lib import flip, transcript_mine
from cousin_lib.config import MissingConfigError, harness_config

SERIES_DAYS = 14


def availability(root):
    """(available, reason)."""
    try:
        cfg = harness_config(root)
    except MissingConfigError as err:
        return False, str(err)
    if cfg is None:
        return False, "config/harness.toml absent"
    if not cfg.get("transcripts_dir"):
        return False, "config/harness.toml has no transcripts_dir"
    return True, ""


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
    creation = usage.get("cache_creation")
    if isinstance(creation, dict):
        for value in creation.values():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                total += int(value)
    return total, output


def _add_line(days, line):
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
    total, output = _usage_total(usage)
    bucket = days.setdefault(day, {"total": 0, "output": 0})
    bucket["total"] += total
    bucket["output"] += output


def day_totals(server, home):
    """{day: {"total", "output"}} for one cousin, scanning only the
    bytes appended since the last call on this server."""
    state = server.state.setdefault("tokens", {})
    key = str(home)
    entry = state.get(key) or {"path": None, "offset": 0, "days": {}}
    session_id = flip._read_session_id(home)
    path = (transcript_mine.transcript_path(home, server.root, session_id)
            if session_id else None)
    if path is None:
        return {}
    if entry["path"] != str(path):
        entry = {"path": str(path), "offset": 0, "days": {}}
    try:
        size = path.stat().st_size
    except OSError:
        state[key] = entry
        return entry["days"]
    if size < entry["offset"]:
        entry = {"path": str(path), "offset": 0, "days": {}}
    if size > entry["offset"]:
        try:
            with open(path, "rb") as fh:
                fh.seek(entry["offset"])
                chunk = fh.read(size - entry["offset"])
        except OSError:
            chunk = b""
        cut = chunk.rfind(b"\n")
        if cut >= 0:
            data = chunk[:cut + 1]
            for line in data.splitlines():
                _add_line(entry["days"], line)
            entry["offset"] += len(data)
    state[key] = entry
    return entry["days"]


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
