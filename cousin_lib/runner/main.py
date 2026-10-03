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
the lock; an account on the other lane, an opencode cousin with no
`[agent] model` or one whose config names the subscription bridge; a
malformed policy.toml, an MCP registry that does not parse or names a
command with no in-process handler, an [agent] effort outside the
levels, a harness that is not config/harness.lock.toml's under
[agent] strict_harness = true), and a tmux runner that gave up on its
pane (five failed starts in a row or too many pane losses; its reason in
data/run/tmux-giving-up.json): the supervisor leaves 2 down, never
restarted; 3 when the runner gave up
(its worker ended, e.g. it could not connect, or `--once` found it
`errored` for longer than ERRORED_GIVE_UP_S past its drain, or could
not read its inbox that long), so a supervisor restarts
it; 4 when `--check-auth`
found the account not logged in (or `--validate`'s one turn did not
answer), or `--once` found the runner waiting for a login: a
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

Before the runner starts, what its kind runs (the Agent SDK and the CLI
it bundles, `claude` on PATH, the opencode binary) is compared once with
config/harness.lock.toml (harness_at_start): a `harness` event right
after the head says the versions, and a mismatch is a warning naming
both, which [agent] strict_harness = true turns into exit 2.
"""
import argparse
import contextlib
import fcntl
import os
import signal
import struct
import sys
import threading
import time
import tomllib
from pathlib import Path

from cousin_lib import accounts
from cousin_lib.delivery import RUNNER_KINDS, lane_refusal
from cousin_lib.runner import restart_note
from cousin_lib.runner.base import RunnerError

KINDS = RUNNER_KINDS      # the runners runner_for builds, one list (delivery)

# The credentials a runner must never inherit from the shell that started
# it: the SDK builds the CLI's environment as {**os.environ, **options.env},
# so an overlay cannot unset them and only removing them here can. The
# cousin's account (accounts.account_env, in options.env) is the only
# source of credentials: the key, the token and the config dir.
AUTH_ENV = accounts.AUTH_VARS

# `--once` gives up on a runner that stays `errored` this long, past the
# runner's own drain_timeout_s when it has one (_errored_budget).
ERRORED_GIVE_UP_S = 10.0

# How long a stopping runner gives its current turn (runner.stop's
# timeout on SIGTERM/SIGINT). The one number every budget above it is
# built from: cousin-supervisor waits STOP_TIMEOUT_S + 5 before SIGKILL.
STOP_TIMEOUT_S = 30.0

RUN_DIR_MODE = 0o700  # <home>/run/ when hold_lock creates it
LOCK_HELD_EXIT = 5    # another runner holds <home>/run/runner.lock
LOCK_TAKE_S = 1.0     # hold_lock retries this long: an older probe (flock) holds the lock for microseconds

# struct flock (fcntl(2)) for the open-file-description lock on
# runner.lock: l_type, l_whence, l_start, l_len, l_pid, native layout
# (64-bit Linux: 32 bytes). l_start = l_len = 0 is the whole file.
_FLOCK = "hhqqi4x"

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


def commit_attribution_of(agent, root):
    """config.commit_attribution, checked eagerly at
    runner start like effort_of: a cousin.toml or config/harness.toml
    value that is not a real boolean is configuration (RunnerError,
    exit 2), never something that reaches a live turn or the head
    stream event only to blow up there."""
    from cousin_lib.config import MissingConfigError
    from cousin_lib.config import commit_attribution as resolve
    try:
        return resolve(root, agent)
    except MissingConfigError as err:
        raise RunnerError(str(err))


def strict_harness_of(agent):
    """[agent] strict_harness, checked like reply_gate: a value that is
    not true or false is configuration (RunnerError, exit 2)."""
    strict = agent.get("strict_harness", False)
    if not isinstance(strict, bool):
        raise RunnerError("cousin.toml [agent] strict_harness must be true or false, got %r"
                          % strict)
    return strict


def harness_at_start(home, kind):
    """What this runner kind runs against config/harness.lock.toml
    (cousin_lib.harness_lock.check), once, before the runner starts: the
    result for the `harness` event, or None for a kind with no harness.
    The result is also the cousin's `harness:<slug>` row in the health
    record. A mismatch (or a version that cannot be read) is a warning
    on stderr and the runner starts; with [agent] strict_harness = true
    it is a RunnerError (exit 2) naming the installed and the locked
    version. A check that cannot run is a mismatch, never a crash."""
    from cousin_lib import harness_lock
    agent = _agent_table(home)
    strict = strict_harness_of(agent)
    try:
        result = harness_lock.check(kind, agent)
    except Exception as err:  # noqa: BLE001 - the check never fails a start by itself
        problem = "harness check failed: %s: %s" % (type(err).__name__, err)
        result = {"kind": kind, "ok": False, "locked": {}, "installed": {},
                  "problems": [problem], "message": problem}
    if result is None:
        return None
    _record_harness_health(home, result)
    if not result["ok"]:
        if strict:
            raise RunnerError("the harness is not the locked one (%s): %s;"
                              " [agent] strict_harness = true refuses it"
                              % (harness_lock.LOCK_PATH.name, result["message"]))
        print("cousin-runner: warning: the harness is not the locked one (%s): %s"
              % (harness_lock.LOCK_PATH.name, result["message"]), file=sys.stderr)
    return result


def _record_harness_health(home, result):
    """The check as one component of the health record (cousin_lib.health),
    `harness:<slug>`; a record that cannot be written says nothing."""
    try:
        from cousin_lib import health
        try:
            slug = tomllib.loads((Path(home) / "cousin.toml").read_text())["cousin"]["slug"]
        except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError):
            slug = Path(home).name
        health.record(root_for(home), [("harness:%s" % slug, result["ok"],
                                        None if result["ok"] else result["message"])])
    except Exception:  # noqa: BLE001 - the record is a view, never a reason to stop
        pass


def account_for(home):
    """The account this cousin runs on, checked before anything starts: an
    unknown account, a secret file that is open to group or others, not
    ours, a symlink or malformed, or a login inside a secret kind's config
    dir is a RunnerError (exit 2). runner_main calls it BEFORE the lock;
    runner_for calls it again to build the runner. A missing secret file
    is let through: a login to do, which the runner waits for in
    `login_required`, never a configuration error."""
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
            pass    # a secret not written yet is a login to do: the runner waits
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
    refused with `delivery.lane_refusal` (2.0.0 has no legacy tmux lane),
    unless `kind` says otherwise. The home's policy.toml is loaded here, once,
    and handed to the runner: a malformed one is a PolicyError, a
    RunnerError, so the process exits 2 naming the key. An sdk or
    opencode runner's MCP registry is checked here too
    (tools.validate_registry): one that does not parse or names a
    command with no handler is a RunnerError, before any session starts; so is an account on the
    other lane (accounts.check_lane: an opencode runner on an opencode
    account only, an sdk runner never on one). No settings file is
    written: the runner's hooks are in-process."""
    agent = _agent_table(home)
    kind = kind or agent.get("runner")
    if not kind:
        raise RunnerError(lane_refusal(home))
    if kind not in KINDS:
        raise RunnerError("cousin.toml [agent] runner must be one of %s, got %r"
                          % (", ".join(KINDS), kind))
    # checked eagerly, for every kind, so a value neither
    # true nor false is exit 2 at runner start, like effort_of - never
    # a MissingConfigError that reaches a live turn or the head event.
    commit_attribution = commit_attribution_of(agent, root_for(home))
    # read as a bool by the sdk and opencode reply gates: a quoted "false" would
    # be truthy and leave the gate on with nothing saying why
    if not isinstance(agent.get("reply_gate", True), bool):
        raise RunnerError("cousin.toml [agent] reply_gate must be true or false, got %r"
                          % agent["reply_gate"])
    strict_harness_of(agent)
    from cousin_lib.runner import sessions
    from cousin_lib.runner.policy import Policy
    policy = Policy.load(home)
    # [agent.sessions]: a SessionsError is a RunnerError, exit 2
    side = sessions.side_kinds(home)
    if kind == "fake":
        if side:
            raise RunnerError("[agent.sessions] maps %s to \"own\", but side sessions need"
                              " runner = \"sdk\"" % ", ".join(side))
        try:
            # the lanes do not mix for the fake either (an opencode account's
            # data dir never reaches another runner); no preflight: it runs no model
            accounts.check_lane(accounts.for_cousin(home, root_for(home)), kind)
        except accounts.AccountsError as err:
            raise RunnerError(str(err))
        from cousin_lib.runner.fake import FakeRunner
        return FakeRunner(home, policy=policy)
    from cousin_lib.runner import tools
    tools.validate_registry(Path(home), root_for(home))
    account = account_for(home)
    try:
        # the Claude kinds never reach opencode, opencode's never the SDK
        accounts.check_lane(account, kind)
    except accounts.AccountsError as err:
        raise RunnerError(str(err))
    if kind == "sdk":
        from cousin_lib.runner.sdk import SdkRunner
        common = dict(account=account, model=agent.get("model"),
                      effort=effort_of(agent), commit_attribution=commit_attribution,
                      policy=policy)
        if side:
            return sessions.Sessions(home, kinds=side, **common)
        return SdkRunner(home, **common)
    if kind == "tmux":
        if side:
            raise RunnerError("[agent.sessions] maps %s to \"own\", but side sessions need"
                              " runner = \"sdk\"" % ", ".join(side))
        if account.kind in ("claude-token", "anthropic-key"):
            raise RunnerError("the tmux kind runs on a subscription login (host or a"
                              " claude-login account); %s accounts are refused until a"
                              " login-free config dir is shown to start with no menu"
                              " (onboarding is skippable by seeding,"
                              " but a token's login screen is not measured)" % account.kind)
        from cousin_lib.runner.tmux_launch import env_allow_of
        from cousin_lib.runner.tmux_runner import TmuxRunner
        try:
            env_allow = env_allow_of(agent)
        except ValueError as exc:
            raise RunnerError(str(exc))
        return TmuxRunner(home, account=account, model=agent.get("model"),
                          effort=effort_of(agent), policy=policy, env_allow=env_allow)
    if kind == "opencode":
        if side:
            raise RunnerError("[agent.sessions] maps %s to \"own\", but side sessions need"
                              " runner = \"sdk\"" % ", ".join(side))
        # the model named, the bridge guard and the models'
        # provider check are the constructor's: each a RunnerError, exit 2
        from cousin_lib.runner.opencode import OpencodeRunner
        return OpencodeRunner(home, account=account, policy=policy)


