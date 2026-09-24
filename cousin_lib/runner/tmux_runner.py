"""TmuxRunner: an interactive Claude Code in a tmux pane behind the runner
protocol (phase 11; plan R2-R25, interfaces I2).

The pane is driven, never trusted: the runner types a row and learns what
happened only from the CLI's own transcript (runner/transcript.py). A row
is TAKEN when a turn start's first line begins with its nonce, and it
CLOSES once, at that turn's end: `turn_duration` delivers it, the
interrupt entry delivers it (interrupted), an API error fails it, a limit
error requeues it (R4, R6). A turn start while a turn is live ends that
turn as interrupted ("send now" writes no end of its own; measured).

Continuity is the session id (P11-2): start() adopts a live pane, else
resumes the recorded session in a new pane, else starts fresh; nothing
depends on the pane outliving the runner. A stop always ends the live
turn; the pane is killed only when the stop is a hold (run/held, P11-10).

The structure is FakeRunner's (the reference runner): one worker thread,
the wake socket, the state machine, `_fail_turn` never silent, the
rollover row's sequence."""
import json
import os
import secrets
import threading
import time
import uuid
from pathlib import Path

from cousin_lib.delivery import DELIVERED, FAILED, QUEUED, Item
from cousin_lib.runner import blocks, transcript, wake
from cousin_lib.runner.base import INTERRUPT, NO_TURN, Receipt, RunnerError
from cousin_lib.runner.inbox import Inbox
from cousin_lib.runner.state import StateMachine
from cousin_lib.runner.stream import EventStream
from cousin_lib.runner.tmux_pane import Outcome, TmuxPane

POLL_S = 0.1              # the transcript poll while nothing wakes the runner
CONSUME_S = 60.0          # a typed row not taken by then, at a turn end with an empty box, is requeued
LIMIT_RETRY_S = 300.0     # how long a usage limit holds the claim loop before trying again
LOGIN_SCREENS = ("trust", "onboarding", "login", "bypass", "mcp_approval")
CLAIMS_FILE = "tmux-claims.json"
CURSOR_FILE = "tmux-cursor.json"
SESSION_FILE = "runner-session.json"


def _atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(json.dumps(data))
    tmp.replace(path)


def _read_json(path):
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


