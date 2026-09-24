"""cousin-migrate: move one cousin from the tmux lane to the SDK runner
(master plan phase 7 tasks 7-8; the runbook is docs/migrating.md).

Nothing here runs on its own. A merge, an upgrade or a boot never
migrates a cousin: only the operator's `cousin-migrate apply <slug>
--yes` does, one cousin at a time, and `cousin-migrate rollback <slug>
--yes` undoes it.

  plan      the checks and the steps; writes nothing (the default look).
            The cousin must be RUNNING on the tmux lane: migrating a
            stopped cousin would start it (the supervisor starts every
            runner cousin), so it is started first or left alone.
  apply     the steps, in order, stopping at the first that fails:
              close    a clean stop of the tmux session (flip.close_session:
                       the handoff, the transcript mined, the generation bumped)
              import   the agent CLI's own auto-memory folded in
                       (memory_import.apply: idempotent, a baseline first)
              toml     cousin.toml [agent] runner = "sdk" (and the account),
                       only once the tmux session is still down
              start    the migration-day boot packet archived (the runner
                       boots on its own digest), the review gate's cursor
                       opened afresh (what the cousin wrote on the tmux lane
                       is not the gate's), the supervisor asked to start
                       the runner, and the cousin's chat server started (the
                       supervisor runs none: a peer's `cousin-chat send`,
                       an MCP send and a hive tell-home all reach the inbox
                       through it)
              verify   the runner child stays `running` and holds its lock
                       for STABLE_S, and the chat server answers /health for
                       the slug
            data/migration.json holds the prior cousin.toml, its exact
            bytes and mode, before the first step, and every step's outcome
  rollback  undo exactly the steps that ran, and refuse what would be
            unsafe: a record already rolled back, inbox rows still
            waiting (nobody reads the inbox on the tmux lane) or an inbox
            that cannot be read (both unless --force), a runner that is not
            down after its stop. When `toml` ran: stop the runner, wait
            until it is down, put the saved bytes and mode back, have the
            supervisor rescan. Then a fresh boot packet (the cousin's state
            now, not the migration day's) and the tmux session started,
            since the cousin was running when `apply` began, unless it
            already runs; last, the supervisor's hold on the runner
            (`run/held`, which its stop wrote) is released: a tmux cousin
            carries none. A step that fails is recorded and reported.
  check     the exit criterion over the runner's own records since the
            migration (or --since): inbox rows that never reached done,
            tool calls with no recorded result, recorder hooks that failed,
            and whether the chat server answers

The supervisor interface assumed (phase 6, round 2 as its drafter stated
it, 9fcf52a): a stop through spawn.stop_cousin waits until the child is
down (up to 35 s) unless told otherwise; `start {slug}` clears the stop's
hold marker; `reload` never restarts a stopped child; snapshot()'s
children rows carry `state` (running, backoff, failing, stopped); a
runner's exit 5 (another runner holds the lock) reads `backoff`.

Every live action is a keyword argument (the tests inject all of them);
_live() gives the real ones."""
import argparse
import base64
import importlib.util
import json
import os
import re
import sqlite3
import sys
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path

RECORD = "data/migration.json"
STEPS = ("close", "import", "toml", "start", "verify")
STALE_S = 3600.0          # an inbox row not done after this long is a lost message
TOOL_GRACE_S = 600.0      # a tool call this recent may still be running
VERIFY_S = 90.0           # how long verify waits for a stable runner
STABLE_S = 10.0           # how long the runner must stay up to count as started
DOWN_S = 60.0             # how long rollback waits for a stopped runner to let go of its lock
PRE_RUNNER_BOOT = "data/pending-boot.pre-runner.json"


class MigrateError(Exception):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat()


