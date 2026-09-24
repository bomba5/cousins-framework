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
import tomllib

from cousin_lib.config import MissingConfigError
from cousin_lib.config import commit_attribution as resolve_commit_attribution
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

# Tracker #112: what this module writes to turn Claude Code's own
# injected attribution (a Co-Authored-By trailer, a "Generated with
# Claude Code" line) off, for the tmux lane (the SDK runner reaches the
# same outcome through options.settings, sdk.py's ATTRIBUTION_OFF_SETTINGS).
ATTRIBUTION_OFF = {"commit": "", "pr": ""}
# The ownership marker's own path: a sidecar under data/, never a key
# inside settings.json itself. settings.json has the harness's OWN
# schema - the harness reads it every session - so a private bookkeeping
# key there risks the harness choking on or surfacing something it does
# not recognise. data/ already holds this module's kind of private,
# per-cousin state elsewhere in the framework (the runner's
# runner-session.json, login-required.json), so a sidecar there is the
# framework's own convention, not a new one.
ATTRIBUTION_MARKER = pathlib.Path("data") / "harness-attribution-owned.json"


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
    return "-m" in argv and JOB_HOOK_MODULE in argv


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


def _cousin_agent_table(home):
    """cousin.toml [agent], or {} when the file is missing or unreadable
    (a home mid-spawn, or a thin toml): never raises, this is a read for
    a default, not a required key."""
    try:
        data = tomllib.loads((pathlib.Path(home) / "cousin.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return data.get("agent") or {}


def _marker_path(home):
    return pathlib.Path(home) / ATTRIBUTION_MARKER


def _read_owned_attribution(home):
    """{key: value} this module itself wrote last time, for the keys
    _apply_attribution owns (includeCoAuthoredBy, attribution); {} when
    the sidecar is absent, unreadable or not an object - never a reason
    to treat an operator's key as ours."""
    try:
        data = json.loads(_marker_path(home).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_owned_attribution(home, owned, *, previous):
    """The sidecar recording exactly what this module currently owns;
    removed (not left as `{}`) when nothing is owned any more, so its
    mere presence answers "does this module own anything here". Skipped
    entirely when `owned` already matches `previous` (round 2 review,
    Minor): settings.json already short-circuits its own unchanged
    write, and the marker deserves the same - it is called after every
    apply_project_settings, not only when something moved."""
    if owned == previous:
        return
    path = _marker_path(home)
    if not owned:
        try:
            path.unlink()
        except OSError:
            pass
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w") as fh:
        fh.write(json.dumps(owned))
    os.replace(tmp, path)


def _reconcile_owned(data, owned):
    """`owned`, with every entry dropped whose key is no longer in
    `data`, or whose value there no longer matches (round 3 review,
    "generally": reconciled against settings.json at the start of every
    run). Both are read events, not writes: a crash in a previous run
    can leave the marker claiming a key settings.json never got (a
    turn-off crash before its write) or one settings.json no longer has
    (a turn-on crash after its write); either way this is the one place
    that notices and drops the stale claim, before anything below
    decides what to do this run."""
    return {key: value for key, value in owned.items() if data.get(key) == value}


def _compute_attribution_off(data, owned):
    """(data, new_owned) for commit_attribution == False: only a key
    ABSENT from `data` is added; a key already present, at any value,
    is left exactly as it is and never claimed - including one that
    already holds the exact off value (round 3 review, C1 reopened:
    claiming an already-matching value with no marker entry is how an
    operator's own pre-existing includeCoAuthoredBy: false got deleted
    on a later turn-on; that claim is gone, not just "accepted", now).
    `owned` is the already-reconciled map (_reconcile_owned), so every
    entry in it is a real, currently-true claim; those keys are kept in
    `new_owned` unchanged whether or not this call adds anything."""
    data = dict(data)
    to_write = {"includeCoAuthoredBy": False, "attribution": dict(ATTRIBUTION_OFF)}
    new_owned = dict(owned)
    for key, value in to_write.items():
        if key not in data:
            data[key] = value
            new_owned[key] = value
    return data, new_owned


def _compute_attribution_on(data, owned):
    """(data, {}) for commit_attribution == True: every key in `owned`
    (already reconciled, so each one still matches `data`) is removed.
    An operator's own key was never in `owned` to begin with (off never
    claims a pre-existing value now), so this never touches it."""
    data = dict(data)
    for key, value in owned.items():
        if data.get(key) == value:
            del data[key]
    return data, {}


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


def _write_settings_file(path, data):
    """settings.json itself: unchanged bytes write nothing (the same
    short-circuit the marker now has)."""
    text = json.dumps(data, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.read_text() != text:
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.replace(tmp, path)


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


def apply_project_settings(home, *, root, python=None, hooks_root=None):
    """Create or merge <home>/.claude/settings.json: this cousin's hooks
    and its `cousin` MCP server approved. Returns {path, events,
    missing}. Idempotent."""
    home = pathlib.Path(home)
    path = settings_path(home)
    data = _load(path)
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
    # Tracker #112: the install's config/harness.toml [agent]
    # commit_attribution, overridden by this cousin's own cousin.toml
    # [agent] commit_attribution - the tmux lane's reach for the same
    # outcome the SDK runner gets through options.settings. A value
    # that is not a real boolean at either level is config.py's
    # MissingConfigError; wrapped as this module's own SettingsError so
    # every caller (cousin-spawn's create and --repair-settings paths)
    # keeps catching what it already catches, and the settings file is
    # left as it is, same as any other SettingsError here.
    try:
        commit_attribution = resolve_commit_attribution(root, _cousin_agent_table(home))
    except MissingConfigError as err:
        raise SettingsError(str(err))
    owned_raw = _read_owned_attribution(home)
    owned = _reconcile_owned(data, owned_raw)
    # The two writes (settings.json, the marker) are ordered by
    # direction (round 3 review), so neither crash window ever needs a
    # claim-by-match to heal:
    #
    # - Turning off ADDS keys: the marker goes first, recording only
    #   the keys it is about to add. A crash before settings.json
    #   catches up leaves the marker claiming a key that is still
    #   absent there; the next run's reconciliation (_reconcile_owned)
    #   would drop that claim, except the key is still absent, so the
    #   off branch below adds it again - the claim was correct all
    #   along, never stale.
    # - Turning on REMOVES keys: settings.json goes first. A crash
    #   before the marker catches up leaves it claiming keys that are
    #   now gone from settings.json; the next run's reconciliation
    #   drops those claims before anything else runs.
    if commit_attribution:
        data, new_owned = _compute_attribution_on(data, owned)
        _write_settings_file(path, data)
        _write_owned_attribution(home, new_owned, previous=owned_raw)
    else:
        data, new_owned = _compute_attribution_off(data, owned)
        _write_owned_attribution(home, new_owned, previous=owned_raw)
        _write_settings_file(path, data)
    return {"path": path, "events": sorted(wanted), "missing": missing}
