"""cousin-supervisor: one process that keeps an install's daemons up.

It is the container's init and a bare host's single unit. It starts the
console, the loops daemon (the one clock), one `cousin-runner` per
runner cousin and that cousin's Telegram bridge when it has one,
restarts what crashes, and stops everything in order on SIGTERM.

Children. Each is `sys.executable -m cousin_lib.<module> ...` (one
interpreter for every child, whatever PATH says), started in its own
session (a terminal's Ctrl-C reaches the supervisor only, which decides
the order), stdin from /dev/null, stdout and stderr merged into one pipe.
One thread per child reads that pipe and writes each line to our stdout
as `<name> | <line>`, flushed per line; our own lines are `supervisor: `.

Reaping. ONE loop, `os.waitpid(-1, WNOHANG)`, owns every exit status: a
pid in the table is that child's exit (its Popen.returncode is set by
hand, so nothing ever calls Popen.wait or poll, which would race the
loop for the same status); any other pid is an orphan a child left
behind (the SDK's CLI of a SIGKILLed runner), reparented to us when we
are PID 1, and reaped so it never stays a zombie.

Exits, by the child's documented exit codes (classify_exit):
  - 2 is configuration, for every kind: `failing` at once, left down;
    restarting it would only bury the line that says what is wrong;
  - a runner's 4 is a login to do: `stopped`, never restarted
    (runner/main.py: "a supervisor must NOT restart on 4");
  - 5 is busy, for a runner (another runner holds the home's lock) and
    for the loops daemon (another loops daemon holds the root's): a
    leftover of a crashed supervisor, one started by hand, a unit still
    enabled. Restarted after the backoff (1, 2, 4 ... 60 s), one line per
    attempt, and never counted toward `failing`: a busy child is waiting
    for a holder, not crash-looping, so it comes back whenever the holder
    goes, however long that takes;
  - the console's 75 is its own restart route: restarted at once and
    not counted (console/routes_admin.py RESTART_EXIT_CODE);
  - anything else (a runner's 3 "gave up", 0, a signal) restarts after
    a backoff of 1, 2, 4 ... 60 s (RestartPolicy); a child that ran
    60 s is healthy and starts again from 1 s; five counted exits
    inside 60 s mark it `failing`, left down with one loud line.

Stop (SIGTERM or SIGINT): the reverse of the start order. Every bridge
is signalled together and waited for (10 s each), then every runner
is signalled together and waited for together (up to
runner.main.STOP_TIMEOUT_S + 5 s each: the runner gives its turn
STOP_TIMEOUT_S), then the loops daemon, then the console
(10 s each); a child still alive at its timeout is SIGKILLed with its
process group. Then the supervisor exits 0.

Control. `<root>/run/supervisor.sock` (run/ is 0700) takes one JSON line
per connection and answers one: `status`, `start`, `stop`, `reload`.
`start` and `stop` name their child by `slug` (a runner cousin, only
ever `runner:<slug>`) or by `name` (`console` or `loops`), never a
guess between the two; `stop` takes `wait` (default true: answered when
the child is down; false: answered at once, `state: "stopping"`) and
`by` (who asked, for the hold marker). The socket's threads only queue
a request; the main loop answers it, so nothing but the main loop
touches the table. `request()` is the client (`cousin-supervisor
status|start|stop|reload`, and the runner lane of
spawn.start_cousin/stop_cousin). A flock on `run/supervisor.lock` keeps
one supervisor per root. `run/supervisor.json` is the `status` body,
rewritten (tmp + rename) on every change, for readers that must not
block on a socket (the console's fleet view); `snapshot()` reads it
only while a supervisor holds that lock.

Which cousins. Every cousin whose cousin.toml `[agent] runner` is one of
delivery.RUNNER_KINDS gets a runner child, unless `[agent] auto_start = false`
(runner_cousins). A tmux cousin is never ours. SIGHUP and `reload`
rescan the registry: a new runner cousin is started, one that is gone
or left the runner lane is stopped and removed, a `failing` child is
cleared and started again; nothing healthy is bounced.

Holds. A `stop` of a runner cousin writes `<home>/run/held` (an ISO time
and who asked) before it signals, and `start` removes it; runner_cousins
skips a held cousin, so a stop survives a supervisor or container
restart, as a stopped tmux cousin stays stopped. `auto_start = false`
stays the config-level opt-out. A `stop` of the console or the loops
daemon holds only until `start` or the next supervisor start.

Bridges (#101). A runner cousin whose `[telegram]` passes
telegram.load_bridge_config (enabled, a token, operators) gets a child
`telegram:<slug>` (`python3 -m cousin_lib.telegram --home <home>`, the
entry point the tmux lane's telegram_admin.start_bridge runs), added
after its runner and stopped with it: a runner held by a `stop` holds
its bridge too, and a `start` brings both back. A config that fails the
check is one line with the reason, never a child. The bridge's pid is
written to data/telegram.pid and its output also goes to
data/telegram.log, where telegram_admin and the console look. A bridge
already running outside the supervisor (that pid file names a live
bridge we did not start) is left alone: ours waits in `backoff`,
uncounted, and starts once that one is gone, never a second poller on
the bot (Telegram answers 409); once the config no longer runs, the
rescan SIGTERMs that outside bridge, so off means off. A bridge
switched off and on again before it is down stays and comes back up. A rescan adds or removes the bridge as
`[telegram] enabled` changes, and restarts it when its token, operators
or port changed; a tmux cousin's bridge is never ours (spawn starts it).
There is no `start`/`stop` of a bridge by name: it follows its runner,
and the console's Telegram switch writes cousin.toml, then asks for a
`reload`.
"""
import argparse
import collections
import fcntl
import hashlib
import json
import os
import queue
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path

from cousin_lib import loops
from cousin_lib.delivery import RUNNER_KINDS  # the one list of runner kinds (M6)
from cousin_lib.runner.main import LOCK_HELD_EXIT, STOP_TIMEOUT_S

STATES = ("running", "backoff", "failing", "stopped")
SOCKET = "run/supervisor.sock"
SNAPSHOT = "run/supervisor.json"
LOCK = "run/supervisor.lock"
KINDS = ("console", "loops", "runner", "telegram")
# Start order by kind; stop order is its reverse (R5).
_KIND_ORDER = {kind: i for i, kind in enumerate(KINDS)}

# Seconds between SIGTERM and SIGKILL. A runner gives its current turn
# STOP_TIMEOUT_S (runner/main.py _serve: runner.stop(timeout=STOP_TIMEOUT_S));
# 5 more for it to close its inbox row and exit. One constant: a longer
# runner stop moves this budget with it.
STOP_TIMEOUTS = {"console": 10.0, "loops": 10.0, "runner": STOP_TIMEOUT_S + 5.0,
                 "telegram": 10.0}