def _agent(home):
    try:
        data = tomllib.loads((Path(home) / "cousin.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError) as err:
        raise MigrateError("cannot read %s/cousin.toml: %s" % (home, err))
    agent = data.get("agent")
    return agent if isinstance(agent, dict) else {}


def read_record(home):
    try:
        data = json.loads((Path(home) / RECORD).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_record(home, rec):
    path = Path(home) / RECORD
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rec, indent=1) + "\n")
    os.replace(tmp, path)


# ------------------------------------------------------------ plan

def _check(name, ok, detail):
    return {"check": name, "ok": bool(ok), "detail": detail}


def plan(home, *, root, account=None, auth_check, supervisor_up, sdk_ok, tmux_alive,
         **_unused):
    """{"slug", "checks": [...], "steps", "ready"}; writes nothing."""
    from cousin_lib import accounts, memory_import
    home, root = Path(home), Path(root)
    checks = []
    agent = _agent(home)
    lane_ok = agent.get("runner") not in ("sdk", "fake")
    checks.append(_check("lane", lane_ok, "on the tmux lane" if lane_ok else
                         "already on the runner lane ([agent] runner = %r)" % agent.get("runner")))
    running = lane_ok and tmux_alive(home)
    if lane_ok:
        checks.append(_check("running", running, "its tmux session is up" if running else
                             "it is stopped: start it first (a migrated cousin runs; the"
                             " supervisor starts every runner cousin)"))
    rec = read_record(home)
    open_rec = rec is not None and rec.get("state") in ("applying", "failed", "migrated")
    checks.append(_check("record", not open_rec, "no migration in progress" if not open_rec else
                         "%s says %s: `cousin-migrate rollback` first" % (RECORD, rec.get("state"))))
    name = account or agent.get("account") or accounts.HOST
    try:
        known = accounts.load(root)
        if name != accounts.HOST and name not in known:
            raise accounts.AccountsError("account %r is not in config/accounts.toml" % name)
        code, line = auth_check(home, root, name)
        checks.append(_check("account", code == 0, line))
    except accounts.AccountsError as err:
        checks.append(_check("account", False, str(err)))
    up = supervisor_up(root)
    checks.append(_check("supervisor", up, "a cousin-supervisor answers for %s" % root if up else
                         "no cousin-supervisor runs for %s: start it (`cousin-supervisor run`,"
                         " or its unit)" % root))
    has_sdk = sdk_ok()
    checks.append(_check("sdk", has_sdk, "claude-agent-sdk is installed" if has_sdk else
                         "claude-agent-sdk is not installed: pip install -e '.[sdk]'"))
    try:
        rows = memory_import.plan(home, root=root)
        conflicts = [r["name"] for r in rows if r["action"] == "conflict"]
        todo = sum(r["action"] in ("import", "update") for r in rows)
        checks.append(_check("import", not conflicts,
                             "%d auto-memory file(s) to fold in" % todo if not conflicts else
                             "merge by hand first (`cousin-memory import-auto`): %s"
                             % ", ".join(conflicts)))
    except Exception as err:  # noqa: BLE001 - ManifestError, an unreadable source
        checks.append(_check("import", False, "%s: %s" % (type(err).__name__, err)))
    return {"slug": home.name, "account": name, "checks": checks, "steps": list(STEPS),
            "ready": all(c["ok"] for c in checks)}


# ------------------------------------------------------------ apply

def set_agent_keys(text, values):
    """cousin.toml's text with [agent] `values` set, the rest untouched:
    a key replaced in the [agent] table's body, else added under its
    header, else an [agent] table appended. Line endings are kept."""
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = ['%s = %s' % (k, json.dumps(v)) for k, v in values.items()]
    header = re.search(r"(?m)^\[agent\][ \t]*\r?$", text)
    if header is None:
        return text.rstrip("\r\n") + nl + nl + "[agent]" + nl + nl.join(lines) + nl
    start = header.end()
    nxt = re.search(r"(?m)^\s*\[", text[start:])
    end = start + nxt.start() if nxt else len(text)
    body = text[start:end]
    for key, line in zip(values, lines):
        key_re = re.compile(r"(?m)^%s\s*=[^\r\n]*" % re.escape(key))
        if key_re.search(body):
            body = key_re.sub(lambda m: line, body, count=1)
        else:
            body = nl + line + body
    return text[:start] + body + text[end:]


def _write_toml(home, data, mode):
    """Write cousin.toml's bytes atomically with `mode`; never a file that
    does not parse."""
    tomllib.loads(data.decode("utf-8"))
    path = Path(home) / "cousin.toml"
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_bytes(data)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def apply(home, *, root, account=None, close, import_auto, start, verify, tmux_alive,
          **checks):
    """Run the steps; the record, with state `migrated` or `failed`.
    MigrateError when the plan is not ready (nothing is written then)."""
    from cousin_lib import accounts
    home, root = Path(home), Path(root)
    p = plan(home, root=root, account=account, tmux_alive=tmux_alive, **checks)
    if not p["ready"]:
        raise MigrateError("not ready: %s" % "; ".join(
            c["detail"] for c in p["checks"] if not c["ok"]))
    path = home / "cousin.toml"
    prior = path.read_bytes()
    mode = path.stat().st_mode & 0o7777
    rec = {"slug": home.name, "state": "applying", "started_at": _now(), "account": p["account"],
           "was_running": True, "prior_toml_b64": base64.b64encode(prior).decode("ascii"),
           "prior_mode": mode, "steps": []}
    _write_record(home, rec)
    values = {"runner": "sdk"}
    if p["account"] != accounts.HOST:
        values["account"] = p["account"]

    def still_down():
        if tmux_alive(home):
            raise MigrateError("the tmux session is up again (a flip, a schedule or a"
                               " console start): stop it, then roll back and apply again")

    def run(step):
        if step == "close":
            out = close(home.name, root)
            return bool(out.get("ok")), out.get("error") or "closed cleanly"
        if step == "import":
            return True, json.dumps(import_auto(home, root))
        if step == "toml":
            still_down()
            text = set_agent_keys(prior.decode("utf-8"), values)
            _write_toml(home, text.encode("utf-8"), mode)
            return True, "[agent] %s" % ", ".join("%s = %r" % kv for kv in values.items())
        if step == "start":
            still_down()
            boot_file = home / "data" / "pending-boot.json"
            if boot_file.exists():
                os.replace(boot_file, home / PRE_RUNNER_BOOT)
            from cousin_lib import review_gate
            review_gate.begin(home, reset=True)
            start(home, root)
            return True, ("the supervisor was asked to start runner:%s and the chat server"
                          " was started" % home.name)
        out = verify(home, root)
        return bool(out.get("ok")), out.get("detail") or ""

    for step in STEPS:
        try:
            ok, detail = run(step)
        except Exception as err:  # noqa: BLE001 - a step's failure is recorded, then we stop
            ok, detail = False, "%s: %s" % (type(err).__name__, err)
        rec["steps"].append({"step": step, "ok": ok, "detail": detail, "at": _now()})
        if not ok:
            rec["state"] = "failed"
            _write_record(home, rec)
            return rec
        _write_record(home, rec)
    rec["state"], rec["migrated_at"] = "migrated", _now()
    _write_record(home, rec)
    return rec


def verify_runner(home, root, *, snapshot, alive, health, stable_s=STABLE_S, timeout=VERIFY_S,
                  sleep=time.sleep, clock=time.monotonic):
    """The default verify: the supervisor's `runner:<slug>` row reads
    `running` and the runner holds the cousin's lock, unbroken for
    `stable_s` (a runner that exits at once - a lock held by another
    runner, a missing secret, a broken policy - never passes), then the
    chat server answers /health for the slug."""
    name = "runner:%s" % Path(home).name
    deadline = clock() + timeout
    since, state = None, None
    while clock() < deadline:
        row = ((snapshot(root) or {}).get("children") or {}).get(name) or {}
        state = row.get("state")
        if state == "running" and alive(home):
            since = since if since is not None else clock()
            if clock() - since >= stable_s:
                break
        else:
            since = None
        sleep(1.0)
    else:
        return {"ok": False, "detail": "%s did not stay running for %ds within %ds (last: %s)"
                % (name, stable_s, timeout, state)}
    ok, detail = health(home)
    return {"ok": ok, "detail": ("%s running %ds; " % (name, stable_s)) + detail}


# ------------------------------------------------------------ rollback

def _inbox_rows(home):
    """[(state, outcome, created_at)] read-only; [] when there is no inbox;
    None when it cannot be read (locked, broken): never "nothing waits"."""
    path = Path(home) / "data" / "inbox.db"
    if not path.exists():
        return []
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=5)
        try:
            return conn.execute("SELECT state, outcome, created_at FROM inbox").fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def fresh_packet(home):
    """A boot packet of the cousin's state now, pending for the next tmux
    start (flip.close_session's tail): the rollback's tmux session must
    not boot on the migration day's packet."""
    from cousin_lib import boot, spawn
    home = Path(home)
    generation = boot.bump_generation(home)
    packet = boot.assemble(home.name, home, generation=generation)
    path = home / "data" / ("boot-packet-gen-%04d.md" % generation)
    path.write_text(packet["text"])
    spawn.pending_boot_path(home).write_text(json.dumps({
        "generation": generation, "packet": str(path), "written_at": _now()}))
    return generation


