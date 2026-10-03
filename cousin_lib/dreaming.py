"""Dreaming: a background pass that consolidates a cousin's memory.

Off unless the operator turns it on for a cousin (`[agent] dreaming`,
the console's Agent panel). A pass is its own short model session in a
child process (`python -m cousin_lib.dreaming --home H --root R`, the
shape of runner/validate_turn.py: the auth scrub is process-wide and
must never run inside the loops daemon). It runs on the cousin's
account, never touches its inbox, transcript or runner stream, and can
run while the cousin is in a turn: the memory writers serialise on the
home's lock.

What a pass may write is structural, not a rule it is asked to follow:
the session gets `tools=[]` (no built-in tool: no file, no shell, no
web) and one in-process MCP server, `dream`, whose tools are the memory
operations `dream_memory` defines. The memory perimeter (perimeter.py)
stays as the second line.

The memory side is `cousin_lib.dream_memory`, the contract below:

    slice_for(home, *, chars) -> Slice   # .text, .through (opaque,
                                         # JSON), .empty, .coverage
    prompt(slice) -> str                 # the doctrine and the slice
    OPERATIONS                           # [{"name", "description",
                                         #   "inputSchema", "fn"}]
        fn(home, pass_id, args) -> change record (dict: op, topic,
        entry_ids, mark_id, why), or raises ValueError (told to the
        model as a tool error)
    begin(home, pass_id) / commit(home, pass_id, through) /
    abandon(home, pass_id, reason)       # the ledger and attempt token
    undo(home, pass_id, changes) -> [change records]

This module owns when a pass runs (the setting, `due`), running it
under the budget (`run_pass`), and the record of what it did:
`data/dreams/YYYY-MM-DD.jsonl`, a `start` line and an `end` line per
pass with the same pass_id, so a pass that dies mid-way still shows."""
import argparse
import asyncio
import json
import os
import re
import sys
import tempfile
import time
import tomllib
import uuid
from datetime import datetime
from pathlib import Path

MODES = ("off", "nightly", "rollover")
DEFAULT_AT = "03:00"
DEFAULT_MODEL = "sonnet"
BUDGET_TOKENS = 32000
# cache reads bill at a tenth of fresh input: they count at that weight
CACHE_READ_WEIGHT = 0.1
MAX_TURNS = 24
# about 10k tokens of slice: the prompt, the replies and the cache reads of
# each later turn have to fit the budget with it
SLICE_CHARS = 40000
PASS_TIMEOUT_S = 900
REQUEST = ("data", "dream.request")
_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


# ---- the setting ------------------------------------------------------------

def settings(home):
    """{"mode", "at"} from cousin.toml [agent]; an unreadable or unknown
    value is "off" (a pass never starts on a guess)."""
    try:
        agent = tomllib.loads((Path(home) / "cousin.toml").read_text()).get("agent") or {}
    except (OSError, tomllib.TOMLDecodeError):
        agent = {}
    mode = agent.get("dreaming", "off")
    at = agent.get("dreaming_at", DEFAULT_AT)
    return {"mode": mode if mode in MODES else "off",
            "at": at if isinstance(at, str) and _TIME.match(at) else DEFAULT_AT}


def check_time(value):
    """A dreaming_at value, or ValueError (agent_settings' check)."""
    if not isinstance(value, str) or not _TIME.match(value):
        raise ValueError("must be a time of day, HH:MM (24 h)")
    return value


def request(home):
    """Ask for a pass at the next loops tick (the runner calls this after a
    rollover; a cousin set to "rollover" runs on it)."""
    path = Path(home).joinpath(*REQUEST)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(datetime.now().astimezone().isoformat(timespec="seconds"))


def due(home, now=None):
    """The trigger a pass is due on now ("nightly", "rollover"), or None."""
    cfg = settings(home)
    if cfg["mode"] == "off":
        return None
    now = now or datetime.now().astimezone()
    last = last_start(home)
    if cfg["mode"] == "rollover":
        req = Path(home).joinpath(*REQUEST)
        try:
            asked = req.stat().st_mtime
        except FileNotFoundError:
            return None
        return "rollover" if last is None or asked > last else None
    hour, minute = (int(x) for x in cfg["at"].split(":"))
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now < target:
        return None
    if last is not None and datetime.fromtimestamp(last).astimezone().date() == now.date():
        return None
    return "nightly"


