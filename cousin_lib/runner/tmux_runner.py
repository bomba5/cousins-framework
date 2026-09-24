"""TmuxRunner: an interactive Claude Code in a tmux pane behind the runner
protocol (phase 11; plan R2-R25, interfaces I2).

The pane is driven, never trusted: the runner types a row and learns what
happened only from the CLI's own transcript (runner/transcript.py). A row
is TAKEN when a turn start's first line begins with its nonce, and it
CLOSES once, at that turn's end: `turn_duration` delivers it, the
interrupt entry delivers it (interrupted), an API error fails it, a limit
error requeues it (R4, R6). A turn start while a turn is live ends that
turn as interrupted ("send now" writes no end of its own; measured).

Continuity is the session id (P11-2): start() adopts a live pane whose
hook record (run/tmux-session.json, runner/tmux_hook.py) names the recorded
session and the pane's CLI pid, else kills it and resumes the recorded
session in a new pane, else starts fresh (R24); nothing
depends on the pane outliving the runner. A stop always ends the live
turn; the pane is killed only when the stop is a hold (run/held, P11-10).

The structure is FakeRunner's (the reference runner): one worker thread,
the wake socket, the state machine, `_fail_turn` never silent, the
rollover row's sequence."""
import collections
import hashlib
import json
import os
import secrets
import threading
import time
import uuid
from pathlib import Path

from cousin_lib.delivery import DELIVERED, FAILED, QUEUED, Item
from cousin_lib.runner import blocks, restart_note, transcript, tmux_hook, tmux_turn, wake
from cousin_lib.runner.base import INTERRUPT, NO_TURN, Receipt, RunnerError
from cousin_lib.runner.inbox import Inbox
from cousin_lib.runner.state import StateMachine
from cousin_lib.runner.stream import EventStream
from cousin_lib.runner.tmux_pane import NO_PANE, Outcome, TmuxPane, printable

POLL_S = 0.1              # the transcript poll while nothing wakes the runner
CONSUME_S = 60.0          # a typed row not taken by then, at a turn end with an empty box, is requeued
LIMIT_RETRY_S = 300.0     # how long a usage limit holds the claim loop before trying again
TURN_FINISH_S = 30.0      # after the handoff file lands, how long the handoff turn may take to end
EXIT_WAIT_S = 10.0        # how long `/exit` gets to end the CLI before the pane is killed
HOOKS_SILENT_S = 5.0      # after the first turn end, how long a pane hook's datagram may still take
KILL_GRACE_S = 3.0        # a refused pane's CLI gets this long after the kill before a SIGKILL
KILL_BOUND_S = 6.0        # and this long in all before the runner gives up on starting
ALIVE_CHECK_S = 1.0       # how often the loop asks tmux whether the pane is still there
REOPEN_BASE_S = 1.0       # a lost pane is started again after this, doubling per failed try
REOPEN_MAX_S = 60.0       # up to this
BLOCKED_BASE_S = 0.5      # a screen that refuses typing is tried again after this, doubling
BLOCKED_MAX_S = 5.0       # up to this
PROBATION_S = 20.0        # a started pane that stays up this long has proven itself
REOPEN_GIVE_UP = 5        # consecutive failed starts before the runner gives up (errored, exit 3)
LOSS_MAX = 5              # more pane losses than this (proven panes or not) inside LOSS_WINDOW_S
LOSS_WINDOW_S = 600.0     # ... and the runner gives up the same way
LINE_REPLAYS = 3          # a transcript line whose handling raises is skipped after this many replays
NOTICE_WAIT_S = 120.0     # a runner line owed at the first idle is tried this long, then said
STOP_SETTLE_S = 15.0      # a stop waits this long (at most half its timeout) for the cut turn's end
MAX_ID = 128              # a hook datagram's session_id; a CLI's is a 36-character uuid
MAX_SOURCE = 32           # and its SessionStart source ("startup", "resume", "clear", "compact")
CHANGES_KEPT = 32         # session ids a session_changed was said for, the newest kept
LOGIN_SCREENS = ("trust", "onboarding", "login", "bypass", "mcp_approval")
CLAIMS_FILE = "tmux-claims.json"
CURSOR_FILE = "tmux-cursor.json"
SESSION_FILE = "runner-session.json"
HOOK_RECORD = ("run", "tmux-session.json")    # written by the pane's SessionStart hook
CONTEXT_FILE = ("data", "run", "tmux-context.md")     # R10: the launcher appends it on --fresh
POINTER_FILE = ("data", "run", "tmux-resume.md")      # R10: the hook's SessionStart context on a resume
ORIGINS_FILE = ("data", "run", "tmux-context-origin.json")   # session id -> the block it was born with
ORIGINS_KEPT = 32
LAUNCH_EXIT = ("data", "run", "tmux-launch-exit.txt")   # tmux_launch's last refusal
GIVING_UP = ("data", "run", "tmux-giving-up.json")      # a give-up the next start honours
GIVE_UP_HOLD_S = 3600.0   # for this long, or until `cousin-supervisor start` clears it
GAVE_UP_EXIT = 2          # cousin-runner's exit on a give-up: the supervisor's CONFIG_EXIT,
                          # `failing` and left down, never restarted (supervisor.classify_exit)
UNREACHABLE_MAX_S = 5.0   # while tmux does not answer, the check backs off to this
UNREACHABLE_LOST_S = 120.0   # and after this long the pane is taken as lost
ENDS = ("turn_end", "interrupt", "api_error", "limit")


def _atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(json.dumps(data))
    tmp.replace(path)


def _atomic_write_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    tmp.replace(path)


def _read_json(path):
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def read_giving_up(home):
    """The give-up marker ({"reason", "at"}), or None."""
    data = _read_json(Path(home).joinpath(*GIVING_UP))
    return data if data and isinstance(data.get("at"), (int, float)) else None


def clear_giving_up(home):
    """An explicit start (supervisor.start_child) clears it, as it releases the hold."""
    Path(home).joinpath(*GIVING_UP).unlink(missing_ok=True)