def rollback(home, *, root, stop, runner_alive, reload, start_tmux, tmux_alive, release,
             new_packet=fresh_packet, force=False, sleep=time.sleep, clock=time.monotonic,
             **_unused):
    """Back to the tmux lane, undoing only what `apply` did. The lane is
    judged by the file too, not only the record: an interrupted `apply`
    can have written the file and not yet recorded it."""
    home, root = Path(home), Path(root)
    rec = read_record(home)
    if rec is None or "prior_toml_b64" not in rec:
        raise MigrateError("no %s with the prior cousin.toml: nothing to roll back" % RECORD)
    if rec.get("state") == "rolled_back":
        raise MigrateError("already rolled back at %s; nothing to undo" % rec.get("rolled_back_at"))
    ran = {s["step"] for s in rec.get("steps", []) if s.get("ok")}
    prior = base64.b64decode(rec["prior_toml_b64"])
    flipped = "toml" in ran or (home / "cousin.toml").read_bytes() != prior
    rows = _inbox_rows(home)
    if rows is None and not force:
        raise MigrateError("data/inbox.db cannot be read, so whether rows wait is unknown;"
                           " look at it, or pass --force")
    waiting = sum(1 for state, _o, _c in rows or () if state != "done")
    if waiting and not force:
        raise MigrateError("%d inbox row(s) still wait for the runner; on the tmux lane nobody"
                           " reads them. Let them finish, or pass --force" % waiting)
    steps = []

    def step(name, fn, detail=None):
        try:
            out = fn()
        except Exception as err:  # noqa: BLE001 - recorded, then the operator decides
            rec.setdefault("rollback_attempts", []).append(
                {"at": _now(), "failed": name, "error": "%s: %s" % (type(err).__name__, err),
                 "steps": steps})
            _write_record(home, rec)
            raise MigrateError("rollback step %s failed: %s: %s (recorded in %s)"
                               % (name, type(err).__name__, err, RECORD))
        steps.append({"step": name, "detail": detail(out) if detail else "done", "at": _now()})
        return out

    if flipped:
        step("stop", lambda: stop(home, root), json.dumps)
        deadline = clock() + DOWN_S
        while runner_alive(home) and clock() < deadline:
            sleep(1.0)
        if runner_alive(home):
            rec.setdefault("rollback_attempts", []).append(
                {"at": _now(), "refused": "the runner still holds its lock after %ds" % DOWN_S})
            _write_record(home, rec)
            raise MigrateError("the runner still holds its lock after %ds; nothing restored."
                               " Stop it (`cousin-supervisor stop %s`), then roll back again"
                               % (DOWN_S, home.name))
        step("restore", lambda: _write_toml(home, prior, int(rec["prior_mode"])),
             lambda _: "cousin.toml as it was, byte for byte")
        step("reload", lambda: reload(root))
    if "close" in ran or flipped:
        if flipped:
            step("packet", lambda: new_packet(home), lambda g: "generation %s" % g)
        if rec.get("was_running"):
            if tmux_alive(home):
                steps.append({"step": "start_tmux", "detail": "its tmux session is already up",
                              "at": _now()})
            else:
                step("start_tmux", lambda: start_tmux(home, root))
    else:
        steps.append({"step": "none", "detail": "close never ran: nothing was changed",
                      "at": _now()})
    step("release", lambda: release(home),
         lambda _: "no runner hold left on the tmux cousin (run/held)")
    rec.update(state="rolled_back", rolled_back_at=_now(), rollback_steps=steps,
               waiting_at_rollback=waiting)
    _write_record(home, rec)
    return rec


