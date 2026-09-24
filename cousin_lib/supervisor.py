"""cousin-supervisor: one process that keeps an install's daemons up.

It is the container's init and a bare host's single unit. It starts the
console, the loops daemon (the one clock) and one `cousin-runner` per
runner cousin, restarts what crashes, and stops everything in order on
SIGTERM.

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
  - a runner's 5 is busy, another runner holds the home's lock (a
    leftover of a crashed supervisor, or one started by hand): restarted
    after the backoff, so the child comes back once the holder is gone;
  - the console's 75 is its own restart route: restarted at once and
    not counted (console/routes_admin.py RESTART_EXIT_CODE);
  - anything else (a runner's 3 "gave up", 0, a signal) restarts after
    a backoff of 1, 2, 4 ... 60 s (RestartPolicy); a child that ran
    60 s is healthy and starts again from 1 s; five counted exits
    inside 60 s mark it `failing`, left down with one loud line.

Stop (SIGTERM or SIGINT): the reverse of the start order. Every runner
is signalled together and waited for together (up to
runner.main.STOP_TIMEOUT_S + 5 s each: the runner gives its turn
STOP_TIMEOUT_S), then the loops daemon, then the console
(10 s each); a child still alive at its timeout is SIGKILLed with its
process group. Then the supervisor exits 0.
"""
import collections
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from cousin_lib.delivery import RUNNER_KINDS  # noqa: F401 (the one list; runner_cousins reads it)
from cousin_lib.runner.main import LOCK_HELD_EXIT, STOP_TIMEOUT_S

STATES = ("running", "backoff", "failing", "stopped")
KINDS = ("console", "loops", "runner")
# Start order by kind; stop order is its reverse (R5).
_KIND_ORDER = {kind: i for i, kind in enumerate(KINDS)}

# Seconds between SIGTERM and SIGKILL. A runner gives its current turn
# STOP_TIMEOUT_S (runner/main.py _serve: runner.stop(timeout=STOP_TIMEOUT_S));
# 5 more for it to close its inbox row and exit. One constant: a longer
# runner stop moves this budget with it.
STOP_TIMEOUTS = {"console": 10.0, "loops": 10.0, "runner": STOP_TIMEOUT_S + 5.0}

BACKOFF = (1, 2, 4, 8, 16, 32, 60)
WINDOW_S = 60.0          # the sliding window five counted exits must fall in
MAX_EXITS = 5            # counted exits inside WINDOW_S that make a child `failing`
HEALTHY_AFTER_S = 60.0   # a child that ran this long starts its backoff again from 1 s
TICK_S = 0.2             # the main loop's period: reap, restart, answer
KILL_GRACE_S = 5.0       # after a SIGKILL, before the ordered stop writes a child off

CONFIG_EXIT = 2              # every child: a configuration problem
RUNNER_LOGIN_EXIT = 4        # cousin-runner: a person must log in
RUNNER_BUSY_EXIT = LOCK_HELD_EXIT  # cousin-runner: another runner holds the home's lock (5)
CONSOLE_RESTART_EXIT = 75    # cousin-console's restart route (routes_admin.RESTART_EXIT_CODE)


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ChildSpec:
    """What to run: a name (`console`, `loops`, `runner:<slug>`), a kind
    (KINDS), the argv, the SIGTERM-to-SIGKILL timeout, the cousin's slug
    for a runner, and extra environment on top of the supervisor's."""

    def __init__(self, name, kind, argv, stop_timeout=None, slug=None, env=None):
        if kind not in KINDS:
            raise ValueError("child kind must be one of %s, got %r" % (", ".join(KINDS), kind))
        self.name = name
        self.kind = kind
        self.argv = list(argv)
        self.stop_timeout = float(STOP_TIMEOUTS[kind] if stop_timeout is None else stop_timeout)
        self.slug = slug
        self.env = dict(env or {})

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
    slug = Path(home).name
    return ChildSpec("runner:%s" % slug, "runner",
                     [sys.executable, "-m", "cousin_lib.runner.main", "--home", str(home)],
                     slug=slug)


