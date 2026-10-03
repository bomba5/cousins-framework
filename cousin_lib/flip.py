"""The flip: end a generation, start the next on a fresh session.

On a runner cousin the flip is a rollover: a `flip` row through the
cousin's inbox, which the runner carries out between turns and answers
(_flip_runner); docs/reference/lifecycle.md has the contract. A cousin
with no runner kind is refused by name before anything runs.

What else lives here are the two tmux reads cousin-migrate still uses
(_session_alive, _read_agent_cmd_template).
"""
import json
import shutil
import subprocess

from cousin_lib.config import CousinConfig, FrameworkConfig, MissingConfigError
from cousin_lib.trace import traced_cli

HANDOFF_DEADLINE_SECONDS = 300
HANDOFF_HALFWAY_SECONDS = 150
RESPAWN_SETTLE_SECONDS = 8


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


def flip(slug, *, dry_run=False, tmux_bin="tmux",
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


@traced_cli("cousin-flip")
def flip_main(argv=None):
    """Console entry point: cousin-flip <slug> [--dry-run].
    Operator-driven; a cousin must never flip itself."""
    import argparse
    import os
    import sys

    parser = argparse.ArgumentParser(prog="cousin-flip")
    parser.add_argument("slug")
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
        result = flip(args.slug, dry_run=args.dry_run)
    except MissingConfigError as err:
        print("cousin-flip: %s" % err, file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, default=str))
    return 0 if result["ok"] else 1
