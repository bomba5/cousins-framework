"""Fleet routes (docs/reference/console-api.md, "Fleet: cousins" and "Tokens"):
the registry read on every call and enriched with liveness, chat
health, activity, the newest reply and today's tokens; spawn, dismiss,
start, stop, restart, the editors, peer delivery and the two flip
paths, each through the library a CLI would use."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path

from cousin_lib import agent_auth, delivery, loops, spawn, supervisor
from cousin_lib.config import (DEFAULT_MODELS, EFFORT_LEVELS, MEMORY_SCOPES,
                               CousinConfig, FrameworkConfig,
                               MissingConfigError, agent_config,
                               harness_config)
from cousin_lib.console import longop, router, tokens
from cousin_lib.console._common import (chat_call, chat_health, check_slug,
                                        cousin_home, load_cousin, read_toml,
                                        session_alive, tmux)
from cousin_lib.console.app import HttpError
from cousin_lib.console.toml_edit import write_key
from cousin_lib.runner import restart_note

ACTIVE_WINDOW_SECONDS = 60
ROLE_MAX_CHARS = 5000
CLAUDE_MD_MAX_CHARS = 200000


# ---- the fleet projection ----------------------------------------------

def _pane_tail(server, config):
    """The pane's last 20 lines, or None when there is no local pane to
    read or tmux fails."""
    if config.chat_host or not config.tmux_session:
        return None
    try:
        r = tmux(server, ["capture-pane", "-p", "-t", config.tmux_session,
                          "-S", "-20"])
    except Exception:
        return None
    return r.stdout or ""


def attention_patterns(root):
    """config/harness.toml attention_patterns; [] when absent or when
    the file is unusable (the fleet listing must not fail on it)."""
    try:
        cfg = harness_config(root)
    except MissingConfigError:
        return []
    return (cfg or {}).get("attention_patterns") or []


def _attention(tail, patterns):
    """The first attention pattern the pane shows, else None."""
    if not tail:
        return None
    for pattern in patterns:
        if pattern in tail:
            return pattern
    return None


def _pane_active(server, config, tail=None):
    """The pane's last 20 lines changed within the window, by hash kept
    per server: a projection of the terminal, not a store."""
    if tail is None:
        tail = _pane_tail(server, config)
    if tail is None:
        return False
    digest = hashlib.sha1(tail.encode()).hexdigest()
    hashes = server.state.setdefault("pane_hashes", {})
    now = time.time()
    prev = hashes.get(config.slug)
    if prev is None:
        hashes[config.slug] = (digest, now)
        return False
    if prev[0] != digest:
        hashes[config.slug] = (digest, now)
        return True
    return now - prev[1] < ACTIVE_WINDOW_SECONDS


def _pane_pid(server, config):
    """The agent process in the cousin's session: tmux's #{pane_pid}
    of the session's first pane, for the exactly named session (the
    `=` prefix refuses prefix matches), through the console's own
    binary and socket. list-panes, not display-message: on a live
    tmux 3.6a `display-message -p -t =name` printed an empty line for
    a session target, which read as "no pid" on every running cousin.
    None when tmux fails or prints nothing usable."""
    try:
        r = tmux(server, ["list-panes", "-t", "=" + config.tmux_session,
                          "-F", "#{pane_pid}"],
                 timeout=2)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    first = ((r.stdout or "").strip().splitlines() or [""])[0]
    try:
        pid = int(first)
    except ValueError:
        return None
    return pid if pid > 0 else None


def uptime_seconds(pid, proc="/proc"):
    """Seconds since the process started: /proc/<pid>/stat's start
    time (field 22, clock ticks since boot) against /proc/uptime,
    else `ps -o etimes=`, else None. Unknown is None, never 0: a zero
    reads as "just started"."""
    if not pid:
        return None
    try:
        stat = (Path(proc) / str(pid) / "stat").read_text()
        up = float((Path(proc) / "uptime").read_text().split()[0])
        # The command name sits in parentheses and may hold spaces;
        # split after the closing one so the field numbers hold.
        start_ticks = int(stat.rsplit(")", 1)[1].split()[19])
        return max(0, int(up - start_ticks / os.sysconf("SC_CLK_TCK")))
    except (OSError, ValueError, IndexError):
        pass
    try:
        r = subprocess.run(["ps", "-o", "etimes=", "-p", str(pid)],
                           capture_output=True, text=True, timeout=2,
                           check=False)
        if r.returncode == 0 and (r.stdout or "").strip():
            return max(0, int(r.stdout.strip()))
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return None


def _to_unix(ts):
    try:
        when = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return 0
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return int(when.timestamp())


def _last_msg_ts(config, chat):
    if chat != "ok" or not config.operator_name:
        return 0
    try:
        status, body = chat_call(
            config, "/api/history?user=%s&limit=20"
            % config.operator_name.replace(" ", "%20"), timeout=1.5)
    except HttpError:
        return 0
    if status != 200 or not isinstance(body, dict):
        return 0
    newest = 0
    for row in body.get("messages") or []:
        if isinstance(row, dict) and row.get("type") == config.slug:
            newest = max(newest, _to_unix(row.get("timestamp")))
    return newest


def _is_runner(config):
    """A runner cousin on this machine (cousin.toml `[agent] runner`): no
    chat server, no tmux session; its row is read from its own stores."""
    if getattr(config, "home", None) is None:
        return False
    from cousin_lib import delivery
    return isinstance(delivery.backend_for(config.home), delivery.InboxBackend)


def _runner_last_msg_ts(config):
    """The newest reply's time among the last 20 live rows of the operator's
    thread, from chat.db (the tmux row's _last_msg_ts asks the chat server
    for the same rows). Read-only: a GET never creates the store, and a
    busy or missing one is "no reply yet", never a 500."""
    import sqlite3
    from cousin_lib.server import chat_api
    from cousin_lib.server.storage import normalize_chat_user
    path = chat_api.db_path(config.home)
    if not config.operator_name or not path.exists():
        return 0
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=1.0)
        try:
            rows = conn.execute(
                "SELECT timestamp, type FROM messages WHERE chat_user=? AND archived=0"
                " ORDER BY id DESC LIMIT 20",
                (normalize_chat_user(config.operator_name),)).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return 0
    return max([_to_unix(ts) for ts, kind in rows if kind == config.slug] or [0])


def _activity(home):
    try:
        return (Path(home) / "data" / "last-activity.txt") \
            .read_text(errors="replace").strip()[:200]
    except OSError:
        return ""


def agent_defaults(root):
    """config/harness.toml [agent] for the fleet projection. A file
    that cannot be read must not take the fleet listing down with it:
    the rows then carry no install default (null), and the spawn
    options route, which can afford to, reports the reason."""
    try:
        return agent_config(root)
    except MissingConfigError:
        return {"default_model": None, "default_effort": None,
                "models": list(DEFAULT_MODELS)}


def effective_runtime(config, defaults):
    """The model and effort the NEXT start of this cousin renders. A tmux
    cousin: its own [runtime] value, else the install default, else null (a
    placeholder would then fail the start, and the row says so by showing
    nothing rather than a guess). A runner cousin (#100): its [agent]
    value, the one its runner reads, else null (the CLI's own default)."""
    if spawn.runner_lane(config.home):
        try:
            agent = tomllib.loads((config.home / "cousin.toml").read_text()).get("agent") or {}
        except (OSError, tomllib.TOMLDecodeError):
            agent = {}
        return {"model": agent.get("model"), "effort": agent.get("effort")}
    return {"model": config.model or defaults["default_model"],
            "effort": config.effort or defaults["default_effort"]}


_UNREAD = object()


def supervisor_state(snap, slug):
    """The supervisor's view of a cousin's runner: {"state", "reason"} from
    its snapshot (run/supervisor.json), or None when there is no live
    snapshot or no `runner:<slug>` child in it. Unknown is null, never
    "stopped" (R7). The reason says why a child is `failing` (a tmux
    runner that gave up on its pane names what it saw)."""
    children = (snap or {}).get("children")
    row = children.get("runner:%s" % slug) if isinstance(children, dict) else None
    if not isinstance(row, dict) or not row.get("state"):
        return None
    return {"state": row["state"], "reason": row.get("reason")}


def lane_fields(home):
    """The row's lane fields: `lane` (the [agent] runner kind, else
    "tmux-legacy"), `account` (null on tmux-legacy), `autoStart` (null on
    tmux-legacy), `held` (<home>/run/held: a stop holds the runner down)
    and `loginRequired`: data/login-required.json's reason, action line and
    since, or null. Never its detail, which carries the API's own words."""
    from cousin_lib import agent_settings
    from cousin_lib.runner import auth as runner_auth
    out = agent_settings.summary(home)
    out["held"] = supervisor.is_held(home)
    login = runner_auth.read_login_required(home)
    out["loginRequired"] = ({k: login.get(k) for k in ("reason", "action", "since")}
                            if isinstance(login, dict) else None)
    return out


def fleet_row(server, config, defaults=None, patterns=None, snap=_UNREAD):
    raw = read_toml(config.home)
    cousin = raw.get("cousin", {}) if isinstance(raw, dict) else {}
    chat = None if _is_runner(config) else chat_health(config)
    if defaults is None:
        defaults = agent_defaults(server.root)
    if patterns is None:
        patterns = attention_patterns(server.root)
    if snap is _UNREAD:
        snap = supervisor.snapshot(server.root)
    attention = None
    runner = None
    if _is_runner(config):
        # the runner's lock and stream, never the chat port or tmux
        from cousin_lib.runner import status as runner_status
        runner = runner_status.status(config.home)
        chat = "console"
        status = "running" if runner["alive"] else "stopped"
        active = bool(runner["alive"]) and runner["state"] in ("running",
                                                                "waiting_permission")
    elif config.type == "worker":
        status = "running"
        active = False
    elif config.chat_host:
        status = "running" if chat == "ok" else "stopped"
        active = False
    else:
        status = "running" if session_alive(server, config) else "stopped"
        active = False
        if status == "running":
            tail = _pane_tail(server, config)
            active = _pane_active(server, config, tail)
            # "running" says the session exists, not that the agent is
            # working: a pane parked on a login menu is flagged.
            attention = _attention(tail, patterns)
    # The agent process and its age: only a local, running session has
    # one to ask tmux about; everything else is null, not zero.
    pid = None
    if runner is not None:
        pid = runner["pid"] if runner["alive"] else None
    elif status == "running" and config.type != "worker" \
            and not config.chat_host:
        pid = _pane_pid(server, config)
    return {
        "slug": config.slug,
        "name": config.name,
        "role": str(cousin.get("role", "")),
        "type": config.type,
        "port": config.chat_port,
        "host": config.chat_host,
        "home": str(config.home),
        "tmuxSession": config.tmux_session,
        "operator": config.operator_name,
        "memoryScope": config.memory_scope,
        "heartbeat": config.heartbeat_seconds,
        "flipAt": config.flip_at,
        **effective_runtime(config, defaults),
        "hidden": bool(cousin.get("hidden", False)),
        "auth": _auth_mode(config.home),
        "status": status,
        "attention": attention,
        "chat": chat,
        "active": active,
        "pid": pid,
        "uptime_seconds": uptime_seconds(pid) if pid else None,
        "activity": _activity(config.home),
        "lastMsgTs": (_runner_last_msg_ts(config) if runner is not None
                      else _last_msg_ts(config, chat)),
        "runner": runner,
        "tokensSpent": tokens.today_total(server, config.home),
        "supervisor": supervisor_state(snap, config.slug),
        **lane_fields(config.home),
    }


def _auth_mode(home):
    """The cousin's auth mode for its row; null when cousin.toml holds
    a value the framework does not know (the start would refuse it)."""
    try:
        return agent_auth.read_mode(home)
    except agent_auth.AuthError:
        return None


def auth_status(server, slug):
    """GET /api/cousins/<slug>/auth: the mode, the modes, and what may
    be shown about the key file (set or not, its last four characters
    at most). The key itself never leaves the host through here."""
    cousin_home(server, slug)
    try:
        out = agent_auth.status(server.root, slug)
    except agent_auth.AuthError as err:
        raise HttpError(500, str(err))
    out.pop("key_file", None)
    out.pop("isolated_dir", None)
    return {"ok": True, **out}


def fleet_rows(server):
    """The local cousins, then (with the hive on) the remote nodes the
    queen knows as `type: "remote"` rows (cousin_lib/console/hive.py)."""
    from cousin_lib.console import hive as console_hive
    defaults = agent_defaults(server.root)
    patterns = attention_patterns(server.root)
    snap = supervisor.snapshot(server.root)
    rows = [fleet_row(server, config, defaults, patterns, snap)
            for config in FrameworkConfig(server.root).list_cousins()]
    return rows + console_hive.remote_rows(
        server, {row["slug"] for row in rows})


def spawn_lane_options(root):
    """What the spawn dialog offers for the lane: `runners` (the kinds,
    from delivery.RUNNER_KINDS; none chosen is the tmux lane),
    `default_runner` (COUSIN_DEFAULT_RUNNER, else null), `accounts` (host,
    then config/accounts.toml's by name, each with its kind, the kinds it
    runs on (agent_settings.check_lane, the runner's rule, the tmux kind's
    refusal of a key or token account included) and, for an opencode
    account, `models`: the "<provider>/" suggestions it offers; no secret
    is in that file)
    with `accounts_error` when the file cannot be read, `lane_keys`, the
    [agent] keys each kind reads, and `lane_models`, how each kind that
    reads a model takes it (agent_settings.model_rule). `tmux_lane` is the
    runner value that names the tmux lane explicitly."""
    from cousin_lib import accounts, agent_settings
    kinds = agent_settings.kinds()
    error = None
    try:
        known = accounts.load(root)
    except accounts.AccountsError as err:
        known, error = {}, str(err)
    listed = [accounts.Account(accounts.HOST, "claude-login", None, None, implicit=True)]
    listed += [known[name] for name in sorted(known)]
    rows = []
    for account in listed:
        lanes = []
        for kind in kinds:
            try:
                agent_settings.check_lane(account, kind)
            except accounts.AccountsError:
                continue
            lanes.append(kind)
        row = {"name": account.name, "kind": account.kind, "lanes": lanes}
        models = agent_settings.account_models(account)
        if models:
            row["models"] = models
        rows.append(row)
    return {"runners": kinds, "tmux_lane": agent_settings.TMUX_LEGACY,
            "lane_models": {kind: rule for kind in kinds
                            for rule in [agent_settings.model_rule(kind)] if rule},
            "default_runner": os.environ.get("COUSIN_DEFAULT_RUNNER") or None,
            "accounts": rows, "accounts_error": error,
            "lane_keys": {kind: agent_settings.lane_keys(kind) for kind in kinds}}


# ---- commands -----------------------------------------------------------

def _start_runner(server, slug, config):
    """The runner lane: the cousin-supervisor starts the runner. No
    config/agent-cmd (a container has none), no chat server, no tmux;
    "already running" is a runner holding the cousin's lock - but a
    stopping runner can still hold it for up to ~35s after a no-wait
    stop, and supervisor.is_held is true for that whole window (the
    stop writes it at once). Trusting the lock alone there would answer
    "already running" for a cousin the supervisor already holds down,
    and never ask it to start. Held skips that short-circuit and asks
    the supervisor instead: "still stopping" while the old runner is on
    its way out, or a fresh start once it is down. 503 when no
    supervisor runs for the root."""
    if delivery.is_alive(config.home) and not supervisor.is_held(config.home):
        return {"ok": True, "slug": slug, "status": "already running"}
    server.emit("cousin-status", {"slug": slug, "status": "starting"})
    try:
        spawn.start_cousin(config.home, agent_cmd=None, root=server.root)
    except spawn.NoSupervisor as err:
        raise HttpError(503, str(err))
    except spawn.SpawnError as err:
        raise HttpError(500, str(err))
    return {"ok": True, "slug": slug, "status": "started"}


def _start(server, slug):
    config = load_cousin(server, slug)
    if spawn.runner_lane(config.home):
        return _start_runner(server, slug, config)
    chat_ok = chat_health(config) == "ok"
    if session_alive(server, config):
        if not chat_ok and not config.chat_host:
            spawn._default_chat_server(config.home)
            return {"ok": True, "slug": slug, "status": "already running",
                    "chat_server": "started"}
        return {"ok": True, "slug": slug, "status": "already running",
                "chat_server": "reused" if chat_ok else "not running"}
    try:
        agent_cmd = spawn._read_agent_cmd(server.root)
    except spawn.SpawnError as err:
        raise HttpError(500, str(err))
    server.emit("cousin-status", {"slug": slug, "status": "starting"})
    try:
        spawn.start_cousin(
            config.home, agent_cmd=agent_cmd, tmux_bin=server.tmux_bin,
            tmux_socket=server.tmux_socket, root=server.root,
            start_chat_server=((lambda home: None) if chat_ok
                               else spawn._default_chat_server))
    except spawn.SpawnError as err:
        raise HttpError(500, str(err))
    return {"ok": True, "slug": slug, "status": "started",
            "chat_server": "reused" if chat_ok else "started"}


def _stop(server, slug, by="console"):
    """Stop at once; `by` names the request in the runner's hold (a
    restart's names itself, restart_note.REQUESTED_RESTART_BY, so the
    resumed session is told to continue, #98). On the runner lane the supervisor is asked with
    wait false (R6'): the answer comes once the runner is signalled,
    `status: "stopping"`, and the fleet row's `supervisor.state` shows
    when it is down; a turn in hand can take up to 35 s. Only `stopping`
    and stopped or not running are outcomes: anything else (the
    supervisor refused, `runner: "unknown"`) is a 502 with its error,
    never `status: "stopped"` (N7)."""
    home = cousin_home(server, slug)
    server.emit("cousin-status", {"slug": slug, "status": "stopping"})
    if spawn.runner_lane(home):
        result = spawn.stop_cousin(home, root=server.root, wait=False, by=by)
        runner = result.get("runner")
        if runner == "stopping":
            status = "stopping"
        elif runner in ("stopped", "not running"):
            status = "stopped"
        else:
            server.emit("cousin-status", {"slug": slug, "status": "stop failed"})
            extra = {k: v for k, v in result.items() if k != "error"}
            raise HttpError(502, "cousin-supervisor refused the stop: %s"
                            % (result.get("error") or "no reason given"),
                            slug=slug, **extra)
        return {"ok": True, "slug": slug, "status": status, **result}
    result = spawn.stop_cousin(home, tmux_bin=server.tmux_bin,
                               tmux_socket=server.tmux_socket)
    return {"ok": True, "slug": slug, "status": "stopped", **result}


def _start_when_down(server, slug, hold, poll=0.2):
    """The second half of a runner-lane restart: wait (bounded by the
    runner's stop budget) until the supervisor's child for this cousin
    has no pid, then start it. The outcome is a cousin-status event.
    `hold` is the restart route's own exclusive mark, handed off to this
    thread since the route itself already answered 202: it stays held
    for this whole wait-then-start, and is released here, once, whatever
    happens."""
    try:
        name = "runner:%s" % slug
        deadline = time.monotonic() + supervisor.STOP_TIMEOUTS["runner"] + supervisor.KILL_GRACE_S + 5
        while time.monotonic() < deadline:
            try:
                row = supervisor.request(server.root, "status", timeout=5.0)["children"].get(name)
            except (supervisor.SupervisorUnavailable, KeyError, AttributeError):
                row = None
            if row is None or row.get("pid") is None:
                break
            time.sleep(poll)
        try:
            _start(server, slug)
        except HttpError as err:
            server.emit("cousin-status", {"slug": slug, "status": "start failed",
                                          "error": (err.body or {}).get("error")})
        else:
            server.emit("cousin-status", {"slug": slug, "status": "started"})
        server.emit("cousins-refresh", fleet_rows(server))
    finally:
        hold.release()


def _pending_flip(slug):
    for row in loops.list_requests(status="pending", limit=500):
        if row["kind"] == "flip" and row["cousin"] == slug:
            try:
                payload = json.loads(row["payload"] or "{}")
            except ValueError:
                payload = {}
            return {"request_id": row["id"],
                    "fire_at": float(payload.get("fire_at") or 0)}
    return None


def _flip_state(server, slug):
    return server.state.setdefault("flips", {}).get(slug)


def _exclusive(server, slug, what):
    """Occupy the cousin for the length of a route's own work (dismiss,
    start, stop, restart, set_auth): 409 (the same message as before) when
    a long operation (console/longop.py), a flip or a clean stop already
    runs on it, else a longop.Hold that marks it busy as `what` in the
    same table longop.start() reads - so nothing, a flip, a migrate, a
    login op or another one of these five, can start underneath it. A
    context manager: `with` releases it when the route's own work ends;
    a route whose work outlives itself (restart's background
    start-when-down) instead calls the Hold's release() itself, once,
    whenever that later work ends."""
    try:
        return longop.exclusive(server, slug, what)
    except longop.Busy as err:
        raise HttpError(409, str(err), busy=True)


def register():
    @router.route("GET", "/api/cousins")
    def list_cousins(req):
        return 200, {"cousins": fleet_rows(req.server)}

    @router.route("POST", "/api/cousins")
    def create(req):
        body = req.body
        slug = body.get("slug")
        role = body.get("role")
        voice = body.get("voice")
        if not isinstance(slug, str) or not slug:
            raise HttpError(400, "slug is required")
        if not isinstance(role, str) or not role.strip():
            raise HttpError(400, "role is required")
        if not isinstance(voice, str) or not voice.strip():
            raise HttpError(400, "voice is required: the template refuses"
                                 " to render without one")
        port = body.get("port")
        if port is not None and (isinstance(port, bool)
                                 or not isinstance(port, int)):
            raise HttpError(400, "port must be an integer")
        # The four runtime fields the dialog sends; each is validated
        # by create_cousin before anything is written (400 below).
        runtime = {}
        for key in ("model", "effort", "heartbeat", "memory_scope"):
            value = body.get(key)
            if value is not None and value != "":
                runtime[key] = value
        # The lane: `runner` (one of RUNNER_KINDS, or "tmux-legacy" for the
        # tmux lane by name) and the `account` it runs on; absent or empty,
        # COUSIN_DEFAULT_RUNNER / COUSIN_DEFAULT_ACCOUNT apply (unset: the
        # tmux lane). The dialog always names the lane.
        for key in ("runner", "account"):
            value = body.get(key)
            if value is None or value == "":
                continue
            if not isinstance(value, str):
                raise HttpError(400, "%s must be a string" % key)
            runtime[key] = value
        try:
            out = spawn.create_cousin(
                req.server.root, slug=slug, role=role,
                name=body.get("name") or None,
                role_paragraph=body.get("role_paragraph") or None,
                voice=voice, port=port, operator=body.get("operator") or None,
                **runtime)
        except spawn.SpawnError as err:
            text = str(err)
            status = 409 if ("already exists" in text or "squats" in text) \
                else 400
            raise HttpError(status, text)
        req.server.emit("cousins-refresh", fleet_rows(req.server))
        return 201, {"ok": True, "slug": out["slug"], "home": str(out["home"]),
                     "port": out["port"]}

    @router.route("DELETE", "/api/cousins/{slug}")
    def dismiss(req, slug):
        cousin_home(req.server, slug)
        with _exclusive(req.server, slug, "dismiss"):
            req.server.emit("cousin-status", {"slug": slug, "status": "stopping"})
            try:
                out = spawn.dismiss_cousin(req.server.root, slug=slug,
                                           tmux_bin=req.server.tmux_bin,
                                           tmux_socket=req.server.tmux_socket)
            except spawn.DismissRefused as err:
                raise HttpError(500, str(err))
            except spawn.SpawnError as err:
                raise HttpError(404, str(err))
        req.server.state.setdefault("flips", {}).pop(slug, None)
        longop.forget(req.server, slug)
        return 200, {"ok": True, **out}

    @router.route("POST", "/api/cousins/{slug}/start")
    def start(req, slug):
        cousin_home(req.server, slug)
        with _exclusive(req.server, slug, "start"):
            return 200, _start(req.server, slug)

    @router.route("POST", "/api/cousins/{slug}/stop")
    def stop(req, slug):
        """A running cousin stops CLEANLY by default: it is asked for
        its pre-exit writes and memory, the transcript is mined and the
        next packet assembled (flip.close_session), in the background,
        202. {"clean": false} stops at once, as kill does for the
        session. A cousin that is not running stops at once either
        way. On the runner lane a clean stop IS the runner's SIGTERM
        path (it finishes its turn, then stops): the supervisor is asked
        without waiting, and the answer is 202 `stopping` (200 `stopped`
        when there was nothing to stop)."""
        server = req.server
        config = load_cousin(server, slug)
        hold = _exclusive(server, slug, "stop")
        try:
            clean = req.body.get("clean", True)
            if not isinstance(clean, bool):
                raise HttpError(400, "clean must be a boolean")
            if spawn.runner_lane(config.home):
                out = _stop(server, slug)
                return (202 if out["status"] == "stopping" else 200), out
            if not clean or not session_alive(server, config):
                return 200, _stop(server, slug)
            # A clean stop runs in the background and marks itself busy in
            # `flips` for as long as it runs (the correct pattern already,
            # below): release this route's own mark first, inline under
            # the same lock as the recheck-and-mark that follows, so the
            # two never see each other's absence - one continuous locked
            # section, not two, closes the gap between them.
            lock = server.state.setdefault("flip_lock", threading.Lock())
            flips = server.state.setdefault("flips", {})
            with lock:
                hold.release_locked()
                current = flips.get(slug)
                if current and current["status"] == "running":
                    raise HttpError(409, "a flip or clean stop is already"
                                         " running")
                if longop.op_running(server, slug):
                    raise HttpError(409, "a %s is running on %s"
                                    % (longop.op_running(server, slug), slug))
                entry = {"status": "running", "started_at": time.time(),
                         "kind": "stop"}
                flips[slug] = entry
            run_close = server.close_fn or _default_close

            def run():
                try:
                    result = run_close(slug, tmux_bin=server.tmux_bin,
                                       tmux_socket=server.tmux_socket)
                except Exception as err:  # noqa: BLE001 - reported on the row
                    result = {"slug": slug, "ok": False, "error": str(err),
                              "stages": []}
                entry["result"] = result
                entry["stages"] = result.get("stages", [])
                entry["status"] = "done" if result.get("ok") else "failed"
                server.emit("cousin-status", {
                    "slug": slug,
                    "status": "stopped" if result.get("ok") else "stop failed"})
                server.emit("cousins-refresh", fleet_rows(server))

            server.emit("cousin-status", {"slug": slug, "status": "closing"})
            threading.Thread(target=run, daemon=True,
                             name="console-close-%s" % slug).start()
            return 202, {"ok": True, "slug": slug, "status": "closing",
                         "started_at": entry["started_at"]}
        finally:
            hold.release()

    @router.route("POST", "/api/cousins/{slug}/restart")
    def restart(req, slug):
        # A restart stays immediate: it applies a setting (a model, an
        # auth mode) and comes straight back; a clean stop is the stop
        # button's. On the runner lane the stop does not wait (R6'): a
        # runner mid-turn is answered 202 and started again in the
        # background once the supervisor reports it down.
        server = req.server
        home = cousin_home(server, slug)
        hold = _exclusive(server, slug, "restart")
        handed_off = False
        try:
            # an earlier stop's hold is the operator's decision: remember it,
            # so a refused start below puts it back instead of dropping it
            held_path = supervisor.held_path(home)
            earlier_hold = held_path.read_text() if held_path.is_file() else None
            stopped = _stop(server, slug, by=restart_note.REQUESTED_RESTART_BY)
            if stopped["status"] == "stopping":          # the runner lane only
                # the second half runs after this route has answered: the
                # mark stays held, released by that thread, not by us -
                # but only once its Thread.start() actually returns; a
                # raise there must still hit our own `finally` below
                thread = threading.Thread(
                    target=_start_when_down, args=(server, slug, hold),
                    daemon=True, name="console-restart-%s" % slug)
                thread.start()
                handed_off = True
                return 202, dict(stopped, target="cousin/%s" % slug)
            time.sleep(server.settle_seconds)
            try:
                started = _start(server, slug)
            except HttpError as err:
                if stopped.get("held"):
                    # the stop half held a cousin no supervisor ran (O9); a
                    # restart asked for it running, so a refused start leaves no
                    # hold of its own - but an earlier hold stays, word for word
                    if earlier_hold is None:
                        supervisor.release(home)
                    else:
                        tmp = held_path.with_name(held_path.name + ".tmp")
                        tmp.write_text(earlier_hold)
                        os.replace(tmp, held_path)
                return err.status, {"ok": False, "target": "cousin/%s" % slug,
                                    "stop": stopped, "start": err.body}
            return 200, {"ok": True, "target": "cousin/%s" % slug,
                         "stop": stopped, "start": started}
        finally:
            if not handed_off:
                hold.release()

    @router.route("GET", "/api/cousins/{slug}/auth")
    def get_auth(req, slug):
        return 200, auth_status(req.server, slug)

    @router.route("POST", "/api/cousins/{slug}/auth")
    def set_auth(req, slug):
        """Switch the auth mode; a running agent restarts on the same
        session unless restart is false. 409 when it is mid-turn (force
        overrides) or a long operation runs on it, 400 when the mode
        cannot be used."""
        cousin_home(req.server, slug)
        with _exclusive(req.server, slug, "auth switch"):
            mode = req.body.get("mode")
            if mode not in agent_auth.AUTH_MODES:
                raise HttpError(400, "mode must be one of %s"
                                     % ", ".join(agent_auth.AUTH_MODES))
            force = req.body.get("force", False)
            restart = req.body.get("restart", True)
            if not isinstance(force, bool) or not isinstance(restart, bool):
                raise HttpError(400, "force and restart must be booleans")
            req.server.emit("cousin-status", {"slug": slug,
                                              "status": "switching auth"})
            try:
                out = agent_auth.switch(
                    req.server.root, slug, mode, restart=restart, force=force,
                    tmux_bin=req.server.tmux_bin,
                    tmux_socket=req.server.tmux_socket)
            except agent_auth.AgentBusy as err:
                raise HttpError(409, str(err), busy=True)
            except agent_auth.AuthError as err:
                raise HttpError(400, str(err))
        req.server.emit("cousins-refresh", fleet_rows(req.server))
        return 200, {"ok": True, **out,
                     "auth": auth_status(req.server, slug)}

    @router.route("POST", "/api/cousins/{slug}/auth/key")
    def set_auth_key(req, slug):
        """Write the cousin's key file from the pasted key. The answer
        says only whether a key is set and its last four characters;
        the key is never echoed, logged or read back."""
        home = cousin_home(req.server, slug)
        key = req.body.get("key")
        if not isinstance(key, str):
            raise HttpError(400, "key must be a string")
        try:
            cfg = agent_auth.api_key_config(req.server.root)
            if cfg is None:
                raise agent_auth.AuthError(
                    "config/harness.toml has no [auth.api_key]")
            agent_auth.write_key(home, key, cfg["key_env"])
        except agent_auth.AuthError as err:
            raise HttpError(400, str(err))
        return 200, auth_status(req.server, slug)

    @router.route("POST", "/api/cousins/{slug}/role")
    def set_role(req, slug):
        home = cousin_home(req.server, slug)
        role = req.body.get("role")
        if not isinstance(role, str) or len(role) > ROLE_MAX_CHARS:
            raise HttpError(400, "role must be a string of at most %d"
                                 " characters" % ROLE_MAX_CHARS)
        write_key(home, "cousin", "role", role)
        return 200, {"ok": True, "slug": slug, "role": role}

    @router.route("GET", "/api/cousins/{slug}/claude-md")
    def get_claude_md(req, slug):
        home = cousin_home(req.server, slug)
        path = home / "CLAUDE.md"
        out = {"ok": True, "slug": slug, "path": str(path)}
        try:
            content = path.read_text(errors="replace")
        except OSError:
            out.update({"content": "", "bytes": 0, "missing": True})
            return 200, out
        out.update({"content": content, "bytes": len(content.encode())})
        return 200, out

    @router.route("POST", "/api/cousins/{slug}/claude-md")
    def set_claude_md(req, slug):
        home = cousin_home(req.server, slug)
        content = req.body.get("content")
        if not isinstance(content, str) or len(content) > CLAUDE_MD_MAX_CHARS:
            raise HttpError(400, "content must be a string of at most %d"
                                 " characters" % CLAUDE_MD_MAX_CHARS)
        path = home / "CLAUDE.md"
        if path.is_file():
            backups = home / "data" / "claude-md-backups"
            backups.mkdir(parents=True, exist_ok=True)
            (backups / ("CLAUDE-%d.md" % int(time.time()))).write_bytes(
                path.read_bytes())
        tmp = path.with_suffix(".md.tmp")
        tmp.write_text(content)
        tmp.replace(path)
        return 200, {"ok": True, "slug": slug, "bytes": len(content.encode())}

    def _set_runtime(req, slug, key):
        home = cousin_home(req.server, slug)
        value = req.body.get(key)
        if not isinstance(value, str):
            raise HttpError(400, "%s must be a string" % key)
        try:
            if spawn.runner_lane(home):
                # the runner reads [agent], never [runtime] (#100)
                changed = spawn.persist_agent_value(home, key, value, root=req.server.root)
            else:
                changed = spawn.persist_runtime(home, key, value)
        except spawn.SpawnError as err:
            raise HttpError(400, str(err))
        # The running agent keeps the value it started with; the row
        # already shows the new one, so the client is told which. A save
        # of the value it already has changes nothing: no restart, no refresh.
        if changed:
            req.server.emit("cousins-refresh", fleet_rows(req.server))
        return 200, {"ok": True, "slug": slug, key: value,
                     "restart_required": changed}

    @router.route("POST", "/api/cousins/{slug}/effort")
    def set_effort(req, slug):
        return _set_runtime(req, slug, "effort")

    @router.route("POST", "/api/cousins/{slug}/model")
    def set_model(req, slug):
        return _set_runtime(req, slug, "model")

    # Whether a saved identity value needs a restart to take effect.
    # The chat server loads cousin.toml once at its start and uses the
    # operator to tell operator messages from peers; a console restart
    # stops and starts it. The memory scope is read from cousin.toml on
    # each shared-tier call, and the loops daemon loads every
    # cousin.toml on each tick, so both apply without one.
    identity_restart = {"operator": True, "memory_scope": False,
                        "heartbeat": False}

    def _set_identity(req, slug, key):
        home = cousin_home(req.server, slug)
        if key not in req.body:
            raise HttpError(400, "%s is required" % key)
        value = req.body.get(key)
        try:
            spawn.persist_identity(home, key, value)
        except spawn.SpawnError as err:
            raise HttpError(400, str(err))
        req.server.emit("cousins-refresh", fleet_rows(req.server))
        return 200, {"ok": True, "slug": slug, key: value,
                     "restart_required": identity_restart[key]}

    @router.route("POST", "/api/cousins/{slug}/operator")
    def set_operator(req, slug):
        return _set_identity(req, slug, "operator")

    @router.route("POST", "/api/cousins/{slug}/memory-scope")
    def set_memory_scope(req, slug):
        return _set_identity(req, slug, "memory_scope")

    @router.route("POST", "/api/cousins/{slug}/heartbeat")
    def set_heartbeat(req, slug):
        return _set_identity(req, slug, "heartbeat")

    @router.route("GET", "/api/spawn/options")
    def spawn_options(req):
        try:
            defaults = agent_config(req.server.root)
        except MissingConfigError as err:
            raise HttpError(500, str(err))
        models = defaults["models"]
        return 200, {
            "models": models,
            "default_model": defaults["default_model"] or (
                models[0] if models else None),
            "efforts": list(EFFORT_LEVELS),
            "default_effort": defaults["default_effort"] or "high",
            "memory_scopes": list(MEMORY_SCOPES),
            "default_memory_scope": CousinConfig.memory_scope,
            "default_heartbeat": CousinConfig.heartbeat_seconds,
            "heartbeat_bounds": [spawn.HEARTBEAT_MIN_SECONDS,
                                 spawn.HEARTBEAT_MAX_SECONDS],
            "operator_max_chars": spawn.OPERATOR_MAX_CHARS,
            **spawn_lane_options(req.server.root),
        }

    @router.route("POST", "/api/cousins/{slug}/hidden")
    def set_hidden(req, slug):
        home = cousin_home(req.server, slug)
        hidden = req.body.get("hidden")
        if not isinstance(hidden, bool):
            raise HttpError(400, "hidden must be a boolean")
        write_key(home, "cousin", "hidden", True if hidden else None)
        return 200, {"ok": True, "slug": slug, "hidden": hidden}

    @router.route("POST", "/api/cousins/{slug}/peer")
    def peer(req, slug):
        source = load_cousin(req.server, slug)
        to = req.body.get("to")
        text = req.body.get("text")
        if not isinstance(text, str) or not text.strip():
            raise HttpError(400, "text is required")
        if not isinstance(to, str) or to == slug:
            raise HttpError(400, "to must name another cousin")
        dest = load_cousin(req.server, to)
        payload = {"user": source.name, "message": text.strip()}
        from cousin_lib import chat
        from cousin_lib.server import chat_api
        if chat.is_local_runner(dest):
            # a runner cousin needs no chat server: its store and inbox
            # directly (phase 10a)
            try:
                body = chat.deliver_local(dest, payload)
            except chat_api.BadRequest as err:
                raise HttpError(400, str(err))
            return 200, {"ok": True, "to": to, "id": body.get("id")}
        status, body = chat_call(dest, "/api/send", method="POST", payload=payload,
                                 timeout=10.0)
        if status != 200:
            return status, body
        return 200, {"ok": True, "to": to, "id": body.get("id")}

    @router.route("GET", "/api/cousins/{slug}/flip")
    def flip_status(req, slug):
        home = cousin_home(req.server, slug)
        entry = _flip_state(req.server, slug)
        out = {"ok": True}
        if entry is not None:
            out["status"] = entry["status"]
            out["started_at"] = entry["started_at"]
            if entry["status"] != "running":
                out["stages"] = entry.get("stages", [])
                out["result"] = entry.get("result", {})
        else:
            marker = home / "data" / ".flip-in-progress.json"
            if marker.exists():
                try:
                    data = json.loads(marker.read_text())
                except (OSError, ValueError):
                    data = {}
                out["status"] = "stale_marker"
                out["recovery"] = {
                    "marker": str(marker),
                    "started_at": data.get("started_at"),
                    "hint": "a flip left its marker behind; the next"
                            " cousin-flip reports and overwrites it"}
            else:
                out["status"] = "idle"
        pending = _pending_flip(slug)
        if pending:
            pending["seconds_until_fire"] = max(
                0, int(pending["fire_at"] - time.time()))
            out["pending"] = pending
        return 200, out

    @router.route("POST", "/api/cousins/{slug}/flip")
    def flip_start(req, slug):
        cousin_home(req.server, slug)
        server = req.server
        body = req.body
        confirm = bool(body.get("confirm", False))
        delay = body.get("delay_seconds", 0)
        if delay is None:
            delay = 0
        if isinstance(delay, bool) or not isinstance(delay, int) or delay < 0:
            raise HttpError(400, "delay_seconds must be a non-negative"
                                 " integer")
        if delay > 0:
            if _pending_flip(slug):
                raise HttpError(409, "a timed flip is already pending")
            fire_at = time.time() + delay
            request_id = loops.submit_request(
                "flip", cousin=slug,
                payload={"fire_at": fire_at, "reason": "console"},
                ttl_seconds=delay + loops.REQUEST_TTL_SECONDS)
            server.emit("cousin-flip", {"slug": slug, "phase": "scheduled",
                                        "fire_at": fire_at,
                                        "delay_seconds": delay})
            return 202, {"ok": True, "slug": slug, "request_id": request_id,
                         "fire_at": fire_at, "delay_seconds": delay}
        lock = server.state.setdefault("flip_lock", threading.Lock())
        flips = server.state.setdefault("flips", {})
        with lock:
            current = flips.get(slug)
            if current and current["status"] == "running":
                raise HttpError(409, "a flip is already running")
            if longop.op_running(server, slug):
                raise HttpError(409, "a %s is running on %s"
                                % (longop.op_running(server, slug), slug))
            entry = {"status": "running", "started_at": time.time()}
            flips[slug] = entry
        run_flip = server.flip_fn or _default_flip

        def run():
            try:
                result = run_flip(slug, confirm=confirm,
                                  tmux_bin=server.tmux_bin,
                                  tmux_socket=server.tmux_socket)
            except Exception as err:  # noqa: BLE001 - reported on the row
                result = {"slug": slug, "ok": False, "error": str(err),
                          "stages": []}
            entry["result"] = result
            entry["stages"] = result.get("stages", [])
            entry["status"] = "done" if result.get("ok") else "failed"
            event = {"slug": slug,
                     "phase": "complete" if result.get("ok") else "failed",
                     "ok": bool(result.get("ok"))}
            for key in ("new_generation", "boot_packet_tokens",
                        "degraded_sections", "error"):
                if key in result:
                    event[key] = result[key]
            server.emit("cousin-flip", event)

        server.emit("cousin-flip", {"slug": slug, "phase": "started"})
        threading.Thread(target=run, daemon=True,
                         name="console-flip-%s" % slug).start()
        return 202, {"ok": True, "slug": slug, "status": "running",
                     "started_at": entry["started_at"]}

    @router.route("POST", "/api/cousins/{slug}/flip/cancel")
    def flip_cancel(req, slug):
        cousin_home(req.server, slug)
        entry = _flip_state(req.server, slug)
        if entry and entry["status"] == "running":
            raise HttpError(409, "a running flip cannot be cancelled")
        pending = _pending_flip(slug)
        was_pending = False
        if pending:
            was_pending = loops.cancel_request(pending["request_id"])
            if was_pending:
                req.server.emit("cousin-flip", {"slug": slug,
                                                "phase": "cancelled"})
        return 200, {"ok": True, "slug": slug, "was_pending": was_pending}

    @router.route("GET", "/api/tokens")
    def token_series(req):
        server = req.server
        available, reason = tokens.availability(server.root)
        if not available:
            return 200, {"available": False, "reason": reason, "cousins": []}
        rows = []
        for config in FrameworkConfig(server.root).list_cousins():
            rows.append({"slug": config.slug, "name": config.name,
                         "series": tokens.series(server, config.home),
                         "cache": tokens.cache(server, config.home)})
        return 200, {"available": True, "cousins": rows}


def _default_flip(slug, **kw):
    from cousin_lib import flip
    return flip.flip(slug, **kw)


def _default_close(slug, **kw):
    from cousin_lib import flip
    return flip.close_session(slug, **kw)


register()
