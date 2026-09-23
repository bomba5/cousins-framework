"""Harness hook: subagents and background shells as rows in the jobs store.

Run by the agent harness as `python -m cousin_lib.job_hooks --home H
--root R`, wired per cousin by cousin_lib.harness_settings, with the
hook payload as JSON on stdin. The logic lives in cousin_lib.recording;
this module is the harness entry.

A hook must never fail or block the tool call it watches: every path
exits 0, and an error is appended to <home>/data/job-hooks.log.
Home and root come from the arguments, else COUSIN_HOME and
FRAMEWORK_ROOT, else the payload's cwd (the nearest directory holding
a cousin.toml) and that home's grandparent.
"""
import argparse
import json
import os
import pathlib
import sys
import traceback

from cousin_lib.recording import (SUBAGENT_TOOLS, SHELL_TOOLS, STATE_DIR,  # noqa: F401
                                  LOG_NAME, handle, is_tracked as _is_tracked,
                                  pre as _pre, post as _post,
                                  failure as _failure,
                                  subagent_stop as _subagent_stop,
                                  wrap_background as _wrap,
                                  mint_log as _mint_log,
                                  remember as _remember, recall as _recall,
                                  prune as _prune, log as _log)


def find_home(start):
    """The nearest directory at or above `start` holding a cousin.toml,
    or None."""
    if not start:
        return None
    p = pathlib.Path(start)
    for candidate in (p, *p.parents):
        if (candidate / "cousin.toml").is_file():
            return candidate
    return None


def resolve_context(args, payload):
    """(home, root) from the arguments, the environment, then the
    payload's cwd. home is None when nothing leads to one."""
    home = (args.home or os.environ.get("COUSIN_HOME")
            or find_home(payload.get("cwd")))
    if not home:
        return None, None
    home = pathlib.Path(home)
    root = args.root or os.environ.get("FRAMEWORK_ROOT") or home.parent.parent
    return home, pathlib.Path(root)


def close_from_trap(job_id, rc):
    """Called by the trap: done on 0, failed otherwise, with the code."""
    from cousin_lib import jobs
    row = jobs.get_job(job_id)
    if row is None or row.get("status") != "running":
        return
    status = "done" if rc == 0 else "failed"
    jobs.finish_job(job_id, status=status, exit_code=rc,
                    summary="exit %d" % rc)


def main(argv=None):
    """Always 0. Stdout carries only the harness's hook output (the
    rewritten command for a background shell); the harness reads it."""
    home = None
    try:
        parser = argparse.ArgumentParser(prog="cousin_lib.job_hooks",
                                         add_help=False)
        parser.add_argument("--home")
        parser.add_argument("--root")
        parser.add_argument("--close", type=int)
        parser.add_argument("--rc", type=int, default=0)
        args, _rest = parser.parse_known_args(argv)
        home = args.home or os.environ.get("COUSIN_HOME")
        if args.close is not None:
            if args.home:
                os.environ["COUSIN_HOME"] = args.home
            if args.root:
                os.environ["FRAMEWORK_ROOT"] = args.root
            close_from_trap(args.close, args.rc)
            return 0
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            raise ValueError("payload is not a JSON object")
        home, root = resolve_context(args, payload)
        if home is None:
            return 0
        output = handle(payload, home, root)
        if output:
            sys.stdout.write(json.dumps(output))
            sys.stdout.flush()
    except BaseException as err:  # noqa: a hook never breaks the harness
        if isinstance(err, KeyboardInterrupt):
            return 0
        _log(home, "%s: %s | %s" % (
            type(err).__name__, err,
            traceback.format_exc().strip().splitlines()[-1]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
