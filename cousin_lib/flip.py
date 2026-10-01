"""The flip: end a generation, start the next on a fresh session.

On a runner cousin the flip is a rollover: a `flip` row through the
cousin's inbox, which the runner carries out between turns and answers
(_flip_runner); docs/reference/lifecycle.md has the contract. A cousin
with no runner kind is refused by name before anything runs.

What else lives here is the legacy lane's clean stop (close_session)
and its helpers: the pre-exit prompt, the bounded handoff wait, the
emergency handoff and the generation archive. Crash recovery is the
operator: a stale in-progress marker is reported and overwritten,
never auto-recovered.
"""
import json
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from cousin_lib import audits, boot
from cousin_lib.config import CousinConfig, FrameworkConfig, MissingConfigError
from cousin_lib import delivery
from cousin_lib.spawn import framework_event, pending_boot_path, stop_cousin
from cousin_lib.trace import traced_cli

HANDOFF_DEADLINE_SECONDS = 300
HANDOFF_HALFWAY_SECONDS = 150
RESPAWN_SETTLE_SECONDS = 8

def handoff_prompt(event="cousin-flip", after="The framework is"
                   " rebuilding your boot packet."):
    """The pre-exit prompt typed into a session that is about to end,
    by a flip or by a clean stop. Step 4 is the one the handoff files
    cannot carry: what the session learned goes into memory now, with
    its truth level, because the next session boots on a packet and
    starts without this context. The handoff comes last because the
    framework ends the session as soon as data/handoff.md changes."""
    return (
        "[%s in progress] Four pre-exit writes required, in this order:\n"
        "1. Reconcile STATUS.md: fold in-flight progress and open loops"
        " into the file - the next session anchors on STATUS.md as"
        " authoritative.\n"
        "2. Write data/active-threads.md, one bullet per in-flight"
        " thread.\n"
        "3. Save what this session learned that is not in memory yet:"
        " cousin-memory remember (operator statements at --level"
        " operator with --cite) and cousin-memory decide for decisions"
        " and their reasons.\n"
        "4. LAST, write your handoff to data/handoff.md (position, next"
        " action, open questions, degraded_state: false). Its write is"
        " the signal that you are done: the session ends right after.\n"
        "Then stop working. %s" % (event, after))


_HANDOFF_PROMPT = handoff_prompt()


def _marker_path(home):
    return Path(home) / "data" / ".flip-in-progress.json"


def _tmux(args, tmux_bin, tmux_socket, *, timeout=5):
    cmd = [tmux_bin]
    if tmux_socket:
        cmd += ["-S", tmux_socket]
    return subprocess.run(cmd + args, capture_output=True, text=True,
                          timeout=timeout, check=False)


def _session_alive(session, tmux_bin, tmux_socket):
    try:
        return _tmux(["has-session", "-t", session],
                     tmux_bin, tmux_socket).returncode == 0
    except OSError:
        return False


def _capture_tail(session, tmux_bin, tmux_socket, lines=100):
    r = _tmux(["capture-pane", "-p", "-t", session, "-S",
               "-%d" % lines], tmux_bin, tmux_socket)
    return r.stdout or ""


def _write_emergency_handoff(home, slug, *, transcript_tail, reason):
    """When the cousin does not hand off, the framework synthesizes one
    from observable state, marked degraded - a real signal loss, never
    the normal flow."""
    path = Path(home) / "data" / "handoff.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join([
        "# EMERGENCY HANDOFF (framework-generated)",
        "degraded_state: true",
        "reason: %s" % reason,
        "generated_at: %s" % datetime.now(timezone.utc).isoformat(),
        "cousin: %s" % slug,
        "",
        "## Observed activity (pane tail)",
        transcript_tail[-2000:] if transcript_tail else "(no capture)",
    ]) + "\n")


def _archive_generation(home, generation, *, transcript_tail):
    arch = Path(home) / "data" / "generations" / ("gen-%04d" % generation)
    arch.mkdir(parents=True, exist_ok=True)
    for name in ("handoff.md", "active-threads.md",
                 "boot-packet-gen-%04d.md" % generation):
        src = Path(home) / "data" / name
        if src.exists():
            (arch / name).write_text(src.read_text())
    if transcript_tail:
        (arch / "transcript-tail.txt").write_text(transcript_tail)
    return arch


def _sender(home, tmux_bin, tmux_socket):
    """A `send(text, source)` bound to one cousin and one tmux."""
    def send(text, source="flip"):
        item = delivery.Item(thread_id=delivery.thread_id("system"),
                             source=source, body=text)
        return delivery.deliver(home, item, tmux_bin=tmux_bin,
                                socket=tmux_socket)
    return send


