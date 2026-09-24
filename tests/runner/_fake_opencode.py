"""A fake `opencode serve` (1.18.31) for the default suite (R15): no node,
no binary, no network.

It implements exactly the v1 routes the runner uses (R1), behind the same
basic auth (user `opencode`), and emits the event shapes and orders
measured on the real server (the phase 9 survey, section 3, and the
queue and abort measurements recorded with Task 2):

  GET  /global/health            {"healthy": true, "version": "1.18.31"}
  POST /session                  a Session, `id` "ses_..."; session.created
  POST /session/{id}/prompt_async  204; the turn runs on a thread, on SSE
  POST /session/{id}/abort       true (also when idle or unknown)
  GET  /session/{id}/message     [{info, parts}]
  GET  /event                    SSE `data: {...}\\n\\n`, server.connected
                                 first, server.heartbeat every `heartbeat` s
  POST /permission/{id}/reply    true; 400 on a bad reply, 404 unknown id
  GET  /config                   the config it was given
  GET  /mcp                      the MCP status it was given

Each prompt consumes the next script (a list of steps); when the scripts
run out a turn says "ok". Steps:

  ("text", s)                         a text part, streamed as deltas
  ("reasoning", s)                    a reasoning part, streamed as deltas
  ("tool", name, input, output)       pending -> running {input} -> completed
  ("tool_error", name, input, error)  pending -> running {input} -> error
  ("ASK", name, input, output)        running, then permission.asked; waits
                                      for POST /permission/{id}/reply
  ("SLOW", seconds)                   silence; ends early on abort
  ("HANG",)                           silence until abort
  ("FAIL", error_name, message)       session.error {name, data: {message}}
  ("AUTH_401",)                       the measured APIError 401

Measured semantics it keeps: a tool call ends the assistant message
(finish "tool-calls") and the next step starts a new one; a turn ends with
`session.status idle` then `session.idle`; a failure or an abort sends
session.error first and the idle pair twice; a prompt sent while the
session is busy is stored at once and answered by the SAME run after the
current one (one session.idle for both); an abort drops a queued prompt.
"""
import base64
import hmac
import itertools
import json
import os
import queue
import re
import secrets
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

VERSION = "1.18.31"
USERNAME = "opencode"
TOKENS = {"total": 14, "input": 11, "output": 3, "reasoning": 0,
          "cache": {"write": 0, "read": 0}}