BACKOFF = (1, 2, 4, 8, 16, 32, 60)
WINDOW_S = 60.0          # the sliding window five counted exits must fall in
MAX_EXITS = 5            # counted exits inside WINDOW_S that make a child `failing`
HEALTHY_AFTER_S = 60.0   # a child that ran this long starts its backoff again from 1 s
TICK_S = 0.2             # the main loop's period: reap, restart, answer
KILL_GRACE_S = 5.0       # after a SIGKILL, before the ordered stop writes a child off

CONFIG_EXIT = 2              # every child: a configuration problem
RUNNER_LOGIN_EXIT = 4        # cousin-runner: a person must log in
RUNNER_BUSY_EXIT = LOCK_HELD_EXIT  # cousin-runner: another runner holds the home's lock (5)
LOOPS_BUSY_EXIT = loops.LOCK_HELD_EXIT  # cousin-loops run: another loops daemon holds the root's lock (5)
CONSOLE_RESTART_EXIT = 75    # cousin-console's restart route (routes_admin.RESTART_EXIT_CODE)

AUTO_START_DEFAULT = True        # R4: a runner cousin starts with the supervisor unless it opts out

REQUEST_LINE_MAX = 1 << 20       # bytes of one request or answer line
ANSWER_WAIT_S = 60.0             # how long a connection waits for the main loop (a runner's stop: 35 s)
_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
NAMED = ("console", "loops")     # the children a request addresses by `name`
HELD = "run/held"                # under a cousin's home: stopped by request, held down
LOCK_TAKE_S = 1.0                # _take_lock retries this long (a snapshot() probe holds LOCK_SH for microseconds)


class SupervisorError(Exception):
    """The supervisor cannot run here: another holds the root's lock, or
    its socket cannot be bound."""


class SupervisorUnavailable(Exception):
    """No supervisor answered on the root's socket."""


class SupervisorAbsent(SupervisorUnavailable):
    """No supervisor at all: nothing to connect to (no socket, a stale one,
    refused). A live one that is slow or answers badly is the plain
    SupervisorUnavailable (#113): a caller whose fallback does the
    supervisor's work itself runs it only for this one."""


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ChildSpec:
    """What to run: a name (`console`, `loops`, `runner:<slug>`,
    `telegram:<slug>`), a kind (KINDS), the argv, the SIGTERM-to-SIGKILL
    timeout, the cousin's slug and home for a runner or a bridge, and
    extra environment on top of the supervisor's. `pid_file` holds the
    live pid while the child runs; `log_file` gets a copy of its output
    lines (unprefixed), on top of the supervisor's own output."""

    def __init__(self, name, kind, argv, stop_timeout=None, slug=None, env=None,
                 home=None, pid_file=None, log_file=None):
        if kind not in KINDS:
            raise ValueError("child kind must be one of %s, got %r" % (", ".join(KINDS), kind))
        self.name = name
        self.kind = kind
        self.argv = list(argv)
        self.stop_timeout = float(STOP_TIMEOUTS[kind] if stop_timeout is None else stop_timeout)
        self.slug = slug
        self.env = dict(env or {})
        self.home = Path(home) if home is not None else None
        self.pid_file = Path(pid_file) if pid_file is not None else None
        self.log_file = Path(log_file) if log_file is not None else None

    def __repr__(self):
        return "ChildSpec(%r, %r)" % (self.name, self.kind)


def console_spec(root, host, port):
    return ChildSpec("console", "console",
                     [sys.executable, "-m", "cousin_lib.console.app", "serve",
                      "--root", str(root), "--host", str(host), "--port", str(port)])


def loops_spec(root, interval):
    # the root travels in the environment (FRAMEWORK_ROOT): cousin-loops has no --root
    return ChildSpec("loops", "loops",
                     [sys.executable, "-m", "cousin_lib.loops", "run",
                      "--interval", "%g" % float(interval)])


def runner_spec(home):
    # the registry's directory name is the slug (cousins/<slug>, as spawn makes it)
    slug = Path(home).name
    return ChildSpec("runner:%s" % slug, "runner",
                     [sys.executable, "-m", "cousin_lib.runner.main", "--home", str(home)],
                     slug=slug, home=home)


def _bridge_argv(home):
    # the entry point telegram_admin.start_bridge runs on the tmux lane
    return [sys.executable, "-m", "cousin_lib.telegram", "--home", str(home)]


def telegram_spec(home):
    """A runner cousin's Telegram bridge. Its pid goes to data/telegram.pid
    and its output also to data/telegram.log, where telegram_admin and
    the console look on either lane."""
    home = Path(home)
    slug = home.name
    return ChildSpec("telegram:%s" % slug, "telegram", _bridge_argv(home), slug=slug,
                     home=home, pid_file=home / "data" / "telegram.pid",
                     log_file=home / "data" / "telegram.log")