def _hand_off(home, slug, session, *, alive, dry_run, send, prompt,
              event, tmux_bin, tmux_socket, deadline_seconds, halfway):
    """The pre-exit half shared by a flip and a clean stop: prompt the
    live session, wait (bounded, one nudge) for data/handoff.md to
    change, else write an emergency handoff; then the session-end audit
    and the active-threads baseline. Returns the stage records."""
    stages = []
    handoff_path = Path(home) / "data" / "handoff.md"
    mtime_before = (handoff_path.stat().st_mtime
                    if handoff_path.exists() else 0)
    if not alive or dry_run:
        stages.append({
            "stage": "prompt_handoff", "sent": False,
            "reason": "dry-run" if dry_run else "no live session"})
        return stages
    send(prompt)
    stages.append({"stage": "prompt_handoff", "sent": True})
    deadline = time.time() + deadline_seconds
    halfway_at = time.time() + halfway
    nudged = wrote = False
    while time.time() < deadline:
        time.sleep(0.1)
        try:
            current = handoff_path.stat().st_mtime
        except FileNotFoundError:
            current = 0
        if current > mtime_before:
            wrote = True
            break
        if not nudged and time.time() >= halfway_at:
            send("[%s] still waiting for the handoff; wrap up now."
                 % event)
            nudged = True
    stages.append({"stage": "wait_handoff", "wrote_clean": wrote,
                   "nudged": nudged})
    if not wrote:
        _write_emergency_handoff(
            home, slug,
            transcript_tail=_capture_tail(session, tmux_bin, tmux_socket),
            reason="handoff timeout (%ss)" % deadline_seconds)
        stages.append({"stage": "emergency_handoff", "written": True})
    # Session-end audit with the prior packet's mtime as session start,
    # then the baseline remediation.
    prior_gen = boot.read_generation(home)
    prior_packet = (Path(home) / "data"
                    / ("boot-packet-gen-%04d.md" % prior_gen))
    since_ts = (prior_packet.stat().st_mtime
                if prior_packet.exists() else 0.0)
    violations = audits.audit_before_exit(slug, home, since_ts=since_ts)
    stages.append({"stage": "audit_before_exit",
                   "violations": len(violations)})
    baseline = audits.write_active_threads_baseline(home, since_ts=since_ts)
    stages.append({"stage": "active_threads_baseline",
                   "wrote": baseline["wrote"], "reason": baseline["reason"]})
    return stages


def _read_agent_cmd_template(root):
    try:
        cmd = (root / "config" / "agent-cmd").read_text().strip()
    except OSError:
        cmd = ""
    return cmd


def _flip_runner(slug, home, *, reason, deadline, queue_if_stopped):
    """A runner cousin's flip is a rollover (spec): the `flip` row through
    the store, the same one Runner.rollover puts (coalesced), awaited.
    Nothing of the tmux flip runs: no marker, no pane, no pending boot,
    no transcript mining (the runner mined every turn)."""
    from cousin_lib.runner import rollover
    from cousin_lib.runner.inbox import Inbox
    from cousin_lib.runner.main import is_running
    result = {"slug": slug, "ok": False, "lane": "runner", "stages": []}
    if not is_running(home) and not queue_if_stopped:
        result["error"] = "runner not running; start it before flipping"
        return result
    answer = rollover.request(Inbox(home), home, reason, alive=lambda: is_running(home),
                              timeout=deadline + rollover.WAIT_SLACK_S)
    result["stages"].append(dict(answer, stage="rollover"))
    result["ok"] = bool(answer.get("ok"))
    if not result["ok"]:
        result["error"] = answer.get("reason", "rollover failed")
    return result


def flip(slug, *, confirm=False, dry_run=False, tmux_bin="tmux",
         tmux_socket=None, handoff_deadline=HANDOFF_DEADLINE_SECONDS,
         halfway=HANDOFF_HALFWAY_SECONDS,
         settle=RESPAWN_SETTLE_SECONDS, which=shutil.which,
         reason="cousin-flip", queue_if_stopped=False):
    """Run the flip for one cousin. Returns a structured result whose
    ok reflects the verified identity write. On a runner cousin it is the
    rollover (_flip_runner); a cousin with no runner kind is refused with
    delivery.lane_refusal before any tmux call."""
    result = {"slug": slug, "ok": False, "stages": []}
    root = FrameworkConfig.from_env().root
    home = root / "cousins" / slug
    try:
        config = CousinConfig.load(home)
    except MissingConfigError as err:
        result["error"] = str(err)
        return result
    from cousin_lib.delivery import RUNNER_KINDS, _runner_kind
    if _runner_kind(home) in RUNNER_KINDS:
        if dry_run:
            result.update(ok=True, lane="runner",
                          stages=[{"stage": "rollover", "skipped": "dry-run"}])
            return result
        return _flip_runner(slug, home, reason=reason, deadline=handoff_deadline,
                            queue_if_stopped=queue_if_stopped)
    # 2.0.0 has no legacy tmux lane; refused by name, before any tmux call.
    from cousin_lib.delivery import lane_refusal
    result["error"] = lane_refusal(home)
    return result


_STOP_AFTER = ("The cousin is being stopped; the next start boots on a"
               " packet built from these files.")