def _ofd_lock(kind):
    """A struct flock over the whole file for F_OFD_SETLK / F_OFD_GETLK."""
    return struct.pack(_FLOCK, kind, os.SEEK_SET, 0, 0, 0)


@contextlib.contextmanager
def hold_lock(home):
    """One runner per cousin: <home>/run/runner.lock, held for the life of
    this context (the kernel drops it when the process dies, even on
    SIGKILL). Taken before anything else, because a second runner's
    `requeue_stale` would steal the first one's live claims.

    Two locks on one descriptor. An exclusive flock excludes a
    second runner, a runner of an older framework version included; an
    open-file-description write lock (F_OFD_SETLK) is what is_running()
    reads with F_OFD_GETLK, a query that takes nothing, so a probe never
    holds the lock a starting runner needs. The two live in separate
    kernel namespaces (flock cannot see an OFD lock nor the reverse),
    which is why the holder takes both. A held flock is still retried
    for LOCK_TAKE_S before LockHeld: an is_running() of an older
    version, still running in a long-lived process, probes by taking
    the flock for microseconds."""
    path = Path(home) / "run" / "runner.lock"
    try:
        # made 0700 (the owner's alone) when this creates it; an existing
        # run/ keeps its mode, and its home's mode is what closes it
        path.parent.mkdir(mode=RUN_DIR_MODE, parents=True, exist_ok=True)
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
        # the flock is ours, so no other runner of this version holds the
        # OFD lock (it takes the flock first): a refusal here is a holder
        # outside the framework's own code, and still busy, not config
        fcntl.fcntl(fd, fcntl.F_OFD_SETLK, _ofd_lock(fcntl.F_WRLCK))
    except OSError:
        os.close(fd)
        raise LockHeld("another cousin-runner holds %s" % path)
    try:
        yield
    finally:
        os.close(fd)


