"""Session bookends: the hooks a cousin runs at session start and end.

A cousin's cousin.toml may carry a `[session]` table:

    [session]
    start_hooks = ["cousin-cycle inc --start",
                   {name = "banner", cmd = "hooks/session_init.sh"}]
    end_hooks = [{name = "sync-state", cmd = "cousin-sync-state"}]

Each entry is a bare command string (auto-named step-N by position) or
a table with `cmd` and an optional `name`. `cousin-session start|end`
runs the phase's hooks in order through the shell, each with
COUSIN_HOME, COUSIN_SLUG and SESSION_PHASE in its environment. A hook
that exits non-zero is reported and the rest still run; the phase's
exit is 1 if any failed. The last run is recorded at data/session.json
and `status` prints it.

The source framework parsed the hook list by hand with a bracket
counter and carried options tied to one install's media habits; here
the list is read by tomllib like every other key in cousin.toml, and
the media options do not ship.
"""
import argparse
import json
import os
import subprocess
import sys
import tomllib
from datetime import datetime
from pathlib import Path

HOOK_TIMEOUT_SECONDS = 300
OUTPUT_TAIL_CHARS = 200
PHASES = ("start", "end")


class SessionConfigError(Exception):
    """The [session] table cannot be used as written. Loud, because a
    hook list that silently parses to nothing is a bookend that never
    runs and nobody notices."""


class _NoContext(Exception):
    pass


def _now():
    return datetime.now().replace(microsecond=0).isoformat()


def _home():
    home = os.environ.get("COUSIN_HOME")
    if not home:
        raise _NoContext("cousin-session: set COUSIN_HOME")
    return Path(home)


def _state_path(home):
    return Path(home) / "data" / "session.json"


def _fresh_state():
    return {"last_start": None, "last_end": None, "running": False,
            "last_run": None}


def load_state(home):
    """The last-run record; a missing or torn file is a fresh state."""
    try:
        data = json.loads(_state_path(home).read_text())
    except (OSError, ValueError):
        return _fresh_state()
    state = _fresh_state()
    if isinstance(data, dict):
        state.update(data)
    return state


def _save_state(home, state):
    path = _state_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(path)


def _read_toml(home):
    path = Path(home) / "cousin.toml"
    if not path.is_file():
        return {}
    try:
        return tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as err:
        raise SessionConfigError("%s is unusable: %s" % (path, err))


def load_hooks(home, phase):
    """[{name, cmd}] for one phase, in file order. No table, no key, or
    no cousin.toml: an empty list. An entry that is neither a string
    nor a table with `cmd` is refused by name."""
    if phase not in PHASES:
        raise ValueError("phase must be one of %s" % (PHASES,))
    entries = _read_toml(home).get("session", {}).get(
        "%s_hooks" % phase, [])
    if not isinstance(entries, list):
        raise SessionConfigError(
            "[session] %s_hooks must be a list" % phase)
    hooks = []
    for index, entry in enumerate(entries, start=1):
        auto_name = "step-%d" % index
        if isinstance(entry, str):
            hooks.append({"name": auto_name, "cmd": entry})
            continue
        if isinstance(entry, dict) and isinstance(entry.get("cmd"), str):
            hooks.append({"name": str(entry.get("name") or auto_name),
                          "cmd": entry["cmd"]})
            continue
        label = entry.get("name", auto_name) if isinstance(entry, dict) \
            else auto_name
        raise SessionConfigError(
            "[session] %s_hooks entry %s has no cmd; each entry is a "
            "command string or {name, cmd}" % (phase, label))
    return hooks


def _slug(home):
    slug = os.environ.get("COUSIN_SLUG")
    if slug:
        return slug
    slug = _read_toml(home).get("cousin", {}).get("slug")
    return slug or Path(home).name


def _hook_env(home, phase):
    env = dict(os.environ)
    env["COUSIN_HOME"] = str(home)
    env["COUSIN_SLUG"] = _slug(home)
    env["SESSION_PHASE"] = phase
    return env


def _run_hook(hook, env, cwd):
    """One hook through the shell; (rc, tail of combined output)."""
    try:
        proc = subprocess.run(hook["cmd"], shell=True, env=env, cwd=cwd,
                              capture_output=True, text=True,
                              timeout=HOOK_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return 124, "timed out after %ds" % HOOK_TIMEOUT_SECONDS
    except OSError as err:
        return 127, "spawn failed: %s" % err
    return proc.returncode, (proc.stdout + proc.stderr).strip()[
        -OUTPUT_TAIL_CHARS:]


def run_phase(home, phase, skip=(), report=None):
    """Run one phase's hooks, record the run, return the run record.

    report: optional callable taking one line of progress; the CLI
    prints, the library default is silent.
    """
    home = Path(home)
    hooks = load_hooks(home, phase)
    skip = set(skip or ())
    env = _hook_env(home, phase)
    say = report or (lambda line: None)
    results = []
    counts = {"ok": 0, "failed": 0, "skipped": 0}
    say("running session %s (%d hook(s))" % (phase, len(hooks)))
    for hook in hooks:
        record = dict(hook)
        if hook["name"] in skip:
            record.update(rc=None, output="", skipped=True)
            counts["skipped"] += 1
            say("  [skip] %s" % hook["name"])
        else:
            rc, tail = _run_hook(hook, env, str(home))
            record.update(rc=rc, output=tail, skipped=False)
            if rc == 0:
                counts["ok"] += 1
                say("  [ok]   %s" % hook["name"])
            else:
                counts["failed"] += 1
                say("  [FAIL] %s (rc=%d) %s" % (hook["name"], rc, tail))
        results.append(record)
    at = _now()
    run = {"phase": phase, "at": at, "results": results, **counts}
    state = load_state(home)
    if phase == "start":
        state["last_start"] = at
        state["running"] = True
    else:
        state["last_end"] = at
        state["running"] = False
    state["last_run"] = run
    _save_state(home, state)
    say("session %s done: %d ok, %d failed, %d skipped"
        % (phase, counts["ok"], counts["failed"], counts["skipped"]))
    return run


def status(home):
    state = load_state(home)
    state["start_hooks"] = load_hooks(home, "start")
    state["end_hooks"] = load_hooks(home, "end")
    return state


def session_main(argv=None):
    parser = argparse.ArgumentParser(
        prog="cousin-session",
        description="run the bookend hooks from cousin.toml [session]")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for phase in PHASES:
        p = sub.add_parser(phase, help="run the %s_hooks in order" % phase)
        p.add_argument("--skip", action="append", default=[],
                       metavar="NAME", help="skip a named hook (repeatable)")
    sub.add_parser("status", help="print the last run and configured hooks")
    args = parser.parse_args(argv)
    try:
        home = _home()
    except _NoContext as err:
        print(str(err), file=sys.stderr)
        return 2
    try:
        if args.cmd == "status":
            print(json.dumps(status(home), indent=1, sort_keys=True))
            return 0
        run = run_phase(home, args.cmd, skip=args.skip, report=print)
    except SessionConfigError as err:
        print("cousin-session: %s" % err, file=sys.stderr)
        return 2
    return 0 if run["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(session_main())
