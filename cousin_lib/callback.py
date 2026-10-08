"""Callback moments: a cousin's own library of moments worth calling
back to later - a running joke, a line the operator liked, a turning
point in a long thread.

One bullet per moment at <home>/memory/callbacks.md, so the memory
search indexes it like any other memory file. The source kept a
markdown table keyed by slug; the home already names the cousin, and a
bullet list survives hand edits a table does not. Pipes are the field
separator, so a pipe inside a moment or a category is escaped on the
way in and restored on the way out.
"""
import argparse
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from cousin_lib import jsonl
from cousin_lib.trace import traced_cli

MOMENT_MAX = 200
HEADER = "# Callback library\n\n"
_ROW = re.compile(r"^- (\S+) \| cycle (\S+) \| (.*?) \| (.*)$")


class _NoContext(Exception):
    pass


def _home(args):
    home = getattr(args, "home", None) or os.environ.get("COUSIN_HOME")
    if not home:
        raise _NoContext(
            "no cousin context - set COUSIN_HOME or pass --home"
            " (refusing to fall back into another cousin's memory)")
    return Path(home).resolve()


def library_path(home):
    return Path(home) / "memory" / "callbacks.md"


def _escape(text):
    return text.replace("|", r"\|")


def _unescape(text):
    return text.replace(r"\|", "|")


def _split_fields(rest):
    """Split `category | moment` on the first UNESCAPED separator."""
    parts = re.split(r"(?<!\\) \| ", rest, maxsplit=1)
    if len(parts) != 2:
        return None
    return parts


def tag(home, text, *, cycle=None, category=None):
    """Append one moment; returns the library path. An empty moment is
    a ValueError, an oversized one is truncated with a marker."""
    moment = (text or "").strip()
    if not moment:
        raise ValueError("empty moment")
    if len(moment) > MOMENT_MAX:
        moment = moment[:MOMENT_MAX - 3] + "..."
    path = library_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(HEADER)
    stamp = datetime.now().replace(microsecond=0).isoformat()
    line = "- %s | cycle %s | %s | %s\n" % (
        stamp, cycle if cycle is not None else "-",
        _escape(category) if category else "-", _escape(moment))
    jsonl.append_line(path, line)
    return path


def _parse(line):
    m = re.match(r"^- (\S+) \| cycle (\S+) \| (.*)$", line.rstrip("\n"))
    if not m:
        return None
    rest = _split_fields(m.group(3))
    if rest is None:
        return None
    cycle_raw, category_raw, moment_raw = m.group(2), rest[0], rest[1]
    try:
        cycle = int(cycle_raw)
    except ValueError:
        cycle = None
    category = _unescape(category_raw) if category_raw != "-" else None
    return {"time": m.group(1), "cycle": cycle, "category": category,
            "moment": _unescape(moment_raw)}


def list_all(home):
    """Every moment, oldest first. A missing library is an empty one."""
    path = library_path(home)
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    entries = []
    for line in lines:
        entry = _parse(line)
        if entry:
            entries.append(entry)
    return entries


def search(home, query):
    """Case-insensitive substring match over moment and category."""
    needle = query.lower()
    return [e for e in list_all(home)
            if needle in e["moment"].lower()
            or needle in (e["category"] or "").lower()]


def _format(entry):
    return "%s | cycle %s | %s | %s" % (
        entry["time"],
        entry["cycle"] if entry["cycle"] is not None else "-",
        entry["category"] or "-", entry["moment"])


def _cmd_tag(args):
    home = _home(args)
    try:
        path = tag(home, args.moment, cycle=args.cycle,
                   category=args.category)
    except ValueError as err:
        print("ERROR: %s" % err, file=sys.stderr)
        return 2
    print("tagged callback in %s" % path)
    return 0


def _cmd_list(args):
    entries = list_all(_home(args))
    if args.category:
        entries = [e for e in entries
                   if (e["category"] or "").lower() == args.category.lower()]
    entries = entries[-args.limit:] if args.limit else entries
    if not entries:
        print("(no callbacks)")
        return 0
    for entry in entries:
        print(_format(entry))
    return 0


def _cmd_search(args):
    hits = search(_home(args), args.query)
    hits = hits[-args.limit:] if args.limit else hits
    if not hits:
        print("(no matches)")
        return 0
    for entry in hits:
        print(_format(entry))
    return 0


@traced_cli("cousin-callback")
def callback_main(argv=None):
    parser = argparse.ArgumentParser(
        prog="cousin-callback",
        description="a cousin's library of moments worth calling back to")
    parser.add_argument("--home")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("tag", help="append a moment")
    p.add_argument("moment")
    p.add_argument("--cycle", type=int)
    p.add_argument("--category")
    p.set_defaults(func=_cmd_tag)
    p = sub.add_parser("list", help="show moments, newest last")
    p.add_argument("--category")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=_cmd_list)
    p = sub.add_parser("search", help="substring search over the library")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=_cmd_search)
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except _NoContext as err:
        print("ERROR: %s" % err, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(callback_main())