def is_running(home):
    """True when a runner holds `<home>/run/runner.lock`. An F_OFD_GETLK
    query on a fresh descriptor: it asks whether a write lock
    could be placed and takes nothing, so a runner starting during the
    probe is never refused. The holder's OFD lock is visible from any
    process, this one included. A missing lock file is False, nothing
    to hold. A runner of an older version holds only a flock, which this
    query cannot see: such a runner reads as stopped until it restarts
    (hold_lock still refuses a second runner beside it)."""
    path = Path(home) / "run" / "runner.lock"
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return False
    try:
        answer = fcntl.fcntl(fd, fcntl.F_OFD_GETLK, _ofd_lock(fcntl.F_WRLCK))
    except OSError:
        return False
    finally:
        os.close(fd)
    return struct.unpack(_FLOCK, answer)[0] != fcntl.F_UNLCK


def _gone(runner):
    """The give-up line when the runner's worker has ended, else None; the
    exit is the runner's own `exit_code` when it set one, else 3."""
    if runner.worker_alive():
        return None
    return "cousin-runner: the runner gave up: %s" % (
        getattr(runner, "fatal", None) or "its worker ended")


def _errored_budget(runner):
    """How long `--once` lets a runner stay `errored`: a failed SDK
    turn is `errored` through its resync, which drains for up to the
    runner's drain_timeout_s before it recovers, so the drain comes on top
    of ERRORED_GIVE_UP_S. A runner with no drain (the fake, opencode, a
    side-session set reads its primary's) gets ERRORED_GIVE_UP_S."""
    for obj in (runner, getattr(runner, "primary", None)):
        drain = getattr(obj, "drain_timeout_s", None)
        if isinstance(drain, (int, float)) and not isinstance(drain, bool):
            return ERRORED_GIVE_UP_S + float(drain)
    return ERRORED_GIVE_UP_S


