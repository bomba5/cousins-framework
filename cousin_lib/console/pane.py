"""The pane: the cousin's tmux session rendered in the browser and typed
into (docs/console-spec.md, "The pane").

Four routes over one session resolution: the session name from the
registry (`[chat] tmux_session`, default the slug), the binary and
socket from the console's flags (`req.tmux_bin`, `req.tmux_socket`),
`ssh <host>` in front for a cousin with `[chat] host`. `404` unknown
cousin, `400` no session configured, `409` session not running.

The stream polls `capture-pane` (the contract leaves the byte source
open; polling needs no tap file to clean up and works identically for a
remote cousin): a `pane` frame on connect and on every change, `geom`
plus a fresh `pane` when the geometry changes, `heartbeat` after 3 s of
silence, `: tick` between polls. Frames, geometry and cursor come from
injectable callables so the stream is tested without tmux.

Input goes through the injection module's process-wide lock, so a chat
delivery and a keystroke never interleave.
"""
from __future__ import annotations

import re
import subprocess
import time
from datetime import datetime

from cousin_lib.console import router, sse
from cousin_lib.console.proxy import RouteError, find_cousin, guarded
from cousin_lib.server import injection

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

    def capture(self, session, lines):
        r = self.run("capture-pane", "-p", "-e", "-t", session,
                     "-S", "-%d" % int(lines), timeout=6)
        if r.returncode != 0:
            raise RouteError(500, {"ok": False,
                                   "error": (r.stderr or "").strip()[:200]})
        return trim_trailing_blank(r.stdout or "")

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

    def cursor(self, session):
        parts = self._display(session,
                              "#{cursor_y} #{cursor_x} #{pane_height}")
        return (parts[0], parts[1], parts[2]) if len(parts) >= 3 else None


def trim_trailing_blank(text):
    """Drop trailing lines that are empty once escapes are stripped, so
    the content sits at the bottom of the view."""
    lines = text.split("\n")
    while lines and not _ANSI_RE.sub("", lines[-1]).strip():
        lines.pop()
    return "\n".join(lines)


def input_tokens(data):
    """xterm bytes -> [(mode, payload)]: 'key' for a tmux key name,
    'literal' for text sent with `-l`. Any CSI or SS3 sequence not in the
    table is consumed and dropped, never forwarded as Escape; SGR mouse
    reports are forwarded literally so a full-screen program can
    scroll."""
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
                        out.append(("literal", seq))
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
    """Type the bytes into the session under the injection lock. Returns
    the token count; raises RouteError(500) with tmux's stderr."""
    tokens = input_tokens(data or "")
    with injection._INJECT_LOCK:
        for mode, payload in tokens:
            if mode == "literal":
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
    return len(tokens)


def _iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def stream_pane(session, lines, *, tmux, capture=None, geometry=None,
                cursor=None, clock=time.monotonic, sleep=time.sleep,
                poll=0.5, heartbeat_after=3.0, geom_every=2.0):
    """Generator of SSE bytes for one pane. `capture(lines) -> str`,
    `geometry() -> (cols, rows)` and `cursor() -> (cy, cx, pane_height)
    | None` default to tmux; tests inject them."""
    capture = capture or (lambda n: tmux.capture(session, n))
    geometry = geometry or (lambda: tmux.geometry(session))
    cursor = cursor or (lambda: tmux.cursor(session))

    def snapshot():
        text = capture(lines)
        pos = cursor()
        if pos:
            cy, cx, ph = pos
            # the viewport's last row is line lines-1 of the snapshot
            row = max(0, min(lines - 1, lines - ph + cy))
            text += "\x1b[%d;%dH" % (row + 1, cx + 1)
        return text

    last_frame = snapshot()
    yield sse.event_frame("pane", {"text": last_frame, "ts": _iso(),
                                   "changed": True})
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
                yield sse.event_frame("pane", {"text": last_frame,
                                               "ts": _iso(),
                                               "changed": True})
                last_output = now
                continue
        frame = snapshot()
        if frame != last_frame:
            last_frame = frame
            last_output = now
            yield sse.event_frame("pane", {"text": frame, "ts": _iso(),
                                           "changed": True})
        elif now - last_output >= heartbeat_after:
            last_output = now
            yield sse.event_frame("heartbeat", {"ts": _iso()})
        else:
            yield b": tick\n\n"


def _tmux_for(req, cousin):
    return Tmux(getattr(req, "tmux_bin", None) or "tmux",
                socket=getattr(req, "tmux_socket", None),
                host=cousin.chat_host)


def _resolve(req, slug):
    """(cousin, session, tmux) or a RouteError per the contract."""
    cousin = find_cousin(req, slug)
    session = cousin.tmux_session
    if not session:
        raise RouteError(400, {"ok": False,
                               "error": "no tmux session configured"})
    tmux = _tmux_for(req, cousin)
    if not tmux.has_session(session):
        raise RouteError(409, {"ok": False, "error": "session not running"})
    return cousin, session, tmux


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
        _, session, tmux = _resolve(req, req.query.get("cousin"))
        return 200, {"text": tmux.capture(session, _lines(req.query))}

    @router.route("GET", "/api/pane/stream")
    @guarded
    def stream(req):
        _, session, tmux = _resolve(req, req.query.get("cousin"))
        return 200, sse.Stream(stream_pane(session, _lines(req.query),
                                           tmux=tmux))

    @router.route("POST", "/api/pane/input")
    @guarded
    def pane_input(req):
        _, session, tmux = _resolve(req, req.body.get("cousin"))
        data = req.body.get("data")
        if data is not None and not isinstance(data, str):
            raise RouteError(400, {"ok": False, "error": "data must be a string"})
        return 200, {"ok": True, "tokens": send_input(tmux, session, data)}

    @router.route("POST", "/api/pane/resize")
    @guarded
    def resize(req):
        _, session, tmux = _resolve(req, req.body.get("cousin"))
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
