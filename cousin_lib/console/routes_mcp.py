"""Console routes for WP-D, MCP and policy (docs/reference/console-api.md,
"MCP and policy"): the per-cousin and install MCP tool registries,
a cousin's .mcp.json servers, cousin-mcp's approve / selftest /
last-connection, and policy.toml.

Every edit is checked by the parser that reads the file: the registry by
mcp_server.parse_registry (strict, the runner's reading, plus the in-process
handlers on a runner cousin), .mcp.json by runner/mcp_config.parse and
policy.toml by runner/policy.Policy.parse. A TOML file is edited through
console/toml_edit (every other line kept); .mcp.json is JSON and is written
whole, with every key this editor does not model kept as it was. Each read
answers an `etag` (the file's hash) that the write must send back: a file
changed meanwhile (the model can rewrite all of these) is 409, never
overwritten.

Secrets: the runner hands .mcp.json's servers to the agent CLI on its
command line, which every user on the host can read, so a value that looks
like a secret is refused and the answer names the `${VAR}` reference to
write instead; a literal already in the file is never answered (it reads
as masked, and a save must replace it). A reference to an account variable
is refused too: the runner skips such a server.

policy.toml can never deny (or ask for, which is enforced as a deny)
mcp__cousin__handoff, and a change that removes a deny entry, an ask entry
or turns the outbound filter off answers 409 `needs_confirm` until it is
sent again with `confirm_loosening`. A change to .mcp.json, policy.toml
or a registry applies at the runner's (or the harness session's) next
start: every write answers `restart_required`, and the browser offers the
fleet's restart route."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import threading
import tomllib
import urllib.parse
from pathlib import Path

from cousin_lib import mcp_server
from cousin_lib.console import router, toml_edit
from cousin_lib.console._common import cousin_home, read_toml
from cousin_lib.console.app import HttpError
from cousin_lib.runner import mcp_config
from cousin_lib.runner import policy as runner_policy

RESTART_NOTE = "applies at the next start: restart the cousin to use it now"
REGISTRY_LIMITS = {"ceiling": (1, 50), "timeout": (5, 3600), "max_output": (1000, 1000000)}
REGISTRY_NUMBERS = ("ceiling", "timeout", "max_output")
REGISTRY_DEFAULTS = {"ceiling": mcp_server.DEFAULT_CEILING,
                     "timeout": mcp_server.DEFAULT_TIMEOUT,
                     "max_output": mcp_server.DEFAULT_MAX_OUTPUT}
POLICY_LISTS = ("deny_tools", "deny_bash_patterns", "ask")
POLICY_HEADER = ("# policy.toml - what this cousin's model may not do, as code.\n"
                 "# Written from the console; every key is explained in\n"
                 "# templates/policy.toml.example. A guardrail, not a sandbox: the model\n"
                 "# can rewrite this file, and a change applies at the runner's next start.\n")
STREAM_SCAN_BYTES = 8 * 1024 * 1024   # the start events sit near a stream's head
TEXT_MAX = 4000                       # a harness log line, cut for the answer
_BARE = re.compile(r"^[A-Za-z0-9_-]+$")
_SERVER_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}$")
_VAR = re.compile(r"\$\{([^}]+)\}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

# -- what looks like a secret -------------------------------------------------
# A heuristic, tuned to refuse what would be a leak and let configuration
# through: a known key prefix, an opaque mixed-case token, or any literal of
# eight characters or more under a name that says secret. A path is never a
# secret (a token FILE is how a secret should travel).
_SECRET_NAME = re.compile(r"(?i)(token|secret|passw|pwd|api[_-]?key|apikey|access[_-]?key|"
                          r"private[_-]?key|credential|authorization|cookie|session[_-]?id|"
                          r"signature)")
_SECRET_PREFIX = re.compile(r"^(sk-|sk_|rk_|pk_live|ghp_|gho_|ghs_|ghu_|ghr_|github_pat_|"
                            r"glpat-|xox[abposr]-|AKIA|ASIA|AIza|hf_|eyJ[A-Za-z0-9_-]+\.)")
_OPAQUE = re.compile(r"^[A-Za-z0-9_\-+=~]{24,}$")
_SCHEMES = {"bearer", "basic", "token", "digest", "apikey", "api-key"}
_PATHISH = ("/", "./", "../", "~/")


def _literal_text(value):
    """The parts of a value the CLI does not substitute: the text around
    each ${...}, plus each ${NAME:-default}'s default (it lands on the
    command line as written)."""
    parts, last = [], 0
    for m in _VAR.finditer(value):
        parts.append(value[last:m.start()])
        _name, sep, default = m.group(1).partition(":-")
        if sep:
            parts.append(" %s " % default)
        last = m.end()
    parts.append(value[last:])
    return " ".join(parts)


def _token_secret(token):
    if _SECRET_PREFIX.match(token):
        return True
    return bool(_OPAQUE.match(token)) and any(c.isupper() for c in token) \
        and any(c.islower() for c in token) and any(c.isdigit() for c in token)


def looks_secret(value, name=None):
    """Whether a literal part of `value` looks like a secret; `name` is
    what the value is called (an env or header name, a flag), if any."""
    if not isinstance(value, str):
        return False
    tokens = _literal_text(value).split()
    if any(_token_secret(t) for t in tokens):
        return True
    if name and _SECRET_NAME.search(name):
        rest = "".join(t for t in tokens
                       if t.lower() not in _SCHEMES and not t.startswith(_PATHISH))
        return len(rest) >= 8
    return False


def url_secret(url):
    """A URL carries a secret in its password, in a query parameter that
    says secret, or as an opaque token anywhere in it."""
    if not isinstance(url, str):
        return False
    literal = _literal_text(url)
    try:
        parts = urllib.parse.urlsplit(literal.replace(" ", ""))
        if parts.password:
            return True
        for key, val in urllib.parse.parse_qsl(parts.query, keep_blank_values=True):
            if looks_secret(val, key):
                return True
    except ValueError:
        pass
    return any(_token_secret(t) for t in re.split(r"[/?&=#:@\s]+", literal) if t)


def arg_secrets(args):
    """The indexes of the args that look like a secret: a `--flag=value` or
    a value after a flag that says secret, or an opaque token anywhere."""
    out = []
    for i, arg in enumerate(args):
        if not isinstance(arg, str):
            continue
        if arg.startswith("-") and "=" in arg:
            flag, _, val = arg.partition("=")
            hit = looks_secret(val, flag)
        else:
            prev = args[i - 1] if i > 0 and isinstance(args[i - 1], str) else ""
            hit = looks_secret(arg, prev if prev.startswith("-") else None)
        if hit:
            out.append(i)
    return out


def _suggest(server, what):
    return "${%s}" % re.sub(r"[^A-Z0-9]+", "_", ("%s_%s" % (server, what)).upper()).strip("_")


# -- shared helpers -----------------------------------------------------------

def _lock(server):
    return server.state.setdefault("mcp_edit_lock", threading.Lock())


def _etag(path):
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]
    except FileNotFoundError:
        return "absent"


def _check_etag(req, path):
    sent = req.body.get("etag")
    now = _etag(path)
    if sent != now:
        raise HttpError(409, "%s changed since it was loaded: reload it and make the change"
                        " again" % Path(path).name, etag=now)


def _atomic_write(path, text, mode=None):
    path = Path(path)
    if mode is None:
        mode = path.stat().st_mode & 0o7777 if path.exists() else 0o644
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix="." + path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _lane(home):
    from cousin_lib import agent_settings
    return agent_settings.summary(home)["lane"]


def _is_runner(lane):
    from cousin_lib.agent_settings import TMUX_LEGACY
    return lane != TMUX_LEGACY


def last_events(home, kinds):
    """{kind: the newest event of that kind} from the runner's primary
    stream, read from its head (the start events sit there), at most
    STREAM_SCAN_BYTES. A `policy` event counts only when it is the start's
    `describe` line (the others are per-call denials)."""
    from cousin_lib.runner import status
    path = status.primary_stream(home)
    out = {}
    if path is None:
        return out
    read = 0
    try:
        with open(path, "rb") as fh:
            for raw in fh:
                read += len(raw)
                if read > STREAM_SCAN_BYTES:
                    break
                if not raw.endswith(b"\n"):
                    break
                try:
                    event = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(event, dict) or event.get("kind") not in kinds:
                    continue
                payload = event.get("payload")
                if event["kind"] == "policy" and not (isinstance(payload, dict)
                                                      and "describe" in payload):
                    continue
                out[event["kind"]] = {"ts": event.get("ts"), "seq": event.get("seq"),
                                      "payload": payload, "stream": path.name}
    except OSError:
        return out
    return out


# -- the registry ---------------------------------------------------------------

def _example_path(root):
    checkout = Path(mcp_server.__file__).resolve().parents[1]
    for path in (Path(root) / "config" / (mcp_server.REGISTRY_NAME + ".example"),
                 checkout / "config" / (mcp_server.REGISTRY_NAME + ".example")):
        if path.is_file():
            return path
    return None


def registry_view(text, source_name):
    """The editor's view of registry text: the numbers, each tool with its
    switch, the strict parser's error (the runner's reading) and the
    tools cousin-mcp would skip (its lenient reading). Read from the raw
    TOML, so a registry over its ceiling still shows its tools."""
    out = {"error": None, "skipped": [], "tools": [], "limits": REGISTRY_LIMITS}
    out.update(REGISTRY_DEFAULTS)
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as err:
        out["error"] = "%s: %s" % (source_name, err)
        return out
    for key in REGISTRY_NUMBERS:
        if key in raw:
            out[key] = raw[key]
    tools = raw.get("tools") if isinstance(raw.get("tools"), dict) else {}
    for name, tool in tools.items():
        tool = tool if isinstance(tool, dict) else {}
        commands = tool.get("commands") if isinstance(tool.get("commands"), dict) else {}
        out["tools"].append({
            "name": name, "kind": tool.get("kind", "command"),
            "enabled": tool.get("enabled", True) is not False,
            "description": str(tool.get("description", ""))[:300],
            "commands": sorted(commands), "editable": bool(_BARE.match(name))})
    try:
        mcp_server.parse_registry(text, source_name, strict=True)
    except mcp_server.RegistryError as err:
        out["error"] = str(err)
    try:
        lenient = mcp_server.parse_registry(text, source_name, strict=False)
        out["skipped"] = [{"name": n, "reason": r} for n, r in lenient["skipped"]]
    except mcp_server.RegistryError:
        pass
    out["enabled_count"] = sum(1 for t in out["tools"] if t["enabled"])
    return out


def _registry_answer(path, *, scope, lane=None, source=None, shown=None, root=None):
    """GET's body for one registry file; `shown` is the file whose tools
    are shown when `path` is absent (the one the cousin reads instead)."""
    path = Path(path)
    body = {"ok": True, "scope": scope, "file": path.name, "exists": path.is_file(),
            "etag": _etag(path), "source": source, "lane": lane, "restart_note": RESTART_NOTE}
    read_from = path if path.is_file() else shown
    if read_from is not None:
        try:
            body.update(registry_view(Path(read_from).read_text(), Path(read_from).name))
        except OSError as err:
            body.update(registry_view("", read_from.name), error=str(err))
        body["shown"] = str(read_from)
    else:
        body.update(registry_view("", "none"), shown=None)
    if root is not None:
        body["example_exists"] = _example_path(root) is not None
    return body


def _cousin_registry(req, slug):
    home = cousin_home(req.server, slug)
    own = home / mcp_server.REGISTRY_NAME
    lane = _lane(home)
    if own.is_file():
        return home, own, _registry_answer(own, scope="cousin", lane=lane, source="own")
    env = {"FRAMEWORK_ROOT": str(req.server.root)}
    shown = mcp_server.default_registry_path(env)
    source = None if shown is None else \
        "example" if shown.name.endswith(".example") else "install"
    if shown is None and _example_path(req.server.root) is not None:
        shown, source = _example_path(req.server.root), "shipped"
    return home, own, _registry_answer(own, scope="cousin", lane=lane, source=source,
                                       shown=shown)


def _registry_changes(req, text):
    """The (table, key, value) changes a registry POST asks for: only what
    differs from the file."""
    view = registry_view(text, "registry")
    raw = tomllib.loads(text)
    changes = []
    for key in REGISTRY_NUMBERS:
        if key not in req.body:
            continue
        value = req.body[key]
        low, high = REGISTRY_LIMITS[key]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise HttpError(400, "%s must be a whole number from %d to %d" % (key, low, high))
        if raw.get(key) != value:
            changes.append(("", key, value))
    tools = req.body.get("tools", {})
    if not isinstance(tools, dict):
        raise HttpError(400, "tools must map a tool name to true or false")
    known = {t["name"]: t for t in view["tools"]}
    for name, enabled in tools.items():
        if name not in known:
            raise HttpError(400, "no tool %s in this registry (adding a tool is a file edit)"
                            % name)
        if not isinstance(enabled, bool):
            raise HttpError(400, "tools.%s must be true or false" % name)
        if not known[name]["editable"]:
            raise HttpError(400, "tool %s is not a bare TOML name: edit it by hand" % name)
        present = isinstance(raw["tools"][name], dict) and "enabled" in raw["tools"][name]
        if known[name]["enabled"] == enabled and (present or enabled):
            continue
        changes.append(("tools.%s" % name, "enabled", enabled))
    return changes


def _write_registry(req, path, *, runner_lane):
    with _lock(req.server):
        if not path.is_file():
            raise HttpError(409, "%s does not exist here: copy the default first" % path.name)
        _check_etag(req, path)
        text = path.read_text()
        try:
            tomllib.loads(text)
        except tomllib.TOMLDecodeError as err:
            raise HttpError(409, "%s does not parse (%s): replace it with the default, or fix"
                            " it by hand" % (path.name, err))
        changes = _registry_changes(req, text)

        def check(new_text):
            registry = mcp_server.parse_registry(new_text, path.name, strict=True)
            if runner_lane:
                from cousin_lib.runner import tools
                missing = tools.missing_handlers(registry)
                if missing:
                    raise ValueError("the runner has no in-process handler for %s: it would"
                                     " refuse to start" % ", ".join(missing))
        if changes:
            try:
                toml_edit.write_file(path, changes, validate_text=check)
            except (ValueError, TypeError) as err:      # RegistryError is a ValueError
                raise HttpError(400, str(err))


def _copy_into(req, target, text):
    with _lock(req.server):
        if target.exists():
            if req.body.get("replace") is not True:
                raise HttpError(409, "%s exists: replacing it needs replace and its etag"
                                % target.name, etag=_etag(target))
            _check_etag(req, target)
        try:
            mcp_server.parse_registry(text, "default", strict=True)
        except mcp_server.RegistryError as err:
            raise HttpError(409, "the default registry does not validate: %s" % err)
        _atomic_write(target, text)


# -- .mcp.json ------------------------------------------------------------------

def _pairs(mapping, field, server, masked):
    out = []
    for name, value in (mapping or {}).items():
        hidden = looks_secret(value, name)
        if hidden:
            masked.append("%s.%s" % (field, name))
        out.append({"name": name, "value": None if hidden else value, "masked": hidden})
    return out


def _server_view(name, kind, entry):
    masked = []
    row = {"name": name, "type": kind,
           "ignored_keys": sorted(k for k in entry if k != "type"
                                  and k not in mcp_config.KEYS[kind])}
    if kind == "stdio":
        args = list(entry.get("args", []))
        hidden = set(arg_secrets(args))
        row["command"] = entry["command"]
        row["args"] = [{"value": None if i in hidden else a, "masked": i in hidden}
                       for i, a in enumerate(args)]
        masked += ["args.%d" % i for i in sorted(hidden)]
        row["env"] = _pairs(entry.get("env"), "env", name, masked)
    else:
        hidden = url_secret(entry["url"])
        row["url"] = None if hidden else entry["url"]
        row["url_masked"] = hidden
        if hidden:
            masked.append("url")
        row["headers"] = _pairs(entry.get("headers"), "headers", name, masked)
    from cousin_lib.accounts import AUTH_VARS
    refs = mcp_config.variables(kind, entry)
    row["account_vars"] = sorted({n for n, _ in refs if n in AUTH_VARS})
    row["unset_vars"] = sorted({n for n, has_default in refs
                                if not has_default and n not in os.environ
                                and n not in AUTH_VARS})
    row["masked"] = masked
    return row


def servers_answer(home):
    path = Path(home) / mcp_config.FILE
    body = {"ok": True, "file": mcp_config.FILE, "exists": path.is_file(),
            "etag": _etag(path), "parse_error": None, "servers": [], "kept": [],
            "lane": _lane(home), "restart_note": RESTART_NOTE,
            "reserved": mcp_config.RESERVED}
    if path.is_file():
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as err:
            text = None
            body["parse_error"] = "%s does not parse: %s" % (mcp_config.FILE, err)
        if text is not None:
            entries, skipped = mcp_config.parse(text)
            for s in skipped:
                if s["name"] is None:
                    body["parse_error"] = s["reason"]
                else:
                    body["kept"].append({"name": s["name"], "reason": s["reason"]})
            body["servers"] = [_server_view(n, k, e) for n, k, e in entries]
    body["last_event"] = last_events(home, ("mcp_config",)).get("mcp_config")
    return body


def _problem(problems, server, field, reason, key=None, suggest=None):
    problems.append({"server": server, "field": field, "key": key, "reason": reason,
                     "suggest": suggest})


def _string(value):
    return isinstance(value, str) and not _CONTROL.search(value)


def _pairs_in(raw, field, name_re, server, problems):
    """A list of {name, value} from the body as an ordered dict."""
    if raw is None:
        return {}
    if not isinstance(raw, list):
        _problem(problems, server, field, "must be a list of {name, value}")
        return {}
    out = {}
    for item in raw:
        if not isinstance(item, dict):
            _problem(problems, server, field, "each entry must be {name, value}")
            continue
        key, value = item.get("name"), item.get("value")
        if not isinstance(key, str) or not name_re.match(key):
            _problem(problems, server, field, "%r is not a valid name" % (key,), key=key)
            continue
        if key in out:
            _problem(problems, server, field, "%s is named twice" % key, key=key)
            continue
        if value is None:
            _problem(problems, server, field, "the file holds a literal secret here: replace"
                     " it with a ${VAR} reference", key=key,
                     suggest="${%s}" % key if field == "env" else _suggest(server, key))
            continue
        if not _string(value):
            _problem(problems, server, field, "must be one line of text", key=key)
            continue
        if looks_secret(value, key):
            if field == "env":
                suggest = "${%s}" % key
            else:
                words = value.split()
                scheme = words[0] + " " if len(words) > 1 and words[0].lower() in _SCHEMES else ""
                suggest = scheme + _suggest(server, key)
            _problem(problems, server, field, "looks like a secret: write a ${VAR} reference"
                     " and set the variable in the runner's environment", key=key,
                     suggest=suggest)
            continue
        out[key] = value
    return out


def _entry_in(raw, problems, names):
    """One server of the POST body as a .mcp.json entry, or None."""
    if not isinstance(raw, dict):
        _problem(problems, None, "server", "each server must be an object")
        return None, None
    name = raw.get("name")
    if not isinstance(name, str) or not _SERVER_NAME.match(name):
        _problem(problems, name if isinstance(name, str) else None, "name",
                 "a server name is 1 to 64 letters, digits, _ or -")
        return None, None
    if name == mcp_config.RESERVED:
        _problem(problems, name, "name", "`%s` is reserved: the runner serves its own tools"
                 " under it" % name)
        return None, None
    if name in names:
        _problem(problems, name, "name", "two servers named %s" % name)
        return None, None
    names.add(name)
    kind = raw.get("type", "stdio")
    if kind not in mcp_config.KEYS:
        _problem(problems, name, "type", "type must be stdio, http or sse")
        return name, None
    entry = {"type": kind}
    before = len(problems)
    if kind == "stdio":
        command = raw.get("command")
        if not isinstance(command, str) or not command.strip() or not _string(command):
            _problem(problems, name, "command", "a stdio server needs a command")
        elif any(_token_secret(t) for t in _literal_text(command).split()):
            _problem(problems, name, "command", "looks like a secret: use a ${VAR} reference",
                     suggest=_suggest(name, "command"))
        else:
            entry["command"] = command
        args = raw.get("args") or []
        # an arg is a string, or {value} as GET answers it (value None: masked)
        values = [a.get("value") if isinstance(a, dict) else a for a in args] \
            if isinstance(args, list) else None
        if values is None or not all(a is None or isinstance(a, str) for a in values):
            _problem(problems, name, "args", "args must be a list of strings")
            values = []
        for i, a in enumerate(values):
            if a is None:
                _problem(problems, name, "args", "the file holds a literal secret here: replace"
                         " it with a ${VAR} reference", key=i, suggest=_suggest(name, "token"))
            elif not _string(a):
                _problem(problems, name, "args", "must be one line of text", key=i)
        for i in arg_secrets(values):
            _problem(problems, name, "args", "looks like a secret: write a ${VAR} reference"
                     " and set the variable in the runner's environment", key=i,
                     suggest=_suggest(name, "token"))
        if values:
            entry["args"] = values
        env = _pairs_in(raw.get("env"), "env", _ENV_NAME, name, problems)
        if env:
            entry["env"] = env
    else:
        url = raw.get("url")
        if not isinstance(url, str) or not url.strip() or not _string(url):
            _problem(problems, name, "url", "an %s server needs a url" % kind)
        elif url_secret(url):
            _problem(problems, name, "url", "looks like it carries a secret: use a ${VAR}"
                     " reference", suggest=_suggest(name, "url"))
        else:
            entry["url"] = url
        headers = _pairs_in(raw.get("headers"), "headers", _HEADER_NAME, name, problems)
        if headers:
            entry["headers"] = headers
    if len(problems) > before:
        return name, None
    from cousin_lib.accounts import AUTH_VARS
    auth = sorted({n for n, _ in mcp_config.variables(kind, entry) if n in AUTH_VARS})
    if auth:
        _problem(problems, name, "variables", "names an account variable, never passed to a"
                 " server: %s" % ", ".join(auth))
        return name, None
    return name, entry


def _write_servers(req, home):
    path = Path(home) / mcp_config.FILE
    raw_servers = req.body.get("servers")
    if not isinstance(raw_servers, list):
        raise HttpError(400, "servers must be a list")
    drop = req.body.get("drop") or []
    if not isinstance(drop, list) or not all(isinstance(d, str) for d in drop):
        raise HttpError(400, "drop must be a list of server names")
    if mcp_config.RESERVED in drop:
        raise HttpError(400, "`%s` is the cousin's own server: it is kept" % mcp_config.RESERVED)
    problems, names, wanted = [], set(), []
    for raw in raw_servers:
        name, entry = _entry_in(raw, problems, names)
        if entry is not None:
            wanted.append((name, entry))
    if problems:
        raise HttpError(400, "%d problem%s in the servers" % (len(problems),
                                                             "" if len(problems) == 1 else "s"),
                        problems=problems)
    with _lock(req.server):
        _check_etag(req, path)
        doc, old = {}, {}
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, ValueError) as err:
                loaded = err
            if isinstance(loaded, dict) and isinstance(loaded.get("mcpServers", {}), dict):
                doc = loaded
                old = loaded.get("mcpServers", {})
            elif req.body.get("replace_broken") is not True:
                raise HttpError(409, "%s does not parse as a server list: replacing it needs"
                                " replace_broken" % mcp_config.FILE)
        editable = {n for n, _k, _e in mcp_config.parse(json.dumps({"mcpServers": old}))[0]}
        new = {}
        for name, entry in old.items():
            if name not in editable and name not in drop:
                new[name] = entry                  # reserved or not ours to model: as it was
        for name, entry in wanted:
            prior = old.get(name) if isinstance(old.get(name), dict) else {}
            kind = entry["type"]
            merged = {k: v for k, v in prior.items()
                      if k != "type" and k not in mcp_config.KEYS[kind]}
            if kind != "stdio" or "type" in prior or name not in old:
                merged["type"] = kind
            for key in mcp_config.KEYS[kind]:
                if key in entry:
                    merged[key] = entry[key]
            new[name] = merged
        doc = dict(doc, mcpServers=new)
        text = json.dumps(doc, indent=2) + "\n"
        loaded, skipped = mcp_config.parse(text)
        got = {n for n, _k, _e in loaded}
        lost = [s for s in skipped if s["name"] in dict(wanted)]
        if lost or not {n for n, _ in wanted} <= got:
            raise HttpError(400, "the runner would skip: %s" % "; ".join(
                "%s (%s)" % (s["name"], s["reason"]) for s in lost))
        _atomic_write(path, text)


# -- policy.toml ------------------------------------------------------------------

def _template_values(root):
    checkout = Path(mcp_server.__file__).resolve().parents[1]
    for path in (Path(root) / "templates" / "policy.toml.example",
                 checkout / "templates" / "policy.toml.example"):
        if path.is_file():
            try:
                data = tomllib.loads(path.read_text())
            except (OSError, tomllib.TOMLDecodeError):
                return None
            return {k: data.get(k) for k in runner_policy.KEYS}
    return None


def _policy_values(text):
    """(values, parse error): the four keys as the file writes them."""
    values = {"deny_tools": [], "deny_bash_patterns": [], "ask": [], "outbound_filter": True}
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as err:
        return values, "%s: cannot read: %s" % (runner_policy.FILE, err)
    for key in POLICY_LISTS:
        got = data.get(key)
        if isinstance(got, list) and all(isinstance(v, str) for v in got):
            values[key] = got
    if isinstance(data.get("outbound_filter"), bool):
        values["outbound_filter"] = data["outbound_filter"]
    try:
        runner_policy.Policy.parse(text)
    except runner_policy.PolicyError as err:
        return values, str(err)
    return values, None


def policy_answer(home, root):
    path = Path(home) / runner_policy.FILE
    body = {"ok": True, "file": runner_policy.FILE, "exists": path.is_file(),
            "etag": _etag(path), "error": None, "lane": _lane(home),
            "protected": runner_policy.HANDOFF_TOOL, "restart_note": RESTART_NOTE,
            "template": _template_values(root)}
    values = {"deny_tools": [], "deny_bash_patterns": [], "ask": [], "outbound_filter": True}
    if path.is_file():
        try:
            values, body["error"] = _policy_values(path.read_text())
        except (OSError, UnicodeDecodeError) as err:
            body["error"] = "%s: cannot read: %s" % (runner_policy.FILE, err)
    body.update(values)
    body["last_event"] = last_events(home, ("policy",)).get("policy")
    return body


def _policy_in(req):
    values, problems = {}, []
    for key in POLICY_LISTS:
        raw = req.body.get(key, [])
        if not isinstance(raw, list) or not all(isinstance(v, str) for v in raw):
            raise HttpError(400, "%s must be a list of strings" % key)
        seen = []
        for i, v in enumerate(raw):
            if not v.strip() or _CONTROL.search(v):
                raise HttpError(400, "%s[%d] must be one non-empty line" % (key, i))
            if v not in seen:
                seen.append(v)
        values[key] = seen
    for i, pattern in enumerate(values["deny_bash_patterns"]):
        try:
            re.compile(pattern)
        except re.error as err:
            problems.append({"key": "deny_bash_patterns", "index": i, "entry": pattern,
                             "reason": str(err)})
    flag = req.body.get("outbound_filter", True)
    if not isinstance(flag, bool):
        raise HttpError(400, "outbound_filter must be true or false")
    values["outbound_filter"] = flag
    if problems:
        raise HttpError(400, "%d pattern%s do%s not compile" % (
            len(problems), "" if len(problems) == 1 else "s", "es" if len(problems) == 1 else ""),
            problems=problems)
    blockers = runner_policy.Policy(deny_tools=tuple(values["deny_tools"]),
                                    ask=tuple(values["ask"])).handoff_blockers()
    if blockers:
        raise HttpError(400, "never deny %s: every generation ends through it (%s)" % (
            runner_policy.HANDOFF_TOOL, ", ".join("%s lists %s" % b for b in blockers)),
            problems=[{"key": k, "entry": e, "reason": "stops the handoff"} for k, e in blockers])
    return values


def _write_policy(req, home):
    path = Path(home) / runner_policy.FILE
    values = _policy_in(req)
    with _lock(req.server):
        _check_etag(req, path)
        fresh = False
        old = {"deny_tools": [], "deny_bash_patterns": [], "ask": [], "outbound_filter": True}
        if path.is_file():
            parsed, error = _policy_values(path.read_text())
            if error is not None:
                if req.body.get("replace_broken") is not True:
                    raise HttpError(409, "%s is broken (%s): the runner refuses to start with"
                                    " it; replacing it needs replace_broken" % (path.name, error))
                fresh = True
            else:
                old = parsed
        removed = {key: [v for v in old[key] if v not in values[key]] for key in POLICY_LISTS}
        removed["outbound_filter"] = old["outbound_filter"] is True and \
            values["outbound_filter"] is False
        if (any(removed[k] for k in POLICY_LISTS) or removed["outbound_filter"]) \
                and req.body.get("confirm_loosening") is not True:
            raise HttpError(409, "this change loosens the policy: send it again with"
                            " confirm_loosening", needs_confirm=True, removed=removed)
        present = _keys_in(path) if path.is_file() and not fresh else set()
        changes = [("", key, values[key]) for key in runner_policy.KEYS
                   if key not in present or old[key] != values[key]]
        if not changes:
            return
        try:
            toml_edit.write_file(path, changes, validate_text=_check_policy_text,
                                 initial=POLICY_HEADER, fresh=fresh)
        except (ValueError, TypeError) as err:
            raise HttpError(400, str(err))


def _check_policy_text(text):
    """Policy.parse as the writer's text hook: a PolicyError is a
    RunnerError, so it is turned into the ValueError the writer expects."""
    try:
        runner_policy.Policy.parse(text)
    except runner_policy.PolicyError as err:
        raise ValueError(str(err))


def _keys_in(path):
    try:
        return set(tomllib.loads(Path(path).read_text()))
    except (OSError, tomllib.TOMLDecodeError):
        return set()


# -- cousin-mcp ---------------------------------------------------------------------

def selftest(home, root, lane):
    """cousin-mcp --selftest as data: the registry the cousin reads, its
    enabled tools, where each command resolves, the SDK, the skipped
    tools and (a runner cousin) the commands with no in-process handler."""
    env = {"COUSIN_HOME": str(home), "FRAMEWORK_ROOT": str(root)}
    path = mcp_server.default_registry_path(env)
    out = {"ok": False, "registry": str(path) if path else None, "tools": [], "commands": [],
           "missing": [], "skipped": [], "missing_handlers": [], "error": None}
    if path is None:
        out["error"] = "no registry: none in the home, none under config/"
        return out
    try:
        registry = mcp_server.load_registry(path, strict=False)
    except mcp_server.RegistryError as err:
        out["error"] = str(err)
        return out
    out.update(ceiling=registry["ceiling"], timeout=registry["timeout"],
               max_output=registry["max_output"])
    for name, tool in registry["tools"].items():
        if not tool["enabled"]:
            continue
        row = {"name": name, "kind": tool["kind"], "commands": sorted(tool["commands"])}
        if tool["kind"] == "send":
            row["operators"] = list(tool["operators"])
        out["tools"].append(row)
    heads = mcp_server.registry_commands(registry)
    missing = mcp_server.unresolved(heads)
    beside_dir = Path(sys.executable).parent
    for head in heads:
        if head in missing:
            where = "not found beside %s or on PATH" % beside_dir
        elif os.sep in head:
            where = "as given"
        else:
            resolved = mcp_server.resolve_command(head)
            where = "beside the interpreter" if resolved == str(beside_dir / head) \
                else "on PATH: %s" % resolved
        out["commands"].append({"command": head, "where": where, "found": head not in missing})
    out["missing"] = list(missing)
    out["skipped"] = [{"name": n, "reason": r} for n, r in registry["skipped"]]
    versions = mcp_server._sdk_versions()
    out["sdk"] = {"present": versions is not None, "versions": versions or []}
    if _is_runner(lane):
        from cousin_lib.runner import tools
        out["missing_handlers"] = tools.missing_handlers(registry)
    out["ok"] = not (out["missing"] or out["skipped"] or out["missing_handlers"])
    return out


def _cut(text, limit=TEXT_MAX):
    text = str(text or "")
    return text if len(text) <= limit else text[:limit] + " [cut]"


def _settings_file(root):
    from cousin_lib.config import MissingConfigError, harness_config
    try:
        cfg = harness_config(root)
    except MissingConfigError as err:
        return None, str(err)
    settings = (cfg or {}).get("settings_file")
    if not settings:
        return None, ("config/harness.toml names no settings_file, so there is nothing to"
                      " approve in")
    return Path(settings).expanduser(), None


def approval_state(home, root):
    """Whether the harness settings file trusts this home and enables
    `cousin` for it: True, False, or None when it cannot be read."""
    path, why = _settings_file(root)
    out = {"settings_file": str(path) if path else None, "approved": None, "reason": why}
    if path is None:
        return out
    try:
        data = json.loads(path.read_text())
        entry = (data.get("projects") or {}).get(str(home)) or {}
    except (OSError, ValueError, AttributeError) as err:
        out["reason"] = "cannot read %s: %s" % (path, type(err).__name__)
        return out
    out["approved"] = bool(entry.get("hasTrustDialogAccepted")) and \
        mcp_server.SERVER_NAME in (entry.get("enabledMcpjsonServers") or [])
    return out


# -- the routes ------------------------------------------------------------------------

def register():
    @router.route("GET", "/api/cousins/{slug}/mcp/registry")
    def cousin_registry(req, slug):
        return 200, _cousin_registry(req, slug)[2]

    @router.route("POST", "/api/cousins/{slug}/mcp/registry")
    def cousin_registry_write(req, slug):
        home, own, _view = _cousin_registry(req, slug)
        _write_registry(req, own, runner_lane=_is_runner(_lane(home)))
        body = _cousin_registry(req, slug)[2]
        body["restart_required"] = True
        return 200, body

    @router.route("POST", "/api/cousins/{slug}/mcp/registry/copy-default")
    def cousin_registry_copy(req, slug):
        home = cousin_home(req.server, slug)
        operator = ((read_toml(home).get("operator") or {}).get("name"))
        try:
            text = mcp_server.render_registry(
                mcp_server.shipped_default_registry(req.server.root),
                [operator] if isinstance(operator, str) and operator else [])
        except mcp_server.RegistryError as err:
            raise HttpError(409, str(err))
        _copy_into(req, home / mcp_server.REGISTRY_NAME, text)
        body = _cousin_registry(req, slug)[2]
        body["restart_required"] = True
        return 200, body

    @router.route("GET", "/api/mcp/registry")
    def install_registry(req):
        path = Path(req.server.root) / "config" / mcp_server.REGISTRY_NAME
        return 200, _registry_answer(path, scope="install", source="install",
                                     shown=_example_path(req.server.root),
                                     root=req.server.root)

    @router.route("POST", "/api/mcp/registry")
    def install_registry_write(req):
        path = Path(req.server.root) / "config" / mcp_server.REGISTRY_NAME
        _write_registry(req, path, runner_lane=False)
        body = _registry_answer(path, scope="install", source="install",
                                shown=_example_path(req.server.root), root=req.server.root)
        body["restart_required"] = True
        return 200, body

    @router.route("POST", "/api/mcp/registry/copy-example")
    def install_registry_copy(req):
        example = _example_path(req.server.root)
        if example is None:
            raise HttpError(409, "no %s.example to copy" % mcp_server.REGISTRY_NAME)
        path = Path(req.server.root) / "config" / mcp_server.REGISTRY_NAME
        _copy_into(req, path, example.read_text())
        body = _registry_answer(path, scope="install", source="install", shown=example,
                                root=req.server.root)
        body["restart_required"] = True
        return 200, body

    @router.route("GET", "/api/cousins/{slug}/mcp/servers")
    def servers(req, slug):
        return 200, servers_answer(cousin_home(req.server, slug))

    @router.route("POST", "/api/cousins/{slug}/mcp/servers")
    def servers_write(req, slug):
        home = cousin_home(req.server, slug)
        _write_servers(req, home)
        body = servers_answer(home)
        body["restart_required"] = True
        return 200, body

    @router.route("GET", "/api/cousins/{slug}/mcp/selftest")
    def mcp_selftest(req, slug):
        home = cousin_home(req.server, slug)
        return 200, dict(selftest(home, req.server.root, _lane(home)), slug=slug)

    @router.route("GET", "/api/cousins/{slug}/mcp/last-connection")
    def last_connection(req, slug):
        from cousin_lib import mcp_logs
        home = cousin_home(req.server, slug)
        last = mcp_logs.last_connection(home, req.server.root)
        if last is not None:
            last = {k: _cut(v) if isinstance(v, str) else v for k, v in last.items()}
        return 200, {"ok": True, "slug": slug, "last": last}

    @router.route("GET", "/api/cousins/{slug}/mcp/status")
    def mcp_status(req, slug):
        home = cousin_home(req.server, slug)
        return 200, dict(approval_state(home, req.server.root), ok=True, slug=slug,
                         lane=_lane(home), mcp_json=(home / mcp_config.FILE).is_file())

    @router.route("POST", "/api/cousins/{slug}/mcp/approve")
    def approve(req, slug):
        home = cousin_home(req.server, slug)
        if not (home / mcp_config.FILE).is_file():
            raise HttpError(409, "%s has no %s to approve" % (slug, mcp_config.FILE))
        path, why = _settings_file(req.server.root)
        if path is None:
            raise HttpError(409, why)
        try:
            mcp_server.approve_registration(path, home)
        except mcp_server.ApproveError as err:
            raise HttpError(409, str(err))
        return 200, dict(approval_state(home, req.server.root), ok=True, slug=slug,
                         note="read at the cousin's next session start")

    @router.route("GET", "/api/cousins/{slug}/policy")
    def policy(req, slug):
        return 200, policy_answer(cousin_home(req.server, slug), req.server.root)

    @router.route("POST", "/api/cousins/{slug}/policy")
    def policy_write(req, slug):
        home = cousin_home(req.server, slug)
        _write_policy(req, home)
        body = policy_answer(home, req.server.root)
        body["restart_required"] = True
        return 200, body


register()
