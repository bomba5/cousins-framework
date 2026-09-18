"""cousin-spawn: how a cousin comes to exist.

The creation sequence and its cleanup contract are specified in
docs/spawn-and-template-spec.md. The rule that shapes the code: a
failed create removes everything it made, a completed create is a real
cousin whatever happens afterwards.
"""
import argparse
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
import uuid
from pathlib import Path

from cousin_lib.config import (
    EFFORT_LEVELS,
    MEMORY_SCOPES,
    CousinConfig,
    FrameworkConfig,
    MissingConfigError,
    agent_config,
    expand_harness_path,
    harness_config,
)
from cousin_lib.harness_settings import SettingsError, apply_project_settings
from cousin_lib.mcp_server import (RegistrationError, provision_mcp,
                                   refresh_mcp_json)
from cousin_lib.template import TemplateError, render_template
from cousin_lib.trace import traced_cli

_SLUG_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
# A [runtime] value renders into the agent command and is then split
# by shlex: anything a shell would treat as more than one word, or as
# quoting, is refused before it is persisted.
_RUNTIME_VALUE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,127}$")
# The [runtime] keys the placeholders read; session_id has its own
# mint-and-persist path and is never set by hand through these.
_RUNTIME_KEYS = ("model", "effort")


class SpawnError(Exception):
    """Creation cannot proceed; the message says why."""


def check_runtime_value(key, value):
    """The value `persist_runtime` and `create_cousin` accept for a
    [runtime] key, or a SpawnError naming what was wrong."""
    if key not in _RUNTIME_KEYS:
        raise SpawnError("runtime.%s is not a key set this way; the"
                         " settable keys are %s"
                         % (key, ", ".join(_RUNTIME_KEYS)))
    if not isinstance(value, str) or not _RUNTIME_VALUE_RE.match(value):
        raise SpawnError(
            "runtime.%s must be one word of letters, digits and ._:/+-"
            " (it is rendered into the agent command), got %r"
            % (key, value))
    if key == "effort" and value not in EFFORT_LEVELS:
        raise SpawnError("runtime.effort must be one of %s, got %r"
                         % (", ".join(EFFORT_LEVELS), value))
    return value


def _check_spawn_options(*, model, effort, heartbeat, memory_scope):
    if model is not None:
        check_runtime_value("model", model)
    if effort is not None:
        check_runtime_value("effort", effort)
    if heartbeat is not None and (
            isinstance(heartbeat, bool) or not isinstance(heartbeat, int)
            or heartbeat <= 0):
        raise SpawnError("heartbeat must be a positive number of seconds,"
                         " got %r" % (heartbeat,))
    if memory_scope is not None and memory_scope not in MEMORY_SCOPES:
        raise SpawnError("memory scope must be one of %s, got %r"
                         % (", ".join(MEMORY_SCOPES), memory_scope))


class DismissRefused(SpawnError):
    """The home is kept: the archive that would hold its only copy could
    not be written, or would land inside the tree being removed."""


def _claimed_ports(root):
    """Every port in any cousin.toml under the root, whether or not it
    falls inside today's scan range - a hand-configured port outside
    the range must never become allocatable by a wider range later."""
    claimed = set()
    for cfg in FrameworkConfig(root).list_cousins():
        if cfg.chat_port:
            claimed.add(cfg.chat_port)
    return claimed