class TmuxRunner:
    kind = "tmux"
    UNSUPPORTED = ("midturn_fold",)   # the CLI queues or interrupts, never folds (S3, S3b)
    PLUGIN_ITEMS = ()
    recovers_claims = True            # _serve leaves recovery to start() (P11-9)
    takes_interrupts = True

    def __init__(self, home, *, account=None, model=None, effort=None, policy=None,
                 pane_factory=None, config_dir=None, launch_argv=None, socket=None,
                 handoff_deadline_s=None, env_allow=()):
        from cousin_lib.runner import rollover as _rollover
        from cousin_lib.runner.main import root_for
        self.home = Path(home)
        self.root = root_for(self.home)       # _serve's head event and the context block read it
        self.account = account
        self.model, self.effort = model, effort
        self.policy = policy
        self.config_dir = config_dir if config_dir is not None else getattr(account, "config_dir", None)
        self._pane_factory = pane_factory
        self._launch_argv = launch_argv
        self.env_allow = tuple(env_allow)     # [agent] env_allow, checked by tmux_launch.env_allow_of
        self._socket = socket
        self.runner_id = "tmux-" + uuid.uuid4().hex[:8]
        self.inbox = Inbox(self.home)
        self.stream = EventStream(self.home, self.runner_id)
        self.machine = StateMachine(on_change=self._on_state)
        self.pane = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._stopping = threading.Event()   # a stop is under way: no claim, the transcript still read
        self._stop_cut = False               # a pane lost during the stop cut a taken row
        self._thread = None
        self._session_id, self._fresh = None, True
        self._path, self._cursor = None, 0
        self._claims = {}          # inbox_id -> {"row", "nonces", "offset", "taken", "typed_at"}
        self._closed_nonces = set()
        self._runner_nonces = set()
        self._attempts = {}        # inbox_id -> retypes after CONSUME_S
        self._live = None          # {"rows", "prompt_id", "who"} while a turn runs
        self._interrupting = False
        self._login_blocked = False
        self._limit_until = 0.0
        self._hold_until = 0.0     # a postponed rollover holds the claim loop this long
        self._handoff_limited = False
        self._turn_seq = 0
        self._runner_turn_seen = False   # a runner-nonce turn started since the flag was cleared
        self._hook_heard = False         # a pane hook's datagram for this session arrived (M-a)
        self._first_end = None           # monotonic time of the first turn end
        self._hooks_silent_said = False
        self._changes_said = {}          # session ids a SessionStart named that are not ours
        self._lost = None                # {"attempt", "next"} while the pane is gone (C3)
        self._next_alive = 0.0
        self._blocked = None             # {"delay", "until"} while typing is refused
        self._notice = None              # {"text", "nonce"}: a runner line owed at the first idle
        self._pane_pid = None            # the pane's CLI, as last seen alive
        self._probation = None           # {"since"} until a started pane has stayed up probation_s
        self._reopen_fails = 0           # consecutive starts that failed or died unproven (N1)
        self._gave_up = False
        self.fatal = None                # why the worker gave up (main prints it)
        self.exit_code = None            # cousin-runner's exit then (GAVE_UP_EXIT)
        self._unreachable = None         # {"since", "delay", "next"} while tmux does not answer
        self.unreachable_lost_s = UNREACHABLE_LOST_S
        self._line_done = None           # (line end, entries handled) of a line half handled
        self._lost_turn = None           # the turn live at a loss: an adopt may find it still live
        self._turn_offset = 0
        self._line_fails = {}            # a line's end -> how often its handling raised
        self._cut_prefix = None          # {"text", "clears_note"}: a notice that expired untyped
        self._losses = collections.deque()   # monotonic times of pane losses, inside the window
        self.loss_max, self.loss_window_s = LOSS_MAX, LOSS_WINDOW_S
        self.probation_s = PROBATION_S
        self.reopen_base_s = REOPEN_BASE_S
        self.notice_wait_s = NOTICE_WAIT_S
        self.hooks_silent_s = HOOKS_SILENT_S   # tests shorten these three
        self.kill_grace_s, self.kill_bound_s = KILL_GRACE_S, KILL_BOUND_S
        self.handoff_deadline_s = float(handoff_deadline_s or _rollover.HANDOFF_DEADLINE_S)

    # -- small helpers ----------------------------------------------------
    def _on_state(self, old, new, detail):
        self.stream.append("state", {"from": old, "to": new, "detail": detail})

    def _to(self, state, detail=""):
        """A turn's own state change; a rollover owns the machine while it
        runs (its handoff turn is a turn, but not a state of the runner)."""
        with self._lock:
            if self.machine.state not in (state, "stopped", "rolling_over"):
                self.machine.to(state, detail)

    def _data(self, name):
        return self.home / "data" / name

    def session_id(self):
        return self._session_id

    # -- Runner protocol --------------------------------------------------
    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def begin_stop(self):
        """A stop was asked for (cousin-runner's signal handler, before
        stop() runs): nothing new is claimed from now on (live proofs
        09-25, finding 3); the live turn is finished or settled by stop()."""
        self._stopping.set()
        wake.poke(self.home)

    def stop(self, *, timeout=30.0):
        """End the live turn and stop at idle (R21, R17's close): no new
        claim, one Escape on a live turn, then the worker reads the turn's
        end from the transcript (the row closes there, once) for at most
        STOP_SETTLE_S; only then does the loop end, and a held stop kill the
        pane. A claim still open once the pane is killed is settled here, as
        R23 settles a dead pane's (review C5): the next runner, of either
        kind, finds nothing claimed. A turn the stop cut leaves the #98 mark
        (restart_note), so the resumed session is told who stopped it."""
        if self.machine.state == "stopped":
            return
        deadline = time.monotonic() + timeout
        self._stopping.set()
        held = restart_note.held_by(self.home)
        cut = False
        if self._live is not None and self.pane is not None and self.worker_alive():
            cut = self._send_interrupt()
            settle = time.monotonic() + min(STOP_SETTLE_S, timeout / 2.0)
            while self._live is not None and self.worker_alive() and time.monotonic() < settle:
                wake.poke(self.home)
                time.sleep(POLL_S)
        self._stop.set()
        wake.poke(self.home)
        if self._thread is not None:
            self._thread.join(max(0.0, deadline - time.monotonic()))
        if self.pane is not None and held is not None:
            self.pane.kill()
            cut = self._settle_on_stop() or cut
        cut = cut or self._stop_cut          # the pane went first (a unit's cgroup kill)
        if cut:
            try:
                restart_note.mark(self.home, "a stop cut the turn in flight", held=held)
            except OSError as exc:
                self.stream.append("error", {"error": "restart mark: %s" % exc})
        self._turn_file(None)
        with self._lock:
            if self.machine.state != "stopped":
                self.machine.to("stopped")

    def _settle_on_stop(self):
        """The claims left once a held stop killed the pane (the worker has
        ended): a taken row closes `delivered`, cut by the stop; an untaken
        one is requeued. True when a taken row was cut."""
        if self.worker_alive() or not self._claims:
            return False
        try:
            self._pump()                     # a turn end written before the kill
        except Exception as exc:  # noqa: BLE001 - settled from what was read
            self.stream.append("error", {"error": "reading the transcript at the stop: %s: %s"
                                         % (type(exc).__name__, exc)})
        cut = []
        for inbox_id, c in sorted(self._claims.items()):
            self._closed_nonces |= set(c["nonces"])
            if c["taken"] is not None:
                self.inbox.done(inbox_id, DELIVERED, "cut by a requested stop")
                cut.append(inbox_id)
            else:
                self.inbox.requeue(inbox_id)
        self._claims = {}
        self._persist_claims()
        if cut:
            self.stream.append("result", {"inbox_ids": cut, "interrupted": True, "is_error": False})
        self._live = None
        return bool(cut)

    def state(self):
        return self.machine.state

    def enqueue(self, item):
        if not isinstance(item, Item):
            raise TypeError("enqueue() takes a delivery.Item")
        inbox_id = self.inbox.put(item)
        wake.poke(self.home)
        return Receipt(inbox_id=inbox_id, outcome=QUEUED)

    def interrupt(self):
        with self._lock:
            if self.machine.state != "running" or self._live is None or self.pane is None:
                return False
        return self._send_interrupt()

    def rollover(self, reason):
        from cousin_lib.runner import rollover as _rollover
        return _rollover.request(self.inbox, self.home, reason, alive=self.worker_alive, timeout=30.0)

    def events(self, after=None):
        return self.stream.tail(after=after)

    def unsupported(self):
        return list(self.UNSUPPORTED)

    def plugin_items(self):
        return list(self.PLUGIN_ITEMS)

    def worker_alive(self):
        return self._thread is not None and self._thread.is_alive()

    def login_required(self):
        return self._login_blocked

    # -- the pane and the session ----------------------------------------
    def _recorded_session(self):
        """(session_id or None, fresh): `fresh` is a rollover's new id whose
        CLI never started (N9): it starts fresh under that id, not resumed."""
        data = _read_json(self._data(SESSION_FILE)) or {}
        sid = data.get("session_id")
        if not (isinstance(sid, str) and sid):
            return None, True
        return sid, data.get("fresh") is True

    def _save_session(self):
        from cousin_lib import boot
        data = {"session_id": self._session_id, "lane": None,
                "generation": boot.read_generation(self.home), "updated": time.time(), "kind": "tmux"}
        if self._fresh and self._size() == 0:
            data["fresh"] = True       # kept until the CLI has written the session
        _atomic_write(self._data(SESSION_FILE), data)

    def _hook_record(self):
        return _read_json(self.home.joinpath(*HOOK_RECORD))

    def _adopt_refusal(self):
        """Why the live pane may not be adopted (R24), or None: the hook's
        record must name the recorded session and the pane's CLI pid."""
        rec = self._hook_record()
        if rec is None:
            return {"reason": "no_record"}
        if rec.get("session_id") != self._session_id:
            return {"reason": "session_mismatch", "record_session_id": rec.get("session_id")}
        pane_pid = self.pane.pid()
        if pane_pid is None or rec.get("pid") != pane_pid:
            return {"reason": "pid_mismatch", "record_pid": rec.get("pid"), "pane_pid": pane_pid}
        return None

    def _hook_path(self):
        data = self._hook_record() or {}
        if data.get("session_id") == self._session_id and data.get("transcript_path"):
            return Path(data["transcript_path"])
        return None

    def _transcript_path(self):
        return self._hook_path() or transcript.locate(self.home, session_id=self._session_id,
                                                      config_dir=self.config_dir)

    def _make_pane(self, path):
        if self._pane_factory is not None:
            return self._pane_factory(path)
        sock = self._socket or (Path(self.root) / "run" / "tmux.sock")
        return TmuxPane(sock, "tmux-%s" % self.home.name)

    def _argv(self, fresh):
        if self._launch_argv is not None:
            return self._launch_argv(self._session_id, fresh)
        try:
            from cousin_lib.runner import tmux_launch
        except ImportError as exc:  # Task 6 lands tmux_launch.py
            raise RunnerError("the tmux kind's launcher is not installed: %s" % exc)
        return tmux_launch.argv(home=self.home, session=("--session-id" if fresh else "--resume",
                                                        self._session_id),
                                model=self.model, effort=self.effort, fresh=fresh)

    def _env_base(self):
        try:
            from cousin_lib.runner import tmux_launch
        except ImportError:          # names only: the pane's login shell supplies the values
            return ("HOME", "PATH", "USER", "LOGNAME", "LANG")
        return tmux_launch.env_base(dict(os.environ), env_allow=self.env_allow)

    def _write_context(self, fresh):
        """R10, before every pane start: the block (law, the pane's contract,
        operator rules; prompt.compose_context_block) to data/run/
        tmux-context.md, which the launcher appends on a fresh start. A fresh
        session's block is remembered by its digest; a resumed one gets
        data/run/tmux-resume.md, the short pointer the pane's SessionStart
        hook hands the model (S7: --append-system-prompt is dropped on a
        resume): the file's path and whether it changed since the session
        started with it."""
        from cousin_lib.runner import prompt, tools
        registry, _notice = tools.resolve_registry(self.home, self.root)
        block = prompt.compose_context_block(self.home, root=self.root, registry=registry)
        path = self.home.joinpath(*CONTEXT_FILE)
        _atomic_write_text(path, block)
        digest = hashlib.sha256(block.encode()).hexdigest()[:16]
        origins_path = self.home.joinpath(*ORIGINS_FILE)
        origins = _read_json(origins_path) or {}
        if fresh:
            self.home.joinpath(*POINTER_FILE).unlink(missing_ok=True)   # another session's
            origins.pop(self._session_id, None)
            origins[self._session_id] = digest
            while len(origins) > ORIGINS_KEPT:
                origins.pop(next(iter(origins)))
            _atomic_write(origins_path, origins)
            return
        born = origins.get(self._session_id)
        if born == digest:
            change = "it is unchanged since this session started with it in its system prompt"
        elif born is None:
            change = ("this session did not start with it (it began on another runner kind, or"
                      " before this runner wrote it): read it now")
        else:
            change = ("it changed since this session started (a release, a registry or a"
                      " shared-rule edit): read it again now")
        _atomic_write_text(self.home.joinpath(*POINTER_FILE), (
            "[runner] This session was resumed in an interactive Claude Code pane under the"
            " cousins framework's tmux runner. The framework's block for this pane (the law,"
            " the contract for this runner, the operator rules) is %s; %s. Where it and"
            " instructions earlier in this session differ (another runner kind's), it wins.\n"
            % (path, change)))

    def _prepare_start(self, fresh):
        """The context block before a pane starts: a fresh start cannot run
        without it (the launcher refuses, exit 2); a resume only loses its
        pointer, which is said."""
        try:
            self._write_context(fresh)
        except Exception as exc:  # noqa: BLE001 - said, and fatal only for a fresh start
            if fresh:
                raise RunnerError("the context block for a fresh start: %s: %s"
                                  % (type(exc).__name__, exc))
            self.stream.append("error", {"error": "the resume pointer: %s: %s"
                                         % (type(exc).__name__, exc)})

    def _turn_file(self, threads=None, nonce=""):
        """run/turn.json for the stdio server (I5, R11): the live turn's
        threads, or cleared (None). Never fails a turn."""
        try:
            if threads is None:
                tmux_turn.clear(self.home)
            else:
                tmux_turn.write(self.home, session_id=self._session_id, turn_nonce=nonce or "",
                                threads=threads)
        except OSError as exc:
            self.stream.append("error", {"error": "run/turn.json: %s" % exc})

    def _open_session(self):
        """Adopt, else resume, else fresh (P11-2). Returns how."""
        recorded, fresh = self._recorded_session()
        self._session_id = recorded or str(uuid.uuid4())
        self._path = self._transcript_path()
        self._fresh = fresh and self._size() == 0
        self.pane = self._make_pane(self._path)
        # a live pane runs the recorded id, fresh or not: a rollover ends the
        # old CLI before it writes the new id, so the pane is never the old one;
        # the hook's record proves it (R24), and a pane it does not prove is
        # killed (reap_pane's own kill: this runner already holds the lock)
        how = None
        if recorded is not None and self.pane.alive():
            refused = self._adopt_refusal()
            if refused is None:
                how, self._fresh = "adopted", False
            else:
                self.stream.append("system", dict({"subtype": "adopt_refused",
                                                   "session_id": self._session_id}, **refused))
                old = self.pane.pid()
                self.pane.kill()
                if not self._gone(old):
                    # a second CLI on the same session would write the same transcript
                    raise RunnerError("the refused pane's CLI (pid %s) outlived its kill and a"
                                      " SIGKILL; not starting a second one on session %s"
                                      % (old, self._session_id))
        if how is None:
            self._start_pane(self._fresh)
            how = "fresh" if self._fresh else "resumed"
        else:
            self._pane_pid, self._probation = self.pane.pid(), None
        self._save_session()
        self.stream.append("session", {"pane_pid": self.pane.pid(), "session_id": self._session_id,
                                       "source": how})
        return how

    def _start_pane(self, fresh):
        """The context block, then the pane; the pane is on probation until
        it has stayed up probation_s: one that dies before is a failed start
        (N1), counted toward REOPEN_GIVE_UP."""
        self._prepare_start(fresh)
        self.home.joinpath(*LAUNCH_EXIT).unlink(missing_ok=True)
        self.pane.start(self._argv(fresh), cwd=str(self.home), env_base=self._env_base())
        self._pane_pid = self.pane.pid()
        self._probation = {"since": time.monotonic()}

    def _launcher_said(self):
        try:
            text = self.home.joinpath(*LAUNCH_EXIT).read_text(errors="replace").strip()
        except OSError:
            return None
        return " ".join(text.split())[:400] or None

    def _prove(self):
        """A started pane proves itself by staying up probation_s; the count
        of failed starts then begins again. Its box or its SessionStart
        datagram is not proof enough: a CLI can draw its box, fire its hook
        and exit a moment later (the re-review's probe D), and counting that
        as healthy restarts it about every second, forever."""
        p = self._probation
        if p is not None and time.monotonic() - p["since"] >= self.probation_s:
            self._probation, self._reopen_fails = None, 0

    def _gone(self, pid):
        """Wait for a killed pane's CLI to be gone: SIGKILL after
        kill_grace_s, False when it is still there at kill_bound_s."""
        if pid is None:
            return True
        start, killed = time.monotonic(), False
        while self.pane.process_alive(pid):
            waited = time.monotonic() - start
            if waited >= self.kill_bound_s:
                return False
            if not killed and waited >= self.kill_grace_s:
                self.pane.process_kill(pid)
                killed = True
            time.sleep(POLL_S)
        return True

    # -- a pane that went away (review C3) -----------------------------------
    def _check_alive(self, force=False):
        """True while the pane is there; asked of tmux at most every
        ALIVE_CHECK_S unless `force`. One failed has-session (its own
        timeout included) is not a dead pane (N2): a second one must fail
        too, and the pane's CLI must be gone; a CLI still running behind a
        tmux that does not answer is said once and settled never. A pane
        found gone is settled here. False whenever the pane is not usable."""
        now = time.monotonic()
        u = self._unreachable
        if u is not None and now < u["next"]:
            return False                         # asked again only after its backoff
        if u is None and not force and now < self._next_alive:
            return True
        self._next_alive = now + ALIVE_CHECK_S
        if self.pane is not None and (self.pane.alive() or self.pane.alive()):
            self._unreachable = None
            return True
        pid = self._pane_pid
        if self.pane is not None and pid is not None and self.pane.process_alive(pid):
            if u is None:
                u = self._unreachable = {"since": now, "delay": ALIVE_CHECK_S}
                self.stream.append("system", {"subtype": "pane_unreachable", "pid": pid,
                                              "session_id": self._session_id,
                                              "detail": "tmux does not answer for the pane but"
                                                        " its CLI runs; nothing is settled for"
                                                        " %.0f s" % self.unreachable_lost_s})
            else:
                u["delay"] = min(UNREACHABLE_MAX_S, u["delay"] * 2)
            if now - u["since"] < self.unreachable_lost_s:
                u["next"] = now + u["delay"]
                return False
            self.stream.append("error", {"error": "tmux has not answered for the pane for %.0f s"
                                                  " while its CLI (pid %s) runs: taken as lost"
                                                  % (now - u["since"], pid)})
        self._unreachable = None
        self._pane_lost()
        return False

    def _pane_lost(self):
        """The CLI exited under a live runner: stop claiming, read what it
        wrote before it went, settle every claim as R23 does for a dead pane
        (a taken row whose turn has no end closes `delivered`, cut, and the
        model is told once the pane is back; an untaken row is requeued;
        never `failed`), say it once, and reopen the session. A pane that
        dies before it proved itself is a failed start (N1): backed off, and
        given up on after REOPEN_GIVE_UP in a row."""
        try:
            self._pump()
        except Exception as exc:  # noqa: BLE001 - settled from what was read
            self.stream.append("error", {"error": "reading the lost pane's transcript: %s: %s"
                                         % (type(exc).__name__, exc)})
        cut, requeued = [], []
        stopping = self._stopping.is_set()
        detail = self._cut_detail() if stopping else "cut by pane loss"
        for inbox_id, c in sorted(self._claims.items()):
            self._closed_nonces |= set(c["nonces"])
            if c["taken"] is not None:
                self.inbox.done(inbox_id, DELIVERED, detail)
                cut.append(inbox_id)
            else:
                self.inbox.requeue(inbox_id)
                requeued.append(inbox_id)
        self._claims = {}
        self._persist_claims()
        self._lost_turn = None
        if self._live is not None:
            self._lost_turn = {"offset": self._turn_offset, "prompt_id": self._live["prompt_id"],
                               "who": self._live["who"]}
            self.stream.append("result", {"inbox_ids": cut, "interrupted": True, "is_error": False})
        self._live, self._interrupting = None, False
        self._turn_file(None)
        self._to("idle", "pane lost")
        if cut and stopping:
            self._stop_cut = True            # stop() marks it; the next start says it (#98)
        elif cut:
            self._owe_notice("The previous turn was cut short: the pane's CLI exited before it"
                             " finished, and the runner resumed this session in a new pane. Its"
                             " message was delivered. Check what it did and continue.")
        booting, self._probation = self._probation, None
        self.stream.append("system", {"subtype": "pane_lost", "session_id": self._session_id,
                                      "cut": cut, "requeued": requeued,
                                      "on_probation": booting is not None})
        now = time.monotonic()
        self._losses.append(now)
        while self._losses and now - self._losses[0] > self.loss_window_s:
            self._losses.popleft()
        if len(self._losses) > self.loss_max:
            # a CLI that outlives its proof and dies, over and over (round 3)
            self._give_up("%d pane losses within %.0f s" % (len(self._losses), self.loss_window_s),
                          fatal="the runner gave up on its pane",
                          counts={"losses": len(self._losses), "window_s": self.loss_window_s})
            return
        if booting is not None:
            self._failed_start("the pane's CLI exited %.1f s after its start, before it proved"
                               " itself" % (time.monotonic() - booting["since"]))
            return
        self._lost = {"next": time.monotonic()}

    def _cut_detail(self):
        """A taken row's close when the pane went during a stop (live proofs
        09-25, finding 5: a unit's cgroup kill takes the tmux server as the
        runner stops): the stop's cut, as exit criterion 2 words it."""
        return "cut by a requested stop" if restart_note.held_by(self.home) else "cut by restart"

    def _failed_start(self, why):
        """One more failed start in a row: the next after REOPEN_BASE_S,
        doubling to REOPEN_MAX_S; at REOPEN_GIVE_UP the runner gives up."""
        said = self._launcher_said()
        if said:
            why += "; the launcher said: %s" % said
        self._reopen_fails += 1
        if self._reopen_fails >= REOPEN_GIVE_UP:
            self._give_up(why)
            return
        delay = min(REOPEN_MAX_S, self.reopen_base_s * 2 ** (self._reopen_fails - 1))
        self._lost = {"next": time.monotonic() + delay}
        self.stream.append("error", {"error": "the pane did not come back (try %d): %s; next try"
                                              " in %.1fs" % (self._reopen_fails, why, delay)})

    def _give_up(self, why, fatal=None, counts=None):
        """errored, said, and the worker ends: cousin-runner exits
        GAVE_UP_EXIT (2), which the supervisor leaves down as `failing`,
        never restarted, with the reason read from the give-up marker; the
        marker also holds the next start down for GIVE_UP_HOLD_S, until an
        explicit `cousin-supervisor start` (or the console's start) clears
        it. `counts` is what the pane_failing event names (the failed starts
        by default, the losses and the window for the loss cap)."""
        self.fatal = ("%s: %s" % (fatal, why) if fatal else
                      "the pane failed %d starts in a row: %s" % (self._reopen_fails, why))
        self._lost, self._gave_up = None, True
        self.exit_code = GAVE_UP_EXIT
        try:
            _atomic_write(self.home.joinpath(*GIVING_UP), {"reason": self.fatal, "at": time.time()})
        except OSError as exc:
            self.stream.append("error", {"error": "the give-up marker: %s" % exc})
        self.stream.append("system", dict({"subtype": "pane_failing", "session_id": self._session_id,
                                           "reason": why},
                                          **(counts or {"tries": self._reopen_fails})))
        self.stream.append("error", {"error": self.fatal})
        with self._lock:
            if self.machine.state not in ("errored", "stopped", "rolling_over"):
                self.machine.to("errored", self.fatal)

    def _reopen(self):
        """One try at a pane on the recorded session once the backoff is
        over: the old CLI must be gone first (a second CLI on one session
        writes one transcript twice); a live pane is adopted and recovered
        (N2), a dead one resumed (or fresh under a rollover's id)."""
        if time.monotonic() < self._lost["next"]:
            return
        try:
            old = self._pane_pid
            if old is not None and not self.pane.alive() and not self._gone(old):
                raise RunnerError("the old CLI (pid %s) is still running" % old)
            how = self._open_session()
            if how != "adopted" and not self.pane.alive():
                raise RunnerError("the new pane's CLI exited at once")
        except Exception as exc:  # noqa: BLE001 - tried again after the backoff
            self._probation = None
            self._failed_start("%s: %s" % (type(exc).__name__, exc))
            return
        self._lost = None
        self._next_alive = time.monotonic() + ALIVE_CHECK_S
        if how == "adopted":
            self._after_adopt_reopen()
        self.stream.append("system", {"subtype": "pane_reopened", "session_id": self._session_id,
                                      "source": how, "failed_before": self._reopen_fails})

    def _after_adopt_reopen(self):
        """The pane was there after all (N2): its claims recovered as a
        start's are, and a turn that was live at the loss and has no end in
        the transcript is live again, so nothing is typed into it; it was
        never cut, so nobody is told it was."""
        self._recover("adopted")
        t, self._lost_turn = self._lost_turn, None
        if self._live is None and t is not None:
            entries, _ = transcript.read_from(self._path, t["offset"])
            at = next((i for i, e in enumerate(entries) if e.kind == "turn_start"), None)
            if at is not None and not any(e.kind in ("turn_end", "interrupt", "api_error", "limit",
                                                     "turn_start") for e in entries[at + 1:]):
                self._live = {"rows": [], "prompt_id": t["prompt_id"], "who": t["who"]}
                self._turn_offset = entries[at].offset
                self._notice = None
        if self._live is not None:
            self._to("running", "adopted mid-turn")
        else:
            self._clear_stranded()
            self._release_adopted_untaken()
        self._persist_cursor()

    def _blocked_stretch(self, what):
        """Typing refused by the screen: the next try waits BLOCKED_BASE_S,
        doubling to BLOCKED_MAX_S; one event per stretch."""
        now = time.monotonic()
        if self._blocked is None:
            self._blocked = {"delay": BLOCKED_BASE_S}
            self.stream.append("system", {"subtype": "typing_blocked", "what": what,
                                          "screen": self.pane.attention() or "box"})
        else:
            self._blocked["delay"] = min(BLOCKED_MAX_S, self._blocked["delay"] * 2)
        self._blocked["until"] = now + self._blocked["delay"]

    def _typing_held(self):
        return self._blocked is not None and time.monotonic() < self._blocked["until"]

    def _owe_notice(self, text, *, clears_note=False):
        self._notice = {"text": text, "nonce": None, "clears_note": clears_note,
                        "until": time.monotonic() + self.notice_wait_s}

    def _owe_start_notice(self, how):
        """What a start owes the model (R23, #98): a turn a restart or a
        stop cut, said as the one it was. The mark a stop left
        (restart_note) says who cut it; it is taken once the line is typed.
        A fresh session has nothing interrupted in it: the mark is dropped."""
        note = restart_note.read(self.home)
        if note is not None and how == "fresh" and not self._cut:
            restart_note.clear(self.home)
            return
        if note is None and not self._cut:
            return
        if note is not None:
            text = restart_note.body(note)
            if self._cut:
                text += " The cut turn's message was delivered; check what it did."
            self._owe_notice(text, clears_note=True)
            return
        held = restart_note.held_by(self.home)
        who = ("a requested stop (%s)" % held) if held else "a restart"
        self._owe_notice("The previous turn was cut short by %s before it finished; its"
                         " message was delivered. Check what it did and continue." % who)

    def _notice_tick(self):
        """The owed notice's clock stands still while nothing could be typed
        for a known reason: the pane being reopened, a login screen, a usage
        limit or a postponed rollover."""
        n = self._notice
        if n is None:
            return
        now = time.monotonic()
        last, n["tick"] = n.get("tick", now), now
        if (self._lost is not None or self._login_blocked or self.machine.state == "rate_limited"
                or now < self._limit_until or now < self._hold_until):
            n["until"] += now - last

    def _drop_cut_prefix(self):
        if self._cut_prefix is not None and self._cut_prefix["clears_note"]:
            restart_note.clear(self.home)
        self._cut_prefix = None

    def _maybe_notice(self):
        """A runner line owed at the first idle (a cut turn, R23), typed
        before any row once the pane takes input (review I1: a pane still
        booting has no box), tried again until NOTICE_WAIT_S, then said as a
        `notice_not_typed` event. True when this tick was spent on it."""
        if self._notice is None or self._pending_typed():
            return False
        if time.monotonic() >= self._notice["until"]:
            self.stream.append("system", {"subtype": "notice_not_typed",
                                          "text": self._notice["text"][:400],
                                          "waited_s": self.notice_wait_s})
            # the next typed row carries it, one line on top of its body
            text = " ".join(self._notice["text"].split())
            self._cut_prefix = {"text": text if text.startswith("[runner]") else "[runner] " + text,
                                "clears_note": self._notice["clears_note"]}
            self._notice = None
            return False
        if self._typing_held():
            return True
        if not self._screen_allows():
            return True
        if self._notice["nonce"] is None:
            self._notice["nonce"] = secrets.token_hex(6)
        out = self._runner_line(self._notice["text"], nonce=self._notice["nonce"])
        if out is Outcome.TYPED:
            if self._notice["clears_note"]:
                restart_note.clear(self.home)
            self._notice, self._blocked = None, None
        elif out is Outcome.BLOCKED:
            self._blocked_stretch("notice")
        else:
            self._check_alive(force=True)
        return True

    # -- claims ------------------------------------------------------------
    def _persist_claims(self):
        _atomic_write(self._data(CLAIMS_FILE), {
            "session_id": self._session_id, "runner_nonces": sorted(self._runner_nonces),
            "claims": [{"inbox_id": i, "nonces": c["nonces"], "offset": c["offset"],
                        "taken": c["taken"]} for i, c in sorted(self._claims.items())]})

    def _persist_cursor(self):
        _atomic_write(self._data(CURSOR_FILE), {"session_id": self._session_id,
                                                "path": str(self._path), "offset": self._cursor})

    def _size(self):
        try:
            return self._path.stat().st_size
        except (OSError, AttributeError):
            return 0

    def _recover(self, how):
        """Claims a previous runner left (R23, P11-9): scan the transcript
        from each claim's own offset; a taken row whose turn ended closes by
        that end; a taken row whose turn never ended closes `delivered`,
        cut by restart, when the pane is new; an untaken row is requeued."""
        # every orphaned claim back to the queue first (what _serve does for
        # the other kinds); the recorded ones are then settled below
        self.inbox.requeue_stale(older_than_s=0.0)
        data = _read_json(self._data(CLAIMS_FILE)) or {}
        claims = data.get("claims") if data.get("session_id") == self._session_id else None
        self._runner_nonces = set(data.get("runner_nonces") or ()) if claims is not None else set()
        cut = []
        for c in claims or ():
            row = self.inbox.get(c.get("inbox_id"))
            if not row or row["state"] == "done":
                continue
            entries, _ = transcript.read_from(self._path, int(c.get("offset") or 0))
            nonces = set(c.get("nonces") or ())
            start = next((i for i, e in enumerate(entries)
                          if transcript.turn_nonce(e, nonces)), None)
            if start is None:
                # untaken. A new pane never saw it: requeued above. A live
                # pane may still hold it (queued behind a turn, or in the
                # box): back with its nonces, and _run or _check_consumed
                # decides (R23, review I4); a retype would deliver it twice
                if how == "adopted" and self.inbox.claim_id(row["id"], claimant=self.runner_id):
                    self._claims[row["id"]] = {"row": row, "nonces": sorted(nonces),
                                               "offset": int(c.get("offset") or 0),
                                               "taken": None, "typed_at": time.monotonic()}
                continue
            end = next((e for e in entries[start + 1:]
                        if e.kind in ("turn_end", "interrupt", "api_error", "limit", "turn_start")), None)
            if end is None:
                if how == "adopted" and self.inbox.claim_id(row["id"], claimant=self.runner_id):
                    self._claims[row["id"]] = {"row": row, "nonces": sorted(nonces),
                                               "offset": int(c.get("offset") or 0),
                                               "taken": {"prompt_id": entries[start].prompt_id,
                                                         "at": time.time()},
                                               "typed_at": time.monotonic()}
                    self._live = {"rows": [row], "prompt_id": entries[start].prompt_id, "who": "row"}
                    self._turn_file([row["thread_id"]], entries[start].nonce)
                    continue
                self.inbox.done(row["id"], DELIVERED, "cut by restart")
                cut.append(row["id"])
            elif end.kind == "limit":
                pass                             # requeued above
            elif end.kind == "api_error":
                self.inbox.done(row["id"], FAILED, "API error (recovered)")
            else:
                self.inbox.done(row["id"], DELIVERED, "turn ended (recovered)")
            self._closed_nonces |= nonces
        self._persist_claims()
        self._cut = cut

    def _clear_stranded(self):
        """An adopted pane with no live turn and text in its box: a paste the
        previous runner left un-entered (R23). Its row was requeued by
        _recover, so the text is cleared, never entered: entering it would
        merge it with the next row's prompt."""
        stranded = self.pane.box_text()
        if stranded and not self.pane.queued():
            self.pane.clear()
            self.stream.append("error", {"error": "stranded input in the adopted pane's box cleared",
                                         "text": stranded[:120]})

    def _release_adopted_untaken(self):
        """R23 on a live pane: an untaken row is requeued only when the tail
        is at a turn end, the box is empty (a stranded paste was just
        cleared) and no queued input shows; otherwise it stays claimed under
        its nonces until its turn takes it or _check_consumed gives up."""
        pending = self._pending_typed()
        if not pending or self._live is not None or self.pane.queued() \
                or self.pane.box_text() != "":
            return
        for c in pending:
            self.inbox.requeue(c["row"]["id"])
            self._claims.pop(c["row"]["id"], None)
            self._closed_nonces |= set(c["nonces"])
        self._persist_claims()

    # -- the worker ---------------------------------------------------------
    def _wake_error(self, message):
        self.stream.append("error", {"error": message})

    def _held_by_give_up(self):
        """A give-up younger than GIVE_UP_HOLD_S holds this start down (the
        supervisor restarted, a container came back): errored, exit 2 again,
        no pane. An older one is dropped."""
        mark = read_giving_up(self.home)
        if mark is None:
            return False
        age = time.time() - mark["at"]
        if age >= GIVE_UP_HOLD_S:
            clear_giving_up(self.home)
            return False
        self.fatal = ("gave up on its pane %.0f s ago (%s); held down for %.0f s, or until"
                      " `cousin-supervisor start %s`" % (age, mark.get("reason") or "no reason",
                                                         GIVE_UP_HOLD_S, self.home.name))
        self.exit_code = GAVE_UP_EXIT
        self.stream.append("system", {"subtype": "pane_failing", "held": True,
                                      "reason": mark.get("reason"), "age_s": round(age)})
        self.stream.append("error", {"error": self.fatal})
        with self._lock:
            self.machine.to("errored", self.fatal)
        return True

    def _run(self):
        if self._held_by_give_up():
            return
        try:
            self._turn_file(None)                # a file a dead runner left names no live turn
            how = self._open_session()
            self._cursor = self._size() if how != "fresh" else 0
            self._recover(how)
            if self._live is not None:
                self._to("running", "adopted mid-turn")
            elif how == "adopted":
                self._clear_stranded()
                self._release_adopted_untaken()
            self._persist_cursor()
            self._owe_start_notice(how)
        except Exception as exc:  # noqa: BLE001 - never a silent death
            self._to("errored", "start failed: %s: %s" % (type(exc).__name__, exc))
            self.stream.append("error", {"error": "start: %s: %s" % (type(exc).__name__, exc)})
            return
        with wake.listen(self.home, self._wake_error) as listener:
            while not self._stop.is_set() and not self._gave_up:
                try:
                    self._notice_tick()
                    if self._lost is not None:
                        if not self._stopping.is_set():
                            self._reopen()
                    else:
                        self._pump()
                        alive = self._check_alive()
                        if alive:
                            self._prove()
                        if not alive:
                            pass                             # said and settled: reopened next
                        elif self._live is not None:
                            self._take_interrupts()
                        elif not self._stopping.is_set() and not self._maybe_notice():
                            self._maybe_claim()
                except Exception as exc:  # noqa: BLE001 - recorded, the loop goes on
                    self._fail_turn([], exc)
                    time.sleep(0.2)
                listener.wait(timeout=POLL_S)
                self._heard(listener)

    def _hook_message(self, raw):
        """A pane hook's datagram as (event, session_id, source), or None for
        anything else: a plain poke, or a shape no hook sends (review minor 5)."""
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(data, dict) or data.get("event") not in tmux_hook.EVENTS:
            return None
        sid, source = data.get("session_id"), data.get("source")
        if sid is not None and not (isinstance(sid, str) and 0 < len(sid) <= MAX_ID):
            return None
        if source is not None and not (isinstance(source, str) and len(source) <= MAX_SOURCE):
            return None
        return data["event"], sid, source

    def _heard(self, listener):
        """The pane hooks' datagrams (runner/tmux_hook.py). One is only a
        wake: it counts as heard when it names this session, and nothing
        else is done on it (R19). No datagram within hooks_silent_s (5 s) of
        the first turn end is a `hooks_silent` event, once (M-a). A runner
        polling without its socket (wake.Poller) hears nothing by design and
        already said why, so it says nothing more."""
        for raw in listener.messages:
            message = self._hook_message(raw)
            if message is None:
                continue
            event, sid, source = message
            if sid == self._session_id:
                self._hook_heard = True
            if event == "SessionStart" and (sid != self._session_id or source == "clear"):
                self._session_changed(sid, source)
        if (not self._hook_heard and not self._hooks_silent_said and self._first_end is not None
                and listener.path is not None
                and time.monotonic() - self._first_end >= self.hooks_silent_s):
            self._hooks_silent_said = True
            self.stream.append("system", {
                "subtype": "hooks_silent", "session_id": self._session_id,
                "detail": "no pane hook datagram for this session within %g s of the first"
                          " turn end (a hook that does not import, or a CLI that is not the"
                          " pane's); the runner polls the transcript instead" % self.hooks_silent_s})

    def _session_changed(self, sid, source):
        """A SessionStart for a session that is not the runner's (a /clear in
        the pane): said once per id, never followed (a known gap). The ids
        said are bounded: the oldest is forgotten past CHANGES_KEPT."""
        if sid in self._changes_said:
            return
        self._changes_said[sid] = True
        while len(self._changes_said) > CHANGES_KEPT:
            self._changes_said.pop(next(iter(self._changes_said)))
        self.stream.append("system", {"subtype": "session_changed", "session_id": self._session_id,
                                      "new_session_id": sid, "source": source})

    # -- the transcript ------------------------------------------------------
    def _pump(self):
        """The new transcript lines, handled in order; the cursor is saved
        after each LINE (the entries a torn line held share its end), and
        the entries of a line already handled are not handled again when a
        later one on the same line raises. An entry that raises is replayed
        LINE_REPLAYS times, then skipped with one error naming its offset;
        a skipped entry that ended the live turn closes it (_close_skipped)."""
        entries, cursor = transcript.read_from(self._path, self._cursor)
        k = 0
        for i, e in enumerate(entries):
            k = k + 1 if i and entries[i - 1].end == e.end else 0      # its index on its line
            done = self._line_done
            if done is not None and done[0] == e.end and k < done[1]:
                continue                                  # handled before a later entry raised
            key = (e.end, k)
            try:
                self._handle(e)
                if self._line_fails:
                    self._line_fails.pop(key, None)
            except Exception as exc:  # noqa: BLE001 - replayed, then skipped
                fails = self._line_fails.get(key, 0) + 1
                self._line_fails[key] = fails
                if fails <= LINE_REPLAYS:
                    raise
                del self._line_fails[key]
                self.stream.append("error", {"error": "skipped the transcript line at offset %d"
                                                      " (%s) after %d failures: %s: %s"
                                                      % (e.offset, e.kind, fails,
                                                         type(exc).__name__, exc)})
                self._close_skipped(e, fails, exc)
            if i + 1 == len(entries) or entries[i + 1].offset >= e.end:
                self._line_done = None
                self._cursor = e.end
                self._persist_cursor()
            else:
                self._line_done = (e.end, k + 1)
        if cursor != self._cursor:
            self._cursor = cursor
            self._persist_cursor()

    def _close_skipped(self, e, fails, exc):
        """A skipped turn end (a turn_duration, an interrupt, an API error
        or a limit) while a turn is live: the turn is closed here, its rows
        `delivered`, or `failed` for an API error that is not a limit, with
        the skip as the reason, and the runner goes idle; a limit is R6's
        limit end (_skipped_limit). Otherwise the live turn would hold the
        claim loop forever."""
        if self._live is None or e.kind not in ENDS:
            return
        if e.kind == "limit":
            self._skipped_limit(e, fails, exc)
            return
        outcome = FAILED if e.kind == "api_error" else DELIVERED
        detail = ("skipped: the turn's %s line at offset %d failed %d times (%s: %s)"
                  % (e.kind, e.offset, fails, type(exc).__name__, exc))
        ids = [row["id"] for row in self._live["rows"]]
        try:
            self._close_rows(outcome, detail)
        except Exception as err:  # noqa: BLE001 - the rows stay claimed; a start's sweep settles them
            self.stream.append("error", {"error": "closing the skipped turn's rows: %s: %s"
                                                  % (type(err).__name__, err)})
        self.stream.append("result", {"inbox_ids": ids, "interrupted": e.kind == "interrupt",
                                      "is_error": outcome == FAILED})
        self._live, self._interrupting = None, False
        self._turn_file(None)
        self._to("idle", "turn closed by a skipped line")

    def _skipped_limit(self, e, fails, exc):
        """R6's limit end for a skipped limit line: the taken rows requeued,
        never failed, and `rate_limited` (_limit_live); if that raises too,
        the same by hand, row by row."""
        try:
            self._limit_live()
            return
        except Exception as err:  # noqa: BLE001 - the same end, by hand
            self.stream.append("error", {"error": "the skipped limit line's end: %s: %s"
                                                  % (type(err).__name__, err)})
        for row in (self._live or {}).get("rows", []):
            try:
                self.inbox.requeue(row["id"])
            except Exception:  # noqa: BLE001 - a start's sweep requeues it
                pass
            self._claims.pop(row["id"], None)
        self._live, self._interrupting = None, False
        self._turn_file(None)
        self._limit_until = time.monotonic() + LIMIT_RETRY_S
        self._to("rate_limited", "usage limit (its line skipped)")

    def _known_nonces(self):
        known = set(self._runner_nonces)
        for c in self._claims.values():
            if c["taken"] is None:
                known |= set(c["nonces"])
        return known

    def _handle(self, e):
        if e.kind == "other" and "_unparsed" in e.raw:
            nonce = transcript.turn_nonce(e, self._known_nonces())
            if nonce is None:
                return
            # a torn line merged with a turn start (P11-9): that turn started
            self.stream.append("error", {"error": "torn transcript line holding [inbox:%s]" % nonce})
            e = transcript.Entry(e.offset, e.end, "turn_start", nonce=nonce, raw={})
        if e.kind == "turn_start":
            if self._live is not None:                  # "send now": the live turn was cut
                self._end_turn(interrupted=True)
            self._begin_turn(e)
        elif e.kind == "assistant":
            for kind, payload in blocks.assistant_events((e.raw.get("message") or {}).get("content")):
                self.stream.append(kind, payload)
        elif e.kind == "tool_result":
            for kind, payload in blocks.user_events((e.raw.get("message") or {}).get("content")):
                self.stream.append(kind, payload)
        elif self._live is None:
            return
        elif e.kind == "turn_end":
            self._end_turn(interrupted=False)
        elif e.kind == "interrupt":
            self._end_turn(interrupted=True)
        elif e.kind == "api_error":
            self._fail_live(" ".join(blocks.assistant_events(
                (e.raw.get("message") or {}).get("content"))[0][1].get("text", "").split()) or "API error")
        elif e.kind == "limit":
            self._limit_live()

    def _row_for_nonce(self, nonce):
        for inbox_id, c in self._claims.items():
            if nonce in c["nonces"]:
                return inbox_id
        return None

    def _begin_turn(self, e):
        self._turn_offset = e.offset
        if self._fresh:
            # the CLI has written the session: it resumes from here on, and
            # runner-session.json loses its "fresh" mark (a kind switch reads it)
            self._fresh = False
            self._save_session()
        content = (e.raw.get("message") or {}).get("content")
        text = blocks.user_events(content)[0][1]["text"] if content else "[inbox:%s]" % e.nonce
        inbox_id = self._row_for_nonce(e.nonce) if e.nonce else None
        if inbox_id is not None and self._claims[inbox_id]["taken"] is None:
            c = self._claims[inbox_id]
            c["taken"] = {"prompt_id": e.prompt_id, "at": time.time()}
            if c.get("prefix") and self._cut_prefix is not None:
                self._drop_cut_prefix()           # the model has read it
            self._persist_claims()
            row = c["row"]
            self._live = {"rows": [row], "prompt_id": e.prompt_id, "who": "row"}
            self._turn_file([row["thread_id"]], e.nonce)
            self._to("running", "turn")
            self.stream.append("turn_start", {"inbox_ids": [row["id"]], "bodies": [row["body"]],
                                              "thread_id": row["thread_id"]})
            self.stream.append("user", {"text": text[:blocks.TEXT_CHARS], "echo_of": row["id"]})
            return
        if e.nonce and e.nonce in self._closed_nonces:
            self.stream.append("duplicate_delivery", {"nonce": e.nonce, "prompt_id": e.prompt_id})
        who = "runner" if e.nonce and e.nonce in self._runner_nonces else "foreign"
        self._runner_turn_seen |= who == "runner"
        if who == "foreign":
            self.stream.append("foreign_turn", {"prompt_id": e.prompt_id})
        self._live = {"rows": [], "prompt_id": e.prompt_id, "who": who}
        self._turn_file(["system"] if who == "runner" else [], e.nonce or e.prompt_id)
        self._to("running", "%s turn" % who)
        self.stream.append("turn_start", {"inbox_ids": [], "bodies": [text.split("\n", 1)[0][:200]],
                                          "thread_id": "system"})

    def _close_rows(self, outcome, detail):
        rows = self._live["rows"] if self._live else []
        for row in rows:
            self.inbox.done(row["id"], outcome, detail)
            c = self._claims.pop(row["id"], None)
            if c:
                self._closed_nonces |= set(c["nonces"])
        self._persist_claims()
        return [r["id"] for r in rows]

    def _end_turn(self, *, interrupted):
        ids = self._close_rows(DELIVERED, "turn %s%s" % (self._session_id, " (interrupted)" if interrupted else ""))
        self.stream.append("result", {"inbox_ids": ids, "interrupted": interrupted, "is_error": False})
        self._live, self._interrupting = None, False
        self._turn_file(None)
        self._turn_seq += 1
        if self._first_end is None:
            self._first_end = time.monotonic()
        if not interrupted:
            self._mine()
        self._to("idle", "turn done")

    def _mine(self, **extra):
        """Extraction at a normal turn end (and a final one at a rollover):
        the transcript's new entries into raw memory (extract.mine_turn);
        the outcome is an `extract` event, never a raise."""
        from cousin_lib.runner import extract
        payload = dict({"session_id": self._session_id, "turn": self._turn_seq}, **extra)
        try:
            payload["written"] = extract.mine_turn(
                self.home, self._session_id, self._turn_seq,
                store=transcript.TranscriptStore(self._path, self._session_id))
        except Exception as exc:  # noqa: BLE001 - extraction must never fail a turn
            payload.update(written=-1, error="%s: %s" % (type(exc).__name__, exc))
        self.stream.append("extract", payload)

    def _fail_live(self, message):
        self._to("errored", message)
        self.stream.append("error", {"error": message})
        ids = self._close_rows(FAILED, message)
        self.stream.append("result", {"inbox_ids": ids, "interrupted": False, "is_error": True})
        self._live, self._interrupting = None, False
        self._turn_file(None)
        self._to("idle", "recovered")

    def _limit_live(self):
        for row in (self._live or {}).get("rows", []):
            self.inbox.requeue(row["id"])
            self._claims.pop(row["id"], None)
        self._persist_claims()
        self._live, self._interrupting = None, False
        self._turn_file(None)
        self._limit_until = time.monotonic() + LIMIT_RETRY_S
        self._handoff_limited = self.machine.state == "rolling_over"
        self.stream.append("rate_limit", {"until_s": LIMIT_RETRY_S, "source": "transcript"})
        self._to("rate_limited", "usage limit")

    def _fail_turn(self, consumed, exc):
        message = "%s: %s" % (type(exc).__name__, exc)
        with self._lock:
            if self.machine.state in ("idle", "running"):
                self.machine.to("errored", message)
        self.stream.append("error", {"error": message})
        for row in consumed:
            self.inbox.done(row["id"], FAILED, message)
        self.stream.append("result", {"inbox_ids": [r["id"] for r in consumed],
                                      "interrupted": False, "is_error": True})
        with self._lock:
            if self.machine.state == "errored":
                self.machine.to("idle", "recovered")

    # -- interrupts ----------------------------------------------------------
    def _send_interrupt(self):
        """One Escape on a live turn (R8); never a second one blind."""
        if self.pane is None or self.pane.attention():
            return False
        with self._lock:
            already, self._interrupting = self._interrupting, True
        if not already:
            self.pane.key("Escape")
        return True

    def _take_interrupts(self):
        for row in self.inbox.open_rows(INTERRUPT):
            if row["state"] != "queued" or self.inbox.claim_id(row["id"], claimant=self.runner_id) is None:
                continue
            already = self._interrupting
            if not self._send_interrupt():
                # an attention screen (or no pane): no Escape was sent, the turn runs on
                screen = self.pane.attention() if self.pane is not None else NO_PANE
                self.inbox.done(row["id"], FAILED, "the pane refused the Escape (%s showing)"
                                % (screen or "a screen"))
                continue
            self.inbox.done(row["id"], DELIVERED, "the live turn was already being interrupted"
                            if already else "interrupted the live turn")

    # -- claiming and typing -------------------------------------------------
    def _screen_allows(self):
        seen = self.pane.attention() if self.pane is not None else NO_PANE
        if seen == NO_PANE:
            self._check_alive(force=True)            # a screen nobody can read is not clear
            return False
        if seen == "rewind":
            self.pane.key("Escape")                  # the one screen the runner answers
            self.stream.append("error", {"error": "the rewind selector was open; dismissed"})
            return False
        if seen in LOGIN_SCREENS:
            if not self._login_blocked:
                self._login_blocked = True
                _atomic_write(self._data("login-required.json"), {"kind": "tmux", "screen": seen,
                                                                   "ts": time.time()})
                self.stream.append("auth", {"login_required": True, "screen": seen})
            return False
        if self._login_blocked:
            self._login_blocked = False
            try:
                self._data("login-required.json").unlink()
            except OSError:
                pass
        if seen == "limit":
            if time.monotonic() >= self._limit_until:
                self._limit_until = time.monotonic() + LIMIT_RETRY_S
                self.stream.append("rate_limit", {"until_s": LIMIT_RETRY_S, "source": "screen"})
            return False
        if time.monotonic() < self._hold_until:
            return False
        if self.machine.state == "rate_limited":
            if time.monotonic() < self._limit_until:
                return False
            self._to("idle", "limit window over")
        return True

    def _pending_typed(self):
        return [c for c in self._claims.values() if c["taken"] is None]

    def _maybe_claim(self):
        pending = self._pending_typed()
        if pending:
            self._check_consumed(pending)
            return
        if self._typing_held():
            return
        if not self._screen_allows():
            return
        rows = self.inbox.claim(limit=1, claimant=self.runner_id)
        if not rows:
            return
        if self._stopping.is_set():                 # the stop raced the claim: back, untyped
            for row in rows:
                self.inbox.requeue(row["id"])
            return
        row = rows[0]
        if row["source"] == INTERRUPT:
            self.inbox.done(row["id"], FAILED, NO_TURN)
            return
        if row["source"] == "flip":
            self._rollover_row(row)
            return
        self._type(row)

    def _render(self, row, nonce):
        sender = row.get("sender") or "someone"
        first = "[inbox:%s] [%s] %s from %s" % (nonce, row["thread_id"], row["source"], sender)
        first = printable(" ".join(first.split()))   # one line whatever the sender is (C4)
        body = row.get("body") or ""
        if self._cut_prefix is not None:
            body = self._cut_prefix["text"] + "\n\n" + body
        context = row.get("context") or ""
        if context:
            body += "\n\n--- context (not the sender's words) ---\n" + context
        return first, body

    def _type(self, row):
        nonce = secrets.token_hex(6)
        self._claims[row["id"]] = {"row": row, "nonces": [nonce], "offset": self._size(),
                                   "taken": None, "typed_at": time.monotonic()}
        self._persist_claims()                      # before any key (P11-9)
        first, body = self._render(row, nonce)
        # the expired notice rides on this row until the row is TAKEN
        # (_begin_turn): a row retyped after it was not taken carries it again
        self._claims[row["id"]]["prefix"] = self._cut_prefix is not None
        out = self.pane.type_row(first, body)
        if out is Outcome.TYPED:
            self._blocked = None
            return
        if out is Outcome.FAILED and not self._check_alive(force=True):
            if row["id"] in self._claims:            # tmux unreachable: nothing settled, row back
                self._claims.pop(row["id"], None)
                self._persist_claims()
                self.inbox.requeue(row["id"])
            return                                  # never failed for a dead pane
        self._claims.pop(row["id"], None)
        self._persist_claims()
        if out is Outcome.BLOCKED:
            self.inbox.requeue(row["id"])
            self._blocked_stretch("row")
            return
        self.inbox.done(row["id"], FAILED, "the pane refused the row")
        self.stream.append("error", {"error": "typing row %d failed" % row["id"]})
        self.stream.append("result", {"inbox_ids": [row["id"]], "interrupted": False, "is_error": True})

    def _check_consumed(self, pending):
        now = time.monotonic()
        for c in pending:
            if now - c["typed_at"] < CONSUME_S:
                continue
            if self.pane.queued() or self.pane.box_text() not in ("", None):
                continue
            row = c["row"]
            self._claims.pop(row["id"], None)
            self._closed_nonces |= set(c["nonces"])
            if self._attempts.get(row["id"], 0) >= 1:
                self.inbox.done(row["id"], FAILED, "the CLI never took the prompt")
                self.stream.append("result", {"inbox_ids": [row["id"]], "interrupted": False,
                                              "is_error": True})
            else:
                self._attempts[row["id"]] = self._attempts.get(row["id"], 0) + 1
                self.inbox.requeue(row["id"])
            self._persist_claims()

    def _runner_line(self, text, nonce=None):
        nonce = nonce or secrets.token_hex(6)
        self._runner_nonces.add(nonce)
        self._persist_claims()
        first = "[inbox:%s] [system] runner from the framework" % nonce
        return self.pane.type_row(first, text)

    # -- rollover -------------------------------------------------------------
    def _mtime(self, path):
        try:
            return path.stat().st_mtime_ns
        except OSError:
            return 0

    def _ask_handoff(self, reason):
        """The handoff turn (R9): the request typed under a runner nonce,
        then the wait for data/handoff.md to change (the handoff tool writes
        it last; flip.py's wait). 'clean' when it changed in time;
        'emergency' when it did not (the file is then written from the
        transcript's tail); 'stopped', 'rate_limited' and 'login_required'
        postpone the rollover, never an emergency."""
        from cousin_lib.runner import rollover as _rollover
        path = self._data("handoff.md")
        before = self._mtime(path)
        self._handoff_limited = self._runner_turn_seen = False
        out = self._runner_line(_rollover.handoff_request_text(reason))
        deadline = time.monotonic() + self.handoff_deadline_s
        why = None
        if out is Outcome.BLOCKED and self.pane.attention() in LOGIN_SCREENS:
            self._screen_allows()                      # records login-required.json
            return "login_required"
        if out is not Outcome.TYPED:
            why = "the pane refused the handoff request (%s)" % out.name
        while why is None:
            if self._stop.is_set():
                return "stopped"
            self._pump()
            if self._mtime(path) != before:
                finish = time.monotonic() + TURN_FINISH_S
                while self._live is not None and time.monotonic() < finish and not self._stop.is_set():
                    time.sleep(POLL_S)
                    self._pump()
                self.stream.append("rollover", {"phase": "handoff", "handoff": "clean"})
                return "clean"
            if self._handoff_limited:
                return "rate_limited"
            if self.pane.attention() in LOGIN_SCREENS:
                self._screen_allows()
                return "login_required"
            if self._runner_turn_seen and self._live is None:
                why = "the model finished its turn without calling handoff"
            if why is None and time.monotonic() >= deadline:
                why = "handoff timeout (%.0fs)" % self.handoff_deadline_s
            if why is None:
                time.sleep(POLL_S)
        if self._live is not None:
            self._send_interrupt()
        tail = transcript.TranscriptStore(self._path, self._session_id).tail_text(self._session_id)
        _rollover.write_emergency_handoff(self.home, name=self.home.name, reason=why, tail=tail)
        self.stream.append("rollover", {"phase": "handoff", "handoff": "emergency", "why": why})
        return "emergency"

    def _exit_pane(self):
        """`/exit` on the old CLI (SessionEnd `prompt_input_exit`), the pane
        killed when it has not ended in EXIT_WAIT_S. Returns how it ended."""
        if not self.pane.alive():
            return "gone"
        end = time.monotonic() + EXIT_WAIT_S
        out = self.pane.type_row("/exit", "")
        while out is Outcome.BLOCKED and time.monotonic() < end:
            # the handoff turn's end is in the transcript a moment before the
            # CLI takes input again: try again, never kill a CLI that is closing
            time.sleep(POLL_S)
            out = self.pane.type_row("/exit", "")
        if out is Outcome.TYPED:
            while time.monotonic() < end:
                if not self.pane.alive():
                    return "exit"
                time.sleep(POLL_S)
        self.pane.kill()
        return "killed"

    def _postpone(self, row, reason, why):
        self.inbox.requeue(row["id"])
        self._hold_until = time.monotonic() + (LIMIT_RETRY_S if why == "rate_limited" else 0.0)
        with self._lock:
            if self.machine.state == "rolling_over":
                self.machine.to("idle", "rollover postponed: " + why)
        self.stream.append("rollover", {"phase": "postponed", "reason": reason, "why": why})

    def _rollover_row(self, row):
        """FakeRunner's sequence on a pane: the handoff turn, end hooks, the
        archive, a final mine, `/exit`, the new session id written before
        its pane starts (N9), then the generation, start hooks and the
        digest. A failure before the new pane runs fails the row and keeps
        the old session recorded; after it, a failure degrades the rollover
        and is named in the detail (the SDK kind's point of no return)."""
        from cousin_lib import boot, session
        from cousin_lib.runner import prompt
        from cousin_lib.runner import rollover as _rollover
        reason = row["body"] or "rollover"
        with self._lock:
            if self.machine.state != "idle":
                self.inbox.requeue(row["id"])
                return
            self.machine.to("rolling_over", reason.splitlines()[0][:120])
        old_sid = self._session_id
        self.stream.append("rollover", {"phase": "start", "reason": reason, "session_id": old_sid})
        exited = None
        try:
            handoff = self._ask_handoff(reason)
            if handoff == "stopped":
                self.inbox.requeue(row["id"])          # the next start finishes this rollover
                self.stream.append("rollover", {"phase": "requeued", "reason": reason})
                return
            if handoff in ("rate_limited", "login_required"):
                self._postpone(row, reason, handoff)
                return
            session.run_phase(self.home, "end")
            _rollover.archive_generation(self.home, boot.read_generation(self.home))
            self._mine(final=True)                     # nothing of the old session arrives after this
            exited = self._exit_pane()
            self._session_id, self._fresh = str(uuid.uuid4()), True
            # a new session had no turn cut: what was owed about the old one is dropped
            self._drop_cut_prefix()
            self._notice = None
            self._path = self._transcript_path()
            self._claims, self._cursor, self._runner_nonces = {}, 0, set()
            self._save_session()                       # the new id before its pane (N9)
            self._persist_claims()
            self.pane = self._make_pane(self._path)
            self._start_pane(True)
            self._persist_cursor()
        except Exception as exc:  # noqa: BLE001 - never a wedged machine
            message = "%s: %s" % (type(exc).__name__, exc)
            with self._lock:
                if self.machine.state == "rolling_over":
                    self.machine.to("errored", "rollover failed: " + message)
                    self.machine.to("idle", "recovered")
            detail = {"reason": reason, "error": message, "old_session": old_sid,
                      "new_session": self._session_id if self._session_id != old_sid else None}
            self.inbox.done(row["id"], FAILED, json.dumps(detail))
            self.stream.append("rollover", dict(detail, phase="failed"))
            return
        problems, generation = [], boot.read_generation(self.home)
        try:
            generation = boot.bump_generation(self.home)
        except Exception as exc:  # noqa: BLE001 - named in the detail
            problems.append("generation not moved: %s: %s" % (type(exc).__name__, exc))
        else:
            try:
                # the record was written before the bump (N9): it names the
                # new generation now, not only from the new session's first
                # turn (live proofs 09-25, finding 6)
                self._save_session()
            except Exception as exc:  # noqa: BLE001 - named in the detail
                problems.append("runner-session.json: %s: %s" % (type(exc).__name__, exc))
        try:
            session.run_phase(self.home, "start")
        except Exception as exc:  # noqa: BLE001 - named in the detail
            problems.append("start hooks: %s: %s" % (type(exc).__name__, exc))
        try:
            try:
                digest = prompt.state_digest(self.home, root=self.root, slug=self.home.name,
                                             generation=generation)["text"]
            except Exception as exc:  # noqa: BLE001 - degraded, never none
                digest = _rollover.degraded_digest(self.home, slug=self.home.name,
                                                   generation=generation, error=exc)
            self.inbox.put(Item(thread_id="system", source="boot", body=digest, sender="runner"))
        except Exception as exc:  # noqa: BLE001 - the session runs on without one
            problems.append("digest: %s: %s" % (type(exc).__name__, exc))
        detail = {"reason": reason, "handoff": handoff, "exit": exited, "generation": generation,
                  "old_session": old_sid, "session_id": self._session_id}
        if problems:
            detail["problems"] = problems
        with self._lock:
            if self.machine.state == "rolling_over":
                self.machine.to("idle", "rolled over")
        self.inbox.done(row["id"], DELIVERED, json.dumps(detail))
        self.stream.append("rollover", dict(detail, phase="done"))


