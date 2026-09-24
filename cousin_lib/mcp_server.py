"""MCP adapter: the cousin's CLI surface over stdio. Spec: docs/mcp.md.

Stdlib-only core - registry -> schemas, JSON arguments -> argv list ->
subprocess.run(list) - and a serve() that imports the SDK lazily. There
is no code path that joins arguments into a string and no shell on the
path: that is the whole reason the adapter exists.

Two more things live here because they are the adapter's own seams:
provision_mcp (what cousin-spawn writes into a new home) and
approve_registration (the harness settings edit that turns a written
.mcp.json into an accepted one).
"""
import argparse
import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
from datetime import datetime, timezone

from cousin_lib.config import FrameworkConfig, MissingConfigError, harness_config

DEFAULT_CEILING = 12
DEFAULT_TIMEOUT = 120
DEFAULT_MAX_OUTPUT = 16000
REGISTRY_NAME = "mcp-registry.toml"
SERVER_NAME = "cousin"
SDK_REMEDIATION = ('the MCP SDK is not importable; serving needs the extra:'
                   ' pip install -e ".[mcp]" from the checkout, in the'
                   ' venv the framework is installed in (the package is'
                   ' not on PyPI)')
_PLACEHOLDER = re.compile(r"^\{([A-Za-z_][A-Za-z0-9_]*)\}$")
_KINDS = ("command", "send", "job")


class RegistryError(ValueError):
    """The registry is malformed; nothing is served."""


class ToolError(Exception):
    """A call could not be made; the message is the tool's error text."""


# --------------------------------------------------------------- registry

def _placeholder(element):
    """The property name if `element` is exactly `{name}`, else None."""
    if not isinstance(element, str):
        return None
    m = _PLACEHOLDER.match(element)
    return m.group(1) if m else None


def _validate_argv(tool_name, cmd_name, argv, properties):
    for element in argv:
        if not isinstance(element, str):
            raise RegistryError("%s.%s: argv element %r is not a string"
                                % (tool_name, cmd_name, element))
        name = _placeholder(element)
        if name is None:
            if "{" in element or "}" in element:
                raise RegistryError(
                    "%s.%s: %r is a partial placeholder; an argv element is"
                    " a literal or exactly {name}"
                    % (tool_name, cmd_name, element))
            continue
        if name not in properties:
            raise RegistryError(
                "%s.%s: placeholder {%s} names no declared property"
                % (tool_name, cmd_name, name))


def _as_list(value):
    return [value] if isinstance(value, str) else list(value)


def _validate_tool(name, tool):
    """Validate one tool table in place and fill its defaults."""
    kind = tool.setdefault("kind", "command")
    if kind not in _KINDS:
        raise RegistryError("%s: unknown kind %r" % (name, kind))
    tool.setdefault("enabled", True)
    tool.setdefault("description", "")
    props = tool.setdefault("properties", {})
    cmds = tool.setdefault("commands", {})
    if not cmds:
        raise RegistryError("%s: no commands" % name)
    if kind == "send":
        tool.setdefault("operators", [])
        tool["peers_from"] = _as_list(
            tool.setdefault("peers_from", ["cousin-chat", "list"]))
        tool.setdefault("errors_name_peers", True)
        for c in ("peer", "operator"):
            if c not in cmds:
                raise RegistryError("%s: send kind needs commands.%s"
                                    % (name, c))
    if kind == "job":
        tool["job_command"] = _as_list(tool.setdefault("job_command",
                                                       "cousin-job"))
        if "gen" not in cmds:
            raise RegistryError("%s: job kind needs commands.gen" % name)
        if "id" not in props:
            raise RegistryError("%s: job kind needs property 'id'" % name)
    for cmd_name, cmd in cmds.items():
        command = cmd.get("command", tool.get("command"))
        if not command:
            raise RegistryError("%s.%s: no command (tool has none either)"
                                % (name, cmd_name))
        cmd["command"] = _as_list(command)
        cmd.setdefault("argv", [])
        cmd.setdefault("options", {})
        cmd.setdefault("stdin", [])
        _validate_argv(name, cmd_name, cmd["argv"], props)
        for prop in list(cmd["options"]) + list(cmd["stdin"]):
            if prop not in props:
                raise RegistryError("%s.%s: %r names no declared property"
                                    % (name, cmd_name, prop))


def parse_registry(text, source="<registry>", *, strict=True):
    """Parse and validate registry text; fill defaults. RegistryError on
    anything malformed.

    With strict=False a tool that does not validate is dropped instead of
    killing the load, and reg["skipped"] carries (name, reason) for each.
    That is what serving uses: a registry is one file for every tool, so a
    single bad table used to cost a cousin its whole MCP surface. Anything
    about the file itself (unparseable TOML, the tool ceiling) stays fatal
    in both modes, because no subset of it is trustworthy."""
    try:
        reg = tomllib.loads(text)
    except tomllib.TOMLDecodeError as err:
        raise RegistryError("%s: %s" % (source, err))
    reg.setdefault("ceiling", DEFAULT_CEILING)
    reg.setdefault("timeout", DEFAULT_TIMEOUT)
    reg.setdefault("max_output", DEFAULT_MAX_OUTPUT)
    tools = reg.setdefault("tools", {})
    skipped = reg.setdefault("skipped", [])
    for name, tool in list(tools.items()):
        try:
            if ("command" not in tool
                    and tool.get("kind", "command") == "command"):
                raise RegistryError("%s: no command" % name)
            _validate_tool(name, tool)
        except RegistryError as err:
            if strict:
                raise
            skipped.append((name, str(err)))
            del tools[name]
    enabled = [n for n, t in tools.items() if t["enabled"]]
    if len(enabled) > reg["ceiling"]:
        raise RegistryError(
            "%d tools registered, ceiling is %d: coarse tools bound the"
            " count, not a tool per subcommand (%s)"
            % (len(enabled), reg["ceiling"], ", ".join(enabled)))
    return reg