def close_session(slug, *, tmux_bin="tmux", tmux_socket=None,
                  handoff_deadline=HANDOFF_DEADLINE_SECONDS,
                  halfway=HANDOFF_HALFWAY_SECONDS):
    """A clean stop: the first half of a flip, then the stop. The live
    session is asked for its pre-exit writes (handoff_prompt, with the
    memory step), the generation is archived
    and bumped and the next packet assembled; then the agent and chat
    server stop. The packet waits in data/pending-boot.json, and the
    next start of any kind boots a fresh session on it instead of
    resuming this one (spawn.pending_boot).

    A cousin that is not running is just stopped: there is no session
    to hand off and nothing new to pack. Shares the flip's marker, so
    a flip and a clean stop never run at once."""
    result = {"slug": slug, "ok": False, "stages": []}
    root = FrameworkConfig.from_env().root
    home = root / "cousins" / slug
    try:
        config = CousinConfig.load(home)
    except MissingConfigError as err:
        result["error"] = str(err)
        return result
    session = config.tmux_session
    alive = _session_alive(session, tmux_bin, tmux_socket)
    if not alive:
        result["stop"] = stop_cousin(home, tmux_bin=tmux_bin,
                                     tmux_socket=tmux_socket)
        result["stages"].append({"stage": "prompt_handoff", "sent": False,
                                 "reason": "no live session"})
        result["ok"] = True
        return result
    marker = _marker_path(home)
    if marker.exists():
        try:
            started = datetime.fromisoformat(
                json.loads(marker.read_text())["started_at"])
            age = (datetime.now(timezone.utc) - started).total_seconds()
        except (ValueError, KeyError, OSError):
            age = None
        if age is not None and age < handoff_deadline + 180:
            result["error"] = ("a flip or clean stop started %ds ago -"
                               " refusing a concurrent one" % int(age))
            return result
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({
        "started_at": datetime.now(timezone.utc).isoformat(),
        "slug": slug, "kind": "stop"}))
    try:
        tail = _capture_tail(session, tmux_bin, tmux_socket)
        result["stages"].append({"stage": "capture", "alive": True,
                                 "transcript_chars": len(tail)})
        send = _sender(home, tmux_bin, tmux_socket)
        result["stages"].extend(_hand_off(
            home, slug, session, alive=True, dry_run=False,
            send=send,
            prompt=handoff_prompt("cousin-stop", _STOP_AFTER),
            event="cousin-stop", tmux_bin=tmux_bin,
            tmux_socket=tmux_socket, deadline_seconds=handoff_deadline,
            halfway=halfway))
        prior_gen = boot.read_generation(home)
        arch = _archive_generation(home, prior_gen, transcript_tail=tail)
        result["stages"].append({"stage": "archive", "path": str(arch)})
        new_gen = boot.bump_generation(home)
        packet = boot.assemble(slug, home, generation=new_gen)
        packet_path = (Path(home) / "data"
                       / ("boot-packet-gen-%04d.md" % new_gen))
        packet_path.write_text(packet["text"])
        pending_boot_path(home).write_text(json.dumps({
            "generation": new_gen, "packet": str(packet_path),
            "written_at": datetime.now(timezone.utc).isoformat()}))
        result["new_generation"] = new_gen
        result["boot_packet_tokens"] = packet["approx_tokens"]
        result["stages"].append({"stage": "assemble_packet",
                                 "path": str(packet_path), "pending": True})
        result["stop"] = stop_cousin(home, tmux_bin=tmux_bin,
                                     tmux_socket=tmux_socket)
        handoff = next((st for st in result["stages"]
                        if st["stage"] == "wait_handoff"), {})
        framework_event(home, "stop", "closed generation %d cleanly"
                        " (handoff %s); the next start boots generation %d"
                        " on its packet" % (
                            prior_gen, "clean" if handoff.get("wrote_clean")
                            else "emergency (timed out)", new_gen),
                        generation=new_gen)
        result["ok"] = True
    finally:
        marker.unlink(missing_ok=True)
    return result


@traced_cli("cousin-flip")
def flip_main(argv=None):
    """Console entry point: cousin-flip <slug> [--confirm] [--dry-run].
    Operator-driven; a cousin must never flip itself."""
    import argparse
    import os
    import sys

    parser = argparse.ArgumentParser(prog="cousin-flip")
    parser.add_argument("slug")
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--root",
        help="the framework root (the checkout); falls back to"
             " FRAMEWORK_ROOT")
    args = parser.parse_args(argv)
    try:
        root = FrameworkConfig.resolve(args.root, cwd_fallback=True).root
        # The flip's stages (boot assembly, audits, the respawned
        # session) read the root from the environment; the CLI owns its
        # process environment, so --root is exported for all of them.
        os.environ["FRAMEWORK_ROOT"] = str(root)
        result = flip(args.slug, confirm=args.confirm,
                      dry_run=args.dry_run)
    except MissingConfigError as err:
        print("cousin-flip: %s" % err, file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, default=str))
    return 0 if result["ok"] else 1
