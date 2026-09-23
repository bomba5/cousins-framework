"""cousin-runner: one process per cousin, runs until told to stop.

`[agent] runner` in cousin.toml picks the implementation. No tmux, no
port: the inbox is the bus and the wake socket is the doorbell.
"""
import argparse
import signal
import sys
import threading
import time
import tomllib
from pathlib import Path

from cousin_lib.runner.base import RunnerError

KINDS = ("sdk", "fake")


def _agent_table(home):
    try:
        data = tomllib.loads((Path(home) / "cousin.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError) as err:
        raise RunnerError("cannot read %s/cousin.toml: %s" % (home, err))
    return data.get("agent") or {}


def _read_key(home, agent):
    """The key lane: `[agent] api_key_file`, a path relative to the
    framework root, read here and handed to the runner's options.env
    and nowhere else."""
    name = agent.get("api_key_file")
    if not name:
        return None
    from cousin_lib.config import FrameworkConfig
    root = FrameworkConfig.root_from_home(Path(home))
    return (Path(root) / name).read_text().strip()


def runner_for(home, *, kind=None):
    agent = _agent_table(home)
    kind = kind or agent.get("runner") or "sdk"
    if kind == "fake":
        from cousin_lib.runner.fake import FakeRunner
        return FakeRunner(home)
    if kind == "sdk":
        from cousin_lib.runner.sdk import SdkRunner
        return SdkRunner(home, api_key=_read_key(home, agent),
                         model=agent.get("model"))
    raise RunnerError("cousin.toml [agent] runner must be one of %s, got %r"
                      % (", ".join(KINDS), kind))


def runner_main(argv=None):
    parser = argparse.ArgumentParser(prog="cousin-runner")
    parser.add_argument("--home", required=True)
    parser.add_argument("--runner", choices=KINDS)
    parser.add_argument("--once", action="store_true",
                        help="drain the inbox, then exit")
    args = parser.parse_args(argv)
    try:
        runner = runner_for(args.home, kind=args.runner)
    except RunnerError as err:
        print("cousin-runner: %s" % err, file=sys.stderr)
        raise SystemExit(2)
    stop = threading.Event()

    def _signal(signum, frame):
        stop.set()

    previous_term = signal.signal(signal.SIGTERM, _signal)
    previous_int = signal.signal(signal.SIGINT, _signal)
    # a claim from a runner that died is ours now
    runner.inbox.requeue_stale(older_than_s=0.0)
    runner.start()
    try:
        if args.once:
            while runner.inbox.unfinished() > 0 or runner.state() == "running":
                time.sleep(0.05)
            return 0
        while not stop.is_set():
            time.sleep(0.2)
        return 0
    finally:
        runner.stop(timeout=30)
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)


if __name__ == "__main__":
    sys.exit(runner_main())
