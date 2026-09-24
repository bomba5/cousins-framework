"""Per-cousin harness project settings: <home>/.claude/settings.json.

A cousin's agent runs with the cousin home as its project directory, so
the harness reads this file for every session the cousin has. Writing
it per cousin is what makes the hooks that fire in a cousin's session
ITS hooks - named by absolute path, with the home written into each
command so a hook never depends on the agent's environment - and what
approves the cousin's own `cousin` MCP server without a hand edit.

The writer merges. Keys it does not own are kept; a hook entry it wrote
before (recognised by its script name or module, wherever the checkout
lived then) is replaced, never doubled; a second run writes the same
bytes. A file that is not a JSON object is refused, never clobbered.
"""
import json
import os
import pathlib
import shlex
import sys
import tempfile

from cousin_lib.mcp_server import SERVER_NAME

PROJECT_SETTINGS = pathlib.Path(".claude") / "settings.json"

# (harness event, script under hooks/) for the three bookend hooks.
SHELL_HOOKS = (("SessionStart", "session_init.sh"),
               ("PreCompact", "pre_compact.sh"),
               ("Stop", "session_checkpoint.sh"))

# The job-tracking hook (cousin_lib.job_hooks): one module, several
# events. A matcher of None means every tool (or, for SubagentStop, an
# event that is not about one tool). PreToolUse needs only subagents and
# shells (it registers their jobs); PostToolUse and PostToolUseFailure
# take every tool, because each call also lands in the cousin's activity
# log (cousin_lib.activity). PostToolUse fires only on success; a failed
# or interrupted call arrives as PostToolUseFailure, and an agent
# launched in the background finishes as a SubagentStop.
JOB_HOOK_MODULE = "cousin_lib.job_hooks"
JOB_HOOK_MATCHERS = ("Agent|Task", "Bash")
JOB_HOOK_EVENTS = (("PreToolUse", JOB_HOOK_MATCHERS),
                   ("PostToolUse", (None,)),
                   ("PostToolUseFailure", (None,)),
                   ("SubagentStop", (None,)))
JOB_HOOK_TIMEOUT = 10

# The tmux kind (phase 11 I7, P11-8, P11-12): its pane runs the host's
# interactive CLI, so its project settings carry the kind's switches (no
# built-in editor, no auto-continue at a limit, no auto-compaction, no
# attribution, no remote control), the policy's deny_tools as the CLI's
# permissions.deny, and the hooks that bridge the pane to its runner.
TMUX_KEYS = {"editorMode": "normal", "autoContinueAtUsageLimit": False,
             "autoCompactEnabled": False, "attribution": {"commit": "", "pr": ""},
             "remoteControlAtStartup": False}
TMUX_HOOK_MODULE = "cousin_lib.runner.tmux_hook"
TMUX_HOOK_EVENTS = ("UserPromptSubmit", "Stop", "Notification", "SessionStart")
# What the kind added, so remove_kind_settings undoes exactly that.
TMUX_OWNED = pathlib.Path(".claude") / "cousin-tmux-owned.json"


class SettingsError(Exception):
    """The settings file cannot be merged; the message says why."""


def settings_path(home):
    return pathlib.Path(home) / PROJECT_SETTINGS


def hooks_dir():
    """The checkout's hooks/ directory, from this module's own location
    (never the working directory): <checkout>/cousin_lib/.. /hooks."""
    return pathlib.Path(__file__).resolve().parents[1] / "hooks"


def _owned(command):
    """True for a hook command this module writes, whichever checkout
    or interpreter it named when it was written."""
    if not isinstance(command, str):
        return False
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    if not argv:
        return False
    head = pathlib.PurePath(argv[0])
    if head.parent.name == "hooks" and head.name in {
            s for _e, s in SHELL_HOOKS}:
        return True
    return "-m" in argv and (JOB_HOOK_MODULE in argv or TMUX_HOOK_MODULE in argv)


def desired_hooks(home, *, root, python=None, hooks_root=None):
    """The hook groups this cousin's settings should carry, per event,
    and the shell scripts that could not be found. The job hook runs
    under `python` (default: this interpreter, so the hook imports the
    same install that wrote it)."""
    # Absolute whatever the caller passed: the harness runs these
    # commands from the cousin home, where a relative path misleads.
    home = pathlib.Path(os.path.abspath(home))
    root = os.path.abspath(root)
    hooks_root = pathlib.Path(hooks_root) if hooks_root else hooks_dir()
    python = python or sys.executable
    wanted, missing = {}, []
    for event, script in SHELL_HOOKS:
        path = hooks_root / script
        if not path.is_file():
            missing.append(str(path))
            continue
        wanted.setdefault(event, []).append({"hooks": [{
            "type": "command",
            "command": shlex.join([str(path), str(home)])}]})
    job_cmd = shlex.join([str(python), "-m", JOB_HOOK_MODULE,
                          "--home", str(home), "--root", str(root)])
    for event, matchers in JOB_HOOK_EVENTS:
        for matcher in matchers:
            group = {"hooks": [{"type": "command", "command": job_cmd,
                                "timeout": JOB_HOOK_TIMEOUT}]}
            if matcher is not None:
                group = {"matcher": matcher, **group}
            wanted.setdefault(event, []).append(group)
    return wanted, missing


def _load(path):
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as err:
        raise SettingsError("%s is unreadable, left as it is: %s"
                            % (path, err))
    if not isinstance(data, dict):
        raise SettingsError("%s is not a JSON object, left as it is"
                            % path)
    return data