# ---- the record -------------------------------------------------------------

def log_dir(home):
    return Path(home) / "data" / "dreams"


def _write(home, record):
    path = log_dir(home) / ("%s.jsonl" % datetime.now().strftime("%Y-%m-%d"))
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def passes(home, *, limit=50):
    """The passes newest first, each the merge of its start and end lines;
    a pass with a start and no end is `running` or, past PASS_TIMEOUT_S,
    `lost`."""
    found = {}
    for path in sorted(log_dir(home).glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict) or not rec.get("pass_id"):
                continue
            found.setdefault(rec["pass_id"], {}).update(rec)
    out = sorted(found.values(), key=lambda r: r.get("started", 0), reverse=True)
    for rec in out:
        if "result" not in rec:
            late = time.time() - rec.get("started", 0) > PASS_TIMEOUT_S + 60
            rec["result"] = "lost" if late else "running"
    return out[:limit]


def last_start(home):
    found = passes(home, limit=1)
    return found[0].get("started") if found else None


# ---- the pass ---------------------------------------------------------------

def _weighted(usage):
    if not isinstance(usage, dict):
        return 0
    return int((usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0)
               + (usage.get("cache_creation_input_tokens") or 0)
               + CACHE_READ_WEIGHT * (usage.get("cache_read_input_tokens") or 0))


def _memory_ops():
    from cousin_lib import dream_memory
    return dream_memory


def tool_server(home, pass_id, ops, changes):
    """The `dream` MCP server: one tool per operation. Each successful call
    appends its change record to `changes` (the pass's log)."""
    from claude_agent_sdk import create_sdk_mcp_server, tool

    def make(op):
        async def handler(args):
            try:
                record = await asyncio.to_thread(op["fn"], home, pass_id, args)
            except ValueError as err:
                return {"content": [{"type": "text", "text": str(err)}], "is_error": True}
            if record:
                changes.append(record)
            return {"content": [{"type": "text", "text": json.dumps(record or {})}]}
        return handler

    return create_sdk_mcp_server("dream", version="1.0.0", tools=[
        tool(op["name"], op["description"], op["inputSchema"])(make(op))
        for op in ops.OPERATIONS])


def run_pass(home, root, *, trigger="manual", model=None, budget=BUDGET_TOKENS,
             ops=None, client_factory=None):
    """One pass: begin the attempt, run the session under `budget`, record
    every change, commit the ledger when the session ended clean, else
    abandon it. Returns the end record (also written to the log)."""
    from cousin_lib import accounts
    home, root = Path(home), Path(root)
    pass_id = "%s-%s" % (datetime.now().strftime("%Y%m%dT%H%M%S"), uuid.uuid4().hex[:6])
    model = model or DEFAULT_MODEL
    start = {"pass_id": pass_id, "event": "start", "started": time.time(),
             "trigger": trigger, "model": model, "budget": budget}
    # the start is written before anything can fail: a pass that dies
    # early is still an attempt, so due() does not fire it again each tick
    _write(home, start)
    changes, end = [], dict(start, event="end")
    try:
        ops = ops or _memory_ops()
        piece = ops.slice_for(home, chars=SLICE_CHARS)
        end["through_before"] = getattr(piece, "through", None)
        # what a bounded slice saw and left out (topics seen / left, new
        # entries): the record never claims more coverage than it had
        end["coverage"] = getattr(piece, "coverage", None)
        if getattr(piece, "empty", False):
            end.update(result="no_change", tokens=0, summary="nothing new since the last pass")
            return end
        ops.begin(home, pass_id)
        account = accounts.for_cousin(home, root)
        env = accounts.account_env(account, root)
        tokens, summary, over = asyncio.run(_session(
            env, model, ops.prompt(piece), tool_server(home, pass_id, ops, changes),
            budget, client_factory))
        end.update(tokens=tokens, summary=summary)
        if over:
            ops.abandon(home, pass_id, "budget")
            end["result"] = "budget"
        else:
            ops.commit(home, pass_id, getattr(piece, "through", None))
            end["result"] = "done" if changes else "no_change"
            end["through_after"] = getattr(piece, "through", None)
    except Exception as err:  # noqa: BLE001 - every failure is a recorded result
        end.update(result="error", error="%s: %s" % (type(err).__name__, err))
        try:
            if ops is not None:
                ops.abandon(home, pass_id, end["error"])
        except Exception:  # noqa: BLE001 - the record says what failed first
            pass
    finally:
        end["changes"] = changes
        end["ended"] = time.time()
        _write(home, end)
    return end


