"""The pane: the cousin's tmux session rendered in the browser and typed
into (docs/reference/console-api.md, "The pane").

Four routes over one session resolution: the session name from the
registry (`[chat] tmux_session`, default the slug), the binary and
socket from the console's flags (`req.tmux_bin`, `req.tmux_socket`),
`ssh <host>` in front for a cousin with `[chat] host`. `404` unknown
cousin, `400` no session configured, `409` session not running.

The stream polls `capture-pane` (the contract leaves the byte source
open; polling needs no tap file to clean up and works identically for a
remote cousin): a `pane` frame on connect and on every change, `geom`
plus a fresh `pane` when the geometry changes, `heartbeat` after 3 s of
silence, `: tick` between polls. Frames, geometry and pane state come
from injectable callables so the stream is tested without tmux.

Each frame carries the pane's state (alternate screen, mouse tracking,
cursor) and ends with a control tail the browser terminal is left in
after it resets and writes the frame: the mouse mode when the program
tracks the mouse, the cursor moved onto tmux's cursor cell, and the
cursor shown or hidden as tmux has it. A full-screen program runs on
tmux's alternate screen, which has no history to capture, so the only
way to scroll it from the browser is the program's own mouse wheel
handling: the browser terminal must itself be in mouse mode to turn a
wheel into an SGR report, and a captured frame never contains the
program's mode-setting escapes, so the frame sets it.

A tmux-kind runner cousin (`[agent] runner = "tmux"`, phase 11) is
addressed where its runner keeps it: the framework's own socket
(`<root>/run/tmux.sock`) and the session `tmux-<slug>`, matched exactly
(tmux_runner.pane_for). The runner types into that pane itself, from
another process, so a person's keys go
in only to answer a one-screen dialog the runner never types into (the
trust, bypass and MCP approval dialogs; the login and onboarding flows
take several screens and text, and are done in a terminal), and only as
a closed set of keys: arrows, Enter, Escape, Tab, Backspace, one digit,
y or n. A request ends at its first Enter or Escape (the screen changes
there; the rest is refused and the answer says how many went in), the
screen is read again before every key under a per-pane lock, and for
ENTER_SETTLE_S after an Enter nothing goes in. Anywhere else keys are
refused (409) and the chat is the way in. It keeps the fixed size its
screen parsers read (a resize is 409).
"""
from __future__ import annotations

import re
import subprocess
import threading
import time
from datetime import datetime

from cousin_lib.console import router, sse
from cousin_lib.console.proxy import RouteError, find_cousin, guarded

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_LITERAL_CHUNK = 4000   # well under tmux's message ceiling

# xterm key sequences tmux send-keys understands by name.
_NAMED = {
    "\x1b[A": "Up", "\x1b[B": "Down", "\x1b[C": "Right", "\x1b[D": "Left",
    "\x1b[1~": "Home", "\x1b[4~": "End", "\x1b[H": "Home", "\x1b[F": "End",
    "\x1b[5~": "PageUp", "\x1b[6~": "PageDown",
    "\x1b[3~": "DC", "\x1b[2~": "IC",
    "\x1bOP": "F1", "\x1bOQ": "F2", "\x1bOR": "F3", "\x1bOS": "F4",
}
_SINGLE = {"\r": "Enter", "\n": "Enter", "\x7f": "BSpace", "\t": "Tab"}

# tmux formats for Tmux.state, in the order of their keys
_STATE_FORMATS = ("alternate_on", "mouse_any_flag", "mouse_sgr_flag",
                  "pane_width", "pane_height", "cursor_x", "cursor_y",
                  "cursor_flag")
_STATE_KEYS = ("alt", "mouse", "sgr", "cols", "rows", "cx", "cy", "cursor")

# Normal tracking (press, release, wheel) with SGR encoding. Not the
# program's own mode: any-motion tracking would turn every mouse move
# over the browser pane into an input request, and the input path
# forwards SGR reports only, whatever encoding the program asked for.
_MOUSE_ON = "\x1b[?1000h\x1b[?1006h"
_CURSOR_SHOW, _CURSOR_HIDE = "\x1b[?25h", "\x1b[?25l"


