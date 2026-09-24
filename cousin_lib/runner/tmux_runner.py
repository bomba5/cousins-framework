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
TURN_FINISH_S = 30.0      # after the handoff file lands, how long the handoff turn may take to end
EXIT_WAIT_S = 10.0        # how long `/exit` gets to end the CLI before the pane is killed
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
                 pane_factory=None, config_dir=None, launch_argv=None, socket=None,
                 handoff_deadline_s=None, env_allow=()):
        from cousin_lib.runner import rollover as _rollover
        self.home = Path(home)
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
        except ImportError:          # names only: the pane's login shell supplies the values
            return ("HOME", "PATH", "USER", "LOGNAME", "LANG")
        return tmux_launch.env_base(dict(os.environ), env_allow=self.env_allow)

    def _open_session(self):
        """Adopt, else resume, else fresh (P11-2). Returns how."""
        recorded, fresh = self._recorded_session()
        self._session_id = recorded or str(uuid.uuid4())
        self._path = self._transcript_path()
        self._fresh = fresh and self._size() == 0
        self.pane = self._make_pane(self._path)
        # a live pane runs the recorded id, fresh or not: a rollover ends the
        # old CLI before it writes the new id, so the pane is never the old one
        if recorded is not None and self.pane.alive():
            how, self._fresh = "adopted", False
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
                continue                         # untaken: already requeued above
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
            elif how == "adopted":
                self._clear_stranded()
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
        content = (e.raw.get("message") or {}).get("content")
        text = blocks.user_events(content)[0][1]["text"] if content else "[inbox:%s]" % e.nonce
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
        self._runner_turn_seen |= who == "runner"
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
        self._turn_seq += 1
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
        self._to("idle", "recovered")

    def _limit_live(self):
        for row in (self._live or {}).get("rows", []):
            self.inbox.requeue(row["id"])
            self._claims.pop(row["id"], None)
        self._persist_claims()
        self._live, self._interrupting = None, False
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
            self._path = self._transcript_path()
            self._claims, self._cursor, self._runner_nonces = {}, 0, set()
            self._save_session()                       # the new id before its pane (N9)
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
