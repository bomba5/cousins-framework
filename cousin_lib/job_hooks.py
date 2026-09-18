"""Harness hook: subagents and background shells as rows in the jobs store.

Run by the agent harness as `python -m cousin_lib.job_hooks --home H
--root R`, wired per cousin by cousin_lib.harness_settings, with the
hook payload as JSON on stdin. What it records, in the same store the
console's Jobs view reads (cousin_lib.jobs):

- a subagent call (tool Agent, or Task under its older name):
  PreToolUse registers a `subagent` job titled by the call's
  description; PostToolUse closes it done with the start of the
  answer; PostToolUseFailure closes it failed (cancelled when it was
  an interrupt). An agent launched in the background returns at once
  with status async_launched: the row stays running with a note and
  closes when that agent's SubagentStop arrives.
- a Bash call with run_in_background: PreToolUse registers a `shell`
  job with the command. When the result only says the command was
  backgrounded, the row stays running with a note naming the task -
  marking it done would be a lie about a process still running.
  Foreground Bash calls are not recorded.

Pre and Post are correlated by tool_use_id, a background agent by its
agent id, through small files under <home>/data/job-hooks/.

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
import re
import sys
import time
import traceback
from datetime import datetime, timezone

SUBAGENT_TOOLS = ("Agent", "Task")
SHELL_TOOLS = ("Bash",)
STATE_DIR = "job-hooks"
LOG_NAME = "job-hooks.log"
STATE_MAX_AGE = 7 * 24 * 3600
SUMMARY_CHARS = 200
_SAFE_ID = re.compile(r"[^A-Za-z0-9_.-]")


# ------------------------------------------------------------ locations

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


def _state_file(home, prefix, ident):
    return (pathlib.Path(home) / "data" / STATE_DIR
            / ("%s-%s" % (prefix, _SAFE_ID.sub("_", str(ident))[:120])))


def _remember(home, prefix, ident, job_id):
    path = _state_file(home, prefix, ident)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("%d\n" % job_id)
    os.replace(tmp, path)


def _recall(home, prefix, ident, *, forget=True):
    if not ident:
        return None
    path = _state_file(home, prefix, ident)
    try:
        job_id = int(path.read_text().strip())
    except (OSError, ValueError):
        return None
    if forget:
        path.unlink(missing_ok=True)
    return job_id


def _prune(home):
    """Drop correlation files older than STATE_MAX_AGE: a Post that
    never came must not leave state behind forever."""
    state = pathlib.Path(home) / "data" / STATE_DIR
    cutoff = time.time() - STATE_MAX_AGE
    try:
        entries = list(state.iterdir())
    except OSError:
        return
    for entry in entries:
        try:
            if entry.stat().st_mtime < cutoff:
                entry.unlink()
        except OSError:
            pass


def _log(home, text):
    if not home:
        return
    try:
        path = pathlib.Path(home) / "data" / LOG_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as fh:
            fh.write("%s %s\n" % (datetime.now(timezone.utc).isoformat(
                timespec="seconds"), text))
    except OSError:
        pass


# ------------------------------------------------------------- payloads

def _one_line(text, limit=SUMMARY_CHARS):
    text = " ".join(str(text or "").split())
    return text[:limit]


def _response_text(response):
    """The readable part of a tool response: text blocks of a content
    list, else stdout, else the value itself."""
    if isinstance(response, dict):
        content = response.get("content")
        if isinstance(content, list):
            return " ".join(block.get("text", "") for block in content
                            if isinstance(block, dict))
        if isinstance(content, str):
            return content
        for key in ("stdout", "result", "output"):
            if isinstance(response.get(key), str):
                return response[key]
        return ""
    return str(response or "")


def _is_error(response):
    if isinstance(response, dict):
        if response.get("is_error") or response.get("isError"):
            return True
        return response.get("status") in ("error", "failed")
    return False


# --------------------------------------------------------------- events

def _pre(home, slug, payload):
    tool = payload.get("tool_name")
    tool_input = payload.get("tool_input") or {}
    tid = payload.get("tool_use_id")
    if not tid:
        return
    from cousin_lib import jobs
    if tool in SUBAGENT_TOOLS:
        prompt = str(tool_input.get("prompt") or "")
        title = (tool_input.get("description") or _one_line(prompt, 60)
                 or "subagent")
        desc = "%s: %s" % (tool_input.get("subagent_type") or "agent",
                           prompt[:400])
        job_id = jobs.register_job(kind="subagent", title=str(title),
                                   description=desc, spawned_by=slug)
    elif tool in SHELL_TOOLS and tool_input.get("run_in_background"):
        command = str(tool_input.get("command") or "")
        title = tool_input.get("description") or _one_line(command, 80)
        job_id = jobs.register_job(kind="shell", title=str(title),
                                   description="background shell",
                                   spawned_by=slug, command=command)
    else:
        return
    _remember(home, "tool", tid, job_id)
    _prune(home)


def _post(home, payload):
    tool = payload.get("tool_name")
    job_id = _recall(home, "tool", payload.get("tool_use_id"))
    if job_id is None:
        return
    from cousin_lib import jobs
    response = payload.get("tool_response")
    if tool in SUBAGENT_TOOLS:
        if isinstance(response, dict) and \
                response.get("status") == "async_launched":
            agent_id = response.get("agentId") or response.get("agent_id")
            note = ("launched in the background as agent %s; closes when"
                    " that agent stops" % agent_id)
            if agent_id:
                _remember(home, "agent", agent_id, job_id)
            else:
                note = ("launched in the background with no agent id; the"
                        " hook cannot see it finish")
            jobs.update_job(job_id, result_summary=note)
            return
        status = "failed" if _is_error(response) else "done"
        jobs.finish_job(job_id, status=status,
                        summary=_one_line(_response_text(response)) or status)
        return
    # A backgrounded shell: the result names the task, not an outcome.
    if isinstance(response, dict):
        task = (response.get("backgroundTaskId")
                or response.get("background_task_id"))
        if task:
            jobs.update_job(job_id, result_summary=(
                "backgrounded as task %s; the harness reports no completion"
                " to hooks, so this row stays running (the store reaps it"
                " after 24h)" % task))
            return
        if response.get("interrupted"):
            jobs.finish_job(job_id, status="failed", summary="interrupted")
            return
    status = "failed" if _is_error(response) else "done"
    jobs.finish_job(job_id, status=status,
                    summary=_one_line(_response_text(response)) or status)


def _failure(home, payload):
    job_id = _recall(home, "tool", payload.get("tool_use_id"))
    if job_id is None:
        return
    from cousin_lib import jobs
    status = "cancelled" if payload.get("is_interrupt") else "failed"
    jobs.finish_job(job_id, status=status,
                    summary=_one_line(payload.get("error")) or status)


def _subagent_stop(home, payload):
    job_id = _recall(home, "agent", payload.get("agent_id"))
    if job_id is None:
        return
    from cousin_lib import jobs
    jobs.finish_job(job_id, status="done", summary=_one_line(
        payload.get("last_assistant_message")) or "done")


def _is_tracked(payload):
    """Cheap filter before any import: the events and tools this hook
    acts on. A foreground Bash PreToolUse returns here."""
    event = payload.get("hook_event_name")
    tool = payload.get("tool_name")
    if event == "SubagentStop":
        return True
    if tool in SUBAGENT_TOOLS:
        return True
    if tool in SHELL_TOOLS:
        if event == "PreToolUse":
            return bool((payload.get("tool_input") or {})
                        .get("run_in_background"))
        return True
    return False


def handle(payload, home, root):
    """Act on one hook payload. Raises on a store error; main() is the
    catch-all."""
    if not _is_tracked(payload):
        return
    os.environ["COUSIN_HOME"] = str(home)
    os.environ["FRAMEWORK_ROOT"] = str(root)
    event = payload.get("hook_event_name")
    if event == "PreToolUse":
        try:
            from cousin_lib.config import CousinConfig
            slug = CousinConfig.load(home).slug
        except Exception:
            slug = pathlib.Path(home).name
        _pre(home, slug, payload)
    elif event == "PostToolUse":
        _post(home, payload)
    elif event == "PostToolUseFailure":
        _failure(home, payload)
    elif event == "SubagentStop":
        _subagent_stop(home, payload)


def main(argv=None):
    """Always 0. Nothing on stdout: the harness would read it back."""
    home = None
    try:
        parser = argparse.ArgumentParser(prog="cousin_lib.job_hooks",
                                         add_help=False)
        parser.add_argument("--home")
        parser.add_argument("--root")
        args, _rest = parser.parse_known_args(argv)
        home = args.home or os.environ.get("COUSIN_HOME")
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            raise ValueError("payload is not a JSON object")
        home, root = resolve_context(args, payload)
        if home is None:
            return 0
        handle(payload, home, root)
    except BaseException as err:  # noqa: a hook never breaks the harness
        if isinstance(err, KeyboardInterrupt):
            return 0
        _log(home, "%s: %s | %s" % (
            type(err).__name__, err,
            traceback.format_exc().strip().splitlines()[-1]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