def bridge_config(home, root):
    """(digest, None) when the cousin's bridge can run
    (telegram.load_bridge_config accepts its config; the digest changes
    when the token, the operators or the port do), else (None, why).
    `why` is None as well when cousin.toml has no [telegram] table: a
    cousin without Telegram is not worth a line."""
    from cousin_lib import telegram
    try:
        data = tomllib.loads((Path(home) / "cousin.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as err:
        return None, "cannot read cousin.toml: %s" % err
    if not data.get("telegram"):
        return None, None
    try:
        cfg = telegram.load_bridge_config(home, root=root)
    except telegram.TelegramConfigError as err:
        return None, str(err)
    except Exception as err:          # noqa: BLE001 - a bad config is a reason, never a crash
        return None, "bad [telegram] configuration (%s: %s)" % (type(err).__name__, err)
    body = json.dumps([cfg.token, sorted(cfg.operator_ids),
                       sorted([str(k), v] for k, v in cfg.operator_name.items()), cfg.port])
    return hashlib.sha256(body.encode()).hexdigest(), None


def _outside_bridge(home, ours=None):
    """The pid of a live bridge for this home that we did not start (its
    data/telegram.pid, checked as telegram_admin checks it; `ours` is our
    own child's pid, never outside), or None."""
    from cousin_lib import telegram_admin
    pid = telegram_admin.bridge_pid(home)
    return None if pid is None or pid == ours else pid


def classify_exit(kind, code):
    """(action, reason) for a child that exited without being asked to.
    `code` is os.waitstatus_to_exitcode's: the exit status, or -N for
    signal N. Actions: "restart" (after the backoff, counted), "busy"
    (after the backoff, not counted: a holder has its lock), "now" (at
    once, not counted), "failing" (left down), "stopped" (left down,
    not an error of ours). The reason is None for a plain crash."""
    if code == CONFIG_EXIT:
        return "failing", "configuration (exit %d)" % code
    if kind == "runner" and code == RUNNER_LOGIN_EXIT:
        return "stopped", "login required (exit %d)" % code
    if kind == "runner" and code == RUNNER_BUSY_EXIT:
        return "busy", "another runner holds its lock (exit %d)" % code
    if kind == "loops" and code == LOOPS_BUSY_EXIT:
        return "busy", "another loops daemon holds its lock (exit %d)" % code
    if kind == "console" and code == CONSOLE_RESTART_EXIT:
        return "now", "restart requested (exit %d)" % code
    return "restart", None


def describe_exit(code):
    """`code 3` or `signal SIGKILL`: how a child ended, for lines and status."""
    if code is None:
        return None
    if code < 0:
        try:
            return "signal %s" % signal.Signals(-code).name
        except ValueError:
            return "signal %d" % -code
    return "code %d" % code


class RestartPolicy:
    """The restart arithmetic of one child, clock-free (the caller passes
    `now` and how long the child ran). on_exit returns the delay before
    the next start, or None when the child is `failing`: MAX_EXITS
    counted exits inside WINDOW_S. The window counts exits, not
    restarts, so a child that dies at once is caught in about 15 s."""

    def __init__(self, backoff=BACKOFF, window=WINDOW_S, max_exits=MAX_EXITS,
                 healthy_after=HEALTHY_AFTER_S):
        self.backoff = tuple(backoff)
        self.window = float(window)
        self.max_exits = int(max_exits)
        self.healthy_after = float(healthy_after)
        self.reset()

    def reset(self):
        self.exits = collections.deque()
        self.step = 0

    def on_exit(self, now, ran_for):
        if ran_for >= self.healthy_after:
            self.step = 0                        # a healthy run: back to the first delay
        self.exits.append(now)
        while self.exits and self.exits[0] <= now - self.window:
            self.exits.popleft()
        if len(self.exits) >= self.max_exits:
            return None
        return self._next_delay()

    def on_busy(self, ran_for):
        """A busy exit (another process holds the child's lock): the
        same backoff, capped at its last step, and never counted, so a
        busy child is never `failing`, however long the holder stays."""
        if ran_for >= self.healthy_after:
            self.step = 0
        return self._next_delay()

    def _next_delay(self):
        delay = self.backoff[min(self.step, len(self.backoff) - 1)]
        self.step += 1
        return delay


class Child:
    """One row of the table: the spec, the live process (or None), and
    what status() reports."""

    def __init__(self, spec, policy):
        self.spec = spec
        self.policy = policy
        self.proc = None
        self.state = "stopped"
        self.reason = None
        self.last_exit = None
        self.restarts = 0
        self.since = _now_iso()
        self.started_at = None       # clock() at the last start
        self.next_start = None       # clock() of a pending backoff restart
        self.stopping = False        # we sent SIGTERM; its exit is not a crash
        self.kill_at = None          # clock() at which a stopping child is SIGKILLed
        self.give_up_at = None       # clock() after which a SIGKILLed child is written off
        self.reader = None
        self.waiters = []            # `stop` requests answered when the child is down
        self.remove_when_down = False  # reload: its cousin left the runner lane
        self.restart_when_down = False  # reload: a bridge whose config changed
        self.config_digest = None      # a bridge: bridge_config's digest it runs with

    @property
    def name(self):
        return self.spec.name

    @property
    def pid(self):
        return self.proc.pid if self.alive else None

    @property
    def alive(self):
        return self.proc is not None and self.proc.returncode is None

    def set_state(self, state, reason=None):
        self.state = state
        self.reason = reason
        self.since = _now_iso()

    def row(self):
        return {"state": self.state, "pid": self.pid, "restarts": self.restarts,
                "since": self.since, "reason": self.reason, "last_exit": self.last_exit}


class Supervisor:
    """The child table and the loop that keeps it. start_all() launches
    every child in start order; step() reaps, restarts what is due and
    enforces stop deadlines; stop_all() is the ordered stop; run() is
    start_all, step until the stop event, stop_all; serve() is run()
    behind the signal handlers (the CLI's body)."""

    def __init__(self, root, specs, *, out=None, clock=time.monotonic, backoff=BACKOFF,
                 window=WINDOW_S, max_exits=MAX_EXITS, healthy_after=HEALTHY_AFTER_S,
                 tick=TICK_S, debug=False):
        self.root = Path(os.path.abspath(root))
        self.out = out if out is not None else sys.stdout
        self.clock = clock
        self.tick = float(tick)
        self.debug = debug
        self._policy_args = dict(backoff=backoff, window=window, max_exits=max_exits,
                                 healthy_after=healthy_after)
        self._out_lock = threading.Lock()
        self._wake = threading.Event()
        self._requests = queue.Queue()     # (request, _Answer) from the socket's threads
        self._reload_requested = False     # set by SIGHUP
        self.publishing = False            # serve() writes run/supervisor.json
        self._serving = False              # the socket's accept loop runs
        self._published = None
        self.started = _now_iso()
        self.children = collections.OrderedDict()
        for spec in specs:
            self._add(spec)

    # ------------------------------------------------------------ output (R8)

    def say(self, text):
        self._write("supervisor: %s\n" % text)

    def _write(self, text):
        with self._out_lock:
            try:
                self.out.write(text)
                self.out.flush()
            except (OSError, ValueError):
                pass                   # our stdout is gone: keep supervising

    def _pump(self, name, stream, log_file=None):
        """One child's merged output, line by line, prefixed. A partial
        last line is written at EOF with its newline. With `log_file`,
        each line is also appended there, unprefixed."""
        log = None
        if log_file is not None:
            try:
                log = open(log_file, "a", encoding="utf-8")
            except OSError:
                log = None
        try:
            for raw in iter(stream.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip("\n")
                self._write("%s | %s\n" % (name, line))
                if log is not None:
                    try:
                        log.write(line + "\n")
                        log.flush()
                    except (OSError, ValueError):
                        log = None
        except (OSError, ValueError):
            pass
        finally:
            try:
                stream.close()
            except OSError:
                pass
            if log is not None:
                try:
                    log.close()
                except OSError:
                    pass

    # ------------------------------------------------------------ the table

    def _add(self, spec):
        if spec.name in self.children:
            raise ValueError("two children named %r" % spec.name)
        child = Child(spec, RestartPolicy(**self._policy_args))
        self.children[spec.name] = child
        return child

    def _ordered(self):
        """The start order: console, loops, runners, bridges (each in
        table order; runner specs are built in slug order)."""
        return sorted(self.children.values(), key=lambda c: _KIND_ORDER[c.spec.kind])

    def _env(self, spec):
        env = dict(os.environ)
        env.update(FRAMEWORK_ROOT=str(self.root), PYTHONUNBUFFERED="1", COUSIN_SUPERVISED="1")
        env.update(spec.env)
        return env

    def _start(self, child):
        spec = child.spec
        if spec.kind == "telegram" and spec.home is not None:
            outside = _outside_bridge(spec.home)
            if outside is not None:
                # never a second bridge on one bot (Telegram answers 409):
                # wait, uncounted, until that one is gone
                reason = "a bridge outside the supervisor runs (pid %d)" % outside
                now = self.clock()
                child.next_start = now + child.policy.on_busy(0.0)
                if child.reason != reason:
                    self.say("%s not started: %s; left alone, started once it is gone"
                             % (child.name, reason))
                child.set_state("backoff", reason)
                return
        try:
            proc = subprocess.Popen(spec.argv, env=self._env(spec), stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    start_new_session=True)
        except OSError as err:
            child.proc = None
            child.set_state("failing", "cannot start: %s" % err)
            self.say("%s failing: cannot start (%s), left down" % (child.name, err))
            return
        child.proc = proc
        child.started_at = self.clock()
        child.next_start = None
        child.stopping = False
        child.kill_at = None
        child.give_up_at = None
        child.set_state("running")
        if spec.pid_file is not None:
            try:
                spec.pid_file.parent.mkdir(parents=True, exist_ok=True)
                spec.pid_file.write_text("%d\n" % proc.pid)
            except OSError as err:
                self.say("cannot write %s: %s" % (spec.pid_file, err))
        child.reader = threading.Thread(target=self._pump,
                                        args=(child.name, proc.stdout, spec.log_file),
                                        name="supervisor-out-%s" % child.name, daemon=True)
        child.reader.start()
        self.say("started %s (pid %d)" % (child.name, proc.pid))

    def _clear_pid_file(self, child):
        """Remove the child's pid file when it still names this child."""
        path = child.spec.pid_file
        if path is None or child.proc is None:
            return
        try:
            if path.read_text().strip() == str(child.proc.pid):
                path.unlink()
        except (OSError, ValueError):
            pass

    def start_all(self):
        """Every child in start order, then each runner cousin's bridge."""
        for child in self._ordered():
            if child.spec.kind != "telegram" and not child.alive:
                self._start(child)
        self._sync_bridges()

    # ------------------------------------------------------------ reaping (R1)

    def reap(self):
        """Every exit status waiting for us, children and orphans alike."""
        by_pid = {c.proc.pid: c for c in self.children.values() if c.alive}
        while True:
            try:
                pid, status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                return                  # no children at all
            except InterruptedError:
                continue
            if pid == 0:
                return                  # children, none exited
            child = by_pid.pop(pid, None)
            if child is None:
                if self.debug:
                    self.say("reaped orphan pid %d (%s)"
                             % (pid, describe_exit(os.waitstatus_to_exitcode(status))))
                continue
            self._exited(child, os.waitstatus_to_exitcode(status))
            if child.alive:
                # restarted inside this loop (the console's 75): its new pid
                # is a child of ours, not an orphan, if it dies before we drain
                by_pid[child.proc.pid] = child

    def _exited(self, child, code):
        child.proc.returncode = code         # by hand: Popen never waits (R1)
        child.last_exit = how = describe_exit(code)
        self._clear_pid_file(child)
        now = self.clock()
        if child.stopping:
            child.stopping = False
            child.kill_at = None
            if child.restart_when_down:
                child.restart_when_down = False
                self.say("%s stopped (%s), starting it again" % (child.name, how))
                child.policy.reset()
                self._start(child)
                return
            child.set_state("stopped", child.reason or "stopped")
            self.say("%s stopped (%s)" % (child.name, how))
            self._settle(child)
            return
        action, reason = classify_exit(child.spec.kind, code)
        if action == "failing":
            child.set_state("failing", reason)
            self.say("%s failing: %s, left down; fix it, then `cousin-supervisor start %s`"
                     % (child.name, reason, child.spec.slug or "--name %s" % child.name))
        elif action == "stopped":
            child.set_state("stopped", reason)
            self.say("%s stopped: %s, not restarted; log in, then `cousin-supervisor start %s`"
                     % (child.name, reason, child.spec.slug or "--name %s" % child.name))
        elif action == "now":
            self.say("%s exited (%s): %s, restarting now" % (child.name, how, reason))
            child.restarts += 1
            self._start(child)
        elif action == "busy":
            delay = child.policy.on_busy(now - (child.started_at or now))
            child.next_start = now + delay
            child.set_state("backoff", reason)
            self.say("%s busy: %s, retrying in %gs; not counted toward failing"
                     % (child.name, reason, delay))
        else:
            delay = child.policy.on_exit(now, now - (child.started_at or now))
            if delay is None:
                why = "%d exits in %gs" % (child.policy.max_exits, child.policy.window)
                child.set_state("failing", why)
                self.say("%s failing: %s, left down (last exit %s)" % (child.name, why, how))
            else:
                child.next_start = now + delay
                child.set_state("backoff", reason or "exited (%s)" % how)
                self.say("%s exited (%s), restarting in %gs"
                         % (child.name, reason or how, delay))

    def _settle(self, child):
        """A stopped child: answer the `stop` requests waiting on it, and
        drop it from the table when a reload removed its cousin."""
        for answer in child.waiters:
            answer.give({"ok": True, "name": child.name, "state": child.state})
        child.waiters = []
        if child.remove_when_down:
            self.children.pop(child.name, None)
            self.say("removed %s" % child.name)

    # ------------------------------------------------------------ the loop

    def step(self):
        """One turn of the main loop: reap, rescan on SIGHUP, answer the
        queued requests, SIGKILL what overstayed its stop, start what is
        due, publish the snapshot."""
        self.reap()
        if self._reload_requested:
            self._reload_requested = False
            self.reload()
        self._answer_requests()
        now = self.clock()
        for child in self.children.values():
            if child.alive and child.kill_at is not None and now >= child.kill_at:
                self._kill(child)
            if child.state == "backoff" and child.next_start is not None \
                    and now >= child.next_start:
                child.restarts += 1
                self._start(child)
        self._publish()

    def _signal_stop(self, child, reason):
        """SIGTERM to the child alone (not its group: a runner finishes
        its turn and stops its own SDK subprocess); SIGKILL at kill_at."""
        child.reason = reason
        child.next_start = None
        if not child.alive:
            child.set_state("stopped", reason)
            return
        if child.stopping:
            return
        child.stopping = True
        child.kill_at = self.clock() + child.spec.stop_timeout
        try:
            os.kill(child.proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass                           # exited already; reap() will see it

    def _kill(self, child):
        """SIGKILL the child's whole process group (it leads its own
        session): what it started dies with it rather than outliving it."""
        self.say("%s did not stop within %gs, killed" % (child.name, child.spec.stop_timeout))
        child.kill_at = None
        child.give_up_at = self.clock() + KILL_GRACE_S
        try:
            os.killpg(child.proc.pid, signal.SIGKILL)
        except OSError:
            try:
                os.kill(child.proc.pid, signal.SIGKILL)
            except OSError:
                pass

    def stop_all(self, reason="supervisor stopping"):
        """The ordered stop (R5): bridges together, then runners
        together, then loops, then the console. Each group is signalled,
        then waited for up to each child's stop_timeout (SIGKILL after),
        reaping as we go."""
        groups = collections.OrderedDict()
        for child in reversed(self._ordered()):
            groups.setdefault(child.spec.kind, []).append(child)
        live = [c for c in self.children.values() if c.alive]
        if live:
            self.say("stopping %d children" % len(live))
        for kind, group in groups.items():
            for child in group:
                child.restart_when_down = False
                self._signal_stop(child, reason)
            self._wait_down(group)
        for child in self.children.values():
            if child.reader is not None:
                child.reader.join(1.0)     # the last lines before our own
        self.say("stopped")

    def _wait_down(self, group):
        while True:
            self.reap()
            live = [c for c in group if c.alive]
            if not live:
                return
            now = self.clock()
            for child in live:
                if child.kill_at is not None and now >= child.kill_at:
                    self._kill(child)
                elif child.give_up_at is not None and now >= child.give_up_at:
                    # unkillable (stuck in the kernel): do not hang the stop on it
                    self.say("%s (pid %d) survived SIGKILL; leaving it"
                             % (child.name, child.proc.pid))
                    child.proc.returncode = -signal.SIGKILL
                    self._clear_pid_file(child)
                    child.set_state("stopped", child.reason)
                    self._settle(child)
            self._answer_requests(stopping=True)
            self._publish()
            time.sleep(min(self.tick, 0.05))

    def run(self, stop_event):
        """start_all, then step every tick until stop_event is set, then
        the ordered stop."""
        self.start_all()
        try:
            while not stop_event.is_set():
                self.step()
                self._wake.wait(self.tick)
                self._wake.clear()
        finally:
            self.stop_all()

    def serve(self):
        """The CLI's body: take the root's lock (SupervisorError if another
        supervisor holds it), open the control socket, then run() behind
        signal handlers that only set flags (the loop does the work):
        SIGTERM and SIGINT stop, SIGHUP rescans. Returns 0."""
        run_dir = self.root / "run"
        run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(run_dir, 0o700)        # as accounts.py makes it: the socket's only guard
        lock_fd = self._take_lock()
        stop = threading.Event()

        def _stop(signum, frame):
            stop.set()
            self._wake.set()

        def _hup(signum, frame):
            self._reload_requested = True
            self._wake.set()

        server = None
        previous = {}
        try:
            server = self._open_socket()
            previous = {signal.SIGTERM: signal.signal(signal.SIGTERM, _stop),
                        signal.SIGINT: signal.signal(signal.SIGINT, _stop),
                        signal.SIGHUP: signal.signal(signal.SIGHUP, _hup)}
            self.say("supervising %s (pid %d)" % (self.root, os.getpid()))
            self.publishing = True
            self.run(stop)
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
            if server is not None:
                self._close_socket(server)
            self._answer_requests(stopping=True)
            if self.publishing:
                # gone, not "stopped": a reader must not mistake an old file for a live one
                try:
                    (self.root / SNAPSHOT).unlink()
                except FileNotFoundError:
                    pass
                self.publishing = False
            os.close(lock_fd)
        return 0

    def _take_lock(self):
        """The root's exclusive lock. Retried for LOCK_TAKE_S: a reader's
        snapshot() probe holds it shared for microseconds, and must never
        be mistaken for a second supervisor. A snapshot file left by a
        supervisor that died is removed once the lock is ours."""
        path = self.root / LOCK
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + LOCK_TAKE_S
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    os.close(fd)
                    raise SupervisorError("another cousin-supervisor holds %s" % path)
                time.sleep(0.02)
            except OSError as err:
                os.close(fd)
                raise SupervisorError("cannot lock %s: %s" % (path, err))
        try:
            (self.root / SNAPSHOT).unlink()
        except FileNotFoundError:
            pass
        return fd

    # ------------------------------------------------------------ control (R6)

    def _open_socket(self):
        path = self.root / SOCKET
        try:
            path.unlink()               # a socket file left by a supervisor that died
        except FileNotFoundError:
            pass
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(str(path))
            server.listen(16)
        except OSError as err:
            server.close()
            raise SupervisorError("cannot listen on %s: %s" % (path, err))
        os.chmod(path, 0o600)
        server.settimeout(0.5)          # so the accept loop sees the close
        self._serving = True
        threading.Thread(target=self._accept_loop, args=(server,), name="supervisor-socket",
                         daemon=True).start()
        return server

    def _close_socket(self, server):
        self._serving = False
        server.close()
        try:
            (self.root / SOCKET).unlink()
        except FileNotFoundError:
            pass

    def _accept_loop(self, server):
        while self._serving:
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._converse, args=(conn,), daemon=True).start()

    def _converse(self, conn):
        """One connection: one request line in, one answer line out. The
        request goes to the main loop's queue; this thread only waits."""
        with conn:
            conn.settimeout(5.0)
            try:
                line = _read_line(conn)
                req = json.loads(line)
                if not isinstance(req, dict) or not isinstance(req.get("op"), str):
                    raise ValueError("a request is a JSON object with an \"op\"")
            except (OSError, ValueError) as err:
                answer = {"ok": False, "error": "bad request: %s" % err}
            else:
                pending = _Answer()
                self._requests.put((req, pending))
                self._wake.set()
                answer = pending.wait(ANSWER_WAIT_S) or {
                    "ok": False, "error": "the supervisor did not answer within %gs"
                    % ANSWER_WAIT_S}
            try:
                conn.sendall((json.dumps(answer) + "\n").encode())
            except OSError:
                pass

    def _answer_requests(self, stopping=False):
        """Every queued request, answered by the main loop. While the
        supervisor stops, only `status` is served."""
        while True:
            try:
                req, pending = self._requests.get_nowait()
            except queue.Empty:
                return
            if stopping and req.get("op") != "status":
                pending.give({"ok": False, "error": "the supervisor is stopping"})
                continue
            try:
                answer = self._handle(req, pending)
            except Exception as err:     # a bad request must not take the loop down
                answer = {"ok": False, "error": "%s: %s" % (type(err).__name__, err)}
            if answer is not None:
                pending.give(answer)

    def _handle(self, req, pending):
        """The answer to one request, or None when it comes later (a
        `stop` with `wait` is answered when the child is down)."""
        op = req["op"]
        if op == "status":
            return self.status()
        if op == "reload":
            added, removed = self.reload()
            return {"ok": True, "added": added, "removed": removed}
        if op in ("start", "stop"):
            name, error = _target(op, req)
            if error:
                return {"ok": False, "error": error}
            if op == "start":
                return self.start_child(name)
            wait = req.get("wait", True)
            if not isinstance(wait, bool):
                return {"ok": False, "error": "wait must be true or false"}
            by = req.get("by", "a stop request")
            if not isinstance(by, str) or not by.strip() or "\n" in by:
                return {"ok": False, "error": "by must be a one-line string"}
            return self.stop_child(name, pending if wait else None, wait=wait, by=by)
        return {"ok": False, "error": "unknown op %r (status, start, stop, reload)" % op}

    def _runner_home(self, name):
        """The home of `runner:<slug>` when it is a runner cousin, else None."""
        if not name.startswith("runner:"):
            return None
        home = self.root / "cousins" / name[len("runner:"):]
        return home if is_runner_cousin(home) else None

    def start_child(self, name):
        """`start` by child name: a runner cousin with no child gets one; a
        stopped or failing child is cleared and started; a running one is
        left be. A runner cousin's hold marker is removed first."""
        child = self.children.get(name)
        home = self._runner_home(name)
        if child is None:
            if home is None:
                return {"ok": False, "error": "%s is not a runner cousin under %s/cousins"
                        " ([agent] runner = \"sdk\" or \"fake\"); a tmux cousin starts with"
                        " cousin-spawn --start" % (name.split(":", 1)[-1], self.root)}
            child = self._add(runner_spec(home))
            self.say("added %s" % child.name)
        if child.stopping:
            return {"ok": False, "name": child.name,
                    "error": "%s is still stopping; start it once it is down" % child.name}
        if home is not None:
            release(home)
        if not child.alive:
            child.policy.reset()
            self._start(child)
        if home is not None:
            self._sync_bridges(child.spec.slug, retry_failing=True)
        answer = {"ok": child.state == "running", "name": child.name, "state": child.state}
        if not answer["ok"]:
            answer["error"] = child.reason
        return answer

    REAP_TIMEOUT_S = 15.0

    def _reap_pane(self, home):
        """A tmux-kind cousin stopped while its runner is down or failing:
        its pane may still be running a turn, and nothing else would kill
        it (phase 11 R21). The kill is the runner module's (`cousin-runner
        --reap-pane`), so no tmux call lives here; it holds the runner lock
        while it kills. Bounded; a failure is said, never raised."""
        if (_agent_table(home) or {}).get("runner") != "tmux":
            return
        try:
            r = subprocess.run([sys.executable, "-m", "cousin_lib.runner.main", "--home", str(home),
                                "--reap-pane"], capture_output=True, text=True,
                               timeout=self.REAP_TIMEOUT_S, check=False)
            self.say("reap %s: rc=%d %s" % (Path(home).name, r.returncode, (r.stdout or "").strip()[:120]))
        except (OSError, subprocess.SubprocessError) as err:
            self.say("reap %s failed: %s" % (Path(home).name, err))

    def stop_child(self, name, pending=None, *, wait=True, by="a stop request"):
        """`stop` by child name: SIGTERM, then the child is held `stopped`
        (never restarted) until `start`; a runner cousin's hold is also
        written to <home>/run/held, so it outlives this supervisor. With
        `pending` the answer comes once the child is down; with wait
        false it is at once, `state: "stopping"`. A runner cousin with no
        child (held, or auto_start = false) is held and answered stopped."""
        child = self.children.get(name)
        home = self._runner_home(name)
        if child is None and home is None:
            return {"ok": False, "error": "no child named %s" % name}
        if home is not None:
            try:
                hold(home, by)
            except OSError as err:
                self.say("cannot write %s: %s" % (Path(home) / HELD, err))
            self._sync_bridges(name[len("runner:"):])      # held: its bridge goes down with it
        if home is not None and (child is None or not child.alive):
            self._reap_pane(home)
        if child is None:
            return {"ok": True, "name": name, "state": "stopped"}
        if not child.alive:
            child.next_start = None
            child.set_state("stopped", "stopped by request")
            return {"ok": True, "name": child.name, "state": child.state}
        self._signal_stop(child, "stopped by request")
        if not wait:
            return {"ok": True, "name": child.name, "state": "stopping"}
        if pending is not None:
            child.waiters.append(pending)
        return None

    # ------------------------------------------------------------ rescan (R9)

    def reload(self):
        """Rescan the registry. Returns (added, removed) child names."""
        wanted = runner_cousins(self.root)
        lane = {Path(c.home).name for c in _lane_homes(self.root)}
        added, removed = [], []
        for config in wanted:
            spec = runner_spec(config.home)
            if spec.name not in self.children:
                self._start(self._add(spec))
                added.append(spec.name)
        for child in list(self.children.values()):
            if child.spec.kind != "runner" or child.spec.slug in lane:
                continue
            removed.append(child.name)
            if child.alive:
                child.remove_when_down = True
                self._signal_stop(child, "removed: no longer a runner cousin")
            else:
                self.children.pop(child.name, None)
        bridges_added, bridges_removed = self._sync_bridges(retry_failing=True)
        added += bridges_added
        removed += bridges_removed
        for child in self.children.values():
            if child.state == "failing" and child.name not in removed \
                    and child.spec.kind != "telegram":
                child.policy.reset()
                self._start(child)
        self.say("reload: added %s; removed %s"
                 % (", ".join(added) or "-", ", ".join(removed) or "-"))
        return added, removed

    # ------------------------------------------------------------ bridges (#101)

    def _sync_bridges(self, slug=None, *, retry_failing=False):
        """Make each runner cousin's `telegram:<slug>` child match its
        cousin.toml (all runner children, or the one `slug`): present
        while `[telegram]` passes bridge_config and its runner child is in
        the table, running unless the runner is held (stopped by
        request), restarted when its config changed. A bridge whose
        runner is gone or leaving goes with it. retry_failing also
        starts a `failing` bridge again (reload, an explicit start).
        Returns (added, removed) child names."""
        added, removed = [], []
        runners = {c.spec.slug: c for c in self.children.values()
                   if c.spec.kind == "runner" and c.spec.slug and c.spec.home is not None}
        for bridge in [c for c in self.children.values() if c.spec.kind == "telegram"]:
            if slug is not None and bridge.spec.slug != slug:
                continue
            runner = runners.get(bridge.spec.slug)
            if runner is None or runner.remove_when_down:
                self._remove_bridge(bridge, "its runner left")
                removed.append(bridge.name)
        for runner in runners.values():
            if slug is not None and runner.spec.slug != slug:
                continue
            if runner.remove_when_down:
                continue
            change = self._sync_bridge(runner, retry_failing)
            if change == "added":
                added.append("telegram:%s" % runner.spec.slug)
            elif change == "removed":
                removed.append("telegram:%s" % runner.spec.slug)
        return added, removed

    def _sync_bridge(self, runner, retry_failing):
        home = runner.spec.home
        name = "telegram:%s" % runner.spec.slug
        bridge = self.children.get(name)
        digest, why = bridge_config(home, self.root)
        if digest is None:
            if why is not None:
                self._stop_outside_bridge(home, bridge, why)
            if bridge is not None and not bridge.remove_when_down:
                self._remove_bridge(bridge, why or "no [telegram] table")
                return "removed"
            if bridge is None and why is not None:
                self.say("%s not started: %s" % (name, why))
            return None
        change = None
        changed = False
        if bridge is None:
            bridge = self._add(telegram_spec(home))
            self.say("added %s" % name)
            change = "added"
        elif bridge.remove_when_down:
            # removed by an earlier rescan and still on its way down: the
            # config is valid again, so it stays and comes back up once down
            bridge.remove_when_down = False
            bridge.restart_when_down = bridge.stopping
            self.say("%s: enabled again before it was down, keeping it" % name)
            change = "added"
        elif bridge.config_digest != digest:
            changed = True
        bridge.config_digest = digest
        if is_held(home):
            bridge.restart_when_down = False
            bridge.next_start = None
            if bridge.alive:
                self._signal_stop(bridge, "stopped with %s" % runner.name)
            elif bridge.state != "stopped":
                bridge.set_state("stopped", "stopped with %s" % runner.name)
            return change
        if bridge.alive:
            if changed and not bridge.stopping:
                self._signal_stop(bridge, "restarting: its configuration changed")
                bridge.restart_when_down = True
                self.say("%s: its configuration changed, restarting it" % name)
            return change
        if bridge.state == "backoff":
            return change                   # its restart is due by itself
        if bridge.state == "failing" and not retry_failing:
            return change
        bridge.policy.reset()
        self._start(bridge)
        return change

    def _stop_outside_bridge(self, home, bridge, why):
        """A cousin whose [telegram] no longer runs, with a bridge still
        polling outside the supervisor (one started by hand or by an
        older console): SIGTERM it, as the console's switch used to, so
        `off` means off. Never our own child, and never waited for."""
        ours = bridge.proc.pid if bridge is not None and bridge.alive else None
        pid = _outside_bridge(home, ours)
        if pid is None:
            return
        self.say("stopping the bridge outside the supervisor (pid %d): %s" % (pid, why))
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass

    def _remove_bridge(self, bridge, why):
        """Stop a bridge and drop it from the table once it is down."""
        bridge.restart_when_down = False
        self.say("removing %s: %s" % (bridge.name, why))
        if bridge.alive:
            bridge.remove_when_down = True
            self._signal_stop(bridge, "removed: %s" % why)
        else:
            self.children.pop(bridge.name, None)

    # ------------------------------------------------------------ snapshot (R7)

    def _publish(self):
        """run/supervisor.json, rewritten (tmp + rename) when status changed."""
        if not self.publishing:
            return
        body = self.status()
        if body == self._published:
            return
        path = self.root / SNAPSHOT
        tmp = path.with_name(path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(body, indent=1) + "\n")
            os.replace(tmp, path)
        except OSError as err:
            self.say("cannot write %s: %s" % (path, err))
            return
        self._published = body

    def status(self):
        return {"ok": True, "pid": os.getpid(), "started": self.started,
                "children": {name: child.row() for name, child in self.children.items()}}


class _Answer:
    """The main loop's answer to one queued request, handed to the
    connection thread waiting on it."""

    def __init__(self):
        self._event = threading.Event()
        self._value = None

    def give(self, value):
        if not self._event.is_set():
            self._value = value
            self._event.set()

    def wait(self, timeout):
        self._event.wait(timeout)
        return self._value


def _read_line(sock):
    data = b""
    while b"\n" not in data:
        chunk = sock.recv(65536)
        if not chunk:
            break
        data += chunk
        if len(data) > REQUEST_LINE_MAX:
            raise ValueError("line longer than %d bytes" % REQUEST_LINE_MAX)
    return data.split(b"\n", 1)[0].decode("utf-8")


def _target(op, req):
    """(child name, None) or (None, error) for a start or stop request:
    `slug` is only ever a runner cousin (`runner:<slug>`), `name` only
    `console` or `loops` (a cousin slugged `loops` never reaches the
    loops daemon). Exactly one of the two."""
    slug, name = req.get("slug"), req.get("name")
    if (slug is None) == (name is None):
        return None, ("%s takes a slug (a runner cousin) or a name (%s), exactly one"
                      % (op, ", ".join(NAMED)))
    if slug is not None:
        if not isinstance(slug, str) or not _SLUG.match(slug):
            return None, "%s: bad slug %r" % (op, slug)
        return "runner:%s" % slug, None
    if name not in NAMED:
        return None, ("%s: name is one of %s; a runner cousin is addressed by slug"
                      % (op, ", ".join(NAMED)))
    return name, None


def held_path(home):
    return Path(home) / HELD


def is_held(home):
    """A runtime `stop` holds this runner cousin down (R4')."""
    return held_path(home).exists()


def hold(home, by):
    """Write <home>/run/held: `<ISO time> <who>`, atomically."""
    path = held_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("%s %s\n" % (_now_iso(), by))
    os.replace(tmp, path)


def release(home):
    try:
        held_path(home).unlink()
    except FileNotFoundError:
        pass


# ------------------------------------------------------------ the registry (R4)

def _agent_table(home):
    """cousin.toml's [agent] table, or None when the file is missing or
    does not parse (then it is nobody's runner cousin: cousin-runner
    would refuse it with exit 2 anyway)."""
    try:
        data = tomllib.loads((Path(home) / "cousin.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return None
    agent = data.get("agent")
    return agent if isinstance(agent, dict) else {}


def is_runner_cousin(home):
    """`[agent] runner` is one of RUNNER_KINDS (delivery._runner_kind's test)."""
    agent = _agent_table(home)
    return agent is not None and agent.get("runner") in RUNNER_KINDS


def _lane_homes(root):
    """Every runner cousin's CousinConfig, auto_start or not, slug order."""
    from cousin_lib.config import CousinConfig, MissingConfigError
    base = Path(root) / "cousins"
    if not base.is_dir():
        return []
    rows = []
    for entry in sorted(base.iterdir()):
        if not is_runner_cousin(entry):
            continue
        try:
            rows.append(CousinConfig.load(entry))
        except (MissingConfigError, OSError, tomllib.TOMLDecodeError):
            continue
    return sorted(rows, key=lambda c: c.slug)


def runner_cousins(root):
    """The cousins the supervisor starts on its own: the runner lane
    (`[agent] runner` one of RUNNER_KINDS) whose `[agent] auto_start` is not
    false and that no runtime `stop` holds (<home>/run/held), in slug
    order. Only a literal `false` opts out."""
    return [c for c in _lane_homes(root)
            if (_agent_table(c.home) or {}).get("auto_start", AUTO_START_DEFAULT) is not False
            and not is_held(c.home)]


# ------------------------------------------------------------ clients

def request(root, op, *, timeout=10.0, **args):
    """One request over `<root>/run/supervisor.sock`; the answer as a dict.
    SupervisorUnavailable when nothing answers: SupervisorAbsent when there
    is nothing to connect to (no socket, a stale one), the plain one when a
    supervisor took the connection but gave no answer within `timeout`. A `stop` answers when the child is
    down: give it the child's stop timeout and some (the CLI uses 60 s)."""
    path = Path(root) / SOCKET
    payload = dict(args, op=op)
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(timeout)
    try:
        try:
            conn.connect(str(path))
        except OSError as err:
            raise SupervisorAbsent("no cousin-supervisor answers on %s (%s)"
                                   % (path, err.strerror or err))
        try:
            conn.sendall((json.dumps(payload) + "\n").encode())
            line = _read_line(conn)
        except socket.timeout:
            raise SupervisorUnavailable("the cousin-supervisor on %s did not answer within %gs"
                                        % (path, timeout))
        except (OSError, ValueError) as err:
            raise SupervisorUnavailable("the cousin-supervisor on %s: %s" % (path, err))
    finally:
        conn.close()
    try:
        answer = json.loads(line)
    except ValueError:
        raise SupervisorUnavailable("the cousin-supervisor on %s gave no answer" % path)
    if not isinstance(answer, dict):
        raise SupervisorUnavailable("the cousin-supervisor on %s gave no answer" % path)
    return answer


def _lock_held(root):
    """A supervisor holds `run/supervisor.lock`: a shared, non-blocking
    probe on a fresh descriptor fails exactly while one holds it
    exclusively. Exact where a pid is not (a reused pid, a PID 1 of an
    earlier container), and it takes no signal permission."""
    try:
        fd = os.open(Path(root) / LOCK, os.O_RDONLY)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError:
        return False
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def snapshot(root):
    """`run/supervisor.json` while a supervisor holds the root's lock,
    else None: a missing or unreadable file, or one no live supervisor
    stands behind, is "unknown", never "stopped"."""
    try:
        data = json.loads((Path(root) / SNAPSHOT).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not _lock_held(root):
        return None
    return data


# ------------------------------------------------------------ the CLI

def _parser():
    parser = argparse.ArgumentParser(
        prog="cousin-supervisor",
        description="run the console, the loops daemon and every runner cousin; or ask the"
                    " running supervisor for its status, to start or stop a child, to rescan")
    root = argparse.ArgumentParser(add_help=False)
    root.add_argument("--root", help="the framework root (else FRAMEWORK_ROOT, else the"
                                     " checkout you are in)")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", parents=[root], help="supervise until SIGTERM")
    run.add_argument("--no-console", action="store_true")
    run.add_argument("--no-loops", action="store_true")
    run.add_argument("--console-host", default="127.0.0.1")
    run.add_argument("--console-port", type=int, default=8600)
    run.add_argument("--loops-interval", type=float, default=30.0)
    status = sub.add_parser("status", parents=[root], help="every child's state")
    status.add_argument("--json", action="store_true")
    for name in ("start", "stop"):
        p = sub.add_parser(name, parents=[root],
                           help="%s a runner cousin by slug, or --name console|loops" % name)
        p.add_argument("slug", nargs="?", help="a runner cousin's slug")
        p.add_argument("--name", choices=NAMED, help="the console or the loops daemon")
        if name == "stop":
            p.add_argument("--no-wait", action="store_true",
                           help="answer once signalled, not once it is down")
    sub.add_parser("reload", parents=[root], help="rescan the cousin registry (as SIGHUP)")
    return parser


def _print_status(body):
    print("cousin-supervisor pid %s, up since %s" % (body.get("pid"), body.get("started")))
    for name, row in body.get("children", {}).items():
        print("  %-20s %-8s %-8s restarts %-3d %s"
              % (name, row["state"], row["pid"] or "-", row["restarts"],
                 row["reason"] or ""))


def supervisor_main(argv=None):
    """cousin-supervisor run|status|start|stop|reload. Exit codes: run 0
    after SIGTERM/SIGINT, 2 when another supervisor holds the root (or
    its socket cannot be bound); the others 0 ok, 1 no supervisor
    running, 2 usage or the supervisor refused."""
    from cousin_lib.config import FrameworkConfig, MissingConfigError
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command in ("start", "stop") and (args.slug is None) == (args.name is None):
        parser.error("%s takes a runner cousin's slug or --name console|loops, exactly one"
                     % args.command)
    try:
        root = FrameworkConfig.resolve(args.root, cwd_fallback=True).root
    except MissingConfigError as err:
        print("cousin-supervisor: %s" % err, file=sys.stderr)
        return 2
    if args.command == "run":
        os.environ["FRAMEWORK_ROOT"] = str(root)     # the flag and the children agree
        specs = []
        if not args.no_console:
            specs.append(console_spec(root, args.console_host, args.console_port))
        if not args.no_loops:
            specs.append(loops_spec(root, args.loops_interval))
        specs += [runner_spec(c.home) for c in runner_cousins(root)]
        try:
            return Supervisor(root, specs).serve()
        except SupervisorError as err:
            print("cousin-supervisor: %s" % err, file=sys.stderr)
            return 2
    op_args = {}
    if args.command in ("start", "stop"):
        op_args = {"slug": args.slug} if args.slug is not None else {"name": args.name}
    if args.command == "stop":
        op_args.update(wait=not args.no_wait, by="cousin-supervisor stop")
    timeout = 60.0 if args.command == "stop" else 10.0
    try:
        answer = request(root, args.command, timeout=timeout, **op_args)
    except SupervisorUnavailable as err:
        print("cousin-supervisor: %s" % err, file=sys.stderr)
        return 1
    if not answer.get("ok"):
        print("cousin-supervisor: %s" % answer.get("error", "refused"), file=sys.stderr)
        return 2
    if args.command == "status":
        if args.json:
            print(json.dumps(answer, indent=1))
        else:
            _print_status(answer)
    elif args.command == "reload":
        print("added: %s; removed: %s" % (", ".join(answer["added"]) or "-",
                                          ", ".join(answer["removed"]) or "-"))
    else:
        print("%s %s" % (answer["name"], answer["state"]))
    return 0


if __name__ == "__main__":
    sys.exit(supervisor_main())
