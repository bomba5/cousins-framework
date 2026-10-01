"""Per-turn usage on the SDK and opencode lanes.

One row per result in <home>/data/usage.db. `total_cost_usd` is
cumulative per CLIENT (measured: 0.0088 then 0.0136 on one client), so
a row stores the DIFFERENCE from the same client's previous row. A
client is identified by a string unique to one connect of one process
(the runner mints a uuid), so neither a reconnect nor a restart
diffs against a client that no longer exists. On the login lane the
figure is the SDK's own estimate, not a bill, and the row says so; on the
opencode lane it is opencode's figure (the model's list price times the
tokens the provider reported), an estimate too. The opencode runner keeps
its own running cost per runner, so a row there is its turn's cost.
Best-effort: recording never fails a turn."""
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cousin_lib.sqlite_util import wal

USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens",
              "cache_creation_input_tokens")
_SCHEMA = ("CREATE TABLE IF NOT EXISTS usage ("
           " id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, day TEXT NOT NULL,"
           " client_id TEXT NOT NULL, session_id TEXT NOT NULL DEFAULT '',"
           " total INTEGER NOT NULL, output INTEGER NOT NULL,"
           " cache_read INTEGER NOT NULL DEFAULT 0, cache_creation INTEGER NOT NULL DEFAULT 0,"
           " cost_usd REAL NOT NULL, cumulative_usd REAL NOT NULL,"
           " lane TEXT NOT NULL, estimate INTEGER NOT NULL)")


def lane_for(api_key_source):
    if api_key_source == "ANTHROPIC_API_KEY":
        return "key"
    if api_key_source == "none":
        return "login"
    return "unknown"


@contextmanager
def _db(home):
    path = Path(home) / "data" / "usage.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    try:
        wal(conn)
        with conn:
            conn.execute(_SCHEMA)
            yield conn
    finally:
        conn.close()


def _totals(u):
    total = output = 0
    for key in USAGE_KEYS:
        value = (u or {}).get(key) or 0
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            total += int(value)
            if key == "output_tokens":
                output += int(value)
    return total, output


def record(home, *, client_id, session_id, result, lane):
    try:
        u = result.get("usage") or {}
        total, output = _totals(u)
        cumulative = float(result.get("total_cost_usd") or 0.0)
        now = time.time()
        with _db(home) as conn:
            prev = conn.execute("SELECT cumulative_usd FROM usage WHERE client_id = ?"
                                " ORDER BY id DESC LIMIT 1", (str(client_id),)).fetchone()
            cost = cumulative - (prev[0] if prev else 0.0)
            if cost < 0:
                cost = cumulative
            row = {"ts": now, "day": datetime.fromtimestamp(now, timezone.utc).date().isoformat(),
                   "client_id": str(client_id), "session_id": session_id or "",
                   "total": total, "output": output,
                   "cache_read": int(u.get("cache_read_input_tokens") or 0),
                   "cache_creation": int(u.get("cache_creation_input_tokens") or 0),
                   "cost_usd": cost, "cumulative_usd": cumulative,
                   "lane": lane, "estimate": lane != "key"}
            conn.execute("INSERT INTO usage (ts, day, client_id, session_id, total, output,"
                         " cache_read, cache_creation, cost_usd, cumulative_usd, lane, estimate)"
                         " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (row["ts"], row["day"], row["client_id"], row["session_id"], total,
                          output, row["cache_read"], row["cache_creation"], cost, cumulative,
                          lane, int(row["estimate"])))
        return row
    except Exception as err:  # noqa: BLE001 - usage never fails a turn
        return {"error": "%s: %s" % (type(err).__name__, err)}


def day_totals(home, *, days=14):
    if not (Path(home) / "data" / "usage.db").exists():
        return {}
    since = (datetime.now(timezone.utc) - timedelta(days=days + 1)).date().isoformat()
    out = {}
    with _db(home) as conn:
        for day, total, output, cost, estimate, read, creation in conn.execute(
                "SELECT day, SUM(total), SUM(output), SUM(cost_usd), MAX(estimate),"
                " SUM(cache_read), SUM(cache_creation)"
                " FROM usage WHERE day >= ? GROUP BY day", (since,)):
            # total is input + output + cache_read + cache_creation (_totals),
            # so the uncached input is exact without a column of its own
            out[day] = {"total": int(total), "output": int(output),
                        "cost_usd": float(cost), "estimate": bool(estimate),
                        "cache_read": int(read), "cache_creation": int(creation),
                        "input": int(total) - int(output) - int(read) - int(creation)}
    return out