def default_registry_path(env):
    """Where a registry is when none was named: the cousin's own
    (<COUSIN_HOME>/mcp-registry.toml), else the install's default under
    <FRAMEWORK_ROOT>/config (the edited copy, else the shipped example).
    None when neither environment variable leads to a file: the adapter
    never guesses a registry from its own location."""
    home = env.get("COUSIN_HOME")
    if home:
        own = pathlib.Path(home) / REGISTRY_NAME
        if own.is_file():
            return own
    root = env.get("FRAMEWORK_ROOT")
    if root:
        for name in (REGISTRY_NAME, REGISTRY_NAME + ".example"):
            candidate = pathlib.Path(root) / "config" / name
            if candidate.is_file():
                return candidate
    return None


def load_registry(path, *, strict=True):
    """Parse and validate a registry file. Raises RegistryError."""
    path = pathlib.Path(path)
    try:
        text = path.read_text()
    except OSError as err:
        raise RegistryError("%s: %s" % (path, err))
    return parse_registry(text, str(path), strict=strict)


# ---------------------------------------------------------------- schemas

_JSON_TYPES = {"string", "integer", "number", "boolean", "array"}


def _uses(tool, prop):
    """Commands that reference `prop` in argv, options or stdin."""
    used = []
    for cmd_name, cmd in tool["commands"].items():
        names = ({_placeholder(e) for e in cmd["argv"]}
                 | set(cmd["options"]) | set(cmd["stdin"]))
        if prop in names:
            used.append(cmd_name)
    return used


def _property_schema(tool, prop, spec):
    """Advisory schema for one property: clean types map, opaque ones
    fall back to string. The CLI's parser is the enforcement point."""
    ptype = spec.get("type", "string")
    if ptype not in _JSON_TYPES:
        ptype = "string"
    out = {"type": ptype}
    if ptype == "array":
        out["items"] = {"type": spec.get("items", "string")}
    if "enum" in spec:
        out["enum"] = list(spec["enum"])
    used = ", ".join(_uses(tool, prop))
    desc = spec.get("description", "")
    if used and used not in desc:
        desc = ("%s (%s)" % (desc, used)).strip()
    out["description"] = desc
    return out


def build_schema(name, tool):
    """The inputSchema for one tool: a `command` enum over its
    subcommands (except the send kind) plus the union of its
    properties."""
    props = {}
    if tool["kind"] != "send":
        commands = set(tool["commands"])
        if tool["kind"] == "job":
            commands |= {"status", "result"}
        props["command"] = {"type": "string", "enum": sorted(commands)}
        required = ["command"]
    else:
        required = sorted(p for p, s in tool["properties"].items()
                          if not s.get("optional"))
    for prop, spec in tool["properties"].items():
        props[prop] = _property_schema(tool, prop, spec)
    return {"type": "object", "properties": props, "required": required,
            "additionalProperties": False}


def list_tools(registry):
    """What tools/list returns: enabled tools with their schemas."""
    return [{"name": name, "description": tool["description"],
             "inputSchema": build_schema(name, tool)}
            for name, tool in registry["tools"].items() if tool["enabled"]]


# ------------------------------------------------------------ resolution

def resolve_command(name):
    """A bare console-script name -> the one beside the running
    interpreter, else the one on PATH, else the name unchanged (the run
    then fails loudly). A path is taken as given. Beside-the-interpreter
    first is what makes two installs on one machine unambiguous."""
    if os.sep in name:
        return name
    beside = pathlib.Path(sys.executable).parent / name
    if beside.is_file() and os.access(beside, os.X_OK):
        return str(beside)
    found = shutil.which(name)
    return found or name


def unresolved(names):
    """The bare names among `names` that resolve nowhere."""
    return [n for n in names if os.sep not in n and resolve_command(n) == n]


def registry_commands(registry):
    """Every distinct command head a registry would run."""
    heads = []
    for tool in registry["tools"].values():
        if not tool["enabled"]:
            continue
        for cmd in tool["commands"].values():
            heads.append(cmd["command"][0])
        if tool["kind"] == "send":
            heads.append(tool["peers_from"][0])
        if tool["kind"] == "job":
            heads.append(tool["job_command"][0])
    seen, out = set(), []
    for h in heads:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out


# --------------------------------------------------------------- assembly

def _as_elements(value):
    """A JSON value as argv elements: lists spread, scalars stringify."""
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, bool):
        return ["true" if value else "false"]
    return [str(value)]