def _once(runner, stop):
    """Until the inbox is drained (0), a signal (0), or the runner gives
    up (3): its worker ended, or it stayed `errored` too long, or its
    inbox could not be read for ERRORED_GIVE_UP_S; 4 when it stayed
    `errored` waiting for a login (a restart cannot log in)."""
    errored_since = None
    unreadable_since = None
    while not stop.is_set():
        why = _gone(runner)
        if why:
            print(why, file=sys.stderr)
            return getattr(runner, "exit_code", None) or 3
        state = runner.state()
        try:
            unfinished = runner.inbox.unfinished()
        except Exception as err:  # noqa: BLE001 - a busy or broken read is retried, then named
            unreadable_since = unreadable_since or time.monotonic()
            if time.monotonic() - unreadable_since > ERRORED_GIVE_UP_S:
                print("cousin-runner: the inbox could not be read for %.0fs: %s: %s"
                      % (ERRORED_GIVE_UP_S, type(err).__name__, err), file=sys.stderr)
                return 3
            time.sleep(0.05)
            continue
        unreadable_since = None
        if unfinished == 0 and state != "running":
            return 0
        # a login is looked at on its own: with side sessions the
        # session waiting for one may not be the one state() reports
        login = getattr(runner, "login_required", lambda: False)()
        # a side session that gave up and waits for its rebuild:
        # the batch mode gives up on it as on an errored runner
        stalled = getattr(runner, "side_stalled", lambda: False)()
        if state == "errored" or login or stalled:
            errored_since = errored_since or time.monotonic()
            # a login or a stalled side session drains nothing: the plain budget
            budget = ERRORED_GIVE_UP_S if (login or stalled) else _errored_budget(runner)
            if time.monotonic() - errored_since > budget:
                if login:
                    print("cousin-runner: the account needs a login (see"
                          " data/login-required.json)", file=sys.stderr)
                    return 4                     # never 3: a restart cannot log in
                if stalled:
                    print("cousin-runner: a side session could not start for %.0fs"
                          % ERRORED_GIVE_UP_S, file=sys.stderr)
                    return 3
                print("cousin-runner: the runner stayed errored for %.0fs"
                      % budget, file=sys.stderr)
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
            return getattr(runner, "exit_code", None) or 3
        time.sleep(0.2)
    return 0


