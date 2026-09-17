"""Prompt-cache audit.

The provider's prompt cache is prefix-matched: a byte changed anywhere
in the prefix invalidates everything after it. Nothing says so; the
only visible symptom is a turn whose cache_read collapses while
cache_creation rises, and the bill. This module reads a cousin's
session transcripts through the harness seam (config/harness.toml,
the same locator the flip-time miner uses), takes the per-turn usage
fields from the assistant messages, and reports:

- the per-cousin hit rate: cache_read / (cache_read + input +
  cache_creation) over the window, with a verdict;
- the per-turn distribution of that rate (min, median, max);
- the suspect invalidators: files under the cousin home, and under
  the harness auto-memory directory when configured, whose mtime
  falls between two successive turns where the rate dropped.

Two transcript facts the reader has to respect. The harness writes one
record per content block, each carrying the whole message's usage
under the same message id, so records are deduplicated by id or every
tool-using turn counts several times. Sidechain (sub-agent) turns run
their own prefix and are excluded, as the miner excludes them.

Read-only: the audit never touches the transcripts or the home. An
absent seam is exit 2 naming the file; an empty window is a null
report, not an error.
"""
import argparse
import json
import os
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cousin_lib.config import (CousinConfig, FrameworkConfig,
                               MissingConfigError, expand_harness_path,
                               harness_config)

DEFAULT_DAYS = 7
DROP_THRESHOLD = 0.10       # a fall of more than 10 points is a drop
TOP_SUSPECTS = 10
HEALTHY = 0.85
MARGINAL = 0.65


class _NoContext(Exception):
    pass


def _now():
    return datetime.now(timezone.utc)


def _parse_stamp(value):
    """Epoch seconds for a harness timestamp, or None. The harness
    writes ISO 8601 with a Z; a naive stamp is taken as UTC."""
    if not isinstance(value, str) or not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.timestamp()


def _cache_creation(usage):
    """The cache_creation_input_tokens field, else the sum of the
    nested breakdown older records carry instead."""
    if usage.get("cache_creation_input_tokens") is not None:
        return int(usage.get("cache_creation_input_tokens") or 0)
    nested = usage.get("cache_creation") or {}
    if not isinstance(nested, dict):
        return 0
    return sum(int(v or 0) for v in nested.values()
               if isinstance(v, (int, float)))


def _rate(read, fresh, created):
    total = read + fresh + created
    return read / total if total > 0 else None


def transcripts_dir(home, root):
    """Where the harness keeps this cousin's transcripts, or None when
    the seam does not name a transcripts_dir."""
    cfg = harness_config(root)
    if not cfg or not cfg.get("transcripts_dir"):
        return None
    return expand_harness_path(cfg["transcripts_dir"], home)


def _auto_memory_dir(home, root):
    cfg = harness_config(root)
    template = (cfg or {}).get("auto_memory_dir")
    if not template:
        return None
    path = expand_harness_path(template, home)
    return path if path.is_dir() else None


def _file_turns(path, since):
    """The deduplicated main-thread assistant turns of one transcript,
    tolerating any line that is not what the harness writes."""
    seen = set()
    rows = []
    with open(path) as fh:
        for line in fh:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict) or record.get("isSidechain"):
                continue
            if record.get("type") != "assistant":
                continue
            message = record.get("message") or {}
            usage = message.get("usage") if isinstance(message, dict) \
                else None
            if not isinstance(usage, dict) or not usage:
                continue
            epoch = _parse_stamp(record.get("timestamp"))
            if epoch is None or (since is not None and epoch < since):
                continue
            msg_id = message.get("id")
            if msg_id:
                if msg_id in seen:
                    continue
                seen.add(msg_id)
            read = int(usage.get("cache_read_input_tokens") or 0)
            fresh = int(usage.get("input_tokens") or 0)
            created = _cache_creation(usage)
            rate = _rate(read, fresh, created)
            if rate is None:
                continue
            rows.append({
                "session": path.stem,
                "timestamp": record.get("timestamp"),
                "epoch": epoch,
                "cache_read": read,
                "input": fresh,
                "cache_creation": created,
                "output": int(usage.get("output_tokens") or 0),
                "hit_rate": round(rate, 4),
            })
    return rows


