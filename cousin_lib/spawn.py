"""cousin-spawn: how a cousin comes to exist.

The creation sequence and its cleanup contract are specified in
docs/cousins.md. The rule that shapes the code: a
failed create removes everything it made, a completed create is a real
cousin whatever happens afterwards.
"""
import argparse
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
import uuid
from pathlib import Path

from cousin_lib import agent_auth, memory
from cousin_lib.delivery import RUNNER_KINDS  # the one list of runner kinds (M6)
from cousin_lib.config import (
    EFFORT_LEVELS,
    MEMORY_SCOPES,
    RETIRED_SCOPES,
    normalize_scope,
    CousinConfig,
    FrameworkConfig,
    MissingConfigError,
    _read_harness_toml,
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
# quoting, is refused before it is persisted. Brackets are admitted
# for context-window suffixes ("some-model[1m]"): shlex keeps them in
# one word and the spawn passes argv, never a shell, so no glob runs.
_RUNTIME_VALUE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+\[\]-]{0,127}$")
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
            "runtime.%s must be one word of letters, digits and ._:/+-[]"
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
    if memory_scope is not None \
            and normalize_scope(memory_scope) not in MEMORY_SCOPES:
        raise SpawnError("memory scope must be one of %s, got %r"
                         % (", ".join(MEMORY_SCOPES), memory_scope))


class DismissRefused(SpawnError):
    """The home is kept: the archive that would hold its only copy could
    not be written, or would land inside the tree being removed."""


def _toml_quote(value):
    return '"%s"' % value.replace("\\", "\\\\").replace('"', '\\"')


def _write_cousin_toml(home, *, slug, name, role, operator=None,
                       model=None, effort=None, heartbeat=None,
                       memory_scope=None, runner=None, account=None):
    """Write via a temporary file, re-parse, then rename into place: a
    config that cannot be read back is never persisted. The [operator],
    [runtime], [heartbeat], [memory] and [agent] tables exist only when
    a value was given for them: an absent key is the documented default,
    never a copied-out one. There is no [chat] table: no cousin runs a
    chat server of its own (R10)."""
    text = (
        "[cousin]\n"
        "slug = %s\n"
        "name = %s\n"
        "role = %s\n"
        % (_toml_quote(slug), _toml_quote(name), _toml_quote(role))
    )
    if operator:
        text += "\n[operator]\nname = %s\n" % _toml_quote(operator)
    # A runner reads [agent] model and effort only ([runtime] is the tmux
    # lane's): a runner cousin's go under [agent], below.
    if runner is None and (model is not None or effort is not None):
        text += "\n[runtime]\n"
        if model is not None:
            text += "model = %s\n" % _toml_quote(model)
        if effort is not None:
            text += "effort = %s\n" % _toml_quote(effort)
    if heartbeat is not None:
        text += "\n[heartbeat]\ncontext_beat_seconds = %d\n" % heartbeat
    if memory_scope is not None:
        text += "\n[memory]\nscope = %s\n" % _toml_quote(
            normalize_scope(memory_scope))
    if runner is not None:
        text += "\n[agent]\nrunner = %s\n" % _toml_quote(runner)
        if account is not None:
            text += "account = %s\n" % _toml_quote(account)
        if model is not None:
            text += "model = %s\n" % _toml_quote(model)
        if effort is not None:
            text += "effort = %s\n" % _toml_quote(effort)
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


# The kind of a new cousin when neither --runner nor COUSIN_DEFAULT_RUNNER
# names one (phase 10b R4).
DEFAULT_RUNNER = "sdk"


def spawn_lane(root, runner=None, account=None):
    """(runner, account) a new cousin is created with. runner None reads
    COUSIN_DEFAULT_RUNNER, where unset or empty is DEFAULT_RUNNER, `sdk`
    (R4: 2.0.0 has no legacy tmux lane, so "tmux-legacy" is refused by
    name); a runner must be one of RUNNER_KINDS. account None reads
    COUSIN_DEFAULT_ACCOUNT. An account must be `host` or one of
    config/accounts.toml's. SpawnError on anything else, before any
    write."""
    from cousin_lib import accounts
    from cousin_lib.agent_settings import TMUX_LEGACY
    if runner == TMUX_LEGACY:
        raise SpawnError("%s is not a lane: 2.0.0 has no legacy tmux lane;"
                         " name one of %s" % (TMUX_LEGACY, ", ".join(RUNNER_KINDS)))
    runner_from = "runner"
    if runner is None:
        runner = os.environ.get("COUSIN_DEFAULT_RUNNER") or DEFAULT_RUNNER
        runner_from = "COUSIN_DEFAULT_RUNNER"
    if runner not in RUNNER_KINDS:
        raise SpawnError("%s must be one of %s, got %r"
                         % (runner_from, ", ".join(RUNNER_KINDS), runner))
    account_from = "account"
    if account is None:
        account = os.environ.get("COUSIN_DEFAULT_ACCOUNT") or None
        account_from = "COUSIN_DEFAULT_ACCOUNT"
    if account is None:
        return runner, None
    if not isinstance(account, str):
        raise SpawnError("%s must be an account name, got %r"
                         % (account_from, account))
    if account != accounts.HOST:
        try:
            known = accounts.load(root)
        except accounts.AccountsError as err:
            raise SpawnError("%s %r: %s" % (account_from, account, err))
        if account not in known:
            raise SpawnError("%s %r is not in config/accounts.toml (known: %s)"
                             % (account_from, account,
                                ", ".join(sorted(known)) or "none"))
    return runner, account


