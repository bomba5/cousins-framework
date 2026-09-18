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
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
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
DEFAULT_TRUTH_LEVEL = "cousin-conclusion"


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
    with open(path, "a") as fh:
        fh.write(json.dumps(entry) + "\n")


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


_DECIDE_USAGE = (
    "Usage: cousin-memory decide TOPIC DECISION REASONING\n"
    "   or: cousin-memory decide --stdin <<'EOF'\n"
    "       topic\n       ---\n       decision\n       ---\n"
    "       reasoning\n       EOF")


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
    # The resolved values replace the originals so the rest of decide
    # (the raw bridge included) never reads an unresolved argument.
    args.topic, args.decision, args.reasoning = topic, decision, reasoning
    entry = {
        # Aware local time: raw-memory entries are UTC-aware and boot
        # staleness math compares the two.
        "timestamp": datetime.now().astimezone().isoformat(),
        "topic": args.topic,
        "decision": args.decision,
        "reasoning": args.reasoning,
    }
    decisions = home / "data" / "decisions.jsonl"
    decisions.parent.mkdir(parents=True, exist_ok=True)
    with open(decisions, "a") as fh:
        fh.write(json.dumps(entry) + "\n")
    print("Decision logged: [%s] %s" % (args.topic, args.decision))
    archive = _rotate_decisions_if_needed(decisions)
    if archive:
        print("(decisions.jsonl rotated: older entries -> %s)"
              % archive.name)
    try:
        _append_raw(home, {
            "topic": args.topic,
            "content": "%s - why: %s" % (args.decision, args.reasoning),
            "truth_level": DEFAULT_TRUTH_LEVEL,
            "source": "decision",
        })
    except OSError as err:
        print("warning: raw-memory bridge failed (%s); decision logged"
              " anyway" % err, file=sys.stderr)
    return 0


def _cmd_recall(args):
    home = _home(args)
    keyword = (args.keyword or "").lower()
    entries = []
    try:
        with open(home / "data" / "decisions.jsonl") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not keyword or any(
                    keyword in str(entry.get(k, "")).lower()
                    for k in ("topic", "decision", "reasoning")
                ):
                    entries.append(entry)
    except FileNotFoundError:
        pass
    for entry in entries[-args.last:]:
        print("[%s] %s: %s" % (entry.get("timestamp", "?")[:16],
                               entry.get("topic", "?"),
                               entry.get("decision", "")))
        print("  Why: %s" % entry.get("reasoning", ""))
        print()
    if not entries:
        print("No decisions found matching '%s'" % (keyword or "(all)"))
    return 0


def _cmd_activity(args):
    home = _home(args)
    text = " ".join(args.text) if args.text else "Idle"
    activity = home / "data" / "last-activity.txt"
    activity.parent.mkdir(parents=True, exist_ok=True)
    activity.write_text("%s: %s\n" % (datetime.now().isoformat(), text))
    print("Activity saved: %s" % text[:80])
    return 0


def _run_distill(home, max_lines=None):
    from cousin_lib import distill

    kwargs = {}
    if max_lines:
        kwargs["max_lines"] = max_lines
    report = distill.distill(home, **kwargs)
    print("distilled %d topics from %d raw entries:"
          % (report["topics"], report["entries"]))
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
    """Topics with 3+ entries across the real memory sources - the
    decisions log and memory/raw - are promotion candidates. (Counting
    sources nothing writes reports 'no candidates' forever; that was a
    real bug, and why the sources here are exactly the two the
    producers feed.) Consolidation is a mechanism, not a reminder: the
    distiller then rebuilds memory/distilled/ from raw right here, so
    consolidate promotes instead of only suggesting."""
    home = _home(args)
    counts = Counter()
    latest = defaultdict(str)

    def feed(topic, sample):
        topic = (topic or "").strip().lower()
        if topic:
            counts[topic] += 1
            latest[topic] = sample[:90]

    try:
        with open(home / "data" / "decisions.jsonl") as fh:
            for line in fh:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                feed(entry.get("topic"), entry.get("decision", ""))
    except OSError:
        pass
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
        print("no promotion candidates (topics with 3+ entries across"
              " decisions.jsonl + memory/raw/)")
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
        print("not eligible: [memory] scope is %r (need 'shared' or"
              " 'both'; private and unset are excluded by design)"
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
    print("proposed %d file(s) into the review queue; nothing landed"
          " in canonical" % count)
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
    p.set_defaults(func=_cmd_decide)
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
