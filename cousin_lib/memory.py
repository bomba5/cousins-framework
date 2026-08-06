"""Memory primitives: decisions, activity checkpoints, recall,
consolidation.

The rule the whole module descends from: no cousin context, no
operation. A defaulted home silently reads and writes somebody else's
memory, so a missing COUSIN_HOME is a refusal with a message, never a
fallback.

Search and reindex dispatch to the keyword search module
(cousin_lib.memory_search); the semantic tier is the declared M2 seam.
"""
import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

# decisions.jsonl grows monotonically; past the threshold the older
# entries move to a dated sibling archive and the newest tail stays
# live so recall keeps its recent-context behavior. Nothing is lost:
# every decision also feeds memory/raw via the producer bridge.
DECISIONS_ROTATE_BYTES = 1_000_000
DECISIONS_KEEP_TAIL = 200


class _NoContext(Exception):
    pass


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


def _cmd_decide(args):
    home = _home(args)
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
            "truth_level": "cousin-conclusion",
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


def _cmd_consolidate(args):
    """Topics with 3+ entries across the real memory sources - the
    decisions log and memory/raw - are promotion candidates. Read-only.
    (Counting sources nothing writes reports 'no candidates' forever;
    that was a real bug, and why the sources here are exactly the two
    the producers feed.)"""
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
        return 0
    print("promotion candidates (3+ entries; consider promoting to the"
          " memory index):")
    for topic, count in candidates[:15]:
        print("  %3dx  %s" % (count, topic))
        print("        latest: %s" % latest[topic])
    return 0


def _cmd_search(args):
    from cousin_lib import memory_search

    home = _home(args)
    hits, notice = memory_search.search(args.query, top=args.top,
                                        home=home)
    memory_search.print_results(hits)
    if notice:
        # The degrade contract: a promised-but-dead semantic leg is
        # never silent.
        print("notice: %s" % notice, file=sys.stderr)
    return 0


def _cmd_reindex(args):
    from cousin_lib import memory_search

    home = _home(args)
    count = memory_search.build_index(home)
    print("indexed %d file(s)" % count)
    return 0


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


def memory_main(argv=None):
    parser = argparse.ArgumentParser(prog="cousin-memory")
    parser.add_argument("--home")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("search")
    p.add_argument("query")
    p.add_argument("--top", type=int, default=5)
    p.set_defaults(func=_cmd_search)
    p = sub.add_parser("reindex")
    p.set_defaults(func=_cmd_reindex)
    p = sub.add_parser(
        "propose-shared",
        help="nominate marked shareable memories into the shared"
             " review queue (dry-run unless --commit; nothing ever"
             " lands in canonical from here)")
    p.add_argument("--commit", action="store_true")
    p.set_defaults(func=_cmd_propose_shared)
    p = sub.add_parser("decide")
    p.add_argument("topic")
    p.add_argument("decision")
    p.add_argument("reasoning")
    p.set_defaults(func=_cmd_decide)
    p = sub.add_parser("recall")
    p.add_argument("keyword", nargs="?", default="")
    p.add_argument("--last", type=int, default=20)
    p.set_defaults(func=_cmd_recall)
    p = sub.add_parser("activity")
    p.add_argument("text", nargs="*")
    p.set_defaults(func=_cmd_activity)
    p = sub.add_parser("consolidate")
    p.set_defaults(func=_cmd_consolidate)
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except _NoContext as err:
        print("ERROR: %s" % err, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(memory_main())