def runner_main(argv=None):
    parser = argparse.ArgumentParser(prog="cousin-runner")
    parser.add_argument("--home", required=True)
    parser.add_argument("--runner", choices=KINDS)
    parser.add_argument("--reap-pane", action="store_true",
                        help="the tmux kind: kill this cousin's pane while no runner holds its lock")
    parser.add_argument("--once", action="store_true",
                        help="drain the inbox, then exit")
    parser.add_argument("--check-auth", action="store_true",
                        help="is this cousin's account logged in (no model call); exit 0 or 4")
    parser.add_argument("--validate", action="store_true",
                        help="with --check-auth: one smallest model turn on a throwaway client")
    args = parser.parse_args(argv)
    args.home = os.path.abspath(args.home)
    if args.reap_pane:
        from cousin_lib.runner.tmux_runner import reap_pane
        return reap_pane(args.home)
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
            try:
                harness = harness_at_start(
                    args.home, args.runner or _agent_table(args.home).get("runner"))
            except RunnerError as err:
                print("cousin-runner: %s" % err, file=sys.stderr)
                return 2
            return _serve(runner, args.once, harness=harness)
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
    try:
        kind = accounts.for_cousin(home, root).kind
    except accounts.AccountsError as err:
        print("cousin-runner: %s" % err, file=sys.stderr)
        return 2
    if kind == "opencode":
        # the validating turn is the SDK's (a bare Claude client): never on this lane
        print("cousin-runner: --validate runs one turn on the sdk lane; an opencode account"
              " has no validating turn (--check-auth alone reports its presence)",
              file=sys.stderr)
        return 2
    for name in AUTH_ENV:         # the SDK merges os.environ under options.env
        os.environ.pop(name, None)
    try:
        account = accounts.for_cousin(home, root)
        agent = _agent_table(home)
        effort = effort_of(agent)
        attribution = commit_attribution_of(agent, root)
    except (accounts.AccountsError, RunnerError) as err:
        print("cousin-runner: %s" % err, file=sys.stderr)
        return 2
    from cousin_lib.runner import sdk
    try:
        rc, line = sdk.validate_account(
            account, root, model=agent.get("model"), effort=effort,
            commit_attribution=attribution)
    except RunnerError as err:        # the sdk extra is not installed
        print("cousin-runner: %s" % err, file=sys.stderr)
        return 2
    print(line)
    return rc


def _name_removed_keys(runner):
    """The keys 2.0.0 removed that this cousin or its install still
    carries, named and never fatal: one `system` `config` event per
    finding (after the head event) and one stderr line. A scan that
    cannot run says nothing: it never stops a start."""
    from cousin_lib import removed_keys
    home = Path(runner.home)
    try:
        found = removed_keys.scan(getattr(runner, "root", None) or root_for(home), home)
    except Exception:  # noqa: BLE001 - a warning must never fail a start
        return
    if not found:
        return
    for finding in found:
        runner.stream.append("system", dict(finding, subtype="config"))
    install = any(f["where"] != "cousin.toml" for f in found)
    print("cousin-runner: 2.0.0 no longer reads %s (inert); `cousin-migrate tidy %s` removes"
          " the cousin's%s (docs/configuration.md, \"Removed in 2.0.0\")"
          % (removed_keys.summary(found), home.name,
             ", `cousin-migrate tidy --all` the install's" if install else ""),
          file=sys.stderr)