REAP_EXIT_OK, REAP_EXIT_NO_PANE = 0, 0


def pane_for(home, *, socket=None):
    """The pane a tmux-kind cousin runs in: the framework socket, the
    cousin's session name (the runner's own choice, interfaces I3)."""
    from cousin_lib.config import FrameworkConfig
    home = Path(home)
    root = FrameworkConfig.root_from_home(home) or home.parent.parent
    return TmuxPane(socket or (Path(root) / "run" / "tmux.sock"), "tmux-%s" % home.name)


def reap_pane(home, *, pane=None):
    """`cousin-runner --home H --reap-pane` (R21, P11-10): kill the pane of a
    cousin whose runner is down, holding the runner lock while it does, so
    no starting runner adopts a pane being killed. Exit 0 whether or not a
    pane was there; LOCK_HELD_EXIT when a runner holds the lock (stop the
    runner instead: its stop ends the turn and, when held, kills the pane)."""
    from cousin_lib.runner.main import LOCK_HELD_EXIT, LockHeld, hold_lock
    try:
        with hold_lock(home):
            pane = pane or pane_for(home)
            if pane.alive():
                pane.kill()
                print("reaped the pane of %s" % Path(home).name)
            else:
                print("no pane for %s" % Path(home).name)
            return REAP_EXIT_OK
    except LockHeld as err:
        print("cousin-runner: %s; stop the runner instead" % err)
        return LOCK_HELD_EXIT
