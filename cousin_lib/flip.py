"""The flip: end a generation, start the next on a fresh session
identity.

Stage order and its guarantees are specified in
docs/reference/lifecycle.md. The properties that matter: nothing
destructive happens before preflight passes; the dying session gets a
bounded chance to hand off before the framework synthesizes an
emergency handoff; the respawn goes through spawn.start_cousin - the
codebase's single tmux-creation site; and the result's ok reflects
whether the new session identity actually persisted, because a flip
that lost its identity write did not succeed, whatever else worked.

Crash recovery is the operator: a stale in-progress marker is reported
and overwritten, never auto-recovered.
"""
import json
import shutil
import subprocess
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path

from cousin_lib import agent_auth, audits, boot, transcript_mine
from cousin_lib.config import (CousinConfig, FrameworkConfig,
                               MissingConfigError, harness_config)
from cousin_lib.server.injection import TmuxInjector
from cousin_lib.spawn import (SpawnError, _mint_session_id,
                              _persist_session_id, render_agent_cmd,
                              start_cousin, start_preflight)
from cousin_lib.trace import traced_cli

HANDOFF_DEADLINE_SECONDS = 300
HANDOFF_HALFWAY_SECONDS = 150
RESPAWN_SETTLE_SECONDS = 8

_HANDOFF_PROMPT = (
    "[cousin-flip in progress] Three pre-exit writes required:\n"
    "1. Reconcile STATUS.md: fold in-flight progress and open loops"
    " into the file - the next session anchors on STATUS.md as"
    " authoritative.\n"
    "2. Write your handoff to data/handoff.md (position, next action,"
    " open questions, degraded_state: false).\n"
    "3. Write data/active-threads.md, one bullet per in-flight"
    " thread.\n"
    "Then stop working. The framework is rebuilding your boot packet."
)


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


