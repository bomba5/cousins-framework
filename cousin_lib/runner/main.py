"""cousin-runner: one process per cousin, runs until told to stop.

`[agent] runner` in cousin.toml picks the implementation. No tmux, no
port: the inbox is the bus and the wake socket is the doorbell.

The home is made absolute and exported as COUSIN_HOME, with its root as
FRAMEWORK_ROOT, before the runner is built: the in-process tools and the
model's own `cousin-*` commands locate the cousin and the install that way.

Exit codes: 0 after SIGTERM/SIGINT, or when `--once` has drained the
inbox, or when `--check-auth` found the account logged in; 2 for a
configuration problem (no or a bad `[agent] runner`, an unknown account,
or a secret file that is open to others or malformed, all checked before
the lock; a malformed policy.toml, an MCP registry that does not parse or
names a command with no in-process handler, an [agent] effort outside the
levels); 3 when the runner gave up
(its worker ended, e.g. it could not connect, or `--once` found it
`errored` for longer than ERRORED_GIVE_UP_S), so a supervisor restarts
it; 4 when `--check-auth`
found the account not logged in (or `--validate`'s one turn did not
answer), or `--once` found the runner waiting for a login (R15): a
supervisor must NOT restart on 4, a person must log in; 5 when another
runner holds the home's lock (busy, not broken: a supervisor retries it
after its backoff, since the holder may be a leftover about to go).

A missing secret file is not a configuration problem: it is a login to
do. The runner starts, says so (data/login-required.json, an `auth`
event) and waits for the file; the long-running mode never exits for a
login (an exit would restart-loop a cousin nobody can log in).

`--check-auth [--validate]` runs before the lock, the runner and the
environment export: a check works beside a live cousin and never
becomes one.
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

from cousin_lib import accounts
from cousin_lib.delivery import RUNNER_KINDS
from cousin_lib.runner.base import RunnerError

KINDS = RUNNER_KINDS      # the runners runner_for builds, one list (delivery)

# The credentials a runner must never inherit from the shell that started
# it: the SDK builds the CLI's environment as {**os.environ, **options.env},
# so an overlay cannot unset them and only removing them here can. The
# cousin's account (accounts.account_env, in options.env) is the only
# source of credentials: the key, the token and the config dir.
AUTH_ENV = accounts.AUTH_VARS

# `--once` gives up on a runner that stays `errored` this long.
ERRORED_GIVE_UP_S = 10.0

# How long a stopping runner gives its current turn (runner.stop's
# timeout on SIGTERM/SIGINT). The one number every budget above it is
# built from: cousin-supervisor waits STOP_TIMEOUT_S + 5 before SIGKILL.
STOP_TIMEOUT_S = 30.0

LOCK_HELD_EXIT = 5    # another runner holds <home>/run/runner.lock
LOCK_TAKE_S = 1.0     # hold_lock retries this long: an is_running() probe holds the lock for microseconds

_UNSET = object()


class LockHeld(RunnerError):
    """Another runner holds this home's lock: exit LOCK_HELD_EXIT, not 2."""