def create_cousin(root, *, slug, role, name=None, role_paragraph=None,
                  voice=None, port=None, template_path=None, operator=None,
                  model=None, effort=None, heartbeat=None, memory_scope=None,
                  runner=None, account=None):
    """The creation sequence from the spec: validate, create, write
    atomically, render, provision the MCP adapter - and on any failure
    after the home exists, remove everything this run created. port is
    refused: no cousin runs a chat server of its own (R10).
    model, effort, heartbeat and memory_scope are optional and land in
    cousin.toml ([runtime], [heartbeat] context_beat_seconds, [memory]
    scope); runner and account land in [agent] (spawn_lane: the
    COUSIN_DEFAULT_RUNNER and COUSIN_DEFAULT_ACCOUNT defaults, where an
    unset runner is `sdk`), and so do a runner cousin's model
    and effort, the keys its runner reads, checked by its lane
    (agent_settings.check_new); each is validated before anything is
    written.
    Returns {slug, home}."""
    root = FrameworkConfig(root).root
    if port is not None:
        raise SpawnError("port %r: 2.0.0 runs no per-cousin chat server, so a"
                         " cousin has no chat port" % (port,))
    if not slug or not _SLUG_RE.match(slug):
        raise SpawnError(
            "invalid slug %r: use ^[a-z][a-z0-9_-]{1,31}$" % (slug,)
        )
    _check_spawn_options(model=model, effort=effort, heartbeat=heartbeat,
                         memory_scope=memory_scope)
    runner, account = spawn_lane(root, runner, account)
    if runner is not None:
        # the lane's own rules, as the console's settings apply them: an
        # account that runs on it, a model and effort only where it reads them
        from cousin_lib import agent_settings
        table = {"runner": runner}
        for key, value in (("account", account), ("model", model), ("effort", effort)):
            if value is not None:
                table[key] = value
        try:
            agent_settings.check_new(root, table)
        except agent_settings.SettingsError as err:
            raise SpawnError("[agent] for runner %s: %s" % (runner, err))
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
    # Render BEFORE anything is written: an incomplete identity must
    # fail while the filesystem is still untouched.
    values = {
        "NAME": name,
        "SLUG": slug,
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
                           operator=operator, model=model,
                           effort=effort, heartbeat=heartbeat,
                           memory_scope=memory_scope, runner=runner,
                           account=account)
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
        apply_project_settings(home, root=root, kind=_settings_kind(home))
    except Exception as err:
        # The partial state is the one that squats a slug; a failed
        # create leaves nothing.
        shutil.rmtree(home, ignore_errors=True)
        raise SpawnError("create failed, home removed: %s" % err)
    return {"slug": slug, "home": home}


def _pid_file(home):
    return Path(home) / "data" / "chat-server.pid"


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


SUPERVISOR_STOP_TIMEOUT = 60.0     # a waited stop is answered once the runner is down (up to 35 s)


class NoSupervisor(SpawnError):
    """A runner-lane start found no cousin-supervisor for the root."""


def runner_lane(home):
    """The cousin runs on cousin-runner (`[agent] runner` is one of RUNNER_KINDS),
    the test every runner-lane caller uses; its start and stop are the
    supervisor's (R10). A cousin.toml that is missing or does not parse
    is the tmux lane, as delivery reads it."""
    from cousin_lib import delivery
    return delivery._runner_kind(home) in RUNNER_KINDS


def _supervisor_root(home, root):
    """The root whose supervisor holds this cousin: the caller's, else the
    one runner.main.root_for derives from the home (the root the runner
    itself runs under)."""
    if root is not None:
        return Path(root)
    from cousin_lib.runner.main import root_for
    return root_for(home)


def _start_runner(home, root):
    from cousin_lib import supervisor
    root = _supervisor_root(home, root)
    try:
        answer = supervisor.request(root, "start", slug=Path(home).name)
    except supervisor.SupervisorUnavailable:
        raise NoSupervisor("no cousin-supervisor is running for %s: start it"
                           " with `cousin-supervisor run`" % root)
    if not answer.get("ok"):
        raise SpawnError("cousin-supervisor refused the start: %s"
                         % (answer.get("error") or "no reason given"))