def _strip_owned(groups):
    """The groups with every hook this module owns removed; a group
    emptied by that goes too, a group with foreign hooks stays."""
    kept = []
    for group in groups:
        if not isinstance(group, dict):
            kept.append(group)
            continue
        hooks = group.get("hooks")
        if not isinstance(hooks, list):
            kept.append(group)
            continue
        rest = [h for h in hooks
                if not (isinstance(h, dict) and _owned(h.get("command")))]
        if rest:
            kept.append(dict(group, hooks=rest))
        elif not hooks:
            kept.append(group)
    return kept


def _tmux_hooks(home, python):
    home = pathlib.Path(os.path.abspath(home))
    python = python or sys.executable
    return {event: [{"hooks": [{"type": "command", "command": shlex.join(
        [str(python), "-m", TMUX_HOOK_MODULE, "--home", str(home), event])}]}]
        for event in TMUX_HOOK_EVENTS}


def _policy_deny(home):
    """policy.toml's deny_tools, as the CLI's permissions.deny entries.
    deny_bash_patterns are regular expressions, which the CLI's rules
    cannot say; the pane's PreToolUse hook is not ours to add (T3 notes)."""
    from cousin_lib.runner.policy import Policy
    return list(Policy.load(home).deny_tools)


def apply_project_settings(home, *, root, python=None, hooks_root=None, kind=None):
    """Create or merge <home>/.claude/settings.json: this cousin's hooks
    and its `cousin` MCP server approved; for kind "tmux", also the kind's
    keys, the policy's deny rules and the bridge hooks (TMUX_*). Returns
    {path, events, missing}. Idempotent."""
    home = pathlib.Path(home)
    path = settings_path(home)
    data = _load(path)
    owned = None
    if kind == "tmux":
        owned = _read_owned(home)
        for key, value in TMUX_KEYS.items():
            data[key] = value
        perms = data.get("permissions", {})
        if not isinstance(perms, dict):
            raise SettingsError("%s: permissions is not an object, left as it is" % path)
        deny = perms.get("deny", [])
        if not isinstance(deny, list):
            raise SettingsError("%s: permissions.deny is not a list, left as it is" % path)
        added = [d for d in owned.get("deny", []) if d in deny]
        deny = [d for d in deny if d not in added]
        mine = [d for d in _policy_deny(home) if d not in deny]
        perms["deny"] = deny + mine
        data["permissions"] = perms
        owned = {"keys": sorted(TMUX_KEYS), "deny": mine,
                 "had_permissions": owned.get("had_permissions",
                                              "permissions" in _load(path))}
    enabled = data.get("enabledMcpjsonServers", [])
    if not isinstance(enabled, list):
        raise SettingsError("%s: enabledMcpjsonServers is not a list, left"
                            " as it is" % path)
    if SERVER_NAME not in enabled:
        enabled = enabled + [SERVER_NAME]
    data["enabledMcpjsonServers"] = enabled
    hooks = data.get("hooks", {})
    if not isinstance(hooks, dict):
        raise SettingsError("%s: hooks is not an object, left as it is"
                            % path)
    wanted, missing = desired_hooks(home, root=root, python=python,
                                    hooks_root=hooks_root)
    if kind == "tmux":
        for event, groups in _tmux_hooks(home, python).items():
            wanted.setdefault(event, []).extend(groups)
    for event in list(hooks):
        if isinstance(hooks[event], list):
            hooks[event] = _strip_owned(hooks[event])
            if not hooks[event]:
                del hooks[event]
    for event, groups in wanted.items():
        current = hooks.get(event, [])
        if not isinstance(current, list):
            raise SettingsError("%s: hooks.%s is not a list, left as it is"
                                % (path, event))
        hooks[event] = current + groups
    data["hooks"] = hooks
    text = json.dumps(data, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.read_text() != text:
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.replace(tmp, path)
    if owned is not None:
        _write_owned(home, owned)
    return {"path": path, "events": sorted(wanted), "missing": missing}


def _read_owned(home):
    try:
        data = json.loads((pathlib.Path(home) / TMUX_OWNED).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_owned(home, owned):
    path = pathlib.Path(home) / TMUX_OWNED
    text = json.dumps(owned, indent=2) + "\n"
    if not path.exists() or path.read_text() != text:
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.replace(tmp, path)


def remove_kind_settings(home):
    """Undo exactly what apply_project_settings(kind="tmux") added (a switch
    back to the sdk kind): the kind's keys, the deny entries it added (the
    operator's own stay), and the bridge hooks; the job hooks and the
    `cousin` server stay. Idempotent."""
    home = pathlib.Path(home)
    path = settings_path(home)
    if not path.exists():
        return
    data = _load(path)
    owned = _read_owned(home)
    for key in TMUX_KEYS:
        if key in data and data[key] == TMUX_KEYS[key]:
            del data[key]
    perms = data.get("permissions")
    if isinstance(perms, dict) and isinstance(perms.get("deny"), list):
        perms["deny"] = [d for d in perms["deny"] if d not in owned.get("deny", [])]
        if not perms["deny"]:
            del perms["deny"]
        if not perms and not owned.get("had_permissions", True):
            del data["permissions"]
    hooks = data.get("hooks")
    if isinstance(hooks, dict):
        for event in list(hooks):
            groups = hooks[event]
            if not isinstance(groups, list):
                continue
            kept = []
            for group in groups:
                inner = group.get("hooks") if isinstance(group, dict) else None
                if isinstance(inner, list):
                    rest = [h for h in inner if not (isinstance(h, dict) and isinstance(
                        h.get("command"), str) and TMUX_HOOK_MODULE in h["command"])]
                    if not rest and inner:
                        continue
                    group = dict(group, hooks=rest)
                kept.append(group)
            if kept:
                hooks[event] = kept
            else:
                del hooks[event]
    text = json.dumps(data, indent=2) + "\n"
    if path.read_text() != text:
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.replace(tmp, path)
    (home / TMUX_OWNED).unlink(missing_ok=True)