def _agent_table(home):
    try:
        data = tomllib.loads((Path(home) / "cousin.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError) as err:
        raise RunnerError("cannot read %s/cousin.toml: %s" % (home, err))
    return data.get("agent") or {}


def effort_of(agent):
    """[agent] effort, checked: it reaches the CLI as
    ClaudeAgentOptions.effort (its --effort), and a level the CLI would
    refuse is configuration (RunnerError, exit 2), not a connect or a
    validating turn that fails."""
    from cousin_lib.config import EFFORT_LEVELS
    effort = agent.get("effort")
    if effort is not None and effort not in EFFORT_LEVELS:
        raise RunnerError("cousin.toml [agent] effort must be one of %s, got %r"
                          % (", ".join(EFFORT_LEVELS), effort))
    return effort


def account_for(home):
    """The account this cousin runs on, checked before anything starts: an
    unknown account, a secret file that is open to group or others, not
    ours, a symlink or malformed, or a login inside a secret kind's config
    dir is a RunnerError (exit 2). runner_main calls it BEFORE the lock;
    runner_for calls it again to build the runner. A missing secret file
    is let through: a login to do, which the runner waits for in
    `login_required` (Task 16), never a configuration error."""
    from cousin_lib.config import FrameworkConfig
    if _agent_table(home).get("api_key_file") \
            and FrameworkConfig.root_from_home(Path(home)) is None:
        raise RunnerError(
            "[agent] api_key_file needs a framework root above %s"
            " (cousins/<slug> under an install with config/)" % home)
    root = root_for(home)
    try:
        account = accounts.for_cousin(home, root)
        try:
            accounts.preflight(account, root)
        except accounts.SecretMissing:
            pass    # a secret not written yet is a login to do: the runner waits (Task 16)
    except accounts.AccountsError as err:
        raise RunnerError(str(err))
    return account


def root_for(home):
    """The framework root of a home: the install above it when there is
    one (FrameworkConfig.root_from_home), else the home's grandparent,
    the same root SdkRunner hands its tools."""
    from cousin_lib.config import FrameworkConfig
    home = Path(os.path.abspath(home))
    return FrameworkConfig.root_from_home(home) or home.parent.parent


def export_environment(home, *, overwrite=True):
    """COUSIN_HOME (the absolute home) and FRAMEWORK_ROOT (root_for) in
    this process's environment, which the tools read and the model's
    commands inherit. `overwrite=False` sets only what is unset."""
    home = Path(os.path.abspath(home))
    for name, value in (("COUSIN_HOME", str(home)),
                        ("FRAMEWORK_ROOT", str(root_for(home)))):
        if overwrite or not os.environ.get(name):
            os.environ[name] = value


def runner_for(home, *, kind=None):
    """The runner cousin.toml names. A cousin with no `[agent] runner` is
    a tmux cousin: it gets no runner (its inbox has no producer), unless
    `kind` says otherwise. The home's policy.toml is loaded here, once,
    and handed to the runner: a malformed one is a PolicyError, a
    RunnerError, so the process exits 2 naming the key. An sdk runner's
    MCP registry is checked here too (tools.validate_registry): one
    that does not parse or names a command with no handler is a
    RunnerError, before any session starts. No settings file is
    written: the runner's hooks are in-process."""
    agent = _agent_table(home)
    kind = kind or agent.get("runner")
    if not kind:
        raise RunnerError("%s/cousin.toml has no [agent] runner: this is a tmux"
                          " cousin (pass --runner %s to run it here anyway)"
                          % (home, "|".join(KINDS)))
    if kind not in KINDS:
        raise RunnerError("cousin.toml [agent] runner must be one of %s, got %r"
                          % (", ".join(KINDS), kind))
    from cousin_lib.runner import sessions
    from cousin_lib.runner.policy import Policy
    policy = Policy.load(home)
    # [agent.sessions] (phase 8): a SessionsError is a RunnerError, exit 2
    side = sessions.side_kinds(home)
    if kind == "fake":
        if side:
            raise RunnerError("[agent.sessions] maps %s to \"own\", but side sessions need"
                              " runner = \"sdk\"" % ", ".join(side))
        from cousin_lib.runner.fake import FakeRunner
        return FakeRunner(home, policy=policy)
    if kind == "sdk":
        from cousin_lib.runner import tools
        from cousin_lib.runner.sdk import SdkRunner
        tools.validate_registry(Path(home), root_for(home))
        common = dict(account=account_for(home), model=agent.get("model"),
                      effort=effort_of(agent), policy=policy)
        if side:
            return sessions.Sessions(home, kinds=side, **common)
        return SdkRunner(home, **common)


@contextlib.contextmanager
def hold_lock(home):
    """One runner per cousin: an exclusive flock on <home>/run/runner.lock,
    held for the life of this context (the kernel drops it when the
    process dies, even on SIGKILL). Taken before anything else, because a
    second runner's `requeue_stale` would steal the first one's live
    claims. A held lock is retried for LOCK_TAKE_S before LockHeld:
    is_running() probes by taking the same lock for microseconds (the
    loops tick, the fleet poll, the console's stream), and a runner
    starting inside that probe must not be refused (#79)."""
    path = Path(home) / "run" / "runner.lock"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    except OSError as err:
        raise RunnerError("cannot open the runner lock %s: %s" % (path, err))
    deadline = time.monotonic() + LOCK_TAKE_S
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.monotonic() < deadline:
                time.sleep(0.02)
                continue
            os.close(fd)
            raise LockHeld("another cousin-runner holds %s" % path)
        except OSError:
            os.close(fd)
            raise LockHeld("another cousin-runner holds %s" % path)
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
    up (3): its worker ended, or it stayed `errored` too long; 4 when it
    stayed `errored` waiting for a login (R15: a restart cannot log in)."""
    errored_since = None
    while not stop.is_set():
        why = _gone(runner)
        if why:
            print(why, file=sys.stderr)
            return 3
        state = runner.state()
        if runner.inbox.unfinished() == 0 and state != "running":
            return 0
        # a login is looked at on its own: with side sessions (phase 8) the
        # session waiting for one may not be the one state() reports
        login = getattr(runner, "login_required", lambda: False)()
        # a side session that gave up and waits for its rebuild (phase 8):
        # the batch mode gives up on it as on an errored runner
        stalled = getattr(runner, "side_stalled", lambda: False)()
        if state == "errored" or login or stalled:
            errored_since = errored_since or time.monotonic()
            if time.monotonic() - errored_since > ERRORED_GIVE_UP_S:
                if login:
                    print("cousin-runner: the account needs a login (see"
                          " data/login-required.json)", file=sys.stderr)
                    return 4                     # R15: never 3, a restart cannot log in
                if stalled:
                    print("cousin-runner: a side session could not start for %.0fs"
                          % ERRORED_GIVE_UP_S, file=sys.stderr)
                    return 3
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
    parser.add_argument("--check-auth", action="store_true",
                        help="is this cousin's account logged in (no model call); exit 0 or 4")
    parser.add_argument("--validate", action="store_true",
                        help="with --check-auth: one smallest model turn on a throwaway client")
    args = parser.parse_args(argv)
    args.home = os.path.abspath(args.home)
    if args.validate and not args.check_auth:
        parser.error("--validate goes with --check-auth")
    if args.check_auth:
        # Before the lock, the runner and export_environment: a check runs
        # beside a live cousin and never becomes one.
        return _check_auth(args.home, validate=args.validate)
    try:
        # the account first, BEFORE the lock: a secret open to others or a
        # wrong account is refused without touching the home's lock
        account_for(args.home)
    except RunnerError as err:
        print("cousin-runner: %s" % err, file=sys.stderr)
        return 2
    try:
        with hold_lock(args.home):
            export_environment(args.home)
            try:
                runner = runner_for(args.home, kind=args.runner)
            except RunnerError as err:
                print("cousin-runner: %s" % err, file=sys.stderr)
                return 2
            for name in AUTH_ENV:
                os.environ.pop(name, None)
            return _serve(runner, args.once)
    except LockHeld as err:
        print("cousin-runner: %s" % err, file=sys.stderr)
        return LOCK_HELD_EXIT
    except RunnerError as err:
        print("cousin-runner: %s" % err, file=sys.stderr)
        return 2


def _check_auth(home, *, validate=False):
    """`--check-auth [--validate]`: exit 0 logged in, 4 not (or the
    validating turn did not answer), 2 a configuration error. The check
    is `claude auth status` under the account (presence, no model call);
    --validate adds ONE turn on a bare throwaway client (sdk.validate_account),
    never this cousin's runner."""
    root = root_for(home)
    try:
        # a secret open to others, a symlink or a malformed one is
        # configuration (2), as at the runner's start; a missing one is a
        # login to do, which the status check reports (4)
        accounts.preflight(accounts.for_cousin(home, root), root)
    except accounts.SecretMissing:
        pass
    except accounts.AccountsError as err:
        print("cousin-runner: %s" % err, file=sys.stderr)
        return 2
    rc, line = accounts.check(home, root)
    print(line)
    if rc != 0 or not validate:
        return rc
    for name in AUTH_ENV:         # the SDK merges os.environ under options.env
        os.environ.pop(name, None)
    try:
        account = accounts.for_cousin(home, root)
        agent = _agent_table(home)
        effort = effort_of(agent)
    except (accounts.AccountsError, RunnerError) as err:
        print("cousin-runner: %s" % err, file=sys.stderr)
        return 2
    from cousin_lib import config
    from cousin_lib.runner import sdk
    try:
        rc, line = sdk.validate_account(
            account, root, model=agent.get("model"), effort=effort,
            commit_attribution=config.commit_attribution(root, agent))
    except RunnerError as err:        # the sdk extra is not installed
        print("cousin-runner: %s" % err, file=sys.stderr)
        return 2
    print(line)
    return rc


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
        # Before start: the head of this process's stream says what runs
        # here, for a reader with no runner object (runner/status.py).
        # commit_attribution (tracker #112) rides along so a reader can
        # see whether this cousin's commits carry the CLI's own injected
        # attribution without reading cousin.toml or config/harness.toml
        # itself.
        from cousin_lib import config
        runner.stream.append("runner", {"kind": getattr(runner, "kind", None),
                                        "pid": os.getpid(),
                                        "unsupported": list(runner.unsupported()),
                                        "commit_attribution": config.commit_attribution(
                                            runner.root, _agent_table(runner.home))})
        runner.start()
        policy = getattr(runner, "policy", None)
        if policy is not None:
            runner.stream.append("policy", {"describe": policy.describe()})
        return _once(runner, stop) if once else _forever(runner, stop)
    finally:
        runner.stop(timeout=STOP_TIMEOUT_S)
        if previous_term is not _UNSET:
            signal.signal(signal.SIGTERM, previous_term)
        if previous_int is not _UNSET:
            signal.signal(signal.SIGINT, previous_int)


if __name__ == "__main__":
    sys.exit(runner_main())