def _is_live(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


def allocate_port(root, *, start=8090, end=8200, is_live=_is_live):
    """First port in [start, end] that is neither claimed nor live.
    Exhaustion is an error: a cousin without a working chat port is a
    spawn failure, not a degraded success."""
    claimed = _claimed_ports(root)
    for port in range(start, end + 1):
        if port in claimed or is_live(port):
            continue
        return port
    raise SpawnError(
        "no free chat port in %d-%d; widen the range or free a port"
        % (start, end)
    )


def _toml_quote(value):
    return '"%s"' % value.replace("\\", "\\\\").replace('"', '\\"')


def _write_cousin_toml(home, *, slug, name, role, port, operator=None,
                       model=None, effort=None, heartbeat=None,
                       memory_scope=None):
    """Write via a temporary file, re-parse, then rename into place: a
    config that cannot be read back is never persisted. The [operator],
    [runtime], [heartbeat] and [memory] tables exist only when a value
    was given for them: an absent key is the documented default, never
    a copied-out one."""
    text = (
        "[cousin]\n"
        "slug = %s\n"
        "name = %s\n"
        "role = %s\n\n"
        "[chat]\n"
        "port = %d\n"
        "tmux_session = %s\n"
        % (_toml_quote(slug), _toml_quote(name), _toml_quote(role),
           port, _toml_quote(slug))
    )
    if operator:
        text += "\n[operator]\nname = %s\n" % _toml_quote(operator)
    if model is not None or effort is not None:
        text += "\n[runtime]\n"
        if model is not None:
            text += "model = %s\n" % _toml_quote(model)
        if effort is not None:
            text += "effort = %s\n" % _toml_quote(effort)
    if heartbeat is not None:
        text += "\n[heartbeat]\ncontext_beat_seconds = %d\n" % heartbeat
    if memory_scope is not None:
        text += "\n[memory]\nscope = %s\n" % _toml_quote(memory_scope)
    tomllib.loads(text)
    fd, tmp = tempfile.mkstemp(dir=home, suffix=".toml.tmp")
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.replace(tmp, home / "cousin.toml")


def _write_identity_files(home, *, claude_md, name, role):
    (home / "CLAUDE.md").write_text(claude_md)
    # The Open-loops section is the seam the session-end baseline
    # derivation reads (cousin_lib.audits); spawn it empty so the
    # convention exists from birth.
    (home / "STATUS.md").write_text(
        "# %s - STATUS\n\n## Open loops\n" % name
    )
    (home / "MEMORY.md").write_text("# %s - memory index\n" % name)


def create_cousin(root, *, slug, role, name=None, role_paragraph=None,
                  voice=None, port=None, template_path=None, operator=None,
                  model=None, effort=None, heartbeat=None, memory_scope=None,
                  _is_live=_is_live):
    """The creation sequence from the spec: validate, allocate, create,
    write atomically, render, provision the MCP adapter - and on any
    failure after the home exists, remove everything this run created.
    model, effort, heartbeat and memory_scope are optional and land in
    cousin.toml ([runtime], [heartbeat] context_beat_seconds, [memory]
    scope); each is validated before anything is written.
    Returns {slug, home, port}."""
    root = FrameworkConfig(root).root
    if not slug or not _SLUG_RE.match(slug):
        raise SpawnError(
            "invalid slug %r: use ^[a-z][a-z0-9_-]{1,31}$" % (slug,)
        )
    _check_spawn_options(model=model, effort=effort, heartbeat=heartbeat,
                         memory_scope=memory_scope)
    home = root / "cousins" / slug
    if (home / "cousin.toml").is_file():
        raise SpawnError("cousin %r already exists" % slug)
    if home.exists():
        raise SpawnError(
            "orphan directory squats the slug %r: %s has no cousin.toml; "
            "remove or complete it" % (slug, home)
        )
    template = template_path or (
        root / "templates" / "cousin-CLAUDE.template.md"
    )
    try:
        template_text = template.read_text()
    except OSError as err:
        raise SpawnError("cannot read template %s: %s" % (template, err))
    name = name or slug.capitalize()
    if port is None:
        port = allocate_port(root, is_live=_is_live)
    # Render BEFORE anything is written: an incomplete identity must
    # fail while the filesystem is still untouched.
    values = {
        "NAME": name,
        "SLUG": slug,
        "PORT": port,
        "ROLE_ONE_LINE": role,
        "ROLE_PARAGRAPH": role_paragraph or role,
    }
    if voice is not None:
        values["VOICE_GUIDE"] = voice
    try:
        claude_md = render_template(template_text, values)
    except TemplateError as err:
        raise SpawnError(str(err))
    try:
        for sub in ("memory", "data", "notes", "scripts"):
            (home / sub).mkdir(parents=True)
        _write_cousin_toml(home, slug=slug, name=name, role=role,
                           port=port, operator=operator, model=model,
                           effort=effort, heartbeat=heartbeat,
                           memory_scope=memory_scope)
        _write_identity_files(home, claude_md=claude_md, name=name,
                              role=role)
        # The harness-side registration of the cousin's tool surface:
        # the registry (default, operator filled) and the .mcp.json
        # that points the harness at it.
        provision_mcp(home, root=root, slug=slug, operator=operator)
        # The cousin's own harness project settings: its hooks, by
        # absolute path with the home in each command, and the approval
        # of its `cousin` server. Without them a session inherits
        # whatever hooks the user-wide settings carry.
        apply_project_settings(home, root=root)
    except Exception as err:
        # The partial state is the one that squats a slug; a failed
        # create leaves nothing.
        shutil.rmtree(home, ignore_errors=True)
        raise SpawnError("create failed, home removed: %s" % err)
    return {"slug": slug, "home": home, "port": port}


def _pid_file(home):
    return Path(home) / "data" / "chat-server.pid"


def _default_chat_server(home):
    """Launch the cousin's chat server detached, logging to its data
    dir, and record its pid so stop_cousin can find it. The daemon owns
    its own lifetime; spawn only starts it."""
    log = open(home / "data" / "chat-server.log", "ab")
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "cousin_lib.server.app",
             "--home", str(home)],
            stdout=log, stderr=log, stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    finally:
        log.close()
    try:
        _pid_file(home).write_text("%d\n" % proc.pid)
    except OSError:
        pass