def check_enums(props, args, cmd_name):
    """A value outside its property's declared enum is a ToolError
    naming the allowed values and the property's description. The enum
    is enforced here, not only advertised in the schema: a registry
    leaves a value out of an enum on purpose, and a client that skips
    schema validation must not get it through."""
    for name, value in args.items():
        spec = props.get(name)
        if not spec or "enum" not in spec or value is None:
            continue
        allowed = list(spec["enum"])
        values = value if isinstance(value, list) else [value]
        for v in values:
            if v not in allowed:
                desc = spec.get("description", "")
                raise ToolError(
                    "%s: %s=%r is not one of %s%s"
                    % (cmd_name, name, v, ", ".join(map(str, allowed)),
                       ("; " + desc) if desc else ""))


_ITEM_TYPES = {"string": str, "integer": int, "number": (int, float),
               "boolean": bool}


def _array_elements(spec, value, cmd_name, name):
    """An array property's value as whole argv elements, checked: a JSON
    array whose items are the declared `items` type (string by default).
    A required one must not be empty. A scalar is refused, so a command
    line given as one string never reaches an argv as one element."""
    if not isinstance(value, list):
        raise ToolError("%s: %s must be an array, not %r"
                        % (cmd_name, name, value))
    if not value and not spec.get("optional"):
        raise ToolError("%s: %s must not be empty" % (cmd_name, name))
    item = spec.get("items", "string")
    want = _ITEM_TYPES.get(item, str)
    for v in value:
        if not isinstance(v, want) or (item != "boolean"
                                       and isinstance(v, bool)):
            raise ToolError("%s: every element of %s must be a %s, not %r"
                            % (cmd_name, name, item, v))
    return [str(v) for v in value]


def build_call(tool, cmd_name, args, extra_argv=(), resolve=resolve_command):
    """The argv list and stdin bytes for one call. Never a string: a
    placeholder becomes whole elements, an option becomes flag elements,
    and nothing is ever formatted into a larger string. An array-typed
    placeholder spreads into one element per item. Options go at the end,
    or just before a literal `--` in the command's argv, so a command
    array after `--` stays the last thing on the line."""
    cmd = tool["commands"].get(cmd_name)
    if cmd is None:
        raise ToolError("unknown command %r; known: %s"
                        % (cmd_name, ", ".join(sorted(tool["commands"]))))
    props = tool["properties"]
    check_enums(props, args, cmd_name)
    head = list(cmd["command"])
    head[0] = resolve(head[0])
    argv = head + list(extra_argv)
    operands_at = None
    for element in cmd["argv"]:
        name = _placeholder(element)
        if name is None:
            if element == "--" and operands_at is None:
                operands_at = len(argv)
            argv.append(element)
            continue
        if name in args and args[name] is not None:
            if props[name].get("type") == "array":
                argv.extend(_array_elements(props[name], args[name],
                                            cmd_name, name))
            else:
                argv.extend(_as_elements(args[name]))
        elif props[name].get("optional"):
            continue
        else:
            raise ToolError("%s: missing required property %r"
                            % (cmd_name, name))
    flags = []
    for name, flag in cmd["options"].items():
        value = args.get(name)
        if value is None or value is False:
            continue
        if value is True:
            flags.append(flag)
        elif isinstance(value, list):
            for v in value:
                flags.extend([flag, str(v)])
        else:
            flags.extend([flag, str(value)])
    if operands_at is None:
        argv.extend(flags)
    else:
        argv[operands_at:operands_at] = flags
    stdin = None
    if cmd["stdin"]:
        parts = []
        for name in cmd["stdin"]:
            if name not in args or args[name] is None:
                raise ToolError("%s: missing required property %r"
                                % (cmd_name, name))
            parts.append(str(args[name]))
        sep = cmd.get("stdin_sep")
        text = (("\n%s\n" % sep).join(parts) + "\n") if sep else "\n".join(parts)
        stdin = text.encode()
    return argv, stdin


# ---------------------------------------------------------------- running

def run_call(argv, stdin, env, timeout):
    """Run one argv list. Returns (text, is_error). No shell, ever; a
    bodiless call gets /dev/null, never the parent's stdin."""
    kwargs = {"capture_output": True, "env": env, "timeout": timeout}
    if stdin is None:
        kwargs["stdin"] = subprocess.DEVNULL
    else:
        kwargs["input"] = stdin
    try:
        proc = subprocess.run(argv, **kwargs)
    except subprocess.TimeoutExpired:
        return ("%s: timed out after %ss (not retried)"
                % (argv[0], timeout), True)
    except OSError as err:
        hint = ""
        if os.sep not in argv[0]:
            hint = (" (not found beside %s or on PATH)"
                    % pathlib.Path(sys.executable).parent)
        return ("%s: cannot run: %s%s" % (argv[0], err, hint), True)
    out = proc.stdout.decode(errors="replace")
    err = proc.stderr.decode(errors="replace")
    if proc.returncode != 0:
        return ("%s: exit %d\n%s%s" % (argv[0], proc.returncode, err, out),
                True)
    return (out, False)


def cap_output(text, limit):
    """Bound what one call returns; the cut is said, never silent."""
    if limit is None or len(text) <= limit:
        return text
    return ("%s\n[output truncated: %d of %d chars shown]"
            % (text[:limit], limit, len(text)))


def _command_call(registry, name, tool, args, env):
    cmd_name = args.get("command")
    if not cmd_name:
        raise ToolError("%s: 'command' is required; one of %s"
                        % (name, ", ".join(sorted(tool["commands"]))))
    argv, stdin = build_call(tool, cmd_name, args)
    return run_call(argv, stdin, env, registry["timeout"])


