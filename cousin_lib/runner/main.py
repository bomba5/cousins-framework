"""cousin-runner: one process per cousin, runs until told to stop.

`[agent] runner` in cousin.toml picks the implementation. No tmux, no
port: the inbox is the bus and the wake socket is the doorbell.

Exit codes: 0 after SIGTERM/SIGINT, or when `--once` has drained the
inbox; 2 for a configuration problem (no or a bad `[agent] runner`, an
unreadable key file) or when another runner holds the home's lock; 3
when the runner gave up (its worker ended, e.g. it could not connect,
or `--once` found it `errored` for longer than ERRORED_GIVE_UP_S), so a
supervisor restarts it.
"""
import argparse
import contextlib
import fcntl
import os
import signal
import sys
import threading
import time
import tomllib
from pathlib import Path

from cousin_lib.runner.base import RunnerError

KINDS = ("sdk", "fake")

# The credentials a runner must never inherit from the shell that started
# it: the SDK builds the CLI's environment as {**os.environ, **options.env},
# so an overlay cannot unset them and only removing them here can. The auth
# lane is `[agent] api_key_file` (options.env) and nothing else.
AUTH_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")

# `--once` gives up on a runner that stays `errored` this long.
ERRORED_GIVE_UP_S = 10.0

_UNSET = object()


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
    if root is None:
        raise RunnerError(
            "[agent] api_key_file needs a framework root above %s"
            " (cousins/<slug> under an install with config/)" % home)
    path = Path(root) / name
    try:
        return path.read_text().strip()
    except OSError as err:
        raise RunnerError("cannot read [agent] api_key_file %s: %s"
                          % (path, err))


def runner_for(home, *, kind=None):
    """The runner cousin.toml names. A cousin with no `[agent] runner` is
    a tmux cousin: it gets no runner (its inbox has no producer), unless
    `kind` says otherwise. The home's policy.toml is loaded here, once,
    and handed to the runner: a malformed one is a PolicyError, a
    RunnerError, so the process exits 2 naming the key. No settings
    file is written: the runner's hooks are in-process."""
    agent = _agent_table(home)
    kind = kind or agent.get("runner")
    if not kind:
        raise RunnerError("%s/cousin.toml has no [agent] runner: this is a tmux"
                          " cousin (pass --runner %s to run it here anyway)"
                          % (home, "|".join(KINDS)))
    if kind not in KINDS:
        raise RunnerError("cousin.toml [agent] runner must be one of %s, got %r"
                          % (", ".join(KINDS), kind))
    from cousin_lib.runner.policy import Policy
    policy = Policy.load(home)
    if kind == "fake":
        from cousin_lib.runner.fake import FakeRunner
        return FakeRunner(home, policy=policy)
    if kind == "sdk":
        from cousin_lib.runner.sdk import SdkRunner
        return SdkRunner(home, api_key=_read_key(home, agent),
                         model=agent.get("model"), policy=policy)


@contextlib.contextmanager
def hold_lock(home):
    """One runner per cousin: an exclusive flock on <home>/run/runner.lock,
    held for the life of this context (the kernel drops it when the
    process dies, even on SIGKILL). Taken before anything else, because a
    second runner's `requeue_stale` would steal the first one's live
    claims."""
    path = Path(home) / "run" / "runner.lock"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    except OSError as err:
        raise RunnerError("cannot open the runner lock %s: %s" % (path, err))
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise RunnerError("another cousin-runner holds %s" % path)
    try:
        yield
    finally:
        os.close(fd)


def is_running(home):
    """True when a runner holds `<home>/run/runner.lock`. A fresh, non-
    blocking flock on its own descriptor: `BlockingIOError` means a
    runner holds it; taking the lock cleanly means nobody does, so the
    probe releases it and answers False; a missing lock file is False,
    nothing to hold."""
    path = Path(home) / "run" / "runner.lock"
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def _gone(runner):
    """The exit-3 line when the runner's worker has ended, else None."""
    if runner.worker_alive():
        return None
    return "cousin-runner: the runner gave up: %s" % (
        getattr(runner, "fatal", None) or "its worker ended")


def _once(runner, stop):
    """Until the inbox is drained (0), a signal (0), or the runner gives
    up (3): its worker ended, or it stayed `errored` too long."""
    errored_since = None
    while not stop.is_set():
        why = _gone(runner)
        if why:
            print(why, file=sys.stderr)
            return 3
        state = runner.state()
        if runner.inbox.unfinished() == 0 and state != "running":
            return 0
        if state == "errored":
            errored_since = errored_since or time.monotonic()
            if time.monotonic() - errored_since > ERRORED_GIVE_UP_S:
                print("cousin-runner: the runner stayed errored for %.0fs"
                      % ERRORED_GIVE_UP_S, file=sys.stderr)
                return 3
        else:
            errored_since = None
        time.sleep(0.05)
    return 0


def _forever(runner, stop):
    while not stop.is_set():
        why = _gone(runner)
        if why:
            print(why, file=sys.stderr)
            return 3
        time.sleep(0.2)
    return 0


def runner_main(argv=None):
    parser = argparse.ArgumentParser(prog="cousin-runner")
    parser.add_argument("--home", required=True)
    parser.add_argument("--runner", choices=KINDS)
    parser.add_argument("--once", action="store_true",
                        help="drain the inbox, then exit")
    args = parser.parse_args(argv)
    try:
        with hold_lock(args.home):
            try:
                runner = runner_for(args.home, kind=args.runner)
            except RunnerError as err:
                print("cousin-runner: %s" % err, file=sys.stderr)
                return 2
            for name in AUTH_ENV:
                os.environ.pop(name, None)
            return _serve(runner, args.once)
    except RunnerError as err:
        print("cousin-runner: %s" % err, file=sys.stderr)
        return 2


def _serve(runner, once):
    stop = threading.Event()

    def _signal(signum, frame):
        stop.set()

    previous_term = previous_int = _UNSET
    try:
        previous_term = signal.signal(signal.SIGTERM, _signal)
        previous_int = signal.signal(signal.SIGINT, _signal)
        # a claim from a runner that died is ours now (the lock says no
        # other runner is alive on this home)
        runner.inbox.requeue_stale(older_than_s=0.0)
        runner.start()
        policy = getattr(runner, "policy", None)
        if policy is not None:
            runner.stream.append("policy", {"describe": policy.describe()})
        return _once(runner, stop) if once else _forever(runner, stop)
    finally:
        runner.stop(timeout=30)
        if previous_term is not _UNSET:
            signal.signal(signal.SIGTERM, previous_term)
        if previous_int is not _UNSET:
            signal.signal(signal.SIGINT, previous_int)


if __name__ == "__main__":
    sys.exit(runner_main())
