"""Chat-server watchdog: one idempotent ensure pass over the fleet.

A cousin's chat server is started detached by `cousin-spawn --start`
and `cousin-flip`, and nothing supervises it afterwards: when it dies
the cousin goes silent on the chat surface until a person notices. The
cure is the same shape as any ensure pass driven by a timer: look at
every cousin, act only on the one state that is safe to act on.

The decision per cousin is a pure table (`ensure_action`):

  no tmux session            -> skip   (a stopped cousin needs no server)
  no chat port configured    -> skip
  /health answers with slug  -> ok     (leave it alone)
  port occupied, health bad  -> alert  (log only: the process may be a
                                        squatter or a wedged server, and
                                        a watchdog must never kill blind)
  port free                  -> spawn  (detached, log appended to
                                        <home>/data/chat-server.log)

One pass per invocation, guarded by an flock on
<root>/data/chat-watchdog.lock so an overlapping timer fire or a manual
run exits cleanly instead of racing. Every probe is an injected seam so
the pass is tested without tmux, sockets or a server.
"""
import argparse
import fcntl
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

from cousin_lib.config import FrameworkConfig, MissingConfigError

ACTIONS = ("skip", "ok", "alert", "spawn")
SPAWN_WAIT_S = 5.0      # how long a fresh spawn has to answer /health
POLL_S = 0.25           # how often it is asked while waiting
HEALTH_TIMEOUT_S = 2.0  # one /health request
TMUX_TIMEOUT_S = 3      # one has-session query


def ensure_action(*, has_tmux, port, health_ok, port_in_use):
    """The pure decision: skip | ok | alert | spawn."""
    if not has_tmux or not port:
        return "skip"
    if health_ok:
        return "ok"
    if port_in_use:
        return "alert"
    return "spawn"


def lock_path(root):
    return root / "data" / "chat-watchdog.lock"


# --- default probes ---------------------------------------------------

def tmux_argv(session, *, tmux_bin="tmux", tmux_socket=None):
    """`=name` asks tmux for an exact session match; without it a
    session named `testa-2` would answer for `testa`."""
    cmd = [tmux_bin]
    if tmux_socket:
        cmd += ["-S", tmux_socket]
    return cmd + ["has-session", "-t", "=%s" % session]


def _default_has_tmux(session):
    """The socket follows the chat server's own seam
    (COUSIN_TMUX_SOCKET), so the watchdog looks where the cousin
    actually runs."""
    argv = tmux_argv(session, tmux_socket=os.environ.get("COUSIN_TMUX_SOCKET"))
    try:
        r = subprocess.run(argv, capture_output=True, timeout=TMUX_TIMEOUT_S)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def health_answers(body, slug):
    """A port answers for THIS cousin only when /health says ok and
    names the slug; a different slug is a squatter, not a server."""
    if not isinstance(body, dict):
        return False
    return body.get("status") == "ok" and body.get("slug") == slug


def _default_health(port, slug):
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/health" % port,
                                    timeout=HEALTH_TIMEOUT_S) as r:
            body = json.loads(r.read())
    except Exception:
        return False
    return health_answers(body, slug)