def _stop_runner(home, root, wait=True, by="spawn.stop_cousin"):
    """Ask the supervisor to stop the cousin's runner child and hold it
    down (it writes <home>/run/held; the next start removes it). With
    wait the answer comes once it is down; without, once it is signalled
    (`stopping`). A timeout while the supervisor is still up reads as
    `stopping`, never as stopped. With no supervisor running nothing
    runs to stop, but the stop still holds (O9): the hold is written
    here, `"held": true`, so a supervisor started later leaves the cousin
    down until `start`, as it would had it been up to take the stop.
    With no supervisor to ask, delivery.is_alive still says whether a
    runner is there to be held: a hand-started one (no supervisor ever
    launched it) reads truthfully as "running", never a guessed "not
    running"; this call cannot signal it either way, only hold it down
    for the next supervisor."""
    from cousin_lib import delivery, supervisor
    root = _supervisor_root(home, root)
    try:
        answer = supervisor.request(root, "stop", slug=Path(home).name,
                                    wait=wait, by=by,
                                    timeout=SUPERVISOR_STOP_TIMEOUT)
    except supervisor.SupervisorUnavailable:
        if supervisor.snapshot(root) is None:
            runner_state = "running" if delivery.is_alive(home) else "not running"
            out = {"runner": runner_state, "supervisor": "not running"}
            try:
                supervisor.hold(home, by)
            except OSError as err:
                return dict(out, held=False, error="cannot hold: %s" % err)
            return dict(out, held=True)
        return {"runner": "stopping", "supervisor": "running"}
    if answer.get("ok"):
        return {"runner": answer.get("state") or "stopped",
                "supervisor": "running"}
    error = answer.get("error") or "refused"
    if error.startswith("no child named"):
        return {"runner": "not running", "supervisor": "running"}
    return {"runner": "unknown", "supervisor": "running", "error": error}


def stop_cousin(home, *, tmux_bin="tmux", tmux_socket=None,
                port_pid=_pid_bound_to_port, term_wait=5.0, root=None,
                wait=True, by="spawn.stop_cousin"):
    """Kill the tmux session and stop the chat server: the pid spawn
    wrote, else the process bound to the cousin's port on this host.
    Idempotent; the result names what each half was found doing.

    On the runner lane the cousin-supervisor stops the runner (the
    runner finishes its turn on SIGTERM) and holds it down, across a
    supervisor restart too, until the next start; the result is
    {"runner": <child state>, "supervisor": "running" | "not running"}
    (with no supervisor, also `"held": true`: the hold is written here);
    root locates the supervisor (else derived from the home). wait
    (default) answers once the runner is down; wait=False once it is
    signalled, `"runner": "stopping"`. by is who asked, for the hold
    marker.

    A worker has no session: its stop is a no-op that says so,
    {"worker": "no session", "note": <lane_refusal>} (R14). Any other
    cousin with no runner kind is refused with delivery.lane_refusal
    before any tmux call (R2)."""
    if runner_lane(home):
        return _stop_runner(home, root, wait=wait, by=by)
    from cousin_lib import delivery
    data = delivery._cousin_toml(home) or {}
    if (data.get("cousin") or {}).get("type") == "worker":
        return {"worker": "no session", "note": delivery.lane_refusal(home)}
    raise SpawnError(delivery.lane_refusal(home))
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
    stopped = [what for what, state in (("agent", tmux_state),
                                        ("chat server", chat_state))
               if state == "stopped"]
    if stopped:
        framework_event(home, "session", "%s stopped" % " and ".join(stopped))
    return {"tmux": tmux_state, "chat_server": chat_state}


def _skip_secrets(info):
    """tarfile filter: the home's .secrets/ (the api_key mode's key
    file) never enters an archive; the key is revoked or reissued, not
    kept beside a dismissed cousin."""
    parts = Path(info.name).parts
    if agent_auth.SECRETS_DIR in parts[1:]:
        return None
    return info


def dismiss_cousin(root, *, slug, tmux_bin="tmux", tmux_socket=None,
                   stop=None):
    """Stop, archive the whole home to <root>/data/dismissed/
    <slug>-<YYYYmmdd-HHMMSS>.tar.gz, then remove the tree. A failed
    archive REFUSES the delete: untracked notes exist only on disk and
    the archive is their one copy. Harness-side directories named by
    config/harness.toml are left in place and reported.

    A cousin with no runner kind (not a worker) is dismissed without the
    stop, which 2.0.0 refuses for it (R2): nothing 2.0.0 started can be
    running for it. The result then carries "stop": "skipped" and a
    "note" with delivery.lane_refusal."""
    root = FrameworkConfig(root).root
    home = root / "cousins" / slug
    if not (home / "cousin.toml").is_file():
        raise SpawnError("no cousin %r under %s" % (slug, root / "cousins"))
    archive_dir = root / "data" / "dismissed"
    if archive_dir.resolve().is_relative_to(home.resolve()):
        raise DismissRefused(
            "refusing to delete: archive dir %s is inside the tree being"
            " deleted; home kept" % archive_dir)
    from cousin_lib import delivery
    skipped = None
    if not runner_lane(home) and \
            ((delivery._cousin_toml(home) or {}).get("cousin") or {}).get("type") != "worker":
        skipped = {"stop": "skipped",
                   "note": "%s; the stop is skipped: nothing 2.0.0 started runs for it"
                           % delivery.lane_refusal(home)}
    else:
        stop_fn = stop or stop_cousin
        stop_fn(home, tmux_bin=tmux_bin, tmux_socket=tmux_socket)
    archive = archive_dir / ("%s-%s.tar.gz"
                             % (slug, time.strftime("%Y%m%d-%H%M%S")))
    try:
        archive_dir.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive, "w:gz") as tf:
            tf.add(home, arcname=slug, filter=_skip_secrets)
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
            "left_in_place": left, **(skipped or {})}


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