_CHAT_SERVER_MARKERS = ("cousin_lib.server.app", "cousin-chat-server")


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _pid_is_chat_server(pid):
    """A pid file can outlive its process and point at whatever reused
    the number; only a process whose command line carries a chat-server
    marker is ours to signal. Without /proc there is nothing to check
    and the pid file is trusted."""
    try:
        cmdline = Path("/proc/%d/cmdline" % pid).read_bytes()
    except OSError:
        return os.path.isdir("/proc") is False
    text = cmdline.replace(b"\0", b" ").decode(errors="replace")
    return any(marker in text for marker in _CHAT_SERVER_MARKERS)


def _pid_bound_to_port(port):
    """The pid listening on a local TCP port, from /proc; None when
    nothing is, or on a platform without /proc."""
    inodes = set()
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = Path(table).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            cols = line.split()
            if len(cols) < 10 or cols[3] != "0A":  # 0A = LISTEN
                continue
            try:
                if int(cols[1].rsplit(":", 1)[1], 16) == port:
                    inodes.add(cols[9])
            except (ValueError, IndexError):
                continue
    if not inodes:
        return None
    try:
        entries = list(Path("/proc").iterdir())
    except OSError:
        return None
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            fds = list((entry / "fd").iterdir())
        except OSError:
            continue
        for fd in fds:
            try:
                target = os.readlink(fd)
            except OSError:
                continue
            if target.startswith("socket:[") and target[8:-1] in inodes:
                return int(entry.name)
    return None


def _tmux_base(tmux_bin, tmux_socket):
    cmd = [tmux_bin]
    if tmux_socket:
        cmd += ["-S", tmux_socket]
    return cmd


def stop_cousin(home, *, tmux_bin="tmux", tmux_socket=None,
                port_pid=_pid_bound_to_port, term_wait=5.0):
    """Kill the tmux session and stop the chat server: the pid spawn
    wrote, else the process bound to the cousin's port on this host.
    Idempotent; the result names what each half was found doing."""
    home = Path(home)
    config = CousinConfig.load(home)
    base = _tmux_base(tmux_bin, tmux_socket)
    has = subprocess.run(base + ["has-session", "-t", config.tmux_session],
                         capture_output=True, text=True, timeout=10,
                         check=False)
    if has.returncode == 0:
        subprocess.run(base + ["kill-session", "-t", config.tmux_session],
                       capture_output=True, text=True, timeout=10,
                       check=False)
        tmux_state = "stopped"
    else:
        tmux_state = "already stopped"
    pid_file = _pid_file(home)
    pid = None
    try:
        pid = int(pid_file.read_text().strip())
    except (OSError, ValueError):
        pid = None
    if pid is not None and not (_pid_alive(pid) and _pid_is_chat_server(pid)):
        pid = None
    if pid is None and config.chat_port:
        candidate = port_pid(config.chat_port)
        if candidate and _pid_alive(candidate) \
                and _pid_is_chat_server(candidate):
            pid = candidate
    chat_state = "not running"
    if pid is not None:
        try:
            os.kill(pid, signal.SIGTERM)
            chat_state = "stopped"
        except (ProcessLookupError, PermissionError):
            pass
        deadline = time.time() + term_wait
        while chat_state == "stopped" and time.time() < deadline:
            if not _pid_alive(pid):
                break
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass
            time.sleep(0.05)
    pid_file.unlink(missing_ok=True)
    return {"tmux": tmux_state, "chat_server": chat_state}