# ------------------------------------------------------------ check

def _stamp(value):
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except ValueError:
        return None


def check(home, *, since=None, now=None, health=None):
    """The exit criterion, from the runner's own records since `since`
    (epoch seconds; default: the migration's end, else everything):
    every inbox row reached done (`stale`: open longer than STALE_S),
    every tool call has a recorded result (`unrecorded`: none after
    TOOL_GRACE_S), no recorder hook failed (`hook_errors`, the jobs rows a
    failed recorder never wrote), an inbox that can be read, and, with
    `health`, a chat server that answers."""
    home = Path(home)
    now = time.time() if now is None else now
    if since is None:
        rec = read_record(home) or {}
        since = _stamp(rec.get("migrated_at")) if rec.get("state") == "migrated" else None
    since = since or 0.0
    inbox = {"done": 0, "failed": 0, "open": 0, "stale": 0}
    rows = _inbox_rows(home)
    for state, outcome, created in rows or ():
        if float(created or 0.0) < since:
            continue
        if state == "done":
            inbox["done"] += 1
            inbox["failed"] += outcome == "failed"
        else:
            inbox["open"] += 1
            inbox["stale"] += (now - float(created or now)) > STALE_S
    calls, results, hook_errors = {}, set(), []
    for path in sorted((home / "data" / "stream").glob("*.jsonl")):
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            ts = float(ev.get("ts") or now)
            if ts < since:
                continue
            payload = ev.get("payload") or {}
            kind = ev.get("kind")
            if kind == "tool" and payload.get("id"):
                calls[payload["id"]] = ts
            elif kind == "tool_result" and payload.get("tool_use_id"):
                results.add(payload["tool_use_id"])
            elif kind == "hook" and payload.get("error") and \
                    str(payload.get("event") or "").startswith(("PreToolUse", "PostToolUse")):
                hook_errors.append("%s: %s" % (payload.get("event"), payload.get("error")))
    unrecorded = sorted(i for i, ts in calls.items()
                        if i not in results and now - ts > TOOL_GRACE_S)
    out = {"since": since, "inbox": inbox, "inbox_readable": rows is not None,
           "tool_calls": len(calls), "unrecorded": unrecorded, "hook_errors": hook_errors}
    ok = rows is not None and inbox["stale"] == 0 and not unrecorded and not hook_errors
    if health is not None:
        out["chat_ok"], out["chat"] = health(home)
        ok = ok and out["chat_ok"]
    out["ok"] = ok
    return out


