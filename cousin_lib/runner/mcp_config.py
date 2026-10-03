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
beside it. A file that does not parse, or an entry that is not one of
the three shapes, is skipped with the reason: never fatal, the cousin
still starts with `cousin`.

`${VAR}` and `${VAR:-default}` are passed through UNEXPANDED: the agent
CLI expands them itself, in command, args, env, url and headers, from
its own environment (measured on the bundled CLI 2.1.281). The runner
hands it these servers in a private file (sdk.SdkRunner._mcp_options),
never on its argv, which the host's users can read. A secret is still
best written as its `${NAME}`: its value then reaches the server through
the CLI's environment, and .mcp.json, which the model can read and edit,
holds the name only. Two checks stay here, reading only whether a
variable is set: a reference with no default to a variable unset in the
runner's environment skips its entry (Claude Code refuses such a
config), and a reference to one of the account variables
(accounts.AUTH_VARS) skips its entry whatever it holds: the CLI's
environment carries the cousin's own credential there, and a .mcp.json
the model can edit must not route it to a server.

Plugins (cousin_lib/plugins.py): each plugin the cousin enables in its
cousin.toml `[plugins] enabled` that declares [mcp] adds one stdio server
named after the plugin (`load(..., root=...)`, `add_plugins`), rendered
from its manifest, with COUSIN_SLUG, COUSIN_HOME, FRAMEWORK_ROOT and
PLUGIN_DIR in its env. A .mcp.json entry of the same name wins; the
plugin's is skipped with the reason. Plugin rows carry
`"source": "plugin"` in the event.

The event (`Loaded.event`) carries server names, types, ignored key
names and reasons, never a value. The set is ordered by name, so the
same file gives the same bytes at every start."""
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
        self.plugins = False              # a plugin was added or skipped (add_plugins)

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


def parse(text):
    """(entries, skipped) of .mcp.json text, nothing expanded: entries is
    [(name, type, entry)] ordered by name. The structural half of read(),
    for text not on disk yet (the console checks an edit with it)."""
    try:
        data = json.loads(text)
    except ValueError as err:
        # a JSONDecodeError says where, never what
        return [], [{"name": None, "reason": "%s does not parse: %s" % (FILE, err)}]
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        return [], [{"name": None, "reason": "%s has no `mcpServers` object" % FILE}]
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
    return entries, skipped


def read(home):
    """(present, entries, skipped) with nothing expanded: entries is
    [(name, type, entry)] ordered by name. The structural half, which
    `cousin-migrate plan` also uses."""
    path = Path(home) / FILE
    if not path.exists():
        return False, [], []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as err:
        return True, [], [{"name": None, "reason": "%s does not parse: %s" % (FILE, err)}]
    entries, skipped = parse(text)
    return True, entries, skipped


def references(value):
    """[(name, has_default)] for every `${NAME}` / `${NAME:-default}` in
    one string, parsed as Claude Code parses them."""
    out = []
    for match in _VAR.finditer(value):
        name, sep, _ = match.group(1).partition(":-")
        out.append((name, bool(sep)))
    return out


def _strings(kind, entry):
    """Every string the CLI expands in this entry."""
    if kind == "stdio":
        return ([entry["command"]] + list(entry.get("args", []))
                + list(entry.get("env", {}).values()))
    return [entry["url"]] + list(entry.get("headers", {}).values())


def variables(kind, entry):
    """[(name, has_default)] for every `${NAME}` the CLI expands in this
    entry (command, args, env values; url, header values)."""
    return [r for v in _strings(kind, entry) for r in references(v)]


def _config(kind, entry):
    """The SDK config: the declared keys only, every value as written."""
    out = {"type": kind}
    for key in KEYS[kind]:
        if key in entry:
            value = entry[key]
            out[key] = list(value) if isinstance(value, list) else \
                dict(value) if isinstance(value, dict) else value
    return out


def _refusal(kind, entry, environ):
    """Why this entry's variables skip it, or None."""
    from cousin_lib.accounts import AUTH_VARS
    refs = variables(kind, entry)
    auth = sorted({name for name, _ in refs if name in AUTH_VARS})
    if auth:
        return "names an account variable, never passed to a server: %s" % ", ".join(auth)
    missing = sorted({name for name, has_default in refs
                      if not has_default and name not in environ})
    if missing:
        return "unset variable%s with no default: %s" % ("s" if len(missing) > 1 else "",
                                                         ", ".join(missing))
    return None


def load(home, environ=None, *, root=None):
    """The servers to merge beside `cousin`, `${VAR}` left for the CLI;
    `environ` (default: this process's environment, which the SDK hands
    the CLI) is read only for whether a variable is set. With `root`,
    the cousin's plugins are added after the file's (add_plugins)."""
    environ = os.environ if environ is None else environ
    present, entries, skipped = read(home)
    loaded = Loaded(present, skipped=list(skipped))
    for name, kind, entry in entries:
        why = _refusal(kind, entry, environ)
        if why:
            loaded.skipped.append({"name": name, "reason": why})
            continue
        loaded.servers[name] = _config(kind, entry)
        row = {"name": name, "type": kind}
        ignored = sorted(k for k in entry if k != "type" and k not in KEYS[kind])
        if ignored:
            row["ignored_keys"] = ignored
        loaded.listed.append(row)
    # one order for the whole set of skips: the file's problems, then by name
    loaded.skipped.sort(key=lambda s: (s["name"] is not None, s["name"] or ""))
    if root is not None:
        add_plugins(loaded, home, root, entries=entries, environ=environ)
    return loaded


def add_plugins(loaded, home, root, *, entries=(), environ=None):
    """The cousin's plugin MCP servers (plugins.mcp_servers) added to
    `loaded` as stdio servers, after the file's, each row and skip marked
    `"source": "plugin"`. A name the file declares (loaded or skipped) or
    `cousin` keeps the file's; the plugin's is skipped with why. Never
    raises: a broken plugin set is one skip."""
    from cousin_lib import plugins
    environ = os.environ if environ is None else environ
    try:
        servers, skipped = plugins.mcp_servers(home, root)
    except Exception as err:  # noqa: BLE001 - never fatal: the cousin keeps its tools
        servers, skipped = {}, [{"name": None, "reason": "plugins not read: %s"
                                 % type(err).__name__}]
    declared = {name for name, _k, _e in entries} | {
        s["name"] for s in loaded.skipped if s["name"] and s.get("source") != "plugin"}
    for skip in skipped:
        loaded.skipped.append(dict(skip, source="plugin"))
    for name, server in servers.items():
        if name == RESERVED:
            why = "reserved: the runner serves its own `%s` tools in-process" % RESERVED
        elif name in declared or name in loaded.servers:
            why = "%s declares a server named %s; that one is kept" % (FILE, name)
        else:
            why = _refusal("stdio", server, environ)
        if why:
            loaded.skipped.append({"name": name, "reason": why, "source": "plugin"})
            continue
        loaded.servers[name] = _config("stdio", server)
        loaded.listed.append({"name": name, "type": "stdio", "source": "plugin"})
    loaded.plugins = bool(servers or skipped)
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
        line += "; ${VAR} is expanded by the runner's CLI, from the runner's environment"
    return line