async def _session(env, model, prompt, server, budget, client_factory):
    """(tokens, summary, over_budget). tools=[]: the dream server's tools
    are the only ones the session has."""
    from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, \
        ResultMessage, TextBlock
    cwd = tempfile.mkdtemp(prefix="cousin-dream-")
    options = ClaudeAgentOptions(
        cwd=cwd, model=model, env=env, setting_sources=[], tools=[],
        mcp_servers={"dream": server}, permission_mode="bypassPermissions",
        max_turns=MAX_TURNS,
        extra_args={"no-session-persistence": None})
    client = (client_factory or (lambda o: ClaudeSDKClient(options=o)))(options)
    tokens, texts, over, seen = 0, [], False, set()
    await client.connect()
    try:
        await client.query(prompt)
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                # one API response can arrive as several messages sharing
                # its id and its usage: count each response once
                mid = getattr(msg, "message_id", None)
                if mid is None or mid not in seen:
                    seen.add(mid)
                    tokens += _weighted(getattr(msg, "usage", None))
                texts += [b.text for b in msg.content or () if isinstance(b, TextBlock)]
                if tokens > budget and not over:
                    over = True
                    await client.interrupt()
            elif isinstance(msg, ResultMessage):
                total = _weighted(getattr(msg, "usage", None))
                tokens = max(tokens, total)
                over = over or tokens > budget
    finally:
        await client.disconnect()
    summary = " ".join(" ".join(texts[-1:]).split())[:500] if texts else ""
    return tokens, summary, over


def undo(home, pass_id, *, by=None, ops=None):
    """Reverse one pass's changes (dream_memory.undo) and log it as its own
    record. ValueError for a pass that is not on record or changed nothing."""
    ops = ops or _memory_ops()
    found = next((p for p in passes(home, limit=10000) if p["pass_id"] == pass_id), None)
    if found is None:
        raise ValueError("no dreaming pass %s" % pass_id)
    if not found.get("changes"):
        raise ValueError("pass %s changed nothing" % pass_id)
    if found.get("undone"):
        raise ValueError("pass %s is already undone" % pass_id)
    reverted = ops.undo(home, pass_id, found["changes"])
    _write(home, {"pass_id": pass_id, "event": "undo", "undone": time.time(),
                  "undone_by": by or "", "reverted": reverted})
    return reverted


# ---- the process --------------------------------------------------------------

def run_child(home, root, trigger, *, timeout=PASS_TIMEOUT_S):
    """Run one pass in a child process (the loops daemon's way): the auth
    scrub never touches the caller. The end record, or an error record."""
    import subprocess
    cmd = [sys.executable, "-m", "cousin_lib.dreaming", "--home", str(home),
           "--root", str(root), "--trigger", trigger]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              env=dict(os.environ, FRAMEWORK_ROOT=str(root)))
    except subprocess.TimeoutExpired:
        return {"result": "error", "error": "the pass ran past %ds" % timeout}
    lines = [l for l in proc.stdout.splitlines() if l.strip()]
    try:
        return json.loads(lines[-1])
    except (IndexError, ValueError):
        return {"result": "error", "error": (proc.stderr or "no verdict")[-400:]}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="cousin_lib.dreaming",
                                     description="run one dreaming pass for a cousin")
    parser.add_argument("--home", required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--trigger", default="manual")
    args = parser.parse_args(argv)
    from cousin_lib import accounts
    saved = {k: os.environ.pop(k) for k in accounts.AUTH_VARS if k in os.environ}
    try:
        end = run_pass(Path(args.home), Path(args.root), trigger=args.trigger)
    finally:
        os.environ.update(saved)
    out = {k: end.get(k) for k in ("pass_id", "result", "tokens", "error")}
    out["changes"] = len(end.get("changes") or ())
    print(json.dumps(out), flush=True)
    return 0 if end.get("result") in ("done", "no_change", "budget") else 1


if __name__ == "__main__":
    sys.exit(main())