# ------------------------------------------------------------ the live actions

def chat_health(home):
    """(ok, detail): the cousin's chat server answers /health for its slug."""
    import urllib.request
    from cousin_lib.config import CousinConfig
    try:
        port = CousinConfig.load(home).chat_port
    except Exception as err:  # noqa: BLE001 - reported, never raised
        return False, "no chat port: %s" % err
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/health" % port, timeout=3) as resp:
            body = json.loads(resp.read().decode("utf-8") or "{}")
    except Exception as err:  # noqa: BLE001 - down is the answer
        return False, "the chat server on :%s does not answer: %s" % (port, err)
    if body.get("slug") != Path(home).name:
        return False, "port %s answers for %r, not %r" % (port, body.get("slug"), Path(home).name)
    return True, "the chat server answers on :%s" % port


def _live():
    from cousin_lib import accounts, delivery, flip, memory_import, spawn, supervisor
    from cousin_lib.config import CousinConfig

    def auth_check(home, root, name):
        acct = accounts.load(root).get(name) or accounts.Account(
            accounts.HOST, "claude-login", None, None, implicit=True)
        return accounts._check_account(acct, root, via=Path(home).name)

    def tmux_alive(home):
        return flip._session_alive(CousinConfig.load(home).tmux_session, "tmux", None)

    def import_auto(home, root):
        rows = memory_import.apply(home, root=root)
        counts = {}
        for r in rows:
            counts[r["action"]] = counts.get(r["action"], 0) + 1
        return counts

    def start(home, root):
        spawn.start_cousin(home, agent_cmd="", root=root)
        spawn._chat_server_unless_live(home)

    def reload(root):
        try:
            supervisor.request(root, "reload")
        except supervisor.SupervisorUnavailable:
            pass

    return dict(
        auth_check=auth_check,
        supervisor_up=lambda root: supervisor.snapshot(root) is not None,
        sdk_ok=lambda: importlib.util.find_spec("claude_agent_sdk") is not None,
        tmux_alive=tmux_alive,
        close=lambda slug, root: flip.close_session(slug),
        import_auto=import_auto,
        start=start,
        verify=lambda home, root: verify_runner(home, root, snapshot=supervisor.snapshot,
                                                alive=delivery.is_alive, health=chat_health),
        stop=lambda home, root: spawn.stop_cousin(home, root=root),
        runner_alive=delivery.is_alive,
        release=supervisor.release,
        reload=reload,
        start_tmux=lambda home, root: spawn.start_cousin(
            home, agent_cmd=flip._read_agent_cmd_template(Path(root)), root=root))


