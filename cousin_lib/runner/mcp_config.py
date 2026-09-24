"""A runner cousin's own MCP servers: <home>/.mcp.json.

The runner starts its session with no settings files, so the CLI never
reads the home's .mcp.json itself; this module reads it, in the format
Claude Code uses, and maps each entry onto the SDK's McpServerConfig:

    {"mcpServers": {"<name>": {"command": ..., "args": [...], "env": {...}},
                    "<name>": {"type": "http" | "sse", "url": ...,
                               "headers": {...}}}}

A stdio entry's `type` may be left out. `cousin` is reserved: the
runner serves its own tools in-process, so an entry of that name (the
tmux lane's cousin-mcp, which spawn writes) is skipped, never started
beside it. `${VAR}` and `${VAR:-default}` are expanded, as Claude Code
does, in command, args, env values, url and header values, against the
runner's own environment; a variable that is unset and has no default
skips its entry. A file that does not parse, or an entry that is not
one of the three shapes, is skipped with the reason: never fatal, the
cousin still starts with `cousin`.

Nothing expanded leaves this module but the configs themselves: the
event (`Loaded.event`) carries server names, types, ignored key names
and reasons, never a value. The set is ordered by name, so the same
file gives the same bytes at every start."""
import json
import os
import re
from pathlib import Path

from cousin_lib.mcp_server import SERVER_NAME

FILE = ".mcp.json"
RESERVED = SERVER_NAME
# the keys each SDK config type declares (claude_agent_sdk.types)
KEYS = {"stdio": ("command", "args", "env"),
        "http": ("url", "headers"),
        "sse": ("url", "headers")}
_VAR = re.compile(r"\$\{([^}]+)\}")


class Loaded:
    """What the runner loads from one read of the file."""

    def __init__(self, present, servers=None, listed=None, skipped=None):
        self.present = present            # the file exists
        self.servers = servers or {}      # name -> SDK config, by name
        self.listed = listed or []        # [{"name", "type"[, "ignored_keys"]}]
        self.skipped = skipped or []      # [{"name" (None: the file), "reason"}]

    def event(self):
        """The stream event's payload: names, types and reasons only."""
        return {"file": FILE, "servers": list(self.listed), "skipped": list(self.skipped)}


def _is_str_list(value):
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def _is_str_map(value):
    return isinstance(value, dict) and all(isinstance(v, str) for v in value.values())


def _shape(entry):
    """(type, None) for an entry of a known shape, else (None, reason)."""
    if not isinstance(entry, dict):
        return None, "not an object"
    kind = entry.get("type", "stdio")
    if kind not in KEYS:
        return None, "unknown type %s (stdio, http or sse)" % json.dumps(kind)[:40]
    if kind == "stdio":
        if not isinstance(entry.get("command"), str) or not entry["command"]:
            return None, "a stdio server needs a `command` string"
        if "args" in entry and not _is_str_list(entry["args"]):
            return None, "`args` must be a list of strings"
        if "env" in entry and not _is_str_map(entry["env"]):
            return None, "`env` must map names to strings"
    else:
        if not isinstance(entry.get("url"), str) or not entry["url"]:
            return None, "an %s server needs a `url` string" % kind
        if "headers" in entry and not _is_str_map(entry["headers"]):
            return None, "`headers` must map names to strings"
    return kind, None


def read(home):
    """(present, entries, skipped) with nothing expanded: entries is
    [(name, type, entry)] ordered by name. The structural half, which
    `cousin-migrate plan` also uses."""
    path = Path(home) / FILE
    if not path.exists():
        return False, [], []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as err:
        # a JSONDecodeError says where, never what
        return True, [], [{"name": None, "reason": "%s does not parse: %s" % (FILE, err)}]
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        return True, [], [{"name": None,
                           "reason": "%s has no `mcpServers` object" % FILE}]
    entries, skipped = [], []
    for name in sorted(servers):
        if name == RESERVED:
            skipped.append({"name": name, "reason": "reserved: the runner serves its own"
                            " `%s` tools in-process" % RESERVED})
            continue
        if not name:
            skipped.append({"name": name, "reason": "an empty server name"})
            continue
        kind, why = _shape(servers[name])
        if why:
            skipped.append({"name": name, "reason": why})
        else:
            entries.append((name, kind, servers[name]))
    return True, entries, skipped


def expand(value, environ, missing):
    """`${VAR}` and `${VAR:-default}` in one string, as Claude Code
    expands them: a set variable wins (even empty), then the default; an
    unset one with no default is added to `missing` and left as is."""
    def one(match):
        name, sep, default = match.group(1).partition(":-")
        if name in environ:
            return environ[name]
        if sep:
            return default
        missing.append(name)
        return match.group(0)
    return _VAR.sub(one, value)


def _config(kind, entry, environ, missing):
    def x(value):
        return expand(value, environ, missing)
    if kind == "stdio":
        out = {"type": "stdio", "command": x(entry["command"])}
        if "args" in entry:
            out["args"] = [x(a) for a in entry["args"]]
        if "env" in entry:
            out["env"] = {k: x(v) for k, v in entry["env"].items()}
        return out
    out = {"type": kind, "url": x(entry["url"])}
    if "headers" in entry:
        out["headers"] = {k: x(v) for k, v in entry["headers"].items()}
    return out


def load(home, environ=None):
    """The servers to merge beside `cousin`, expanded against `environ`
    (default: this process's environment)."""
    environ = os.environ if environ is None else environ
    present, entries, skipped = read(home)
    loaded = Loaded(present, skipped=list(skipped))
    for name, kind, entry in entries:
        missing = []
        config = _config(kind, entry, environ, missing)
        if missing:
            loaded.skipped.append({"name": name, "reason": "unset variable%s with no default: %s"
                                   % ("s" if len(set(missing)) > 1 else "",
                                      ", ".join(sorted(set(missing))))})
            continue
        loaded.servers[name] = config
        row = {"name": name, "type": kind}
        ignored = sorted(k for k in entry if k != "type" and k not in KEYS[kind])
        if ignored:
            row["ignored_keys"] = ignored
        loaded.listed.append(row)
    # one order for the whole set of skips: the file's problems, then by name
    loaded.skipped.sort(key=lambda s: (s["name"] is not None, s["name"] or ""))
    return loaded


def describe(home):
    """One line for `cousin-migrate plan`: which servers the runner will
    load, by name, and which it will skip. Nothing is expanded here: the
    runner's environment is not the operator's shell, so a `${VAR}` is
    resolved at the runner's start."""
    present, entries, skipped = read(home)
    if not present:
        return "no %s: the runner loads only `%s`" % (FILE, RESERVED)
    names = ", ".join(name for name, _, _ in entries) or "none"
    line = "the runner loads from %s: %s" % (FILE, names)
    if skipped:
        line += "; skips %s" % "; ".join(
            "%s (%s)" % (s["name"] if s["name"] is not None else FILE, s["reason"])
            for s in skipped)
    if any("${" in json.dumps(e) for _, _, e in entries):
        line += "; ${VAR} is expanded at the runner's start, from its own environment"
    return line