def dismiss_cousin(root, *, slug, tmux_bin="tmux", tmux_socket=None,
                   stop=None):
    """Stop, archive the whole home to <root>/data/dismissed/
    <slug>-<YYYYmmdd-HHMMSS>.tar.gz, then remove the tree. A failed
    archive REFUSES the delete: untracked notes exist only on disk and
    the archive is their one copy. Harness-side directories named by
    config/harness.toml are left in place and reported."""
    root = FrameworkConfig(root).root
    home = root / "cousins" / slug
    if not (home / "cousin.toml").is_file():
        raise SpawnError("no cousin %r under %s" % (slug, root / "cousins"))
    archive_dir = root / "data" / "dismissed"
    if archive_dir.resolve().is_relative_to(home.resolve()):
        raise DismissRefused(
            "refusing to delete: archive dir %s is inside the tree being"
            " deleted; home kept" % archive_dir)
    stop_fn = stop or stop_cousin
    stop_fn(home, tmux_bin=tmux_bin, tmux_socket=tmux_socket)
    archive = archive_dir / ("%s-%s.tar.gz"
                             % (slug, time.strftime("%Y%m%d-%H%M%S")))
    try:
        archive_dir.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive, "w:gz") as tf:
            tf.add(home, arcname=slug)
    except (OSError, tarfile.TarError) as err:
        try:
            archive.unlink(missing_ok=True)
        except OSError:
            pass
        raise DismissRefused(
            "refusing to delete: archive failed (%s); home kept" % err)
    shutil.rmtree(home)
    left = []
    try:
        cfg = harness_config(root)
    except MissingConfigError:
        cfg = None
    for key in ("transcripts_dir", "auto_memory_dir"):
        if cfg and cfg.get(key):
            left.append(str(expand_harness_path(cfg[key], home)))
    return {"slug": slug, "status": "deleted", "archive": str(archive),
            "left_in_place": left}


def _mint_session_id():
    """Mint the new generation's session identity.

    The id renders into a command string, so its charset ([a-z0-9-],
    per the lifecycle spec) is a safety property - and the check lives
    HERE, in the constructor, so it travels with the mint: a future
    edit to the generation line faces the ValueError in the same
    function rather than an assertion elsewhere that quietly stopped
    matching (or vanished under -O). The format stays a real UUID
    because agent harnesses that accept a session id typically
    validate RFC4122; a bespoke constrained alphabet would satisfy the
    charset and break the consumer."""
    session_id = str(uuid.uuid4())
    if not re.fullmatch(r"[a-z0-9-]+", session_id):
        raise ValueError(
            "minted session id violates its charset: %r" % session_id)
    return session_id