def discover_peers(registry, env):
    """Peer slugs at session start: the first token of each line the
    peers_from command prints, dropping the line marked (self). Read
    once; the spec promises no hot registration. Empty on failure."""
    peers = set()
    for tool in registry["tools"].values():
        if tool["kind"] != "send" or not tool["enabled"]:
            continue
        argv = list(tool["peers_from"])
        argv[0] = resolve_command(argv[0])
        text, is_error = run_call(argv, None, env, registry["timeout"])
        if is_error:
            print("cousin-mcp: peer discovery failed: %s" % text.strip(),
                  file=sys.stderr)
            continue
        for line in text.splitlines():
            if not line.strip() or "(self)" in line:
                continue
            peers.add(line.split()[0])
    return peers


def resolve_send(tool, to, peers):
    """A known slug is a peer, a configured operator name is a reply,
    anything else is an error. Never a default: a fallthrough on a
    delivery decision is how a misdirected reply happens."""
    if to in peers:
        return "peer"
    if to in tool["operators"]:
        return "operator"
    parts = []
    if not tool["operators"]:
        parts.append("no operator is configured for this cousin"
                     " (registry: operators = [])")
    # The error text is a perimeter surface: a deployment whose peer
    # list carries names a caller must not learn withholds them and
    # gives the count instead.
    if tool.get("errors_name_peers", True):
        known = ", ".join(sorted(peers)) or "(none)"
    else:
        known = "%d known (see the peer list command)" % len(peers)
    parts.append("unknown destination %r; known peers: %s; known operators: %s"
                 % (to, known, ", ".join(tool["operators"]) or "(none)"))
    raise ToolError("; ".join(parts))


def _send_call(registry, name, tool, args, env, peers):
    to = args.get("to")
    if not to:
        raise ToolError("%s: 'to' is required" % name)
    route = resolve_send(tool, to, peers)
    argv, stdin = build_call(tool, route, args)
    return run_call(argv, stdin, env, registry["timeout"])


_MEDIA_MIME = {".png": "image/png", ".jpg": "image/jpeg",
               ".jpeg": "image/jpeg", ".gif": "image/gif",
               ".webp": "image/webp", ".mp3": "audio/mpeg",
               ".wav": "audio/wav", ".ogg": "audio/ogg"}
_TAIL_LINES = 20


def _log_lines(log_path):
    try:
        return pathlib.Path(log_path).read_text(errors="replace").splitlines()
    except OSError:
        return []


def _job_call(registry, name, tool, args, env):
    """Work on a job handle: gen starts a tracked job and returns its id
    (never a blocking call, never a handle that does not resolve);
    status and result poll it; result carries the log tail. Returns
    (text, is_error, attachments)."""
    cmd_name = args.get("command")
    job = list(tool["job_command"])
    job[0] = resolve_command(job[0])
    timeout = registry["timeout"]
    if cmd_name == "gen":
        argv, _stdin = build_call(tool, "gen", args)
        title = "%s gen: %s" % (name, str(args.get("prompt", ""))[:40])
        text, is_error = run_call(
            job + ["start", "shell", title, "--json", "--"] + argv,
            None, env, timeout)
        if is_error:
            return (text, True, [])
        try:
            info = json.loads(text)
        except ValueError:
            info = {}
        if not info.get("job_id"):
            raise ToolError(
                "%s: the job tracker did not register the job, so there is"
                " no handle to return (a handle that never resolves would"
                " look like a slow generation); run `%s` from a shell instead"
                % (name, shlex.join(argv)))
        return (json.dumps({"job_id": info["job_id"],
                            "log_path": info.get("log_path")}), False, [])
    if cmd_name in ("status", "result"):
        job_id = args.get("id")
        if job_id is None:
            raise ToolError("%s %s: 'id' is required" % (name, cmd_name))
        text, is_error = run_call(job + ["show", str(job_id), "--json"],
                                  None, env, timeout)
        if is_error:
            return (text, True, [])
        if cmd_name == "status":
            return (text, False, [])
        try:
            info = json.loads(text)
        except ValueError:
            raise ToolError("%s result: the job tracker returned no JSON"
                            " for job %s" % (name, job_id))
        status = info.get("status")
        lines = _log_lines(info.get("log_path") or "")
        tail = "\n".join(lines[-_TAIL_LINES:])
        if status == "running":
            return ("job %s is running; poll again" % job_id, False, [])
        if status != "done":
            return ("job %s is %s (exit %s)\n%s"
                    % (job_id, status, info.get("exit_code"), tail), True, [])
        path = next((l for l in reversed(lines) if l.strip()), "")
        attachments = []
        if args.get("return_image") and path:
            mime = _MEDIA_MIME.get(pathlib.Path(path).suffix.lower())
            if mime and pathlib.Path(path).exists():
                attachments.append({"path": path, "mime": mime})
        return ("job %s done\n%s" % (job_id, tail), False, attachments)
    raise ToolError("%s: unknown command %r; known: gen, status, result"
                    % (name, cmd_name))