def read_runtime_value(home, key):
    """cousin.toml [runtime] <key> as a string, or None when unset or
    the file cannot be read."""
    try:
        data = tomllib.loads((Path(home) / "cousin.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return None
    value = (data.get("runtime") or {}).get(key)
    return None if value is None else str(value)


def framework_event(home, topic, content, **extra):
    """An L1_FRAMEWORK raw entry: a state change the framework itself
    made or observed. Best-effort (memory.record_event never raises).
    Topics are `framework:<kind>`, stable so the distiller folds
    repeats into one line per kind."""
    return memory.record_event(home, "L1_FRAMEWORK", "framework:%s" % topic,
                               content, "framework", **extra)


def persist_runtime(home, key, value):
    """Set cousin.toml [runtime] model or effort the way the session id
    is persisted, after check_runtime_value. The console's effort and
    model routes and any CLI that edits these go through here; the
    running agent keeps its old value until the next start. A real
    change (not a same-value save) is recorded as an L1 event. True when
    the value changed."""
    check_runtime_value(key, value)
    previous = read_runtime_value(home, key)
    _persist_runtime_line(home, key, value)
    if previous == value:
        return False
    framework_event(home, key, "%s %s -> %s (applies at the next"
                    " start)" % (key, previous or "(install default)",
                                 value))
    return True


# The validating turn of a model change, run as a child process: the
# turn's own budget (sdk.validate_account's 90 s) plus a grace for the
# child's start and teardown, after which it is killed.
VALIDATE_TURN_MODULE = "cousin_lib.runner.validate_turn"
VALIDATE_TURN_TIMEOUT = 90.0
VALIDATE_TURN_GRACE = 15.0


def validate_turn_out_of_process(home, root, model, effort, *, account=None,
                                 timeout=VALIDATE_TURN_TIMEOUT,
                                 grace=VALIDATE_TURN_GRACE, command=None):
    """(rc, line) of one validating turn on the cousin's account, run in a
    child of the same interpreter (runner.validate_turn), never in this
    process: sdk.validate_account scrubs os.environ process-wide for the
    turn, and the console has other threads (#100 review). The child
    starts without any accounts.AUTH_VARS variable and in a session of its
    own, so a timeout kills it and the CLI it started. Only its JSON
    verdict is read; anything else is a failed turn (4). `account` names
    the account to run on (the one a pending change writes); None is the
    one cousin.toml names."""
    from cousin_lib import accounts
    argv = list(command or [sys.executable, "-m", VALIDATE_TURN_MODULE])
    argv += ["--home", str(home), "--root", str(root), "--model", model]
    if effort:
        argv += ["--effort", effort]
    if account:
        argv += ["--account", account]
    argv += ["--timeout", "%g" % timeout]
    env = {k: v for k, v in os.environ.items() if k not in accounts.AUTH_VARS}
    # the child imports the cousin_lib this process runs, wherever its cwd is
    package_root = str(Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (package_root, env.get("PYTHONPATH")) if p)
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, encoding="utf-8",
                                errors="replace", env=env, start_new_session=True)
    except OSError as err:
        return 2, "validate: cannot start the validating process: %s" % err
    try:
        out, err = proc.communicate(timeout=timeout + grace)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        proc.communicate()
        return 4, "validate: no answer within %.0fs" % timeout
    for line in reversed(out.splitlines()):
        try:
            verdict = json.loads(line)
        except ValueError:
            continue
        if isinstance(verdict, dict) and isinstance(verdict.get("rc"), int) \
                and isinstance(verdict.get("line"), str):
            return verdict["rc"], verdict["line"]
    said = (err.strip().splitlines() or ["no output"])[-1][:300]
    return 4, "validate: the validating process exited %s without a verdict: %s" % (
        proc.returncode, said)


def _agent_unchanged(agent, key, value):
    """Whether writing `value` to [agent] `key` leaves the table as it is:
    the same value, a removal of a key that is not there, or [agent.sessions]
    modes it already has ("primary" is the absent default)."""
    if value is None:
        return key not in agent
    if key == "sessions":
        if not isinstance(value, dict):
            return False            # validate refuses it with the parser's reason
        current = agent.get("sessions") or {}
        return all(current.get(kind, "primary") == mode for kind, mode in value.items())
    if key not in agent:
        return False
    current = agent[key]
    if isinstance(current, bool) or isinstance(value, bool):
        return type(current) is type(value) and current == value
    return current == value         # 80 and 80.0 are the same percentage


def persist_agent_values(home, changes, *, root=None):
    """Write `changes` ({key: value}, None removes the key) into a runner-lane
    cousin's [agent], the keys its runner reads: the ONE write path for
    them (the console's settings panel, its model and effort routes, #100).
    A value the table already holds is dropped first: a same-value save
    runs no turn, writes nothing and records nothing. What is left is
    checked as the runner checks it (agent_settings.validate: every key on
    its own, then the table as a whole: the account on this lane, an
    opencode model against its account); an sdk model then passes one
    smallest turn on the cousin's own account (the runner's
    validate_account: NEVER_UNRUN), run in a child process
    (validate_turn_out_of_process) on the effort being written with it;
    and agent_settings.apply writes every change in one atomic write. A
    tmux model is written as given (no pane here to run a turn in).
    Refusals: agent_settings.SettingsError with a reason per key; nothing
    is written then. SpawnError when the cousin is on the tmux lane.
    Returns the keys that changed, each recorded as an L1 event."""
    from cousin_lib import accounts, agent_settings, delivery
    from cousin_lib.config import FrameworkConfig
    home = Path(home)
    if not runner_lane(home):
        raise SpawnError("%s is a tmux cousin: its model and effort are [runtime]'s"
                         " and it has no [agent] settings" % home.name)
    lane = delivery._runner_kind(home)
    agent = tomllib.loads((home / "cousin.toml").read_text()).get("agent") or {}
    root = Path(root) if root is not None else FrameworkConfig.root_from_home(home)
    changes = {k: v for k, v in dict(changes).items()
               if not _agent_unchanged(agent, k, v)}
    if not changes:
        return []
    out = agent_settings.validate(home, root, changes)
    model = out.get("model")
    if model and lane == agent_settings.TURN_LANE:
        # the table as it will be: the turn runs on the account and the effort
        # the same change writes, never the ones still in the file
        merged = agent_settings.merged(agent, out)
        try:
            account = agent_settings.account_of(root, merged, home)
        except accounts.AccountsError as err:
            raise agent_settings.SettingsError({"account": str(err)})
        # a deprecated api_key_file account has no name the child can look
        # up; the file still holds it (the key is read-only here)
        name = None if (account.implicit and account.kind == "anthropic-key") else account.name
        rc, line = validate_turn_out_of_process(home, root, model, merged.get("effort"),
                                                account=name)
        if rc != 0:
            raise agent_settings.SettingsError({"model": "model %s did not pass one turn on"
                                                         " account %s: %s"
                                                         % (model, account.name, line)})
    try:
        agent_settings.apply(home, root, out)
    except (TypeError, ValueError) as err:
        if isinstance(err, agent_settings.SettingsError):
            raise
        # a file toml_edit cannot edit in place (an inline [agent.sessions]
        # table, say): a refusal naming the keys, the file untouched
        raise agent_settings.SettingsError({key: str(err) for key in out})
    for key, value in out.items():
        previous = agent.get(key)
        framework_event(home, key, "[agent] %s %s -> %s (applies at the next start)"
                        % (key, "(unset)" if previous is None else previous,
                           "(unset)" if value is None else value))
    return list(out)


def persist_agent_value(home, key, value, *, root=None):
    """Set a runner-lane cousin's [agent] model or effort (the console's
    model and effort routes, #100; [runtime] is the tmux lane's and the
    runner never reads it): check_runtime_value's one-word rule, then
    persist_agent_values, the one write path, with its refusal as a
    SpawnError. True when the value changed."""
    from cousin_lib import agent_settings
    check_runtime_value(key, value)
    try:
        return bool(persist_agent_values(home, {key: value}, root=root))
    except agent_settings.SettingsError as err:
        raise SpawnError(str(err))

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
        if normalize_scope(value) not in MEMORY_SCOPES:
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
    if key == "memory_scope":
        value = normalize_scope(value)
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


def resume_rule(root):
    """config/harness.toml [agent.resume]: how the agent command resumes
    an existing session instead of starting a new one. session_arg is
    the words of config/agent-cmd that start a session under a given
    id (e.g. "--session-id {session_id}"), resume_arg the words that
    resume one (e.g. "--resume {session_id}"). None when absent."""
    try:
        data = _read_harness_toml(root) or {}
    except MissingConfigError as err:
        raise SpawnError(str(err))
    table = (data.get("agent") or {}).get("resume")
    if table is None:
        return None
    if not isinstance(table, dict) or not all(
            isinstance(table.get(k), str) and "{session_id}" in table[k]
            for k in ("session_arg", "resume_arg")):
        raise SpawnError(
            "config/harness.toml [agent.resume] needs session_arg and"
            " resume_arg, each carrying {session_id}")
    return {"session_arg": table["session_arg"],
            "resume_arg": table["resume_arg"]}


def resume_agent_cmd(agent_cmd, root, session_id):
    """The agent command that resumes session_id: the [agent.resume]
    session_arg in agent_cmd swapped for resume_arg, the id rendered.
    A SpawnError says why a resume is not possible."""
    if not re.fullmatch(r"[a-z0-9-]+", session_id or ""):
        raise SpawnError("session id %r is not resumable" % (session_id,))
    rule = resume_rule(root)
    if rule is None:
        raise SpawnError("config/harness.toml has no [agent.resume]")
    if rule["session_arg"] not in agent_cmd:
        raise SpawnError("config/agent-cmd does not carry %r, the"
                         " [agent.resume] session_arg"
                         % rule["session_arg"])
    return agent_cmd.replace(rule["session_arg"], rule["resume_arg"]) \
        .replace("{session_id}", session_id)


# A clean stop (flip.close_session) ends the generation the way a flip
# does and leaves the next generation's boot packet here. The next
# start, whatever starts it, consumes it: a fresh session (never a
# resume of the closed one) with the packet typed in once the agent is
# up. A flip supersedes a pending packet with its own.
PENDING_BOOT = "pending-boot.json"
BOOT_SETTLE_SECONDS = 8


def pending_boot_path(home):
    return Path(home) / "data" / PENDING_BOOT


def pending_boot(home):
    """The pending packet's record ({generation, packet, written_at}),
    or None when there is none or its packet file is gone."""
    try:
        data = json.loads(pending_boot_path(home).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("packet"):
        return None
    if not Path(data["packet"]).is_file():
        return None
    return data


def _inject_pending_boot(home, config, *, tmux_bin, tmux_socket, settle):
    """Type a pending packet into the just-started session and clear
    it. Best-effort: a failure leaves the record for the next start and
    never fails this one."""
    pending = pending_boot(home)
    if pending is None:
        pending_boot_path(home).unlink(missing_ok=True)
        return False
    try:
        text = Path(pending["packet"]).read_text()
        from cousin_lib import delivery
        time.sleep(settle)
        item = delivery.Item(
            thread_id=delivery.thread_id("system"), source="boot",
            body="[cousin-start] the last session closed cleanly; boot packet"
                 " follows. Do not announce the restart.\n" + text)
        delivery.deliver(home, item, tmux_bin=tmux_bin, socket=tmux_socket)
    except Exception:  # noqa: BLE001 - best-effort, see docstring
        return False
    pending_boot_path(home).unlink(missing_ok=True)
    return True


def start_cousin(home, *, agent_cmd, tmux_bin="tmux", tmux_socket=None,
                 start_chat_server=None, root=None,
                 record=True, note=None, boot_settle=BOOT_SETTLE_SECONDS):
    """THE tmux-session-creation site - the only one in this codebase,
    by spec. Any future respawn machinery calls this function.

    agent_cmd is host configuration: what it means to 'run an agent'
    (binary, flags, trust model) differs per install and is never
    hardcoded here. Its {model} and {effort} placeholders render from
    the cousin's [runtime], else the install's [agent] defaults
    (render_agent_cmd); root locates config/harness.toml for those
    defaults and falls back to FRAMEWORK_ROOT, then the home's
    grandparent.

    A successful start is recorded in the cousin's raw memory as an L1
    event (framework:session) unless record is False - the flip records
    its own, richer entry. note, when given, is appended to it.

    A packet a clean stop left (pending_boot) is typed in after
    boot_settle seconds; the caller is responsible for not resuming
    the closed session (resume_plan declines while one is pending).

    On the runner lane (runner_lane) nothing here runs: the
    cousin-supervisor is asked to start the cousin's runner, the one
    launcher of a runner process (R10), and agent_cmd, the tmux
    arguments and start_chat_server are not used. No supervisor is
    NoSupervisor; a refused start is a SpawnError with its reason.

    A cousin with no runner kind is refused with delivery.lane_refusal
    before anything runs: 2.0.0 has no legacy tmux lane (R2)."""
    if runner_lane(home):
        return _start_runner(home, root)
    from cousin_lib import delivery
    raise SpawnError(delivery.lane_refusal(home))
    config = CousinConfig.load(home)
    agent_cmd = render_agent_cmd(agent_cmd, home, root=root)
    # The auth mode's checks (key file, isolated harness config) run
    # here so a refusal is this call's error, not a pane that closes;
    # the launcher repeats them at exec time, where they bind.
    launch_root = _resolve_root(home, root)
    try:
        agent_auth.preflight(home, launch_root)
    except agent_auth.AuthError as err:
        raise SpawnError("auth: %s" % err)
    # The durable floor is a derived view of raw memory; the boot
    # packet regenerates it, but only a flip assembles one. A plain
    # start or a --resume at boot never did, so a cousin that was only
    # ever resumed had no distilled views at all (2026-09-18). Every
    # start refreshes it. Best-effort: a failed distill never stops a
    # start.
    try:
        from cousin_lib import distill
        distill.distill(home)
    except Exception:
        pass
    # The framework part of CLAUDE.md follows the template at every
    # start and flip, before the agent reads it: otherwise a template
    # change reaches only cousins spawned after it. The old file is
    # kept in data/claude-md-backups/. Best-effort: a file that cannot
    # be synced (no marker line) starts as it is.
    try:
        from cousin_lib import template_sync
        template_sync.sync(home, launch_root, apply=True)
    except Exception:
        pass
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
    ] + agent_auth.launcher_argv(home, launch_root) + shlex.split(agent_cmd)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=10,
                       check=False)
    if r.returncode != 0:
        raise SpawnError(
            "tmux new-session failed (rc=%d): %s"
            % (r.returncode, (r.stderr or "").strip()[:200])
        )
    if session_id is not None:
        _persist_session_id(home, session_id)
    if record:
        text = "agent started"
        if session_id is not None:
            text += " on new session %s" % session_id[:8]
        if note:
            text += "; %s" % note
        if pending_boot(home) is not None:
            text += "; boot packet from the clean stop injected"
        framework_event(home, "session", text)
    if pending_boot_path(home).exists():
        _inject_pending_boot(home, config, tmux_bin=tmux_bin,
                             tmux_socket=tmux_socket, settle=boot_settle)


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