class Tmux:
    """Runs tmux for one console: binary, socket, optional ssh host."""

    def __init__(self, tmux_bin="tmux", socket=None, host=None):
        self.tmux_bin = tmux_bin or "tmux"
        self.socket = socket
        self.host = host

    def _prefix(self):
        if self.host:
            # the remote user's default socket, as the contract says
            return ["ssh", self.host, "tmux"]
        cmd = [self.tmux_bin]
        if self.socket:
            cmd += ["-S", self.socket]
        return cmd

    def run(self, *args, timeout=3):
        return subprocess.run(self._prefix() + list(args),
                              capture_output=True, text=True,
                              timeout=timeout, check=False)

    def has_session(self, session):
        try:
            return self.run("has-session", "-t", session).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def capture(self, session, lines, trim=True):
        r = self.run("capture-pane", "-p", "-e", "-t", session,
                     "-S", "-%d" % int(lines), timeout=6)
        if r.returncode != 0:
            raise RouteError(500, {"ok": False,
                                   "error": (r.stderr or "").strip()[:200]})
        out = r.stdout or ""
        return trim_trailing_blank(out) if trim else out

    def _display(self, session, fmt):
        try:
            r = self.run("display-message", "-p", "-t", session, fmt,
                         timeout=2)
            return [int(p) for p in (r.stdout or "").split()]
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return []

    def geometry(self, session):
        parts = self._display(session, "#{pane_width} #{pane_height}")
        return (parts[0], parts[1]) if len(parts) >= 2 else (0, 0)

    def state(self, session):
        """The pane's screen state in one query, or None when tmux
        prints nothing usable: `alt` alternate screen on, `mouse` the
        program tracks the mouse (any mode), `sgr` it asked for SGR
        reports, `cols`/`rows` the pane size, `cx`/`cy` tmux's cursor
        cell (0-based), `cursor` the cursor is visible."""
        parts = self._display(session, " ".join(
            "#{%s}" % f for f in _STATE_FORMATS))
        if len(parts) != len(_STATE_KEYS):
            return None
        return dict(zip(_STATE_KEYS, parts))


def trim_trailing_blank(text):
    """Drop trailing lines that are empty once escapes are stripped, so
    the content sits at the bottom of the view."""
    lines = text.split("\n")
    while lines and not _ANSI_RE.sub("", lines[-1]).strip():
        lines.pop()
    return "\n".join(lines)


def _blank(line):
    return not _ANSI_RE.sub("", line).strip()


def compose_frame(raw, state):
    """A raw capture (history lines, then the screen's rows) and the
    pane state -> (text, top): the frame with trailing blank lines
    trimmed, never above the cursor's line, followed by the control
    tail; `top` is the number of frame lines above the screen's first
    row.

    The cursor is placed relative to the frame's last line (up, then to
    the column) because the terminal writing the frame may be shorter
    than the pane: where the last line lands is known to it, the
    frame's absolute row is not."""
    lines = raw.split("\n")
    if lines and lines[-1] == "":
        lines.pop()                     # capture-pane ends every line
    keep = len(lines)
    while keep and _blank(lines[keep - 1]):
        keep -= 1
    if not state:
        return "\n".join(lines[:keep]), 0
    rows = state.get("rows") or 0
    top = max(0, len(lines) - rows)
    row = None
    if rows and len(lines) >= rows:
        row = top + max(0, min(rows - 1, state.get("cy", 0)))
        keep = max(keep, row + 1)
    tail = ""
    if state.get("mouse") and state.get("sgr"):
        tail += _MOUSE_ON
    if row is not None:
        up = keep - 1 - row
        if up:
            tail += "\x1b[%dA" % up
        tail += "\x1b[%dG" % (max(0, state.get("cx", 0)) + 1)
    tail += _CURSOR_SHOW if state.get("cursor") else _CURSOR_HIDE
    return "\n".join(lines[:keep]) + tail, top


