"""Reasoning capsules, retired (3.50.0, meeting 11 F): a conclusion with
its evidence and rejected alternatives, which the memory tool's `decide`
records as well (the decision, its reasoning, `derived_from`), and which
the fleet had stopped writing (four capsules in all, the newest weeks
old). Law rule 9 names `decide` since law 1.4.

What stays for one release, read-only: the record
(<home>/memory/capsules.jsonl), `cousin-reason list`, and the console's
capsule view. `cousin-reason capsule` refuses, naming `decide`; the boot
packet no longer reads capsules. All of it goes in the next major
release.
"""
import argparse
import json
import os
import sys
from pathlib import Path

from cousin_lib import memory
from cousin_lib.trace import traced_cli

CONFIDENCES = ("low", "medium", "high")
DEFAULT_CONFIDENCE = "medium"
DEFAULT_TRUTH_LEVEL = memory.DEFAULT_TRUTH_LEVEL


class _NoContext(Exception):
    pass


def capsules_path(home):
    return Path(home) / "memory" / "capsules.jsonl"


def markdown_path(home):
    return Path(home) / "memory" / "distilled" / "reasoning-capsules.md"


def list_capsules(home, n=10):
    """The newest n capsules, newest first. A missing store is empty;
    an unparsable line is skipped."""
    try:
        lines = capsules_path(home).read_text().splitlines()
    except OSError:
        return []
    entries = []
    for line in lines:
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and entry.get("conclusion"):
            entries.append(entry)
    entries.reverse()
    return entries[:n] if n else entries


def _home(args):
    home = getattr(args, "home", None) or os.environ.get("COUSIN_HOME")
    if not home:
        raise _NoContext(
            "no cousin context - set COUSIN_HOME or pass --home"
            " (refusing to fall back into another cousin's memory)")
    return Path(home).resolve()


def _cmd_capsule(args):
    print("cousin-reason capsule is retired: record a consequential chain of reasoning"
          " with the memory tool's decide (the decision, its reasoning, derived_from),"
          " or `cousin-memory decide` from a shell. Law rule 9 says so since law 1.4.",
          file=sys.stderr)
    return 2


def _format(entry):
    lines = ["%s  [%s] %s" % (entry["id"], entry.get("confidence", "?"),
                               str(entry.get("timestamp", ""))[:19])]
    if entry.get("topic"):
        lines.append("  topic: %s" % entry["topic"])
    lines.append("  %s" % entry["conclusion"])
    lines.extend("  + %s" % item for item in entry.get("evidence", []))
    lines.extend("  - %s" % item for item in entry.get("rejected", []))
    return "\n".join(lines)


def _cmd_list(args):
    entries = list_capsules(_home(args), n=args.n)
    if not entries:
        print("(no capsules)")
        return 0
    print("\n\n".join(_format(e) for e in entries))
    return 0


@traced_cli("cousin-reason")
def reason_main(argv=None):
    parser = argparse.ArgumentParser(
        prog="cousin-reason",
        description="retired: reasoning capsules (list still reads the old"
                    " ones; record reasoning with the memory tool's decide)")
    parser.add_argument("--home")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("capsule", help="retired: use the memory tool's decide")
    p.add_argument("--conclusion", required=True)
    p.add_argument("--evidence", action="append", required=True,
                   help="evidence bullet, repeatable")
    p.add_argument("--rejected", action="append", default=[],
                   help="rejected alternative, repeatable")
    p.add_argument("--confidence", default=DEFAULT_CONFIDENCE,
                   choices=CONFIDENCES)
    p.add_argument("--truth-level", dest="truth_level",
                   default=DEFAULT_TRUTH_LEVEL)
    p.add_argument("--topic")
    p.set_defaults(func=_cmd_capsule)
    p = sub.add_parser("list", help="show recent capsules, newest first")
    p.add_argument("--n", type=int, default=10)
    p.set_defaults(func=_cmd_list)
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except _NoContext as err:
        print("ERROR: %s" % err, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(reason_main())