def resume_plan(home, root, agent_cmd):
    """(agent command, note) that resumes the cousin's last session, or
    (None, why not). Resuming needs [agent.resume] in harness.toml, a
    runtime.session_id, and - when harness.toml names transcripts_dir -
    that session's transcript on disk (a resume of a session the
    harness no longer has would open an empty pane)."""
    try:
        data = tomllib.loads((Path(home) / "cousin.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return None, "cousin.toml unreadable"
    if pending_boot(home) is not None:
        return None, ("the last session closed cleanly; starting fresh on"
                      " its boot packet")
    session_id = str((data.get("runtime") or {}).get("session_id") or "")
    if not session_id:
        return None, "no runtime.session_id yet"
    try:
        cmd = resume_agent_cmd(agent_cmd, root, session_id)
    except SpawnError as err:
        return None, str(err)
    from cousin_lib import transcript_mine
    path = transcript_mine.transcript_path(home, root, session_id)
    if path is not None and not path.is_file():
        return None, "no transcript for session %s" % session_id[:8]
    return cmd, "resumed session %s" % session_id[:8]


def _start_existing(root, slug, agent_cmd, resume=False):
    home = root / "cousins" / slug
    config = CousinConfig.load(home)
    if _session_alive(config.tmux_session):
        print("%s is already running (tmux session %s); nothing started"
              % (slug, config.tmux_session))
        return 0
    cmd, note = agent_cmd, None
    if resume:
        resumed, why = resume_plan(home, root, agent_cmd)
        if resumed is None:
            print("%s: not resuming (%s); starting a new session"
                  % (slug, why))
        else:
            cmd, note = resumed, why
    try:
        start_cousin(home, agent_cmd=cmd, root=root, note=note)
    except SpawnError as err:
        print("cousin-spawn: start failed: %s" % err, file=sys.stderr)
        return 1
    print("started %s%s" % (slug, " (%s)" % note if note else ""))
    return 0


def _start_existing_runner(root, slug):
    """`cousin-spawn <slug> --start` on a runner cousin: the supervisor
    starts it (a no-op when a runner already holds its lock).

    delivery.is_alive reads the runner's own lock, which a stopping
    runner can still hold for up to ~35s after a no-wait stop; trusting
    that alone would report "already running" for a cousin the
    supervisor already holds down (supervisor.is_held, written at once
    by the stop) and never ask it to start. When held, the alive
    short-circuit is skipped and the supervisor is asked instead: it
    answers "still stopping" while the old runner is on its way out, or
    starts a fresh one once it is down - never a silent no-op."""
    from cousin_lib import delivery, supervisor
    home = root / "cousins" / slug
    if delivery.is_alive(home) and not supervisor.is_held(home):
        print("%s is already running (cousin-runner); nothing started" % slug)
        return 0
    try:
        start_cousin(home, agent_cmd=None, root=root)
    except SpawnError as err:
        print("cousin-spawn: start failed: %s" % err, file=sys.stderr)
        return 1
    print("started %s (cousin-supervisor)" % slug)
    return 0


def _settings_kind(home):
    """"tmux" when the home's [agent] runner is the tmux kind (its settings
    carry the kind's keys and bridge hooks, harness_settings.TMUX_KEYS), else
    None. An unreadable cousin.toml is None: the file's own error is the
    caller's to report."""
    try:
        data = tomllib.loads((Path(home) / "cousin.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return None
    return "tmux" if (data.get("agent") or {}).get("runner") == "tmux" else None


def _repair_settings(root, slug):
    home = root / "cousins" / slug
    if not (home / "cousin.toml").is_file():
        print("cousin-spawn: no cousin %r under %s"
              % (slug, root / "cousins"), file=sys.stderr)
        return 2
    try:
        out = apply_project_settings(home, root=root, kind=_settings_kind(home))
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
    for warning in out.get("warnings") or ():
        print("cousin-spawn: warning: %s" % warning, file=sys.stderr)
    return 0


def _sync_template(root, slug, *, apply):
    from cousin_lib import template_sync

    home = Path(root) / "cousins" / slug
    if not (home / "cousin.toml").is_file():
        print("cousin-spawn: no cousin %r" % slug, file=sys.stderr)
        return 2
    try:
        text, notes = template_sync.diff(home, root)
        result = template_sync.sync(home, root, apply=apply)
    except template_sync.SyncError as err:
        print("cousin-spawn: %s: %s" % (slug, err), file=sys.stderr)
        return 2
    if text and not apply:
        sys.stdout.write(text)
    for note in notes:
        print("note: %s" % note)
    if result["registry_added"]:
        print("mcp-registry.toml: %s addition(s) %s: %s" % (
            len(result["registry_added"]),
            "added" if apply else "would be added",
            ", ".join(result["registry_added"])))
    if result["registry_corrected"]:
        print("mcp-registry.toml: %s field(s) %s to the current wording: %s" % (
            len(result["registry_corrected"]),
            "corrected" if apply else "would be corrected",
            ", ".join(result["registry_corrected"])))
    if not result["changed"]:
        print("%s: CLAUDE.md already follows the template" % slug)
    elif apply:
        print("%s: CLAUDE.md synced; the old one is %s"
              % (slug, result["backup"]))
    else:
        print("%s: dry run; --apply writes it (a start or flip does too)"
              % slug)
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
    parser.add_argument("--operator",
                        help="the operator's name: written to cousin.toml"
                             " [operator] and named in the cousin's MCP"
                             " registry so `send` can reach them")
    parser.add_argument("--model",
                        help="cousin.toml [agent] model, the key the"
                             " cousin's runner reads (checked by its kind);"
                             " absent, the kind's own default applies")
    parser.add_argument("--effort", choices=EFFORT_LEVELS,
                        help="cousin.toml [agent] effort (the sdk and tmux"
                             " kinds read it)")
    parser.add_argument("--heartbeat", type=int, metavar="SECONDS",
                        help="cousin.toml [heartbeat] context_beat_seconds"
                             " (absent: the documented default)")
    parser.add_argument("--memory-scope",
                        choices=MEMORY_SCOPES + tuple(RETIRED_SCOPES),
                        metavar="{%s}" % ",".join(MEMORY_SCOPES),
                        help="cousin.toml [memory] scope (absent: private);"
                             " shared = may propose to the shared tier")
    parser.add_argument("--runner", choices=RUNNER_KINDS,
                        help="cousin.toml [agent] runner: the kind"
                             " cousin-supervisor runs the cousin on (absent:"
                             " COUSIN_DEFAULT_RUNNER, else sdk)")
    parser.add_argument("--account",
                        help="cousin.toml [agent] account, one of"
                             " config/accounts.toml's (absent:"
                             " COUSIN_DEFAULT_ACCOUNT, else host)")
    parser.add_argument("--start", action="store_true",
                        help="start the cousin (a runner cousin, through"
                             " cousin-supervisor) after creating it; on an EXISTING"
                             " cousin, given without --role/--voice, just"
                             " start it (a no-op when already running)")
    parser.add_argument("--resume", action="store_true",
                        help="with --start on an existing cousin: resume"
                             " its last session (config/harness.toml"
                             " [agent.resume]) instead of a new one; falls"
                             " back to a new session when that is not"
                             " possible. What the start-at-boot unit uses")
    parser.add_argument("--sync-template", action="store_true",
                        help="create nothing: show how an EXISTING"
                             " cousin's CLAUDE.md framework part differs"
                             " from the current template (every start"
                             " and flip syncs it by itself); with"
                             " --apply, write it")
    parser.add_argument("--apply", action="store_true",
                        help="with --sync-template: write the sync")
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
    if not args.repair_settings and not args.sync_template \
            and not start_existing:
        for flag, value in (("--role", args.role), ("--voice", args.voice)):
            if not value:
                parser.error("%s is required to create a cousin" % flag)
    if root is None:
        print("cousin-spawn: %s" % root_error, file=sys.stderr)
        return 2
    if args.repair_settings:
        return _repair_settings(root, args.slug)
    if args.sync_template:
        return _sync_template(root, args.slug, apply=args.apply)
    if exists and not start_existing:
        print("cousin-spawn: cousin %r already exists; to start it:"
              " cousin-spawn %s --start" % (args.slug, args.slug),
              file=sys.stderr)
        return 2
    on_runner = False
    if start_existing:
        on_runner = runner_lane(root / "cousins" / args.slug)
    elif args.start:
        try:
            on_runner = spawn_lane(root, args.runner, args.account)[0] \
                is not None
        except SpawnError as err:
            print("cousin-spawn: %s" % err, file=sys.stderr)
            return 2
    if start_existing and on_runner:
        return _start_existing_runner(root, args.slug)
    if args.start and not on_runner:
        # R2: 2.0.0 has no legacy tmux lane: nothing is created or started
        # for a cousin with no runner kind.
        from cousin_lib import delivery
        # (A new cousin always has one: spawn_lane, R4.)
        why = delivery.lane_refusal(root / "cousins" / args.slug)
        print("cousin-spawn: %s" % why, file=sys.stderr)
        return 2
    agent_cmd = None
    if args.start and not on_runner:
        # Everything a start needs is checked before anything is
        # created: a half-made cousin whose start then crashes is the
        # failure this exists to prevent. A runner cousin needs neither
        # config/agent-cmd nor tmux: cousin-supervisor starts it.
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
        return _start_existing(root, args.slug, agent_cmd,
                               resume=args.resume)
    try:
        out = create_cousin(
            root, slug=args.slug, role=args.role, name=args.name,
            role_paragraph=args.role_paragraph, voice=args.voice,
            operator=args.operator, model=args.model,
            effort=args.effort, heartbeat=args.heartbeat,
            memory_scope=args.memory_scope, runner=args.runner,
            account=args.account,
        )
    except SpawnError as err:
        print("cousin-spawn: %s" % err, file=sys.stderr)
        return 2
    print("created %s at %s" % (out["slug"], out["home"]))
    if args.start:
        try:
            start_cousin(out["home"], agent_cmd=agent_cmd, root=root)
        except SpawnError as err:
            print("cousin-spawn: created but start failed: %s\n"
                  "the home is kept; fix the cause and start it with:"
                  " cousin-spawn %s --start" % (err, out["slug"]),
                  file=sys.stderr)
            return 1
        print("started %s" % out["slug"])
    return 0