ZERO_TOKENS = {"input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}}
DEFAULT_SCRIPT = [("text", "ok")]
REJECTED = ("The user rejected permission to use this specific tool call.")
AUTH_401 = {"name": "APIError", "data": {
    "message": "Incorrect API key provided", "statusCode": 401, "isRetryable": False,
    "responseHeaders": {"content-type": "application/json"},
    "responseBody": json.dumps({"error": {
        "message": "Incorrect API key provided", "type": "invalid_request_error",
        "code": "invalid_api_key"}}),
    "metadata": {"url": "http://127.0.0.1:9/v1/chat/completions"}}}
_ARITY = {"text": 2, "reasoning": 2, "tool": 4, "tool_error": 4, "ASK": 4, "SLOW": 2,
          "HANG": 1, "FAIL": 3, "AUTH_401": 1}
_ROUTES = [
    ("GET", re.compile(r"^/global/health$"), "health"),
    ("POST", re.compile(r"^/session$"), "create"),
    ("POST", re.compile(r"^/session/([^/]+)/prompt_async$"), "prompt"),
    ("POST", re.compile(r"^/session/([^/]+)/abort$"), "abort"),
    ("GET", re.compile(r"^/session/([^/]+)/message$"), "messages"),
    ("GET", re.compile(r"^/event$"), "event"),
    ("POST", re.compile(r"^/permission/([^/]+)/reply$"), "reply"),
    ("GET", re.compile(r"^/config$"), "config"),
    ("GET", re.compile(r"^/mcp$"), "mcp"),
]
_CLOSE = object()


def _now_ms():
    return int(time.time() * 1000)


def _copy(value):
    return json.loads(json.dumps(value))


def _compile(script):
    steps = [tuple(step) if not isinstance(step, str) else (step,) for step in script]
    for step in steps:
        if not step or step[0] not in _ARITY or len(step) != _ARITY[step[0]]:
            raise ValueError("unknown or malformed fake opencode step: %r" % (step,))
    return steps


def _chunks(text):
    return re.findall(r"\s*\S+\s*", text) or ([text] if text else [])


class _Session:
    def __init__(self, info):
        self.info = info
        self.busy = False
        self.pending = []
        self.abort = threading.Event()
        self.step = None        # the open step's snapshot hash, None before step-start


class FakeOpencode:
    """One fake server on 127.0.0.1:<port> (0 = a free one). `scripts` is a
    list of turns, each a list of steps. `start()` serves; `close()` stops
    it and every turn thread. `requests` records every request, `events`
    every bus event (not the per-connection server.connected/heartbeat)."""

    def __init__(self, scripts=(), *, password="pw", config=None, mcp=None, heartbeat=10.0,
                 tokens=None, directory=None, port=0):
        self.password = password
        self.config = _copy(config if config is not None else {})
        self.mcp = _copy(mcp if mcp is not None else {})
        self.heartbeat = heartbeat
        self.tokens = _copy(tokens if tokens is not None else TOKENS)
        self.directory = directory or os.getcwd()
        self.requests = []
        self.events = []
        self.aborts = []
        self._scripts = [_compile(s) for s in scripts]
        self._sessions = {}
        self._messages = {}
        self._permissions = {}
        self._subscribers = []
        self._threads = []
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self._closing = threading.Event()
        self._ids = itertools.count(1)
        self._httpd = ThreadingHTTPServer(("127.0.0.1", port), self._handler())
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self.url = "http://127.0.0.1:%d" % self.port
        self._serve = None

    # -- lifecycle ---------------------------------------------------------

    def start(self):
        self._serve = threading.Thread(target=self._httpd.serve_forever, kwargs={
            "poll_interval": 0.05}, name="fake-opencode", daemon=True)
        self._serve.start()
        return self

    def serve_forever(self):
        """For the fake binary: serve on this thread until close()."""
        self._httpd.serve_forever(poll_interval=0.05)

    def close(self):
        if self._closing.is_set():
            return
        self._closing.set()
        with self._lock:
            for q in list(self._subscribers):
                q.put(_CLOSE)
            self._changed.notify_all()
        if self._serve is not None:
            self._httpd.shutdown()
        self._httpd.server_close()
        for t in list(self._threads):
            t.join(5)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()
        return False

    # -- test helpers ------------------------------------------------------

    def add_script(self, script):
        with self._lock:
            self._scripts.append(_compile(script))

    def events_since(self, index):
        with self._lock:
            return _copy(self.events[index:])

    def wait_event(self, type_, *, after=0, pred=None, timeout=5.0):
        """The first bus event at index >= `after` of `type_` (and `pred`);
        AssertionError when none arrives within `timeout`."""
        deadline = time.monotonic() + timeout
        with self._changed:
            while True:
                for event in self.events[after:]:
                    if event["type"] == type_ and (pred is None or pred(event)):
                        return _copy(event)
                left = deadline - time.monotonic()
                if left <= 0:
                    raise AssertionError("fake opencode: no %s event within %ss" % (type_, timeout))
                self._changed.wait(min(left, 0.1))

    def wait_subscribers(self, n, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if len(self._subscribers) == n:
                    return True
            time.sleep(0.01)
        return False

    def settle(self, timeout=5.0):
        """Wait for every turn thread to finish; True when they all did."""
        deadline = time.monotonic() + timeout
        for t in list(self._threads):
            t.join(max(0.0, deadline - time.monotonic()))
        return not any(t.is_alive() for t in self._threads)

    def drop_event_streams(self):
        """Close every open /event connection, as a dropped stream."""
        with self._lock:
            for q in list(self._subscribers):
                q.put(_CLOSE)

    # -- the bus -----------------------------------------------------------

    def _id(self, prefix):
        return "%s_%012x%s" % (prefix, int(time.time() * 1000) * 4096 + next(self._ids) % 4096,
                               secrets.token_hex(7))

    def _frame(self, type_, properties):
        return {"id": self._id("evt"), "type": type_, "properties": _copy(properties)}

    def _emit(self, type_, properties):
        event = self._frame(type_, properties)
        with self._lock:
            self.events.append(event)
            for q in self._subscribers:
                q.put(event)
            self._changed.notify_all()
        return event

    def _status(self, sid, kind):
        self._emit("session.status", {"sessionID": sid, "status": {"type": kind}})

    def _idle(self, sid):
        self._status(sid, "idle")
        self._emit("session.idle", {"sessionID": sid})

    # -- messages ----------------------------------------------------------

    def _store(self, sid, info):
        with self._lock:
            self._messages[sid].append({"info": info, "parts": []})

    def _message_updated(self, sid, info):
        with self._lock:
            for m in self._messages[sid]:
                if m["info"]["id"] == info["id"]:
                    m["info"] = _copy(info)
        self._emit("message.updated", {"sessionID": sid, "info": info})

    def _part_updated(self, sid, part):
        with self._lock:
            for m in self._messages[sid]:
                if m["info"]["id"] == part["messageID"]:
                    for i, old in enumerate(m["parts"]):
                        if old["id"] == part["id"]:
                            m["parts"][i] = _copy(part)
                            break
                    else:
                        m["parts"].append(_copy(part))
        self._emit("message.part.updated", {"sessionID": sid, "part": part, "time": _now_ms()})

    def _default_model(self):
        name = self.config.get("model") if isinstance(self.config.get("model"), str) else ""
        provider, _, model = name.partition("/")
        if provider and model:
            return {"providerID": provider, "modelID": model}
        return {"providerID": "fake", "modelID": "m1"}

    def _new_user(self, sid, body):
        text = "".join(p.get("text", "") for p in body.get("parts", [])
                       if isinstance(p, dict) and p.get("type") == "text")
        info = {"id": self._id("msg"), "sessionID": sid, "role": "user",
                "time": {"created": _now_ms()}, "agent": body.get("agent") or "build",
                "model": body.get("model") or self._default_model()}
        if body.get("system") is not None:
            info["system"] = body["system"]
        part = {"id": self._id("prt"), "sessionID": sid, "messageID": info["id"],
                "type": "text", "text": text}
        self._store(sid, info)
        with self._lock:
            self._messages[sid][-1]["parts"].append(_copy(part))
        return info, part

    def _announce_user(self, sid, user):
        info, part = user
        self._emit("message.updated", {"sessionID": sid, "info": info})
        self._emit("message.part.updated", {"sessionID": sid, "part": part, "time": _now_ms()})

    def _new_assistant(self, sid, user_info):
        model = user_info["model"]
        info = {"id": self._id("msg"), "sessionID": sid, "parentID": user_info["id"],
                "role": "assistant", "mode": user_info["agent"], "agent": user_info["agent"],
                "path": {"cwd": self.directory, "root": self.directory}, "cost": 0,
                "tokens": _copy(ZERO_TOKENS), "modelID": model["modelID"],
                "providerID": model["providerID"], "time": {"created": _now_ms()}}
        self._store(sid, info)
        self._emit("message.updated", {"sessionID": sid, "info": info})
        return info

    # -- the turn engine ---------------------------------------------------

    def _next_script(self):
        with self._lock:
            return self._scripts.pop(0) if self._scripts else list(DEFAULT_SCRIPT)

    def _sleep(self, state, seconds):
        """Silence for `seconds` (None: forever); True when aborted or closing."""
        deadline = None if seconds is None else time.monotonic() + seconds
        while True:
            if self._closing.is_set():
                return True
            left = 0.05 if deadline is None else min(0.05, deadline - time.monotonic())
            if left <= 0:
                return state.abort.is_set()
            if state.abort.wait(left):
                return True

    def _run(self, sid, user):
        state = self._sessions[sid]
        self._announce_user(sid, user)
        self._status(sid, "busy")
        while True:
            outcome = self._play(sid, state, user[0], self._next_script())
            if outcome != "ok":
                return
            with self._lock:
                if state.pending and not state.abort.is_set():
                    user = state.pending.pop(0)
                else:
                    self._status(sid, "busy")
                    self._idle(sid)
                    last = _copy(user[0])
                    last["summary"] = {"diffs": []}
                    self._emit("message.updated", {"sessionID": sid, "info": last})
                    state.busy = False
                    return
            self._status(sid, "busy")

    def _fail(self, sid, state, info, error):
        """The measured failure tail: session.error, the idle pair, the
        assistant message with its error, the idle pair again."""
        with self._lock:
            self._emit("session.error", {"sessionID": sid, "error": error})
            self._idle(sid)
            info["error"] = error
            info["time"]["completed"] = _now_ms()
            self._message_updated(sid, info)
            self._idle(sid)
            state.pending.clear()
            state.busy = False

    def _finish(self, sid, state, info, reason):
        start = state.step
        if start is not None:
            self._part_updated(sid, {"id": self._id("prt"), "sessionID": sid,
                                     "messageID": info["id"], "type": "step-finish",
                                     "reason": reason, "snapshot": start, "tokens": self.tokens,
                                     "cost": 0})
        info["finish"] = reason
        if start is not None:
            info["tokens"] = _copy(self.tokens)
        self._message_updated(sid, info)
        info["time"]["completed"] = _now_ms()
        self._message_updated(sid, info)

    def _open_step(self, sid, state, info):
        if state.step is None:
            state.step = secrets.token_hex(20)
            self._part_updated(sid, {"id": self._id("prt"), "sessionID": sid,
                                     "messageID": info["id"], "type": "step-start",
                                     "snapshot": state.step})

    def _stream_part(self, sid, state, info, kind, text):
        self._open_step(sid, state, info)
        part = {"id": self._id("prt"), "sessionID": sid, "messageID": info["id"], "type": kind,
                "text": "", "time": {"start": _now_ms()}}
        self._part_updated(sid, part)
        for chunk in _chunks(text):
            self._emit("message.part.delta", {"sessionID": sid, "messageID": info["id"],
                                              "partID": part["id"], "field": "text",
                                              "delta": chunk})
        part["text"] = text
        part["time"]["end"] = _now_ms()
        self._part_updated(sid, part)

    def _tool(self, sid, state, info, step):
        kind, name, args, result = step
        self._open_step(sid, state, info)
        part = {"id": self._id("prt"), "sessionID": sid, "messageID": info["id"], "type": "tool",
                "tool": name, "callID": "call_%d" % (_now_ms() * 1000 + next(self._ids) % 1000),
                "state": {"status": "pending", "input": {}, "raw": ""}}
        self._part_updated(sid, part)
        started = _now_ms()
        part["state"] = {"status": "running", "input": args, "time": {"start": started}}
        self._part_updated(sid, part)
        ok = kind == "tool"
        if kind == "ASK":
            reply = self._ask(sid, state, info, part, name, args)
            if reply is None:
                return False
            ok = reply != "reject"
            result = result if ok else REJECTED
        if ok:
            part["state"] = {"status": "completed", "input": args, "output": result, "title": "",
                             "metadata": {"truncated": False},
                             "time": {"start": started, "end": _now_ms()}}
        else:
            part["state"] = {"status": "error", "input": args, "error": result,
                             "time": {"start": started, "end": _now_ms()}}
        self._part_updated(sid, part)
        return True

    def _ask(self, sid, state, info, part, name, args):
        pattern = args.get("command") if isinstance(args, dict) and "command" in args \
            else json.dumps(args, sort_keys=True)
        request = {"id": self._id("per"), "sessionID": sid, "permission": name,
                   "patterns": [pattern], "metadata": args, "always": [pattern],
                   "tool": {"messageID": info["id"], "callID": part["callID"]}}
        answer = {"event": threading.Event(), "reply": None}
        with self._lock:
            self._permissions[request["id"]] = answer
        self._emit("permission.asked", request)
        while not answer["event"].wait(0.05):
            if state.abort.is_set() or self._closing.is_set():
                with self._lock:
                    self._permissions.pop(request["id"], None)
                return None
        self._emit("permission.replied", {"sessionID": sid, "requestID": request["id"],
                                          "reply": answer["reply"]})
        return answer["reply"]

    def _play(self, sid, state, user_info, script):
        info = self._new_assistant(sid, user_info)
        state.step = None
        for step in script:
            if self._closing.is_set():
                return "closed"
            if state.abort.is_set():
                break
            kind = step[0]
            if kind in ("text", "reasoning"):
                self._stream_part(sid, state, info, kind, step[1])
            elif kind in ("tool", "tool_error", "ASK"):
                if not self._tool(sid, state, info, step):
                    break
                self._finish(sid, state, info, "tool-calls")
                self._status(sid, "busy")
                info = self._new_assistant(sid, user_info)
                state.step = None
            elif kind in ("SLOW", "HANG"):
                if self._sleep(state, step[1] if kind == "SLOW" else None):
                    break
            elif kind == "FAIL":
                self._fail(sid, state, info, {"name": step[1], "data": {"message": step[2]}})
                return "error"
            elif kind == "AUTH_401":
                self._fail(sid, state, info, _copy(AUTH_401))
                return "error"
        if self._closing.is_set():
            return "closed"
        if state.abort.is_set():
            self._fail(sid, state, info, {"name": "MessageAbortedError",
                                          "data": {"message": "Aborted"}})
            return "aborted"
        self._open_step(sid, state, info)
        self._finish(sid, state, info, "stop")
        return "ok"

    # -- routes ------------------------------------------------------------

    def _route_health(self, req, match, body):
        return 200, {"healthy": True, "version": VERSION}

    def _route_create(self, req, match, body):
        sid = self._id("ses")
        created = _now_ms()
        title = body.get("title") if isinstance(body, dict) and body.get("title") else (
            "New session - %s" % datetime.now(timezone.utc).isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"))
        info = {"id": sid, "slug": "fake-%s" % sid[-6:].lower(), "projectID": "global",
                "directory": self.directory, "path": "", "cost": 0, "tokens": _copy(ZERO_TOKENS),
                "title": title, "version": VERSION, "time": {"created": created,
                                                             "updated": created}}
        with self._lock:
            self._sessions[sid] = _Session(info)
            self._messages[sid] = []
        self._emit("session.created", {"sessionID": sid, "info": info})
        return 200, info

    def _bad_request(self, message):
        return 400, {"name": "BadRequest", "data": {"message": message, "kind": "Payload"}}

    def _route_prompt(self, req, match, body):
        sid = match.group(1)
        if not isinstance(body, dict):
            return self._bad_request("Expected object, got %s" % json.dumps(body))
        if sid not in self._sessions:
            return 404, {"name": "NotFoundError", "data": {"message": "Session not found: %s" % sid}}
        if not isinstance(body.get("parts"), list):
            return self._bad_request('Expected array, got %s\n  at ["parts"]'
                                     % json.dumps(body.get("parts")))
        model = body.get("model")
        if model is not None and not (isinstance(model, dict)
                                      and isinstance(model.get("providerID"), str)
                                      and isinstance(model.get("modelID"), str)):
            return self._bad_request('Expected object | null, got %s\n  at ["model"]'
                                     % json.dumps(model))
        for key in ("system", "agent"):
            if body.get(key) is not None and not isinstance(body[key], str):
                return self._bad_request('Expected string, got %s\n  at ["%s"]'
                                         % (json.dumps(body[key]), key))
        with self._lock:
            state = self._sessions[sid]
            user = self._new_user(sid, body)
            if state.busy:
                state.pending.append(user)
                self._announce_user(sid, user)
                return 204, None
            state.busy = True
            state.abort.clear()
            t = threading.Thread(target=self._run, args=(sid, user), name="fake-opencode-turn",
                                 daemon=True)
            self._threads.append(t)
        t.start()
        return 204, None

    def _route_abort(self, req, match, body):
        sid = match.group(1)
        with self._lock:
            self.aborts.append(sid)
            state = self._sessions.get(sid)
            if state is not None and state.busy:
                state.abort.set()
            elif state is not None:
                self._idle(sid)
        return 200, True

    def _route_messages(self, req, match, body):
        sid = match.group(1)
        with self._lock:
            if sid not in self._messages:
                return 404, {"name": "NotFoundError",
                             "data": {"message": "Session not found: %s" % sid}}
            return 200, _copy(self._messages[sid])

    def _route_reply(self, req, match, body):
        rid = match.group(1)
        reply = body.get("reply") if isinstance(body, dict) else None
        if reply not in ("once", "always", "reject"):
            return self._bad_request('Expected "once" | "always" | "reject", got %s\n'
                                     '  at ["reply"]' % json.dumps(reply))
        with self._lock:
            answer = self._permissions.pop(rid, None)
        if answer is None:
            return 404, {"_tag": "PermissionNotFoundError", "requestID": rid,
                         "message": "Permission request not found: %s" % rid}
        answer["reply"] = reply
        answer["event"].set()
        return 200, True

    def _route_config(self, req, match, body):
        return 200, self.config

    def _route_mcp(self, req, match, body):
        return 200, self.mcp

    # -- HTTP --------------------------------------------------------------

    def _authorized(self, header):
        if not header or not header.startswith("Basic "):
            return False
        try:
            user, _, pw = base64.b64decode(header[6:]).decode().partition(":")
        except (ValueError, UnicodeDecodeError):
            return False
        return (hmac.compare_digest(user, USERNAME)
                and hmac.compare_digest(pw.encode(), str(self.password).encode()))

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _send(self, status, payload, headers=()):
                data = b"" if payload is None else json.dumps(payload).encode()
                self.send_response(status)
                for k, v in headers:
                    self.send_header(k, v)
                if payload is not None:
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _dispatch(self, method):
                split = urlsplit(self.path)
                path = unquote(split.path)
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                authorized = fake._authorized(self.headers.get("Authorization"))
                try:
                    body = json.loads(raw) if raw else {}
                    bad_json = False
                except ValueError:
                    body, bad_json = raw.decode("utf-8", "replace"), True
                fake.requests.append({"method": method, "path": path, "query": split.query,
                                      "authorized": authorized,
                                      "headers": {k.lower(): v for k, v in self.headers.items()},
                                      "body": body if raw else None})
                if not authorized:
                    return self._send(401, None, [("www-authenticate",
                                                   'Basic realm="Secure Area"')])
                if bad_json:
                    return self._send(500, {"name": "UnknownError", "data": {
                        "message": "Unexpected server error. Check server logs for details.",
                        "ref": fake._id("err")[:12]}})
                for m, pattern, name in _ROUTES:
                    match = pattern.match(path)
                    if match and m == method:
                        if name == "event":
                            return self._event_stream()
                        status, payload = getattr(fake, "_route_" + name)(self, match, body)
                        return self._send(status, payload)
                return self._send(404, {"name": "NotFoundError",
                                        "data": {"message": "Not found: %s" % path}})

            def do_GET(self):
                self._dispatch("GET")

            def do_POST(self):
                self._dispatch("POST")

            def _chunk(self, event):
                data = ("data: %s\n\n" % json.dumps(event)).encode()
                self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
                self.wfile.flush()

            def _event_stream(self):
                self.close_connection = True
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache, no-transform")
                self.send_header("x-accel-buffering", "no")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                q = queue.Queue()
                try:
                    self._chunk(fake._frame("server.connected", {}))
                    with fake._lock:
                        fake._subscribers.append(q)
                    while not fake._closing.is_set():
                        try:
                            event = q.get(timeout=fake.heartbeat)
                        except queue.Empty:
                            event = fake._frame("server.heartbeat", {})
                        if event is _CLOSE:
                            break
                        self._chunk(event)
                except OSError:
                    pass
                finally:
                    with fake._lock:
                        if q in fake._subscribers:
                            fake._subscribers.remove(q)

        return Handler


def main(argv=None):
    """A fake `opencode` binary: `serve --hostname H --port P` serves this
    fake until SIGTERM. The password is OPENCODE_SERVER_PASSWORD, the
    config the JSON file OPENCODE_CONFIG names. For OpencodeServer's own
    tests, FAKE_OPENCODE_ENV_DUMP names a file that receives the argv, cwd,
    process group and environment; FAKE_OPENCODE_MODE=mute never serves,
    =exit exits at once with a message, =ignore-term ignores SIGTERM."""
    import argparse
    import signal
    args = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(prog="opencode")
    parser.add_argument("command")
    parser.add_argument("--hostname", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    opts = parser.parse_args(args)
    dump = os.environ.get("FAKE_OPENCODE_ENV_DUMP")
    if dump:
        with open(dump, "w") as f:
            json.dump({"argv": args, "cwd": os.getcwd(), "pid": os.getpid(),
                       "pgid": os.getpgid(0), "env": dict(os.environ)}, f)
    mode = os.environ.get("FAKE_OPENCODE_MODE", "")
    if mode == "exit":
        print("Error: fake opencode refuses to start", flush=True)
        return 3
    if mode == "ignore-term":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if mode == "mute":
        while True:
            time.sleep(1)
    config = {}
    if os.environ.get("OPENCODE_CONFIG"):
        with open(os.environ["OPENCODE_CONFIG"]) as f:
            config = json.load(f)
    fake = FakeOpencode(password=os.environ.get("OPENCODE_SERVER_PASSWORD", ""), config=config,
                        port=opts.port)
    if mode != "ignore-term":
        signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
    print("opencode server listening on %s" % fake.url, flush=True)
    fake.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