def _persist_runtime_line(home, key, value):
    """Write runtime.<key> into cousin.toml without disturbing anything
    else in the file: targeted line replace (function repl, so no
    group-reference surprises), else a line under an existing
    [runtime] header, else an appended [runtime] table. Re-parse
    before persisting; atomic rename into place."""
    path = Path(home) / "cousin.toml"
    text = path.read_text()
    line = '%s = "%s"' % (key, value)
    header = re.search(r"(?m)^\[runtime\]\s*$", text)
    if header is None:
        new_text = text.rstrip() + "\n\n[runtime]\n" + line + "\n"
    else:
        # The table body: from the header to the next header or EOF,
        # so a same-named key in another table is never the one hit.
        body_start = header.end()
        nxt = re.search(r"(?m)^\s*\[", text[body_start:])
        body_end = body_start + nxt.start() if nxt else len(text)
        body = text[body_start:body_end]
        key_re = re.compile(r"(?m)^%s\s*=.*$" % re.escape(key))
        if key_re.search(body):
            body = key_re.sub(lambda m: line, body, count=1)
        else:
            body = "\n" + line + body
        new_text = text[:body_start] + body + text[body_end:]
    parsed = tomllib.loads(new_text)
    if parsed.get("runtime", {}).get(key) != value:
        raise SpawnError("runtime.%s did not round-trip through %s"
                         % (key, path))
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text(new_text)
    os.replace(tmp, path)


def _persist_session_id(home, session_id):
    """The minted identity's write-back (see _mint_session_id)."""
    _persist_runtime_line(home, "session_id", session_id)


def persist_runtime(home, key, value):
    """Set cousin.toml [runtime] model or effort the way the session id
    is persisted, after check_runtime_value. The console's effort and
    model routes and any CLI that edits these go through here; the
    running agent keeps its old value until the next start."""
    check_runtime_value(key, value)
    _persist_runtime_line(home, key, value)



# The identity keys the console edits in place, by the name its routes
# use: (table, key) in cousin.toml.
IDENTITY_KEYS = {
    "operator": ("operator", "name"),
    "memory_scope": ("memory", "scope"),
    "heartbeat": ("heartbeat", "context_beat_seconds"),
}
OPERATOR_MAX_CHARS = 64
HEARTBEAT_MIN_SECONDS = 60
HEARTBEAT_MAX_SECONDS = 30 * 86400


def check_identity_value(key, value):
    """The value `persist_identity` accepts for an identity key, or a
    SpawnError naming what was wrong. The operator is a display name
    compared against chat senders, so it is one non-empty line; the
    heartbeat is whole seconds between a minute and thirty days."""
    if key not in IDENTITY_KEYS:
        raise SpawnError("%s is not an identity key set this way; the"
                         " settable keys are %s"
                         % (key, ", ".join(IDENTITY_KEYS)))
    if key == "operator":
        if not isinstance(value, str) or not value.strip():
            raise SpawnError("operator must be a non-empty name")
        if value != value.strip():
            raise SpawnError("operator must not start or end with"
                             " whitespace")
        if len(value) > OPERATOR_MAX_CHARS:
            raise SpawnError("operator must be at most %d characters"
                             % OPERATOR_MAX_CHARS)
        if any(ord(ch) < 32 or 127 <= ord(ch) < 160 for ch in value):
            raise SpawnError("operator must not contain control"
                             " characters")
    elif key == "memory_scope":
        if value not in MEMORY_SCOPES:
            raise SpawnError("memory_scope must be one of %s, got %r"
                             % (", ".join(MEMORY_SCOPES), value))
    elif key == "heartbeat":
        if isinstance(value, bool) or not isinstance(value, int) \
                or not HEARTBEAT_MIN_SECONDS <= value \
                <= HEARTBEAT_MAX_SECONDS:
            raise SpawnError("heartbeat must be whole seconds from %d to"
                             " %d, got %r" % (HEARTBEAT_MIN_SECONDS,
                                              HEARTBEAT_MAX_SECONDS, value))
    return value


def persist_identity(home, key, value):
    """Set one identity key in cousin.toml after check_identity_value:
    a targeted edit that keeps comments and every other table,
    re-parsed and round-trip checked before the atomic rename, so a
    refused value leaves the file as it was."""
    from cousin_lib.console.toml_edit import write_key
    check_identity_value(key, value)
    table, toml_key = IDENTITY_KEYS[key]
    try:
        write_key(home, table, toml_key, value)
    except (ValueError, tomllib.TOMLDecodeError) as err:
        raise SpawnError("%s.%s could not be written: %s"
                         % (table, toml_key, err))