def classify_exit(kind, code):
    """(action, reason) for a child that exited without being asked to.
    `code` is os.waitstatus_to_exitcode's: the exit status, or -N for
    signal N. Actions: "restart" (after the backoff, counted), "now" (at
    once, not counted), "failing" (left down), "stopped" (left down,
    not an error of ours). The reason is None for a plain crash."""
    if code == CONFIG_EXIT:
        return "failing", "configuration (exit %d)" % code
    if kind == "runner" and code == RUNNER_LOGIN_EXIT:
        return "stopped", "login required (exit %d)" % code
    if kind == "runner" and code == RUNNER_BUSY_EXIT:
        return "restart", "another runner holds its lock (exit %d)" % code
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

    def _pump(self, name, stream):
        """One child's merged output, line by line, prefixed. A partial
        last line is written at EOF with its newline."""
        try:
            for raw in iter(stream.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip("\n")
                self._write("%s | %s\n" % (name, line))
        except (OSError, ValueError):
            pass
        finally:
            try:
                stream.close()
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
        """The start order: console, loops, runners (each in table order;
        runner specs are built in slug order)."""
        return sorted(self.children.values(), key=lambda c: _KIND_ORDER[c.spec.kind])

    def _env(self, spec):
        env = dict(os.environ)
        env.update(FRAMEWORK_ROOT=str(self.root), PYTHONUNBUFFERED="1", COUSIN_SUPERVISED="1")
        env.update(spec.env)
        return env

    def _start(self, child):
        spec = child.spec
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
        child.reader = threading.Thread(target=self._pump, args=(child.name, proc.stdout),
                                        name="supervisor-out-%s" % child.name, daemon=True)
        child.reader.start()
        self.say("started %s (pid %d)" % (child.name, proc.pid))

    def start_all(self):
        for child in self._ordered():
            if not child.alive:
                self._start(child)

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
        now = self.clock()
        if child.stopping:
            child.stopping = False
            child.kill_at = None
            child.set_state("stopped", child.reason or "stopped")
            self.say("%s stopped (%s)" % (child.name, how))
            return
        action, reason = classify_exit(child.spec.kind, code)
        if action == "failing":
            child.set_state("failing", reason)
            self.say("%s failing: %s, left down; fix it, then `cousin-supervisor start %s`"
                     % (child.name, reason, child.spec.slug or child.name))
        elif action == "stopped":
            child.set_state("stopped", reason)
            self.say("%s stopped: %s, not restarted; log in, then `cousin-supervisor start %s`"
                     % (child.name, reason, child.spec.slug or child.name))
        elif action == "now":
            self.say("%s exited (%s): %s, restarting now" % (child.name, how, reason))
            child.restarts += 1
            self._start(child)
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

    # ------------------------------------------------------------ the loop

    def step(self):
        """One turn of the main loop: reap, SIGKILL what overstayed its
        stop, start what is due."""
        self.reap()
        now = self.clock()
        for child in self.children.values():
            if child.alive and child.kill_at is not None and now >= child.kill_at:
                self._kill(child)
            if child.state == "backoff" and child.next_start is not None \
                    and now >= child.next_start:
                child.restarts += 1
                self._start(child)

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
        """The ordered stop (R5): runners together, then loops, then the
        console. Each group is signalled, then waited for up to each
        child's stop_timeout (SIGKILL after), reaping as we go."""
        groups = collections.OrderedDict()
        for child in reversed(self._ordered()):
            groups.setdefault(child.spec.kind, []).append(child)
        live = [c for c in self.children.values() if c.alive]
        if live:
            self.say("stopping %d children" % len(live))
        for kind, group in groups.items():
            for child in group:
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
                    child.set_state("stopped", child.reason)
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
        """The CLI's body: run() behind SIGTERM/SIGINT handlers that only
        set a flag (the loop does the work). Returns 0."""
        stop = threading.Event()

        def _stop(signum, frame):
            stop.set()
            self._wake.set()

        previous = {sig: signal.signal(sig, _stop) for sig in (signal.SIGTERM, signal.SIGINT)}
        try:
            self.say("supervising %s (pid %d)" % (self.root, os.getpid()))
            self.run(stop)
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
        return 0

    def status(self):
        return {"ok": True, "pid": os.getpid(), "started": self.started,
                "children": {name: child.row() for name, child in self.children.items()}}