def call_tool_rich(registry, name, args, env, peers=frozenset()):
    """Dispatch one tool call. Returns (text, is_error, attachments);
    errors come back as text with is_error True, never raised: a bad
    call is a tool execution error, not a protocol error."""
    tool = registry["tools"].get(name)
    if tool is None or not tool["enabled"]:
        return ("unknown tool %r; known: %s"
                % (name, ", ".join(t["name"] for t in list_tools(registry))),
                True, [])
    limit = registry.get("max_output", DEFAULT_MAX_OUTPUT)
    try:
        if tool["kind"] == "command":
            text, is_error = _command_call(registry, name, tool, args, env)
            attachments = []
        elif tool["kind"] == "send":
            text, is_error = _send_call(registry, name, tool, args, env, peers)
            attachments = []
        elif tool["kind"] == "job":
            text, is_error, attachments = _job_call(registry, name, tool,
                                                    args, env)
        else:
            return ("%s: unsupported kind %r" % (name, tool["kind"]), True, [])
    except ToolError as err:
        return (str(err), True, [])
    return (cap_output(text, limit), is_error, attachments)


def call_tool(registry, name, args, env, peers=frozenset()):
    """Dispatch one tool call; (text, is_error). See call_tool_rich."""
    text, is_error, _attachments = call_tool_rich(registry, name, args, env,
                                                  peers)
    return (text, is_error)


# ---------------------------------------------------------- the era guard

def record_client_version(home, version, now=None):
    """Bounded map version -> last seen, at <home>/data/mcp-client.json.
    One entry per distinct version, so "every recorded version" means
    exactly that; a corrupt file is reset, never fatal."""
    path = pathlib.Path(home) / "data" / "mcp-client.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data[version] = now or datetime.now(timezone.utc).astimezone().isoformat(
        timespec="seconds")
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    return path


def _sdk_versions():
    """The SDK's supported protocol versions, or None if it is absent."""
    try:
        from mcp.shared.version import SUPPORTED_PROTOCOL_VERSIONS
    except ImportError:
        return None
    return list(SUPPORTED_PROTOCOL_VERSIONS)


# ------------------------------------------------------------------ serve

PANE_TOOLS = ("reply", "handoff")


def _is_pane_home(home):
    if not home:
        return False
    from cousin_lib.delivery import _runner_kind
    return _runner_kind(pathlib.Path(home)) == "tmux"


def pane_tools(home):
    """`reply` and `handoff` for a tmux-kind home (phase 11 R11): its pane's
    CLI reaches the framework through this stdio server, which has no live
    turn of its own. Any other home gets none: the SDK and opencode
    runners serve them in-process."""
    if not _is_pane_home(home):
        return []
    from cousin_lib.runner.tools import RUNNER_TOOLS
    return [dict(t) for t in RUNNER_TOOLS if t["name"] in PANE_TOOLS]


class _PaneTurn:
    """The live turn as run/turn.json says, read at the call (tmux_turn)."""

    def __init__(self, home):
        self.home = home

    def snapshot(self):
        from cousin_lib.runner import tmux_turn
        return tmux_turn.live(self.home)


def call_pane_tool(home, name, args):
    """One `reply` or `handoff` call from a tmux-kind pane: (text, is_error),
    through the runner's own tool code with the turn file as the turn."""
    if name not in PANE_TOOLS or not _is_pane_home(home):
        return "%s is served here for a tmux-kind home only" % name, True
    from cousin_lib.config import CousinConfig, FrameworkConfig
    from cousin_lib.runner import tools
    from cousin_lib.runner.policy import Policy
    home = pathlib.Path(home)
    try:
        cfg = CousinConfig.load(home)
        slug, display = cfg.slug, cfg.name
    except Exception:  # noqa: BLE001 - a thin toml still answers; the dir names it
        slug, display = home.name, home.name.capitalize()
    try:
        policy = Policy.load(home)
    except Exception as err:  # noqa: BLE001 - a broken policy is a tool error, never a pass
        return "policy.toml: %s" % err, True
    ctx = tools.ToolContext(home=home, slug=slug, name=display,
                            root=FrameworkConfig.root_from_home(home),
                            turn=_PaneTurn(home), policy=policy)
    return tools.call(ctx, name, dict(args or {}))


def serve(registry, env, home):
    """Speak MCP over stdio. The only place the SDK is imported."""
    import anyio
    import mcp.types as types
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server

    server = Server("cousin-mcp")
    peers = discover_peers(registry, env)
    seen = set()

    def _record():
        try:
            params = server.request_context.session.client_params
        except LookupError:
            return
        version = getattr(params, "protocolVersion", None)
        if not version or version in seen:
            return
        seen.add(version)
        print("cousin-mcp: client declares protocol %s" % version,
              file=sys.stderr)
        if home:
            try:
                record_client_version(home, version)
            except OSError as err:
                print("cousin-mcp: could not record client version: %s"
                      % err, file=sys.stderr)

    @server.list_tools()
    async def _list():
        _record()
        return [types.Tool(name=t["name"], description=t["description"],
                           inputSchema=t["inputSchema"])
                for t in list_tools(registry) + pane_tools(home)]

    @server.call_tool(validate_input=False)
    async def _call(name, arguments):
        _record()
        if name in PANE_TOOLS and _is_pane_home(home):
            text, is_error = call_pane_tool(home, name, arguments or {})
            return types.CallToolResult(content=[types.TextContent(type="text", text=text)],
                                        isError=is_error)
        text, is_error, attachments = call_tool_rich(
            registry, name, arguments or {}, env, peers)
        content = [types.TextContent(type="text", text=text)]
        for att in attachments:
            import base64
            data = base64.b64encode(
                pathlib.Path(att["path"]).read_bytes()).decode()
            if att["mime"].startswith("image/"):
                content.append(types.ImageContent(
                    type="image", data=data, mimeType=att["mime"]))
            elif (att["mime"].startswith("audio/")
                  and hasattr(types, "AudioContent")):
                content.append(types.AudioContent(
                    type="audio", data=data, mimeType=att["mime"]))
        return types.CallToolResult(content=content, isError=is_error)

    async def _run():
        async with stdio_server() as (read, write):
            await server.run(read, write,
                             server.create_initialization_options())

    anyio.run(_run)