def _resolve_root(home, root):
    """The framework root a start needs for config/harness.toml: the
    caller's, else FRAMEWORK_ROOT, else the home's grandparent (homes
    live at <root>/cousins/<slug>)."""
    if root is not None:
        return Path(root)
    env = os.environ.get("FRAMEWORK_ROOT")
    if env:
        return Path(env)
    return Path(home).parent.parent


def render_agent_cmd(agent_cmd, home, *, root=None):
    """Render the {model} and {effort} placeholders of an agent command
    for one cousin: cousin.toml [runtime], else config/harness.toml
    [agent] default_model / default_effort. A placeholder with no value
    in either file is a SpawnError naming both, never a guessed vendor
    default. {session_id} is left for the spawn site (or flip) to mint.
    Exposed so a flip can preflight the render before killing anything."""
    root = _resolve_root(home, root)
    config = CousinConfig.load(home)
    try:
        defaults = agent_config(root)
    except MissingConfigError as err:
        raise SpawnError(str(err))
    values = {"model": config.model or defaults["default_model"],
              "effort": config.effort or defaults["default_effort"]}
    for key in _RUNTIME_KEYS:
        placeholder = "{%s}" % key
        if placeholder not in agent_cmd:
            continue
        if not values[key]:
            raise SpawnError(
                "the agent command carries %s but neither %s [runtime]"
                " %s nor %s [agent] default_%s defines it; set one"
                % (placeholder, Path(home) / "cousin.toml", key,
                   root / "config" / "harness.toml", key))
        agent_cmd = agent_cmd.replace(placeholder, values[key])
    return agent_cmd


def start_cousin(home, *, agent_cmd, tmux_bin="tmux", tmux_socket=None,
                 start_chat_server=_default_chat_server, root=None):
    """THE tmux-session-creation site - the only one in this codebase,
    by spec. Any future respawn machinery calls this function.

    agent_cmd is host configuration: what it means to 'run an agent'
    (binary, flags, trust model) differs per install and is never
    hardcoded here. Its {model} and {effort} placeholders render from
    the cousin's [runtime], else the install's [agent] defaults
    (render_agent_cmd); root locates config/harness.toml for those
    defaults and falls back to FRAMEWORK_ROOT, then the home's
    grandparent."""
    config = CousinConfig.load(home)
    agent_cmd = render_agent_cmd(agent_cmd, home, root=root)
    # A {session_id} placeholder is rendered HERE, at the single
    # spawn site, so a plain start (console, cousin-spawn --start) and
    # a flip mint identity the same way. flip.py renders its own copy
    # before calling in (it needs the id for the generation record),
    # so by the time it arrives here the placeholder is already gone.
    session_id = None
    if "{session_id}" in agent_cmd:
        session_id = _mint_session_id()
        agent_cmd = agent_cmd.replace("{session_id}", session_id)
    cmd = [tmux_bin]
    if tmux_socket:
        cmd += ["-S", tmux_socket]
    cmd += [
        "new-session", "-d", "-s", config.tmux_session,
        "-c", str(home),
        "-e", "COUSIN_HOME=%s" % home,
        "/usr/bin/env", "COUSIN_HOME=%s" % home,
    ] + shlex.split(agent_cmd)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=10,
                       check=False)
    if r.returncode != 0:
        raise SpawnError(
            "tmux new-session failed (rc=%d): %s"
            % (r.returncode, (r.stderr or "").strip()[:200])
        )
    if session_id is not None:
        _persist_session_id(home, session_id)
    start_chat_server(home)


def _read_agent_cmd(root):
    """The agent command comes from <root>/config/agent-cmd - one line,
    host configuration. Starting a cousin without it is an error with a
    remediation, not a guessed default binary."""
    path = root / "config" / "agent-cmd"
    try:
        cmd = path.read_text().strip()
    except OSError:
        cmd = ""
    if not cmd:
        raise SpawnError(
            "no agent command configured: write the command line that "
            "runs your agent into %s" % path
        )
    return cmd