def input_tokens(data):
    """xterm bytes -> [(mode, payload)]: 'key' for a tmux key name,
    'literal' for text sent with `-l`. Any CSI or SS3 sequence not in the
    table is consumed and dropped, never forwarded as Escape; an SGR
    mouse report is a 'mouse' token, sent literally only while the
    program tracks the mouse (send_input)."""
    out = []
    i, n = 0, len(data)
    while i < n:
        ch = data[i]
        if ch == "\x1b":
            for seq, name in _NAMED.items():
                if data.startswith(seq, i):
                    out.append(("key", name))
                    i += len(seq)
                    break
            else:
                nxt = data[i + 1] if i + 1 < n else ""
                if nxt == "[":
                    j = i + 2
                    while j < n and not ("\x40" <= data[j] <= "\x7e"):
                        j += 1
                    end = j + 1 if j < n else n
                    seq = data[i:end]
                    if len(seq) >= 4 and seq[2] == "<" \
                            and seq[-1] in ("M", "m"):
                        out.append(("mouse", seq))
                    i = end
                elif nxt == "O":
                    i = min(i + 3, n)
                else:
                    out.append(("key", "Escape"))
                    i += 1
            continue
        if ch in _SINGLE:
            out.append(("key", _SINGLE[ch]))
            i += 1
            continue
        if "\x01" <= ch <= "\x1a":
            out.append(("key", "C-%s" % chr(ord(ch) + 96)))
            i += 1
            continue
        j = i
        while j < n and data[j] not in _SINGLE and data[j] != "\x1b" \
                and data[j] > "\x1a":
            j += 1
        out.append(("literal", data[i:j]))
        i = j
    return out


def chunk_literal(text, limit=_LITERAL_CHUNK):
    return [text[k:k + limit] for k in range(0, len(text), limit)]


def send_input(tmux, session, data):
    """Type the bytes into the session. Returns the token count; raises
    RouteError(500) with tmux's stderr."""
    tokens = input_tokens(data or "")
    if any(mode == "mouse" for mode, _ in tokens):
        # A report reaches the program as bytes (send-keys -l bypasses
        # tmux's own mouse handling). A program tracking the mouse with
        # SGR reads it as a mouse event; to any other program it is an
        # Escape followed by text, which throws a modal editor out of
        # insert mode and eats the next typed or injected line.
        st = tmux.state(session)
        tracked = bool(st and st.get("mouse") and st.get("sgr"))
        tokens = [t for t in tokens if t[0] != "mouse" or tracked]
    _send_tokens(tmux, session, tokens)
    return len(tokens)


def _send_tokens(tmux, session, tokens):
    """send-keys for each token; RouteError(500) with tmux's stderr."""
    for mode, payload in tokens:
        if mode in ("literal", "mouse"):
            # -l and -- are load-bearing: a run starting with '-' is
            # otherwise parsed as flags
            batches = [["-l", "--", piece]
                       for piece in chunk_literal(payload)]
        else:
            batches = [[payload]]
        for tail in batches:
            try:
                r = tmux.run("send-keys", "-t", session, *tail,
                             timeout=8)
            except (OSError, subprocess.TimeoutExpired) as err:
                raise RouteError(500, {"ok": False, "error": str(err)})
            if r.returncode != 0:
                raise RouteError(500, {
                    "ok": False,
                    "error": (r.stderr or "").strip()[:200]
                             or "tmux send-keys failed (rc=%d)"
                                % r.returncode})


def _iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def stream_pane(session, lines, *, tmux, capture=None, geometry=None,
                state=None, clock=time.monotonic, sleep=time.sleep,
                poll=0.5, heartbeat_after=3.0, geom_every=2.0):
    """Generator of SSE bytes for one pane. `capture(lines) -> str` (the
    raw capture, untrimmed), `geometry() -> (cols, rows)` and `state()
    -> dict | None` (Tmux.state) default to tmux; tests inject them."""
    capture = capture or (lambda n: tmux.capture(session, n, trim=False))
    geometry = geometry or (lambda: tmux.geometry(session))
    state = state or (lambda: tmux.state(session))

    def snapshot():
        raw = capture(lines)
        st = state()
        text, top = compose_frame(raw, st)
        if st:
            st = dict(st, top=top)
        return text, st

    def frame(text, st):
        return sse.event_frame("pane", {"text": text, "state": st,
                                        "ts": _iso(), "changed": True})

    last_frame = snapshot()
    yield frame(*last_frame)
    last_output = clock()
    last_geom_check = clock()
    last_geom = geometry()
    while True:
        sleep(poll)
        now = clock()
        if now - last_geom_check >= geom_every:
            last_geom_check = now
            g = geometry()
            if g != (0, 0) and g != last_geom:
                last_geom = g
                yield sse.event_frame("geom", {"cols": g[0], "rows": g[1],
                                               "ts": _iso()})
                last_frame = snapshot()
                yield frame(*last_frame)
                last_output = now
                continue
        current = snapshot()
        if current != last_frame:
            last_frame = current
            last_output = now
            yield frame(*current)
        elif now - last_output >= heartbeat_after:
            last_output = now
            yield sse.event_frame("heartbeat", {"ts": _iso()})
        else:
            yield b": tick\n\n"