def turns(home, root, *, days=DEFAULT_DAYS):
    """Every assistant turn of every transcript in the window, oldest
    first. Empty when the seam is unset or the directory is absent."""
    base = transcripts_dir(home, root)
    if base is None or not base.is_dir():
        return []
    since = None
    if days is not None:
        since = (_now() - timedelta(days=days)).timestamp()
    rows = []
    for path in sorted(base.glob("*.jsonl")):
        try:
            rows.extend(_file_turns(path, since))
        except OSError:
            continue
    rows.sort(key=lambda t: (t["epoch"], t["session"]))
    return rows


def summarize(turn_rows):
    """Per-cousin totals, hit rate, verdict and per-turn distribution."""
    read = sum(t["cache_read"] for t in turn_rows)
    fresh = sum(t["input"] for t in turn_rows)
    created = sum(t["cache_creation"] for t in turn_rows)
    rate = _rate(read, fresh, created)
    rates = [t["hit_rate"] for t in turn_rows]
    if rate is None:
        verdict = "no data"
    elif rate >= HEALTHY:
        verdict = "healthy"
    elif rate >= MARGINAL:
        verdict = "marginal"
    else:
        verdict = "poor"
    return {
        "turns": len(turn_rows),
        "hit_rate": round(rate, 4) if rate is not None else None,
        "verdict": verdict,
        "distribution": {
            "min": round(min(rates), 4),
            "median": round(statistics.median(rates), 4),
            "max": round(max(rates), 4),
        } if rates else None,
        "tokens": {"cache_read": read, "input": fresh,
                   "cache_creation": created},
    }


def drops(turn_rows, *, threshold=DROP_THRESHOLD):
    """Successive turn pairs where the hit rate fell by more than the
    threshold, oldest first."""
    found = []
    for before, after in zip(turn_rows, turn_rows[1:]):
        fell_by = before["hit_rate"] - after["hit_rate"]
        if fell_by > threshold:
            found.append({
                "before": {"timestamp": before["timestamp"],
                           "epoch": before["epoch"],
                           "hit_rate": before["hit_rate"]},
                "after": {"timestamp": after["timestamp"],
                          "epoch": after["epoch"],
                          "hit_rate": after["hit_rate"]},
                "fell_by": round(fell_by, 4),
            })
    return found


def _walk_files(base, *, exclude):
    """Regular files under base, skipping .git and any excluded tree."""
    base = Path(base)
    if not base.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(base):
        here = Path(dirpath)
        dirnames[:] = sorted(
            d for d in dirnames
            if d != ".git" and (here / d).resolve() not in exclude)
        for name in sorted(filenames):
            yield here / name


def suspects(home, root, drop_pairs, *, top=TOP_SUSPECTS):
    """Files under the home (and the harness auto-memory dir when
    configured) whose mtime sits inside a drop window, ranked by the
    size of the fall. The transcripts themselves are never suspects:
    a transcript under the home would name itself on every drop."""
    if not drop_pairs:
        return []
    exclude = set()
    tdir = transcripts_dir(home, root)
    if tdir is not None:
        exclude.add(tdir.resolve())
    roots = [Path(home)]
    auto = _auto_memory_dir(home, root)
    if auto is not None:
        roots.append(auto)
    found = []
    for base in roots:
        for path in _walk_files(base, exclude=exclude):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            for pair in drop_pairs:
                if pair["before"]["epoch"] <= mtime \
                        <= pair["after"]["epoch"]:
                    found.append({
                        "path": str(path),
                        "mtime": datetime.fromtimestamp(
                            mtime, timezone.utc).isoformat(),
                        "fell_by": pair["fell_by"],
                        "pair": pair,
                    })
                    break
    found.sort(key=lambda s: (-s["fell_by"], s["path"]))
    return found[:top]