def start_preflight(agent_cmd, *, tmux_bin="tmux", which=shutil.which):
    """What a start needs from the host, checked with no side effects:
    tmux (it hosts every agent session) and the agent command's
    executable, resolved the way the session will resolve it. Returns
    the failures as remediation lines; empty means go. The agent check
    is best-effort by nature: only the first word is resolvable, and a
    wrapper such as `env` passes it."""
    failures = []
    if which(tmux_bin) is None:
        failures.append(
            "tmux not found (%r on PATH): it hosts every cousin's agent"
            " session; install it first (Debian/Ubuntu: sudo apt-get"
            " install -y tmux)" % tmux_bin)
    try:
        argv = shlex.split(agent_cmd)
    except ValueError as err:
        failures.append("the agent command in config/agent-cmd does not"
                        " parse: %s" % err)
        return failures
    head = argv[0] if argv else ""
    if head and which(head) is None:
        failures.append(
            "agent command %r (config/agent-cmd) not found on PATH; install"
            " the agent, or write its absolute path into config/agent-cmd"
            " (services started by systemd do not see a login shell's"
            " PATH)" % head)
    return failures


def _session_alive(session, tmux_bin="tmux"):
    try:
        r = subprocess.run([tmux_bin, "has-session", "-t", "=" + session],
                           capture_output=True, text=True, timeout=10,
                           check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def _chat_server_unless_live(home):
    """Start the chat server only when nothing answers on the cousin's
    port: a watchdog or unit may already own it, and a second server
    would only fail to bind."""
    try:
        port = CousinConfig.load(home).chat_port
    except MissingConfigError:
        port = None
    if port and _is_live(port):
        return
    _default_chat_server(home)


def _start_existing(root, slug, agent_cmd):
    home = root / "cousins" / slug
    config = CousinConfig.load(home)
    if _session_alive(config.tmux_session):
        print("%s is already running (tmux session %s); nothing started"
              % (slug, config.tmux_session))
        return 0
    try:
        start_cousin(home, agent_cmd=agent_cmd, root=root,
                     start_chat_server=_chat_server_unless_live)
    except SpawnError as err:
        print("cousin-spawn: start failed: %s" % err, file=sys.stderr)
        return 1
    print("started %s" % slug)
    return 0


def _repair_settings(root, slug):
    home = root / "cousins" / slug
    if not (home / "cousin.toml").is_file():
        print("cousin-spawn: no cousin %r under %s"
              % (slug, root / "cousins"), file=sys.stderr)
        return 2
    try:
        out = apply_project_settings(home, root=root)
        reg = refresh_mcp_json(home, root=root, slug=slug)
    except (SettingsError, RegistrationError) as err:
        print("cousin-spawn: %s" % err, file=sys.stderr)
        return 2
    print("settings %s: hooks for %s; \"cousin\" MCP server approved;"
          " read at the cousin's next session start"
          % (out["path"], ", ".join(out["events"])))
    print("registration %s: %s" % (
        reg["path"], "rewritten with absolute paths" if reg["changed"]
        else "already current"))
    for missing in out["missing"]:
        print("cousin-spawn: hook script not found, not wired: %s"
              % missing, file=sys.stderr)
    return 0


@traced_cli("cousin-spawn")
def spawn_main(argv=None):
    """Console entry point. Exit codes are the interface: 0 created
    (and started, if asked), 1 create succeeded but --start failed
    (home kept, restartable), 2 validation or configuration error
    (nothing written)."""
    parser = argparse.ArgumentParser(prog="cousin-spawn")
    parser.add_argument("slug")
    parser.add_argument(
        "--root",
        help="the framework root: a directory containing templates/"
             " and cousins/ (typically the checkout itself), not an"
             " install prefix. Falls back to FRAMEWORK_ROOT.")
    parser.add_argument("--name")
    parser.add_argument("--role", help="required to create")
    parser.add_argument("--role-paragraph")
    parser.add_argument("--voice",
                        help="the authored voice guide; a cousin is "
                             "never shipped without one (required to"
                             " create)")
    parser.add_argument("--port", type=int)
    parser.add_argument("--operator",
                        help="the operator's name: written to cousin.toml"
                             " [operator] and named in the cousin's MCP"
                             " registry so `send` can reach them")
    parser.add_argument("--model",
                        help="cousin.toml [runtime] model: what the agent"
                             " command's {model} placeholder renders to;"
                             " absent, config/harness.toml [agent]"
                             " default_model applies")
    parser.add_argument("--effort", choices=EFFORT_LEVELS,
                        help="cousin.toml [runtime] effort, rendered into"
                             " the {effort} placeholder; absent, [agent]"
                             " default_effort applies")
    parser.add_argument("--heartbeat", type=int, metavar="SECONDS",
                        help="cousin.toml [heartbeat] context_beat_seconds"
                             " (absent: the documented default)")
    parser.add_argument("--memory-scope", choices=MEMORY_SCOPES,
                        help="cousin.toml [memory] scope (absent: private)")
    parser.add_argument("--start", action="store_true",
                        help="start the cousin (tmux session + chat"
                             " server) after creating it; on an EXISTING"
                             " cousin, given without --role/--voice, just"
                             " start it (a no-op when already running)")
    parser.add_argument("--repair-settings", action="store_true",
                        help="create nothing: (re)write an EXISTING"
                             " cousin's harness project settings"
                             " (<home>/.claude/settings.json: its hooks"
                             " and its MCP server approval) and its"
                             " <home>/.mcp.json `cousin` entry (absolute"
                             " paths), merging with what is there; safe"
                             " to repeat")
    args = parser.parse_args(argv)
    try:
        root = FrameworkConfig.resolve(args.root, cwd_fallback=True).root
    except MissingConfigError as err:
        root = None
        root_error = err
    exists = root is not None and (
        root / "cousins" / args.slug / "cousin.toml").is_file()
    # `cousin-spawn <slug> --start` on an existing cousin starts it;
    # with --role or --voice it is still a create, refused below.
    start_existing = (args.start and exists and not args.role
                      and not args.voice)
    if not args.repair_settings and not start_existing:
        for flag, value in (("--role", args.role), ("--voice", args.voice)):
            if not value:
                parser.error("%s is required to create a cousin" % flag)
    if root is None:
        print("cousin-spawn: %s" % root_error, file=sys.stderr)
        return 2
    if args.repair_settings:
        return _repair_settings(root, args.slug)
    if exists and not start_existing:
        print("cousin-spawn: cousin %r already exists; to start it:"
              " cousin-spawn %s --start" % (args.slug, args.slug),
              file=sys.stderr)
        return 2
    agent_cmd = None
    if args.start:
        # Everything a start needs is checked before anything is
        # created: a half-made cousin whose start then crashes is the
        # failure this exists to prevent.
        try:
            agent_cmd = _read_agent_cmd(root)
        except SpawnError as err:
            print("cousin-spawn: %s; nothing created or started" % err,
                  file=sys.stderr)
            return 2
        failures = start_preflight(agent_cmd)
        if failures:
            for line in failures:
                print("cousin-spawn: %s" % line, file=sys.stderr)
            print("cousin-spawn: nothing created or started",
                  file=sys.stderr)
            return 2
    if start_existing:
        return _start_existing(root, args.slug, agent_cmd)
    try:
        out = create_cousin(
            root, slug=args.slug, role=args.role, name=args.name,
            role_paragraph=args.role_paragraph, voice=args.voice,
            port=args.port, operator=args.operator, model=args.model,
            effort=args.effort, heartbeat=args.heartbeat,
            memory_scope=args.memory_scope,
        )
    except SpawnError as err:
        print("cousin-spawn: %s" % err, file=sys.stderr)
        return 2
    print("created %s at %s (chat port %d)"
          % (out["slug"], out["home"], out["port"]))
    if args.start:
        try:
            start_cousin(out["home"], agent_cmd=agent_cmd, root=root,
                         start_chat_server=_chat_server_unless_live)
        except SpawnError as err:
            print("cousin-spawn: created but start failed: %s\n"
                  "the home is kept; fix the cause and start it with:"
                  " cousin-spawn %s --start" % (err, out["slug"]),
                  file=sys.stderr)
            return 1
        print("started %s" % out["slug"])
    return 0