def _tmux_for(req, cousin):
    return Tmux(getattr(req, "tmux_bin", None) or "tmux",
                socket=getattr(req, "tmux_socket", None),
                host=cousin.chat_host)


def kind_pane(cousin):
    """A tmux-kind runner cousin's pane (tmux_runner.pane_for: the
    framework socket, the runner's session name), or None for any other
    cousin, a remote one included."""
    home = getattr(cousin, "home", None)
    if home is None or getattr(cousin, "chat_host", None):
        return None
    from cousin_lib.delivery import _runner_kind
    if _runner_kind(home) != "tmux":
        return None
    from cousin_lib.runner.tmux_runner import pane_for
    return pane_for(home)


def _resolve(req, slug):
    """(cousin, session, tmux, kind) or a RouteError per the contract;
    `kind` is the tmux-kind runner's pane (kind_pane), else None."""
    cousin = find_cousin(req, slug)
    kind = kind_pane(cousin)
    if kind is not None:
        tmux = Tmux(getattr(req, "tmux_bin", None) or "tmux", socket=str(kind.socket))
        # exact matches: `=name` for the session, `=name:` for its pane
        if not tmux.has_session("=" + kind.name):
            raise RouteError(409, {"ok": False, "error": "session not running"})
        return cousin, "=%s:" % kind.name, tmux, kind
    session = cousin.tmux_session
    if not session:
        raise RouteError(400, {"ok": False,
                               "error": "no tmux session configured"})
    tmux = _tmux_for(req, cousin)
    if not tmux.has_session(session):
        raise RouteError(409, {"ok": False, "error": "session not running"})
    return cousin, session, tmux, None


# The dialogs a person answers in a tmux-kind pane from here: one screen
# each, answered with arrows, a digit or y/n, and Enter. The login menu and
# onboarding go on to a URL, a code or text: those are done in a terminal.
ANSWERABLE = ("trust", "bypass", "mcp_approval")
KIND_KEYS = ("Up", "Down", "Left", "Right", "Enter", "Escape", "Tab", "BSpace")
KIND_CHARS = "0123456789yYnN"
ENDS = ("Enter", "Escape")          # the screen changes after these
ENTER_SETTLE_S = 0.3
_KIND_GUARD = threading.Lock()
_FALLBACK_STATE = {}                # a request with no server (a unit test's duck type)


def _kind_state(req):
    state = getattr(getattr(req, "server", None), "state", None)
    return _FALLBACK_STATE if state is None else state


def _kind_locks(req):
    with _KIND_GUARD:
        return _kind_state(req).setdefault("pane.kind_locks", {})


def _kind_lock(req, key):
    locks = _kind_locks(req)
    with _KIND_GUARD:
        return locks.setdefault(key, threading.Lock())


def _screen_of(tmux, session):
    """The attention screen the visible pane shows (tmux_pane.attention_in),
    None for none or an unreadable pane. No history: an old dialog that
    scrolled away is not one."""
    from cousin_lib.runner.tmux_pane import attention_in
    try:
        r = tmux.run("capture-pane", "-p", "-t", session)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return attention_in(r.stdout or "")


def _answerable_screen(tmux, session):
    """The screen, when it is a dialog a person answers from here."""
    seen = _screen_of(tmux, session)
    return seen if seen in ANSWERABLE else None


def _kind_refusal(token):
    """Why a token is not in the closed set, or None when it is."""
    mode, payload = token
    if mode == "key":
        return None if payload in KIND_KEYS else "a control key (%s)" % payload
    if mode == "mouse":
        return "a mouse report"
    if len(payload) != 1:
        return "a paste or a run of text"
    return None if payload in KIND_CHARS else "a character other than a digit, y or n"


def _refuse(sent, refused, why):
    raise RouteError(409, {"ok": False, "sent": sent, "refused": refused, "error": "%d key%s went in; %d refused: %s" % (
        sent, "" if sent == 1 else "s", refused, why)})