def _default_port_in_use(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            return True
    except OSError:
        return False


def _default_spawn(home):
    """Start the chat server exactly as spawn and flip do: the module
    entry point under this interpreter, detached, stdout and stderr
    appended to the cousin's own log. True when the launch happened;
    whether it ANSWERS is the caller's wait, not this function's
    claim."""
    log_path = home / "data" / "chat-server.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "ab") as log:
            subprocess.Popen(
                [sys.executable, "-m", "cousin_lib.server.app",
                 "--home", str(home)],
                stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                start_new_session=True)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


# --- the pass ---------------------------------------------------------

def _wait_for_health(port, slug, *, health, sleep):
    waited = 0.0
    while True:
        if health(port, slug):
            return True
        if waited >= SPAWN_WAIT_S:
            return False
        sleep(POLL_S)
        waited += POLL_S


def ensure_pass(root, *, dry_run=False, has_tmux=_default_has_tmux,
                health=_default_health, port_in_use=_default_port_in_use,
                spawn=_default_spawn, sleep=time.sleep):
    """One result per cousin, in registry order:
    {slug, port, action, ok}. `ok` is False for an alert, a spawn that
    did not answer within SPAWN_WAIT_S, or a probe that raised; a
    raised probe is isolated to its cousin and reported as action
    "error"."""
    results = []
    for config in FrameworkConfig(root).list_cousins():
        slug, port = config.slug, config.chat_port
        try:
            results.append(_ensure_one(
                config, dry_run=dry_run, has_tmux=has_tmux, health=health,
                port_in_use=port_in_use, spawn=spawn, sleep=sleep))
        except Exception as err:  # the pass outlives any one cousin
            print("[chat-watchdog] ERROR %s: %s: %s"
                  % (slug, type(err).__name__, err), file=sys.stderr)
            results.append({"slug": slug, "port": port,
                            "action": "error", "ok": False})
    return results


def _ensure_one(config, *, dry_run, has_tmux, health, port_in_use, spawn,
                sleep):
    slug, port, home = config.slug, config.chat_port, config.home
    alive = has_tmux(config.tmux_session)
    probe = bool(alive and port)
    action = ensure_action(
        has_tmux=alive, port=port,
        health_ok=health(port, slug) if probe else False,
        port_in_use=port_in_use(port) if probe else False)
    result = {"slug": slug, "port": port, "action": action, "ok": True}
    if dry_run:
        detail = {"spawn": " (would spawn cousin-chat-server --home %s)"
                  % home}.get(action, "")
        print("[chat-watchdog] %s: %s%s" % (slug, action, detail))
    if action == "alert":
        print("[chat-watchdog] ALERT %s: port %s occupied but /health "
              "failing - not touching it; see <home>/data/chat-server.log"
              " and docs/operations.md" % (slug, port), file=sys.stderr)
        result["ok"] = False
    elif action == "spawn" and not dry_run:
        launched = spawn(home)
        answered = launched and _wait_for_health(
            port, slug, health=health, sleep=sleep)
        print("[chat-watchdog] spawned %s on :%s health=%s"
              % (slug, port,
                 "ok" if answered else
                 ("no answer within %.0fs" % SPAWN_WAIT_S if launched
                  else "launch failed")))
        result["ok"] = bool(answered)
        # A spawn only happens on a transition (the agent runs, its chat
        # server is gone), so this is a crash record, not a tick.
        from cousin_lib import memory
        memory.record_event(
            home, "L1_FRAMEWORK", "framework:respawn",
            "chat server found down while the agent ran; respawned on :%s,"
            " health %s" % (port, "ok" if answered else
                            ("no answer" if launched else "launch failed")),
            "framework")
    return result


def exit_code(results):
    return 0 if all(r["ok"] for r in results) else 1


def watchdog_run(root, *, dry_run=False, **seams):
    """The lock around one pass. A held lock means another pass is in
    flight: exit 0 with a line, since the work is being done."""
    path = lock_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("[chat-watchdog] another pass is running; exiting",
                  file=sys.stderr)
            return 0
        return exit_code(ensure_pass(root, dry_run=dry_run, **seams))


def watchdog_main(argv=None):
    """cousin-chat-watchdog [--dry-run] [--root R]. Exit 0 when every
    running cousin's chat server answers (or nothing needed doing), 1
    when any cousin ended in alert or a spawn did not answer, 2 usage."""
    parser = argparse.ArgumentParser(
        prog="cousin-chat-watchdog",
        description="ensure every running cousin's chat server answers;"
                    " spawn a missing one, alert on a sick one, never kill")
    parser.add_argument("--dry-run", action="store_true",
                        help="print each decision; spawn nothing")
    parser.add_argument("--root", default=None,
                        help="framework root (else FRAMEWORK_ROOT)")
    args = parser.parse_args(argv)
    try:
        root = FrameworkConfig.resolve(args.root, cwd_fallback=True).root
        return watchdog_run(root, dry_run=args.dry_run)
    except (MissingConfigError, OSError) as err:
        # a root that is absent or unwritable cannot hold the lock: a
        # configuration error with a line, not a traceback
        print("cousin-chat-watchdog: %s" % err, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(watchdog_main())