# ----------------------------------------------------------- provisioning

def shipped_default_registry(root):
    """The install's default registry text: <root>/config/
    mcp-registry.toml when an operator edited one, else the shipped
    mcp-registry.toml.example there, else the example beside this
    package's checkout (a root that holds only templates/)."""
    root = pathlib.Path(root)
    checkout = pathlib.Path(__file__).resolve().parents[1]
    candidates = [root / "config" / REGISTRY_NAME,
                  root / "config" / (REGISTRY_NAME + ".example"),
                  checkout / "config" / (REGISTRY_NAME + ".example")]
    for path in candidates:
        if path.is_file():
            return path.read_text()
    raise RegistryError("no default registry: none of %s exists"
                        % ", ".join(str(c) for c in candidates))


_OPERATORS_LINE = re.compile(r"^operators = \[.*\]$", re.MULTILINE)


def render_registry(text, operators):
    """The default registry with its `operators` line filled. The result
    is re-parsed and validated: a registry that cannot be read back is
    never written."""
    line = "operators = %s" % json.dumps(list(operators))
    rendered, n = _OPERATORS_LINE.subn(line, text, count=1)
    if n != 1:
        raise RegistryError("the default registry has no 'operators = [...]'"
                            " line to fill")
    parse_registry(rendered, "rendered registry")
    return rendered


ADAPTER_NAME = "cousin-mcp"


def adapter_command():
    """The cousin-mcp of the install that is running: the one beside
    this interpreter, by absolute path, else the bare name. PATH is
    never consulted - on a machine carrying two installs, whatever is
    first on PATH is exactly the one that must not be baked in."""
    beside = pathlib.Path(sys.executable).parent / ADAPTER_NAME
    if beside.is_file() and os.access(beside, os.X_OK):
        return str(beside)
    return ADAPTER_NAME


def mcp_json(home, slug, root):
    """The harness-side registration: the adapter over stdio (the one
    beside this interpreter, see adapter_command), pointed at the
    home's own registry, with the cousin's identity in env."""
    home = pathlib.Path(os.path.abspath(home))
    root = os.path.abspath(root)
    return {"mcpServers": {SERVER_NAME: {
        "type": "stdio",
        "command": adapter_command(),
        "args": ["--registry", str(home / REGISTRY_NAME)],
        "env": {"COUSIN_HOME": str(home), "COUSIN_SLUG": slug,
                "FRAMEWORK_ROOT": str(pathlib.Path(root))}}}}


class RegistrationError(Exception):
    """An existing .mcp.json cannot be merged; the message says why."""


def refresh_mcp_json(home, *, root, slug):
    """Bring <home>/.mcp.json's `cousin` entry up to date: its command,
    its --registry path and the three identity env values are rewritten
    to what spawn would write today (absolute paths), everything else
    in the file - other servers, other args, other env keys - is kept.
    Absent: written whole. Not a JSON object: refused, never clobbered.
    Idempotent: a second run writes the same bytes. Returns
    {path, changed}."""
    home = pathlib.Path(os.path.abspath(home))
    path = home / ".mcp.json"
    fresh = mcp_json(home, slug, root)
    want = fresh["mcpServers"][SERVER_NAME]
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError) as err:
            raise RegistrationError("%s is unreadable, left as it is: %s"
                                    % (path, err))
        if not isinstance(data, dict):
            raise RegistrationError("%s is not a JSON object, left as it is"
                                    % path)
        servers = data.setdefault("mcpServers", {})
        if not isinstance(servers, dict):
            raise RegistrationError("%s: mcpServers is not an object, left"
                                    " as it is" % path)
        entry = servers.get(SERVER_NAME)
        if not isinstance(entry, dict):
            entry = {}
        args = entry.get("args")
        if isinstance(args, list) and "--registry" in args \
                and args.index("--registry") + 1 < len(args):
            args = list(args)
            args[args.index("--registry") + 1] = want["args"][1]
        else:
            args = list(want["args"])
        env = entry.get("env")
        env = dict(env) if isinstance(env, dict) else {}
        env.update(want["env"])
        servers[SERVER_NAME] = dict(entry, type=want["type"],
                                    command=want["command"], args=args,
                                    env=env)
    else:
        data = fresh
    text = json.dumps(data, indent=2) + "\n"
    changed = not path.exists() or path.read_text() != text
    if changed:
        fd, tmp = tempfile.mkstemp(dir=home, suffix=".tmp")
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.replace(tmp, path)
    return {"path": path, "changed": changed}


def provision_mcp(home, *, root, slug, operator=None):
    """Write <home>/mcp-registry.toml and <home>/.mcp.json, each only if
    absent. Returns the paths written. Called by cousin-spawn; safe to
    call again on a cousin that predates it."""
    home = pathlib.Path(home)
    written = []
    registry = home / REGISTRY_NAME
    if not registry.exists():
        text = render_registry(shipped_default_registry(root),
                               [operator] if operator else [])
        registry.write_text(text)
        written.append(registry)
    registration = home / ".mcp.json"
    if not registration.exists():
        registration.write_text(
            json.dumps(mcp_json(home, slug, root), indent=2) + "\n")
        written.append(registration)
    return written