def _read_session_id(home):
    """The dying generation's runtime.session_id, or "" when none was
    ever persisted (a hand-made cousin, or a first flip)."""
    try:
        data = tomllib.loads((Path(home) / "cousin.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return ""
    return str((data.get("runtime") or {}).get("session_id") or "")


def _mine_transcript(home, root, *, dry_run):
    """Stage record for mining the dying session's transcript into raw
    memory. Best-effort by construction: every way this can go wrong
    becomes a named skip, never an exception out of the flip."""
    stage = {"stage": "transcript_mine"}
    if dry_run:
        stage["skipped"] = "dry-run"
        return stage
    try:
        if harness_config(root) is None:
            stage["skipped"] = "config/harness.toml absent"
            return stage
        session_id = _read_session_id(home)
        if not session_id:
            stage["skipped"] = "no runtime.session_id in cousin.toml"
            return stage
        stage["mined"] = transcript_mine.mine(home, root, session_id)
    except Exception as err:  # noqa: BLE001 - best-effort stage
        stage["skipped"] = "error: %s" % err
    return stage


def _read_agent_cmd_template(root):
    try:
        cmd = (root / "config" / "agent-cmd").read_text().strip()
    except OSError:
        cmd = ""
    return cmd


def flip(slug, *, confirm=False, dry_run=False, tmux_bin="tmux",
         tmux_socket=None, handoff_deadline=HANDOFF_DEADLINE_SECONDS,
         halfway=HANDOFF_HALFWAY_SECONDS,
         settle=RESPAWN_SETTLE_SECONDS, which=shutil.which):
    """Run the flip for one cousin. Returns a structured result whose
    ok reflects the verified identity write."""
    result = {"slug": slug, "ok": False, "stages": []}
    root = FrameworkConfig.from_env().root
    home = root / "cousins" / slug
    try:
        config = CousinConfig.load(home)
    except MissingConfigError as err:
        result["error"] = str(err)
        return result
    session = config.tmux_session

    # Concurrency guard: a FRESH marker means another flip is mid-run;
    # a second one would double-bump the generation and inject into the
    # first flip's new session. Stale markers pass through - crashed
    # flips are reported to the operator, never auto-recovered.
    marker = _marker_path(home)
    if marker.exists() and not dry_run:
        age = None
        try:
            started = datetime.fromisoformat(
                json.loads(marker.read_text())["started_at"])
            age = (datetime.now(timezone.utc) - started).total_seconds()
        except (ValueError, KeyError, OSError):
            pass
        if age is not None and age < handoff_deadline + 180:
            result["stages"].append(
                {"stage": "concurrency_guard", "ok": False})
            result["error"] = (
                "another flip started %ds ago - refusing concurrent"
                " flip" % int(age))
            return result

    # Preflight: zero side effects, runs even under dry-run - a
    # dry-run that skips preflight lies about what a real flip would
    # do.
    failures = []
    agent_cmd_template = _read_agent_cmd_template(root)
    if not agent_cmd_template:
        failures.append("no agent command at %s"
                        % (root / "config" / "agent-cmd"))
    else:
        # The {model}/{effort} render is checked here, before the
        # kill: a placeholder nothing defines would otherwise fail the
        # respawn with the old session already gone.
        try:
            rendered = render_agent_cmd(agent_cmd_template, home, root=root)
        except SpawnError as err:
            failures.append(str(err))
        else:
            # The host half: tmux and the agent's executable, checked
            # before the kill for the same reason. PATH is this
            # process's; a unit's PATH is often narrower than a login
            # shell's, which is exactly the case this catches.
            failures.extend(start_preflight(
                rendered.replace("{session_id}", "x"), tmux_bin=tmux_bin,
                which=which))
        # The auth mode (cousin.toml [runtime] auth) is checked before
        # the kill too: a missing key file would otherwise leave the
        # cousin with no session at all.
        try:
            agent_auth.preflight(home, root)
        except agent_auth.AuthError as err:
            failures.append("auth: %s" % err)
    if failures:
        result["stages"].append({"stage": "preflight", "ok": False,
                                 "failures": failures})
        result["error"] = "preflight failed: " + "; ".join(failures)
        return result
    result["stages"].append({"stage": "preflight", "ok": True})

    if not dry_run:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({
            "started_at": datetime.now(timezone.utc).isoformat(),
            "slug": slug,
        }))

    # Capture before anything destructive.
    alive = _session_alive(session, tmux_bin, tmux_socket)
    tail = _capture_tail(session, tmux_bin, tmux_socket) if alive else ""
    result["stages"].append({"stage": "capture", "alive": alive,
                             "transcript_chars": len(tail)})

    # Bounded handoff window with one halfway nudge.
    handoff_path = Path(home) / "data" / "handoff.md"
    mtime_before = (handoff_path.stat().st_mtime
                    if handoff_path.exists() else 0)
    injector = TmuxInjector(session, tmux_bin=tmux_bin,
                            socket=tmux_socket)
    if alive and not dry_run:
        injector.inject(_HANDOFF_PROMPT)
        result["stages"].append({"stage": "prompt_handoff", "sent": True})
        deadline = time.time() + handoff_deadline
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
                injector.inject("[cousin-flip] still waiting for the"
                                " handoff; wrap up now.")
                nudged = True
        result["stages"].append({"stage": "wait_handoff",
                                 "wrote_clean": wrote, "nudged": nudged})
        if not wrote:
            _write_emergency_handoff(
                home, slug,
                transcript_tail=_capture_tail(session, tmux_bin,
                                              tmux_socket),
                reason="handoff timeout (%ss)" % handoff_deadline)
            result["stages"].append({"stage": "emergency_handoff",
                                     "written": True})
        # Session-end audit with the prior packet's mtime as session
        # start, then the baseline remediation.
        prior_gen = boot.read_generation(home)
        prior_packet = (Path(home) / "data"
                        / ("boot-packet-gen-%04d.md" % prior_gen))
        since_ts = (prior_packet.stat().st_mtime
                    if prior_packet.exists() else 0.0)
        violations = audits.audit_before_exit(slug, home,
                                              since_ts=since_ts)
        result["stages"].append({"stage": "audit_before_exit",
                                 "violations": len(violations)})
        baseline = audits.write_active_threads_baseline(
            home, since_ts=since_ts)
        result["stages"].append({"stage": "active_threads_baseline",
                                 "wrote": baseline["wrote"],
                                 "reason": baseline["reason"]})
    else:
        result["stages"].append({
            "stage": "prompt_handoff", "sent": False,
            "reason": "dry-run" if dry_run else "no live session"})

    # Mine the dying session's transcript into raw candidates BEFORE
    # the identity is re-minted (the transcript belongs to the old id)
    # and before archive. Best-effort: a stage record, never a failure.
    result["stages"].append(_mine_transcript(home, root, dry_run=dry_run))

    # Archive, bump, assemble.
    prior_gen = boot.read_generation(home)
    if not dry_run:
        arch = _archive_generation(home, prior_gen, transcript_tail=tail)
        result["stages"].append({"stage": "archive", "path": str(arch)})
    new_gen = (boot.bump_generation(home) if not dry_run
               else prior_gen + 1)
    result["new_generation"] = new_gen
    packet = boot.assemble(slug, home, generation=new_gen)
    result["boot_packet_tokens"] = packet["approx_tokens"]
    result["degraded_sections"] = packet["degraded_sections"]
    if dry_run:
        result["stages"].append({"stage": "kill_and_respawn",
                                 "skipped": "dry-run"})
        result["ok"] = True
        return result
    packet_path = (Path(home) / "data"
                   / ("boot-packet-gen-%04d.md" % new_gen))
    packet_path.write_text(packet["text"])
    result["stages"].append({"stage": "assemble_packet",
                             "path": str(packet_path)})

    # Kill, mint identity, respawn through the single tmux site,
    # persist the identity - ok hangs on that persist.
    if alive:
        _tmux(["kill-session", "-t", session], tmux_bin, tmux_socket)
    # Minted and persisted even when the agent-cmd carries no
    # {session_id} placeholder: the generation record is more useful
    # with it.
    session_id = _mint_session_id()
    agent_cmd = agent_cmd_template.replace("{session_id}", session_id)
    try:
        start_cousin(home, agent_cmd=agent_cmd, tmux_bin=tmux_bin,
                     tmux_socket=tmux_socket, root=root)
    except SpawnError as err:
        result["stages"].append({"stage": "respawn", "ok": False,
                                 "error": str(err)})
        result["error"] = "respawn failed: %s" % err
        return result
    result["stages"].append({"stage": "respawn", "ok": True})
    try:
        _persist_session_id(home, session_id)
        persisted = True
    except (OSError, tomllib.TOMLDecodeError) as err:
        result["stages"].append({"stage": "persist_identity",
                                 "ok": False, "error": str(err)})
        persisted = False
    if persisted:
        result["stages"].append({"stage": "persist_identity", "ok": True})

    # Inject the packet after the session settles. The seam should be
    # invisible: no announcement unless the operator asked.
    time.sleep(settle)
    preamble = ("[cousin-flip] boot packet follows. Do not announce"
                " the respawn." if not confirm else
                "[cousin-flip] boot packet follows. Operator asked for"
                " confirmation: post one line to your chat surface"
                " when oriented.")
    injector.inject(preamble + "\n" + packet["text"])
    result["stages"].append({"stage": "inject_packet",
                             "tokens": packet["approx_tokens"]})

    marker.unlink(missing_ok=True)
    result["ok"] = persisted
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