def send_kind_input(req, tmux, session, kind, data):
    """Type a person's keys into a tmux-kind pane (the module docstring's
    rules); the count sent, or a RouteError 409 with `sent` and `refused`."""
    tokens = input_tokens(data or "")
    if not tokens:
        return 0
    cut = next((i + 1 for i, (mode, payload) in enumerate(tokens)
                if mode == "key" and payload in ENDS), len(tokens))
    head, rest = tokens[:cut], tokens[cut:]
    for token in head:
        why = _kind_refusal(token)
        if why:
            _refuse(0, len(tokens), "%s; only arrows, Enter, Escape, Tab, Backspace, one digit,"
                                    " y or n go into this pane" % why)
    key = "%s|%s" % (kind.socket, kind.name)
    state = _kind_state(req)
    with _kind_lock(req, key):
        ended = state.setdefault("pane.kind_ended", {})
        if time.monotonic() - ended.get(key, float("-inf")) < ENTER_SETTLE_S:
            _refuse(0, len(tokens), "the screen is changing after Enter or Escape; type again"
                                    " once it has settled")
        sent = 0
        for mode, payload in head:
            if _answerable_screen(tmux, session) is None:
                if sent:
                    _refuse(sent, len(tokens) - sent, "the pane no longer shows the dialog")
                seen = _screen_of(tmux, session)
                raise RouteError(409, {"ok": False, "sent": 0, "refused": len(tokens), "error": (
                    "the %s flow takes several screens: do it in a terminal (tmux -S %s attach"
                    " -t %s)" % (seen, kind.socket, kind.name) if seen in ("login", "onboarding")
                    else "the tmux runner types into this pane: keys go in only while it waits on"
                    " a person at the trust, bypass or MCP approval dialog; write to the cousin"
                    " through the chat")})
            _send_tokens(tmux, session, [(mode, payload)])
            sent += 1
            if mode == "key" and payload in ENDS:
                ended[key] = time.monotonic()
        if rest:
            _refuse(sent, len(rest), "a request ends at its first Enter or Escape, where the"
                                     " screen changes")
    return sent


def _lines(query):
    try:
        return max(1, int(query.get("lines") or 200))
    except ValueError:
        raise RouteError(400, {"ok": False, "error": "lines must be an integer"})


def _int(value, lo, hi):
    if isinstance(value, bool) or not isinstance(value, int):
        raise RouteError(400, {"ok": False, "error": "cols and rows must be integers"})
    return max(lo, min(hi, value))


def register():
    @router.route("GET", "/api/pane")
    @guarded
    def pane(req):
        _, session, tmux, _kind = _resolve(req, req.query.get("cousin"))
        return 200, {"text": tmux.capture(session, _lines(req.query))}

    @router.route("GET", "/api/pane/stream")
    @guarded
    def stream(req):
        _, session, tmux, _kind = _resolve(req, req.query.get("cousin"))
        return 200, sse.Stream(stream_pane(session, _lines(req.query),
                                           tmux=tmux))

    @router.route("POST", "/api/pane/input")
    @guarded
    def pane_input(req):
        _, session, tmux, kind = _resolve(req, req.body.get("cousin"))
        data = req.body.get("data")
        if data is not None and not isinstance(data, str):
            raise RouteError(400, {"ok": False, "error": "data must be a string"})
        if kind is not None:
            return 200, {"ok": True, "tokens": send_kind_input(req, tmux, session, kind, data)}
        return 200, {"ok": True, "tokens": send_input(tmux, session, data)}

    @router.route("POST", "/api/pane/resize")
    @guarded
    def resize(req):
        _, session, tmux, kind = _resolve(req, req.body.get("cousin"))
        if kind is not None:
            raise RouteError(409, {"ok": False, "error": (
                "the tmux kind's pane keeps its fixed size (%dx%d): the runner reads its"
                " screen at that size" % (kind.width, kind.height))})
        cols = _int(req.body.get("cols"), 20, 400)
        rows = _int(req.body.get("rows"), 5, 200)
        try:
            r = tmux.run("resize-window", "-t", "%s:0" % session,
                         "-x", str(cols), "-y", str(rows))
        except (OSError, subprocess.TimeoutExpired) as err:
            raise RouteError(500, {"ok": False, "error": str(err)})
        if r.returncode != 0:
            raise RouteError(500, {"ok": False,
                                   "error": (r.stderr or "").strip()[:200]})
        return 200, {"ok": True, "cols": cols, "rows": rows}


register()