class ApproveError(Exception):
    """The approval cannot be made; the message carries the remediation."""


_MANUAL_APPROVAL = (
    "to approve by hand, in the harness settings JSON add the home under"
    " \"projects\" with \"hasTrustDialogAccepted\": true and \"cousin\" in"
    " \"enabledMcpjsonServers\"")


def approve_registration(settings_path, home):
    """Edit exactly the named harness settings file: trust the home and
    enable the `cousin` server for it. Everything else in the file is
    kept. Returns the project entry after the edit."""
    settings_path = pathlib.Path(settings_path)
    if not settings_path.is_file():
        raise ApproveError(
            "harness settings file %s does not exist; start the harness"
            " once so it writes its settings, then approve (%s)"
            % (settings_path, _MANUAL_APPROVAL))
    try:
        data = json.loads(settings_path.read_text())
    except (OSError, ValueError) as err:
        raise ApproveError("harness settings file %s is unreadable: %s"
                           % (settings_path, err))
    if not isinstance(data, dict):
        raise ApproveError("harness settings file %s is not a JSON object"
                           % settings_path)
    projects = data.setdefault("projects", {})
    if not isinstance(projects, dict):
        raise ApproveError("%s: \"projects\" is not an object" % settings_path)
    entry = projects.setdefault(str(home), {})
    entry["hasTrustDialogAccepted"] = True
    enabled = [s for s in entry.get("enabledMcpjsonServers", [])
               if s != SERVER_NAME]
    enabled.append(SERVER_NAME)
    entry["enabledMcpjsonServers"] = enabled
    if "disabledMcpjsonServers" in entry:
        entry["disabledMcpjsonServers"] = [
            s for s in entry["disabledMcpjsonServers"] if s != SERVER_NAME]
    fd, tmp = tempfile.mkstemp(dir=settings_path.parent, suffix=".tmp")
    with os.fdopen(fd, "w") as fh:
        fh.write(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, settings_path)
    return entry


def _approve(args):
    try:
        root = FrameworkConfig.resolve(args.root, cwd_fallback=True).root
    except MissingConfigError as err:
        print("cousin-mcp: %s" % err, file=sys.stderr)
        return 2
    home = root / "cousins" / args.slug
    if not (home / "cousin.toml").is_file():
        print("cousin-mcp: no cousin %r under %s" % (args.slug, root / "cousins"),
              file=sys.stderr)
        return 2
    if not (home / ".mcp.json").is_file():
        print("cousin-mcp: %s has no .mcp.json; provision it first (spawn"
              " writes it; for an older cousin copy config/%s.example to"
              " the home as %s and write the .mcp.json by hand, see"
              " docs/mcp.md)" % (home, REGISTRY_NAME, REGISTRY_NAME),
              file=sys.stderr)
        return 2
    try:
        cfg = harness_config(root)
    except MissingConfigError as err:
        print("cousin-mcp: %s" % err, file=sys.stderr)
        return 2
    settings = (cfg or {}).get("settings_file")
    if not settings:
        print("cousin-mcp: config/harness.toml names no settings_file, so"
              " there is nothing to approve in; set settings_file to the"
              " harness's settings JSON (the file that holds"
              " \"enabledMcpjsonServers\" per project), or %s"
              % _MANUAL_APPROVAL, file=sys.stderr)
        return 2
    try:
        approve_registration(pathlib.Path(settings).expanduser(), home)
    except ApproveError as err:
        print("cousin-mcp: %s" % err, file=sys.stderr)
        return 2
    print("approved %s in %s: trusted, \"%s\" enabled; the registration is"
          " read at the cousin's next session start"
          % (home, settings, SERVER_NAME))
    return 0


# -------------------------------------------------------------------- CLI

def _selftest(registry, path):
    """Load, build every schema, print the tool list, resolve every
    command, say whether the SDK is present. Exit 1 when a command
    resolves nowhere: a tool list whose commands cannot run is not ok."""
    tools = list_tools(registry)
    print("registry: %s (%d tool%s, ceiling %d, timeout %ds, output cap"
          " %d chars)" % (path, len(tools), "" if len(tools) == 1 else "s",
                          registry["ceiling"], registry["timeout"],
                          registry["max_output"]))
    for name, tool in registry["tools"].items():
        if not tool["enabled"]:
            continue
        heads = sorted({c["command"][0] for c in tool["commands"].values()})
        extra = ""
        if tool["kind"] == "send":
            extra = " (operators: %s)" % (
                ", ".join(tool["operators"]) or "none configured")
        print("  %-9s %-28s %s%s"
              % (name, ", ".join(pathlib.Path(h).name for h in heads),
                 ", ".join(sorted(tool["commands"])), extra))
    missing = unresolved(registry_commands(registry))
    for head in registry_commands(registry):
        if head in missing:
            where = "NOT FOUND beside %s or on PATH" % pathlib.Path(
                sys.executable).parent
        else:
            resolved = resolve_command(head)
            beside = pathlib.Path(sys.executable).parent / head
            if os.sep in head:
                where = "as given"
            elif resolved == str(beside):
                where = "beside the interpreter"
            else:
                where = "on PATH: %s" % resolved
        print("  %s -> %s" % (head, where))
    versions = _sdk_versions()
    if versions is None:
        print("mcp sdk: absent; %s" % SDK_REMEDIATION)
    else:
        print("mcp sdk: present, protocol versions %s"
              % ", ".join(versions))
    for name, reason in registry.get("skipped", []):
        print("  SKIPPED %s: %s" % (name, reason))
    if missing:
        print("selftest FAILED: %d command(s) resolve nowhere: %s"
              % (len(missing), ", ".join(missing)))
        return 1
    if registry.get("skipped"):
        print("selftest FAILED: %d tool(s) did not validate and would be"
              " skipped when serving: %s"
              % (len(registry["skipped"]),
                 ", ".join(n for n, _ in registry["skipped"])))
        return 1
    print("selftest ok: %d schema(s) built" % len(tools))
    return 0


