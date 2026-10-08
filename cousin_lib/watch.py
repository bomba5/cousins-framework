"""cousin-watch <slug>: a runner cousin's reasoning stream in any terminal
(spec, "Views"). The same events the console's pane shows, one line each
(a multi-line text indented under its first), read from the cousin's own
`data/stream/<session>.jsonl` through runner.status.follow: no console,
no port.

    cousin-watch <slug> [--follow] [--tail N | --after N] [--json] [--home HOME]

It starts at the newest `--tail` events of the runner's primary stream
(200; 0 prints the whole stream), or after event `--after N` of it. Without
--follow it prints those and exits; with it, it keeps printing as the
runner appends, following a restarted runner to its new stream, until
interrupted. --json prints each event as the JSON line it is. A tmux
cousin has no stream (its view is its tmux pane): exit 2, as for an
unknown cousin.
"""
import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

WIDTH = 160            # one-line summaries are cut here
_LABEL = {"text": "text", "thinking": "think", "tool": "tool", "tool_result": "output",
          "tool_call": "call", "result": "done", "turn_start": "turn", "state": "state",
          "user": "user", "error": "ERROR", "auth": "AUTH", "session": "session"}


def _cut(text, width=WIDTH):
    text = " ".join(str(text).split())
    return text if len(text) <= width else text[:width - 3] + "..."


def _generic(payload):
    if not isinstance(payload, dict):
        return _cut(payload)
    return _cut(" ".join("%s=%s" % (k, v if isinstance(v, (int, float, bool)) or v is None
                                    else json.dumps(v, ensure_ascii=False))
                         for k, v in payload.items()))


def summary(kind, p):
    """The text after the label for one event's payload."""
    p = p if isinstance(p, dict) else {}
    if kind == "state":
        detail = p.get("detail")
        # the state a runner starts in has no `from` (runner/main.py)
        before = "%s -> " % p["from"] if p.get("from") else ""
        return "%s%s%s" % (before, p.get("to"), " (%s)" % detail if detail else "")
    if kind == "turn_start":
        bodies = p.get("bodies") or [""]
        return "%s: %s" % (p.get("thread_id"), _cut(bodies[0], WIDTH - 20))
    if kind in ("text", "user"):
        return str(p.get("text") or "")
    if kind == "thinking":
        text = str(p.get("text") or "")
        if not text:
            return "(%s chars, not recorded)" % p.get("length")
        return text + (" [truncated]" if p.get("truncated") else "")
    if kind == "tool":
        return "%s %s" % (p.get("name"), _cut(json.dumps(p.get("input"), ensure_ascii=False),
                                              WIDTH - 20))
    if kind == "tool_result":
        return ("error: " if p.get("is_error") else "") + _cut(p.get("text") or "")
    if kind == "tool_call":
        return "%s %s%s (%s ms)" % (p.get("tool"), p.get("command") or "",
                                    " error" if p.get("is_error") else "", p.get("ms"))
    if kind == "result":
        flags = [f for f in ("interrupted", "is_error", "background") if p.get(f)]
        return "rows %s%s" % (p.get("inbox_ids"), " " + " ".join(flags) if flags else "")
    if kind == "error":
        return str(p.get("error") or "")
    return _generic(p)


def format_event(event):
    """`HH:MM:SS label  summary`; a multi-line summary's later lines indented."""
    kind = str(event.get("kind") or "?")
    try:
        when = datetime.fromtimestamp(float(event.get("ts"))).strftime("%H:%M:%S")
    except (TypeError, ValueError, OverflowError, OSError):
        when = "--:--:--"
    head = "%s %-7s " % (when, _LABEL.get(kind, kind)[:7])
    lines = summary(kind, event.get("payload")).split("\n")
    return "\n".join([head + lines[0]] + [" " * len(head) + line for line in lines[1:]])


def run(home, *, after=None, tail=None, follow=False, as_json=False, out=None,
        sleep=time.sleep):
    """Print the stream to `out` (stdout); returns 0. `tail` None is the
    reader's default (status.TAIL_EVENTS), 0 the whole stream."""
    from cousin_lib.runner import status
    out = out or sys.stdout
    tail = status.TAIL_EVENTS if tail is None else tail
    for what, value in status.follow(home, after=after, tail=tail, forever=follow,
                                     sleep=sleep):
        if what == "event":
            out.write((json.dumps(value) if as_json else format_event(value)) + "\n")
        elif what == "start":
            continue
        elif what == "session":
            out.write((json.dumps({"kind": "session", "session": value}) if as_json
                       else "-- a new runner: %s --" % value) + "\n")
        else:
            continue
        out.flush()
    return 0


def _home_for(slug, home):
    if home:
        return Path(home)
    from cousin_lib.config import FrameworkConfig, MissingConfigError
    try:
        root = FrameworkConfig.resolve(cwd_fallback=True)
    except MissingConfigError as err:
        raise LookupError(str(err))
    for cousin in root.list_cousins():
        if cousin.slug == slug:
            return cousin.home
    raise LookupError("no cousin %r under %s" % (slug, root.root))


def watch_main(argv=None):
    parser = argparse.ArgumentParser(prog="cousin-watch",
                                     description="a runner cousin's reasoning stream")
    parser.add_argument("slug")
    parser.add_argument("--follow", "-f", action="store_true",
                        help="keep printing as the runner appends")
    start = parser.add_mutually_exclusive_group()
    start.add_argument("--tail", type=int, default=None,
                       help="start at the newest N events (default 200; 0 for the whole stream)")
    start.add_argument("--after", type=int, default=None,
                       help="start after event N of the runner's current stream")
    parser.add_argument("--json", action="store_true", help="print each event as its JSON line")
    parser.add_argument("--home", help="the cousin home (default: found by slug)")
    args = parser.parse_args(argv)
    try:
        home = _home_for(args.slug, args.home)
    except LookupError as err:
        print("cousin-watch: %s" % err, file=sys.stderr)
        return 2
    from cousin_lib import delivery
    if not isinstance(delivery.backend_for(home), delivery.InboxBackend):
        print("cousin-watch: %s is not a runner cousin (its view is its tmux pane)"
              % args.slug, file=sys.stderr)
        return 2
    try:
        return run(home, after=args.after, tail=args.tail, follow=args.follow, as_json=args.json)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(watch_main())