def audit(home, root, *, days=DEFAULT_DAYS, top=TOP_SUSPECTS):
    """The full report for one cousin as a plain dict."""
    home = Path(home)
    try:
        slug = CousinConfig.load(home).slug
    except MissingConfigError:
        slug = home.name
    rows = turns(home, root, days=days)
    report = {"slug": slug, "home": str(home), "days": days}
    report.update(summarize(rows))
    pairs = drops(rows)
    report["drops"] = len(pairs)
    report["drop_pairs"] = pairs
    report["suspects"] = suspects(home, root, pairs, top=top)
    return report


# ---------------------------------------------------------------- cli

def _pct(value):
    return "%.1f%%" % (value * 100)


def _render(report, *, diagnose):
    lines = ["%s: %d turn(s)%s" % (
        report["slug"], report["turns"],
        " over %d day(s)" % report["days"] if report["days"] else "")]
    if report["hit_rate"] is None:
        lines.append("hit rate: no data (no assistant turns in window)")
        return "\n".join(lines)
    lines.append("hit rate: %s  [%s]" % (_pct(report["hit_rate"]),
                                         report["verdict"]))
    dist = report["distribution"]
    lines.append("per turn: min %s  median %s  max %s" % (
        _pct(dist["min"]), _pct(dist["median"]), _pct(dist["max"])))
    tok = report["tokens"]
    lines.append("tokens: cache_read %s  input %s  cache_creation %s" % (
        format(tok["cache_read"], ","), format(tok["input"], ","),
        format(tok["cache_creation"], ",")))
    lines.append("drops: %d (hit rate fell by more than %d points "
                 "between successive turns)"
                 % (report["drops"], round(DROP_THRESHOLD * 100)))
    if report["suspects"]:
        lines.append("suspects (touched inside a drop window):")
        for s in report["suspects"]:
            lines.append("  %s  (fell %s -> %s)" % (
                s["path"], _pct(s["pair"]["before"]["hit_rate"]),
                _pct(s["pair"]["after"]["hit_rate"])))
            if diagnose:
                lines.append("      between %s and %s, mtime %s" % (
                    s["pair"]["before"]["timestamp"],
                    s["pair"]["after"]["timestamp"], s["mtime"]))
    elif report["drops"]:
        lines.append("suspects: none (no file under the home changed "
                     "inside a drop window)")
    if report["verdict"] != "healthy":
        lines.append("recommendation: move volatile content after the "
                     "cache breakpoint; a file that changes between "
                     "turns and sits in the prefix invalidates it")
    return "\n".join(lines)


def _home(args):
    home = args.home or os.environ.get("COUSIN_HOME")
    if not home:
        raise _NoContext(
            "cousin-cache-audit: no cousin context - set COUSIN_HOME "
            "or pass --home")
    return Path(home)


def cache_audit_main(argv=None):
    parser = argparse.ArgumentParser(
        prog="cousin-cache-audit",
        description="Prompt-cache hit rate and suspect invalidators "
                    "from the harness transcripts.")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS,
                        help="window in days (default %d; 0 = all)"
                        % DEFAULT_DAYS)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--diagnose", action="store_true",
                        help="list each suspect with its turn pair")
    parser.add_argument("--home")
    args = parser.parse_args(argv)
    try:
        home = _home(args)
        root = FrameworkConfig.resolve().root
    except (_NoContext, MissingConfigError) as err:
        print(str(err), file=sys.stderr)
        return 2
    try:
        cfg = harness_config(root)
    except MissingConfigError as err:
        print("cousin-cache-audit: %s" % err, file=sys.stderr)
        return 2
    if cfg is None:
        print("cousin-cache-audit: config/harness.toml is absent; the "
              "audit reads transcripts only through that seam "
              "(see config/harness.toml.example)", file=sys.stderr)
        return 2
    if not cfg.get("transcripts_dir"):
        print("cousin-cache-audit: config/harness.toml sets no "
              "transcripts_dir; nothing to audit", file=sys.stderr)
        return 2
    days = args.days if args.days and args.days > 0 else None
    report = audit(home, root, days=days)
    if args.json:
        print(json.dumps(report, indent=1))
    else:
        print(_render(report, diagnose=args.diagnose))
    return 0