def mcp_main(argv=None):
    parser = argparse.ArgumentParser(
        prog="cousin-mcp",
        description="the cousin's CLI surface over MCP (stdio)")
    parser.add_argument("--registry",
                        help="registry TOML (default: the cousin home's"
                             " mcp-registry.toml, else the install's"
                             " config/mcp-registry.toml[.example])")
    parser.add_argument("--selftest", action="store_true",
                        help="load the registry, build every schema, print"
                             " the tools and where each command resolves;"
                             " no SDK needed")
    parser.add_argument("--list-tools", action="store_true",
                        help="print tool schemas as JSON and exit")
    parser.add_argument("--last-connection", action="store_true",
                        help="what the harness recorded about this"
                             " cousin's MCP server last time it tried to"
                             " connect, stderr included; exit 1 if it"
                             " failed, 2 if nothing was recorded or the"
                             " outcome was never written down")
    parser.add_argument("--versions", action="store_true",
                        help="print the SDK's supported protocol versions")
    parser.add_argument("--call", nargs=2, metavar=("TOOL", "JSON"),
                        help="call one tool from the shell and print the"
                             " result")
    sub = parser.add_subparsers(dest="cmd")
    p = sub.add_parser("approve",
                       help="record the harness's approval of a cousin's"
                            " .mcp.json in the settings file"
                            " config/harness.toml names")
    p.add_argument("slug")
    p.add_argument("--root", help="the framework root (else FRAMEWORK_ROOT)")
    args = parser.parse_args(argv)
    if args.cmd == "approve":
        return _approve(args)
    env = dict(os.environ)
    if args.last_connection:
        from cousin_lib import mcp_logs
        home = env.get("COUSIN_HOME")
        if not home:
            print("cousin-mcp: --last-connection needs COUSIN_HOME",
                  file=sys.stderr)
            return 2
        last = mcp_logs.last_connection(home)
        if last is None:
            print("no MCP connection recorded for %s (looked in %s)"
                  % (home, mcp_logs.logs_dir(home)))
            return 2
        label = {"connected": "connected", "failed": "FAILED",
                 "unrecorded": "no outcome recorded"}[last["state"]]
        print("%s at %s (session %s)"
              % (label, last["when"] or "?", last["session_id"] or "?"))
        if last["earlier"]:
            print("  an earlier attempt in this session %s" % last["earlier"])
        if last["detail"]:
            print("  %s" % last["detail"])
        for line in last["stderr"].splitlines():
            print("  server stderr: %s" % line)
        print("  %s" % last["path"])
        return {"connected": 0, "failed": 1, "unrecorded": 2}[last["state"]]
    path = args.registry or default_registry_path(env)
    if not path:
        print("cousin-mcp: no registry: pass --registry PATH, or set"
              " COUSIN_HOME to a provisioned home or FRAMEWORK_ROOT to an"
              " install with config/%s[.example]" % REGISTRY_NAME,
              file=sys.stderr)
        return 2
    try:
        registry = load_registry(path, strict=False)
    except RegistryError as err:
        print("cousin-mcp: registry: %s" % err, file=sys.stderr)
        return 2
    for name, reason in registry["skipped"]:
        print("cousin-mcp: registry: tool %s skipped, the other tools still"
              " serve: %s" % (name, reason), file=sys.stderr)
    home = env.get("COUSIN_HOME", "")
    if args.selftest:
        return _selftest(registry, path)
    if args.list_tools:
        if registry["skipped"]:
            return 2      # an inspection command answers for the whole file
        print(json.dumps(list_tools(registry), indent=2, sort_keys=True))
        return 0
    if args.versions:
        versions = _sdk_versions()
        if versions is None:
            print("cousin-mcp: %s" % SDK_REMEDIATION, file=sys.stderr)
            return 2
        print(json.dumps({"supported_protocol_versions": versions,
                          "tools": [t["name"] for t in list_tools(registry)]},
                         indent=2))
        return 0
    if args.call:
        name, raw = args.call
        try:
            call_args = json.loads(raw)
        except ValueError as err:
            print("cousin-mcp: arguments are not JSON: %s" % err,
                  file=sys.stderr)
            return 2
        peers = discover_peers(registry, env)
        text, is_error = call_tool(registry, name, call_args, env, peers)
        sys.stdout.write(text)
        return 1 if is_error else 0
    try:
        serve(registry, env, home)
    except ImportError:
        print("cousin-mcp: %s" % SDK_REMEDIATION, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(mcp_main())