class TmuxRunner:
    kind = "tmux"
    UNSUPPORTED = ("midturn_fold",)   # the CLI queues or interrupts, never folds (S3, S3b)
    PLUGIN_ITEMS = ()
    recovers_claims = True            # _serve leaves recovery to start() (P11-9)
    takes_interrupts = True

    def __init__(self, home, *, account=None, model=None, effort=None, policy=None,
                 pane_factory=None, config_dir=None, launch_argv=None, socket=None):
        self.home = Path(home)
        self.account = account
        self.model, self.effort = model, effort
        self.policy = policy
        self.config_dir = config_dir if config_dir is not None else getattr(account, "config_dir", None)
        self._pane_factory = pane_factory
        self._launch_argv = launch_argv
        self._socket = socket
        self.runner_id = "tmux-" + uuid.uuid4().hex[:8]
        self.inbox = Inbox(self.home)
        self.stream = EventStream(self.home, self.runner_id)
        self.machine = StateMachine(on_change=self._on_state)
        self.pane = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
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

    # -- small helpers ----------------------------------------------------
    def _on_state(self, old, new, detail):
        self.stream.append("state", {"from": old, "to": new, "detail": detail})

    def _to(self, state, detail=""):
        with self._lock:
            if self.machine.state != state and self.machine.state != "stopped":
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

    def stop(self, *, timeout=30.0):
        if self.machine.state == "stopped":
            return
        self._stop.set()
        if self._live is not None and self.pane is not None:
            self._send_interrupt()
        if self.pane is not None and (self.home / "run" / "held").exists():
            self.pane.kill()
        wake.poke(self.home)
        if self._thread is not None:
            self._thread.join(timeout)
        with self._lock:
            if self.machine.state != "stopped":
                self.machine.to("stopped")

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
        data = _read_json(self._data(SESSION_FILE)) or {}
        sid = data.get("session_id")
        return sid if isinstance(sid, str) and sid else None

    def _save_session(self):
        from cousin_lib import boot
        _atomic_write(self._data(SESSION_FILE), {"session_id": self._session_id, "lane": None,
                                                 "generation": boot.read_generation(self.home),
                                                 "updated": time.time(), "kind": "tmux"})

    def _hook_path(self):
        data = _read_json(self.home / "run" / "tmux-session.json") or {}
        if data.get("session_id") == self._session_id and data.get("transcript_path"):
            return Path(data["transcript_path"])
        return None

    def _transcript_path(self):
        return self._hook_path() or transcript.locate(self.home, session_id=self._session_id,
                                                      config_dir=self.config_dir)

    def _make_pane(self, path):
        if self._pane_factory is not None:
            return self._pane_factory(path)
        from cousin_lib.config import FrameworkConfig
        root = FrameworkConfig.root_from_home(self.home) or self.home.parent.parent
        sock = self._socket or (Path(root) / "run" / "tmux.sock")
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
        except ImportError:
            keep = ("HOME", "PATH", "USER", "LOGNAME", "LANG", "TERM")
            return {k: os.environ[k] for k in keep if k in os.environ}
        return tmux_launch.env_base(dict(os.environ))

    def _open_session(self):
        """Adopt, else resume, else fresh (P11-2). Returns how."""
        recorded = self._recorded_session()
        self._session_id = recorded or str(uuid.uuid4())
        self._fresh = recorded is None
        self._path = self._transcript_path()
        self.pane = self._make_pane(self._path)
        if not self._fresh and self.pane.alive():
            how = "adopted"
        else:
            self.pane.start(self._argv(self._fresh), cwd=str(self.home), env_base=self._env_base())
            how = "fresh" if self._fresh else "resumed"
        self._save_session()
        self.stream.append("session", {"pane_pid": self.pane.pid(), "session_id": self._session_id,
                                       "source": how})
        return how

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
        except OSError:
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
                          if e.kind == "turn_start" and e.nonce in nonces), None)
            if start is None:
                continue                         # untaken: already requeued above
            end = next((e for e in entries[start + 1:]
                        if e.kind in ("turn_end", "interrupt", "api_error", "limit", "turn_start")), None)
            if end is None:
                if how == "adopted" and self.inbox.claim_id(row["id"], claimant=self.runner_id):
                    self._claims[row["id"]] = {"row": row, "nonces": sorted(nonces),
                                               "offset": int(c.get("offset") or 0),
                                               "taken": {"prompt_id": entries[start].prompt_id},
                                               "typed_at": time.monotonic()}
                    self._live = {"rows": [row], "prompt_id": entries[start].prompt_id, "who": "row"}
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

    # -- the worker ---------------------------------------------------------
    def _wake_error(self, message):
        self.stream.append("error", {"error": message})

    def _run(self):
        try:
            how = self._open_session()
            self._cursor = self._size() if how != "fresh" else 0
            self._recover(how)
            if self._live is not None:
                self._to("running", "adopted mid-turn")
            self._persist_cursor()
            if self._cut:
                self._runner_line("The previous turn was cut short by a restart before it "
                                  "finished; its message was delivered. Check what it did and "
                                  "continue.")
        except Exception as exc:  # noqa: BLE001 - never a silent death
            self._to("errored", "start failed: %s: %s" % (type(exc).__name__, exc))
            self.stream.append("error", {"error": "start: %s: %s" % (type(exc).__name__, exc)})
            return
        with wake.listen(self.home, self._wake_error) as listener:
            while not self._stop.is_set():
                try:
                    self._pump()
                    if self._live is not None:
                        self._take_interrupts()
                    elif not self._stop.is_set():
                        self._maybe_claim()
                except Exception as exc:  # noqa: BLE001 - recorded, the loop goes on
                    self._fail_turn([], exc)
                    time.sleep(0.2)
                listener.wait(timeout=POLL_S)

    # -- the transcript ------------------------------------------------------
    def _pump(self):
        entries, cursor = transcript.read_from(self._path, self._cursor)
        for e in entries:
            self._handle(e)
        if cursor != self._cursor:
            self._cursor = cursor
            self._persist_cursor()

    def _handle(self, e):
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
        text = blocks.user_events((e.raw.get("message") or {}).get("content"))[0][1]["text"]
        inbox_id = self._row_for_nonce(e.nonce) if e.nonce else None
        if inbox_id is not None and self._claims[inbox_id]["taken"] is None:
            c = self._claims[inbox_id]
            c["taken"] = {"prompt_id": e.prompt_id, "at": time.time()}
            self._persist_claims()
            row = c["row"]
            self._live = {"rows": [row], "prompt_id": e.prompt_id, "who": "row"}
            self._to("running", "turn")
            self.stream.append("turn_start", {"inbox_ids": [row["id"]], "bodies": [row["body"]],
                                              "thread_id": row["thread_id"]})
            self.stream.append("user", {"text": text[:blocks.TEXT_CHARS], "echo_of": row["id"]})
            return
        if e.nonce and e.nonce in self._closed_nonces:
            self.stream.append("duplicate_delivery", {"nonce": e.nonce, "prompt_id": e.prompt_id})
        who = "runner" if e.nonce and e.nonce in self._runner_nonces else "foreign"
        if who == "foreign":
            self.stream.append("foreign_turn", {"prompt_id": e.prompt_id})
        self._live = {"rows": [], "prompt_id": e.prompt_id, "who": who}
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
        self._to("idle", "turn done")

    def _fail_live(self, message):
        self._to("errored", message)
        self.stream.append("error", {"error": message})
        ids = self._close_rows(FAILED, message)
        self.stream.append("result", {"inbox_ids": ids, "interrupted": False, "is_error": True})
        self._live, self._interrupting = None, False
        self._to("idle", "recovered")

    def _limit_live(self):
        for row in (self._live or {}).get("rows", []):
            self.inbox.requeue(row["id"])
            self._claims.pop(row["id"], None)
        self._persist_claims()
        self._live, self._interrupting = None, False
        self._limit_until = time.monotonic() + LIMIT_RETRY_S
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
            self._send_interrupt()
            self.inbox.done(row["id"], DELIVERED, "the live turn was already being interrupted"
                            if already else "interrupted the live turn")

    # -- claiming and typing -------------------------------------------------
    def _screen_allows(self):
        seen = self.pane.attention() if self.pane is not None else None
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
        if not self._screen_allows():
            return
        rows = self.inbox.claim(limit=1, claimant=self.runner_id)
        if not rows:
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
        body = row.get("body") or ""
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
        out = self.pane.type_row(first, body)
        if out is Outcome.TYPED:
            return
        self._claims.pop(row["id"], None)
        self._persist_claims()
        if out is Outcome.BLOCKED:
            self.inbox.requeue(row["id"])
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

    def _runner_line(self, text):
        nonce = secrets.token_hex(6)
        self._runner_nonces.add(nonce)
        self._persist_claims()
        first = "[inbox:%s] [system] runner from the framework" % nonce
        return self.pane.type_row(first, text)

    # -- rollover -------------------------------------------------------------
    def _rollover_row(self, row):
        """FakeRunner's sequence on a pane: the generation moves once the new
        session exists; the new session id is written before its pane starts
        (N9). The handoff before it is Task 7's."""
        from cousin_lib import boot, session
        from cousin_lib.runner import prompt
        from cousin_lib.runner import rollover as _rollover
        with self._lock:
            if self.machine.state != "idle":
                self.inbox.requeue(row["id"])
                return
            self.machine.to("rolling_over", row["body"].splitlines()[0][:120] if row["body"] else "rollover")
        try:
            session.run_phase(self.home, "end")
            _rollover.archive_generation(self.home, boot.read_generation(self.home))
            self._session_id, self._fresh = str(uuid.uuid4()), True
            self._save_session()
            self.pane.kill()
            self._path = self._transcript_path()
            self._claims, self._cursor = {}, 0
            self._persist_claims()
            self.pane = self._make_pane(self._path)
            self.pane.start(self._argv(True), cwd=str(self.home), env_base=self._env_base())
            self._persist_cursor()
        except Exception as exc:  # noqa: BLE001 - never a wedged machine
            message = "%s: %s" % (type(exc).__name__, exc)
            with self._lock:
                if self.machine.state == "rolling_over":
                    self.machine.to("errored", "rollover failed: " + message)
                    self.machine.to("idle", "recovered")
            self.inbox.done(row["id"], FAILED, json.dumps({"reason": row["body"], "error": message}))
            return
        problems, generation = [], boot.read_generation(self.home)
        try:
            generation = boot.bump_generation(self.home)
        except Exception as exc:  # noqa: BLE001 - named in the detail
            problems.append("generation not moved: %s: %s" % (type(exc).__name__, exc))
        try:
            session.run_phase(self.home, "start")
        except Exception as exc:  # noqa: BLE001 - named in the detail
            problems.append("start hooks: %s: %s" % (type(exc).__name__, exc))
        try:
            from cousin_lib.config import FrameworkConfig
            root = FrameworkConfig.root_from_home(self.home) or self.home.parent.parent
            try:
                digest = prompt.state_digest(self.home, root=root, slug=self.home.name,
                                             generation=generation)["text"]
            except Exception as exc:  # noqa: BLE001 - degraded, never none
                digest = _rollover.degraded_digest(self.home, slug=self.home.name,
                                                   generation=generation, error=exc)
            self.inbox.put(Item(thread_id="system", source="boot", body=digest, sender="runner"))
        except Exception as exc:  # noqa: BLE001 - the session runs on without one
            problems.append("digest: %s: %s" % (type(exc).__name__, exc))
        detail = {"reason": row["body"], "handoff": "not asked (Task 7)", "generation": generation,
                  "session_id": self._session_id}
        if problems:
            detail["problems"] = problems
        with self._lock:
            if self.machine.state == "rolling_over":
                self.machine.to("idle", "rolled over")
        self.inbox.done(row["id"], DELIVERED, json.dumps(detail))
        self.stream.append("rollover", dict(detail, phase="done"))