def _name_lane_gaps(runner):
    """A kind with no tool gate (perimeter.UNGATED_KINDS) says so at
    start: one `system` `perimeter` event and one stderr line. A warning,
    never a refusal: the cousin runs."""
    from cousin_lib import perimeter
    kind = getattr(runner, "kind", None)
    line = perimeter.lane_warning(kind)
    if line is None:
        return
    runner.stream.append("system", {"subtype": "perimeter", "kind": kind,
                                    "level": "warning", "line": line})
    print("cousin-runner: %s" % line, file=sys.stderr)


def _serve(runner, once, harness=None):
    stop = threading.Event()

    def _signal(signum, frame):
        if stop.is_set():
            return                  # the stop is under way (the finally set it)
        stop.set()
        # at once, not when _forever next polls: a runner asked to stop
        # claims nothing new
        begin = getattr(runner, "begin_stop", None)
        if begin is not None:
            begin()

    previous_term = previous_int = _UNSET
    try:
        previous_term = signal.signal(signal.SIGTERM, _signal)
        previous_int = signal.signal(signal.SIGINT, _signal)
        # a claim from a runner that died is ours now (the lock says no
        # other runner is alive on this home). That runner died in a turn:
        # the resumed session is told so (restart_note). A mark a
        # requested stop left for the same turn keeps its hold: that stop
        # was asked for, whatever the sweep finds after it.
        # A kind whose claims can be live in a pane that outlived its runner
        # (tmux, recovers_claims) recovers them itself in start(),
        # and closes a turn the restart cut there; it takes no restart note.
        if not getattr(runner, "recovers_claims", False) \
                and runner.inbox.requeue_stale(older_than_s=0.0):
            try:
                earlier = restart_note.read(runner.home) or {}
                restart_note.mark(runner.home, "the last runner died with a row claimed",
                                  held=earlier.get("held"), requeued=True)
            except OSError:
                pass
        # Before start: the head of this process's stream says what runs
        # here, for a reader with no runner object (runner/status.py).
        # commit_attribution rides along so a reader can
        # see whether this cousin's commits carry the CLI's own injected
        # attribution without reading cousin.toml or config/harness.toml
        # itself. runner_for already validated it (exit 2 before this
        # point on a bad value); commit_attribution_of here is a second,
        # cheap read - never the first place a bad value can surface.
        runner.stream.append("runner", {"kind": getattr(runner, "kind", None),
                                        "pid": os.getpid(),
                                        "unsupported": list(runner.unsupported()),
                                        "commit_attribution": commit_attribution_of(
                                            _agent_table(runner.home), runner.root)})
        if harness is not None:
            # the versions this runner runs (harness_at_start), right after
            # the head: a mismatch is a warning with both versions
            runner.stream.append("harness", dict(harness, level="info" if harness["ok"]
                                                 else "warning"))
        _name_removed_keys(runner)
        _name_lane_gaps(runner)
        runner.start()
        policy = getattr(runner, "policy", None)
        if policy is not None:
            runner.stream.append("policy", {"describe": policy.describe()})
        return _once(runner, stop) if once else _forever(runner, stop)
    finally:
        stop.set()                  # a signal from here on does nothing more
        runner.stop(timeout=STOP_TIMEOUT_S)
        if previous_term is not _UNSET:
            signal.signal(signal.SIGTERM, previous_term)
        if previous_int is not _UNSET:
            signal.signal(signal.SIGINT, previous_int)


if __name__ == "__main__":
    sys.exit(runner_main())