# ------------------------------------------------------------ the CLI

def _print_plan(p):
    for c in p["checks"]:
        print("  %s %-10s %s" % ("ok " if c["ok"] else "NO ", c["check"], c["detail"]))
    print("steps: %s" % " -> ".join(p["steps"]))
    print("%s: %s" % (p["slug"], "ready (run: cousin-migrate apply %s --yes)" % p["slug"]
                      if p["ready"] else "not ready"))


def migrate_main(argv=None):
    from cousin_lib.config import FrameworkConfig
    parser = argparse.ArgumentParser(
        prog="cousin-migrate",
        description="move one cousin from the tmux lane to the SDK runner, and back")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "apply"):
        p = sub.add_parser(name)
        p.add_argument("slug")
        p.add_argument("--account", default=None,
                       help="the config/accounts.toml account it runs on (default: the host login)")
        if name == "apply":
            p.add_argument("--yes", action="store_true", help="really run the steps")
    p = sub.add_parser("rollback")
    p.add_argument("slug")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--force", action="store_true",
                   help="roll back with inbox rows waiting, or an inbox that cannot be read")
    p = sub.add_parser("check")
    p.add_argument("slug")
    p.add_argument("--since", default=None,
                   help="an ISO time (default: when the migration finished)")
    p.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    root = FrameworkConfig.resolve().root
    home = root / "cousins" / args.slug
    if not (home / "cousin.toml").exists():
        print("error: no cousin %r under %s" % (args.slug, root), file=sys.stderr)
        return 2
    if args.cmd == "check":
        since = _stamp(args.since) if args.since else None
        if args.since and since is None:
            print("error: --since %r is not an ISO time" % args.since, file=sys.stderr)
            return 2
        c = check(home, since=since, health=chat_health)
        if args.json:
            print(json.dumps(c, indent=1))
        else:
            if not c["inbox_readable"]:
                print("inbox: UNREADABLE (data/inbox.db)")
            print("inbox: %(done)d done (%(failed)d failed), %(open)d open, %(stale)d stale"
                  % c["inbox"])
            print("tool calls: %d, unrecorded: %s" % (c["tool_calls"],
                                                      ", ".join(c["unrecorded"]) or "none"))
            print("recorder hook errors: %s" % ("; ".join(c["hook_errors"]) or "none"))
            print("chat server: %s" % c["chat"])
            print("ok" if c["ok"] else "NOT ok")
        return 0 if c["ok"] else 1
    if args.cmd in ("apply", "rollback") and not args.yes:
        print("error: %s changes a live cousin; run `cousin-migrate plan %s` first, then"
              " pass --yes" % (args.cmd, args.slug), file=sys.stderr)
        return 2
    live = _live()
    try:
        if args.cmd == "plan":
            p = plan(home, root=root, account=args.account, **live)
            _print_plan(p)
            return 0 if p["ready"] else 1
        if args.cmd == "apply":
            rec = apply(home, root=root, account=args.account, **live)
        else:
            rec = rollback(home, root=root, force=args.force, **live)
    except MigrateError as err:
        print("error: %s" % err, file=sys.stderr)
        return 2
    for s in rec.get("steps", []) if args.cmd == "apply" else rec.get("rollback_steps", []):
        print("  %s %-10s %s" % ("ok " if s.get("ok", True) else "NO ", s["step"], s["detail"]))
    print("%s: %s" % (args.slug, rec["state"]))
    if rec["state"] == "failed":
        print("undo with: cousin-migrate rollback %s --yes" % args.slug)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(migrate_main())
