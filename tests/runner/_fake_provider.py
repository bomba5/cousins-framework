"""A fake OpenAI-compatible provider on loopback: what the real
`opencode serve` talks to in the live tests, so no credential and no
network is ever needed. A reusable module.

It answers `POST /v1/chat/completions` (streaming, as opencode's bundled
`@ai-sdk/openai-compatible` asks, or one JSON object when `stream` is
false) from a script of replies, one reply per request:

  ("text", s)                 an answer, streamed as word deltas
  ("tool", name, args)        one tool call to `name` with `args` (a dict),
                              finish_reason "tool_calls": opencode runs the
                              tool and sends the NEXT request with its result
  ("slow", seconds, s)        the first word at once, then silence (SSE
                              comments every 0.2 s, which notice a client
                              that went away) for `seconds`, then the rest
  ("status", code, message)   an HTTP error with OpenAI's error body (401:
                              `invalid_api_key`, as opencode sees it)

When the script runs out a request is answered ("text", "ok").
A request with no tools is opencode's own auxiliary call (the session
title, made with `small_model`): it is answered with a short title and
never consumes the script, so a test scripts only the agent's turns.

Every request is recorded in `requests` (method, path, headers, body,
the tool names offered, the roles, the last user text, the tool results
sent back, `aux`, and `reply`/`disconnected` once answered). Any other
path is recorded and answered 404, so a test can assert this provider
was the only endpoint called. Stdlib only."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = "m1"
KEY_ERROR = {"401": "invalid_api_key", "403": "permission_denied"}
TITLE = "Fake session title"
_ARITY = {"text": 2, "tool": 3, "slow": 3, "status": 3}


def _compile(reply):
    reply = tuple(reply)
    if not reply or reply[0] not in _ARITY or len(reply) != _ARITY[reply[0]]:
        raise ValueError("unknown or malformed fake provider reply: %r" % (reply,))
    return reply


def _words(text):
    out, word = [], ""
    for ch in text:
        word += ch
        if ch.isspace():
            out.append(word)
            word = ""
    if word:
        out.append(word)
    return out or [""]


def _content(message):
    """A message's text, whether `content` is a string or a list of parts."""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


def summarize(body):
    """The recorded view of one chat request."""
    messages = body.get("messages") if isinstance(body.get("messages"), list) else []
    tools = [((t.get("function") or {}).get("name") or t.get("name"))
             for t in body.get("tools") or [] if isinstance(t, dict)]
    users = [_content(m) for m in messages if m.get("role") == "user"]
    systems = [_content(m) for m in messages if m.get("role") == "system"]
    return {"model": body.get("model"), "stream": body.get("stream"),
            "roles": [m.get("role") for m in messages], "tools": tools,
            "last_user": users[-1] if users else None,
            "users": users,
            "system": "\n".join(systems),
            "tool_results": [{"tool_call_id": m.get("tool_call_id"), "content": _content(m)}
                             for m in messages if m.get("role") == "tool"],
            "aux": not tools}


class FakeProvider:
    """One provider on 127.0.0.1:<port> (0 = a free one). `url` is the base
    URL an opencode `provider` block names (`.../v1`). `start()` serves,
    `close()` stops it and ends every slow reply at once."""

    def __init__(self, script=(), *, model=MODEL, port=0):
        self.model = model
        self.requests = []
        self._script = [_compile(r) for r in script]
        self._lock = threading.Lock()
        self._changed = threading.Condition(self._lock)
        self._closing = threading.Event()
        self._ids = 0
        self._httpd = ThreadingHTTPServer(("127.0.0.1", port), self._handler())
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self.url = "http://127.0.0.1:%d/v1" % self.port
        self._thread = None

    # -- lifecycle -----------------------------------------------------------
    def start(self):
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        kwargs={"poll_interval": 0.05},
                                        name="fake-provider", daemon=True)
        self._thread.start()
        return self

    def close(self):
        if self._closing.is_set():
            return
        self._closing.set()
        with self._changed:
            self._changed.notify_all()
        if self._thread is not None:
            self._httpd.shutdown()
        self._httpd.server_close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()
        return False

    # -- test helpers ----------------------------------------------------------
    def add(self, *replies):
        with self._lock:
            self._script.extend(_compile(r) for r in replies)

    def chats(self, *, aux=False):
        """The chat requests recorded so far (the agent's; `aux=True`: all)."""
        with self._lock:
            return [dict(r) for r in self.requests
                    if r.get("chat") and (aux or not r.get("aux"))]

    def wait(self, pred, timeout=10.0):
        """Wait until `pred(requests)` is true; the requests, or AssertionError."""
        deadline = time.monotonic() + timeout
        with self._changed:
            while True:
                snapshot = [dict(r) for r in self.requests]
                if pred(snapshot):
                    return snapshot
                left = deadline - time.monotonic()
                if left <= 0:
                    raise AssertionError("fake provider: condition not met within %ss; saw %s"
                                         % (timeout, [(r["path"], r.get("last_user"))
                                                      for r in snapshot]))
                self._changed.wait(min(left, 0.1))

    # -- internals -------------------------------------------------------------
    def _next(self, aux):
        with self._lock:
            if aux:
                return ("text", TITLE)
            return self._script.pop(0) if self._script else ("text", "ok")

    def _record(self, entry):
        with self._changed:
            self.requests.append(entry)
            self._changed.notify_all()
            return len(self.requests) - 1

    def _update(self, index, **fields):
        with self._changed:
            self.requests[index].update(fields)
            self._changed.notify_all()

    def _new_id(self, prefix):
        with self._lock:
            self._ids += 1
            return "%s-%d" % (prefix, self._ids)

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"      # a stream ends when the connection closes

            def log_message(self, *args):
                pass

            def _json(self, status, payload):
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _read(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    return json.loads(raw) if raw else {}
                except ValueError:
                    return {"_unparsed": raw.decode("utf-8", "replace")}

            def do_GET(self):
                fake._record({"method": "GET", "path": self.path, "chat": False,
                              "headers": {k.lower(): v for k, v in self.headers.items()},
                              "time": time.time()})
                self._json(404, {"error": {"message": "not found: %s" % self.path}})

            def do_POST(self):
                body = self._read()
                entry = {"method": "POST", "path": self.path, "time": time.time(),
                         "headers": {k.lower(): v for k, v in self.headers.items()},
                         "body": body, "chat": self.path.rstrip("/").endswith("/chat/completions")}
                if not entry["chat"]:
                    fake._record(entry)
                    return self._json(404, {"error": {"message": "not found: %s" % self.path}})
                entry.update(summarize(body if isinstance(body, dict) else {}))
                reply = fake._next(entry["aux"])
                entry["reply"] = list(reply)
                index = fake._record(entry)
                try:
                    self._answer(index, reply, stream=body.get("stream") is not False)
                except OSError:
                    fake._update(index, disconnected=True)

            # -- answers ---------------------------------------------------------
            def _answer(self, index, reply, *, stream):
                kind = reply[0]
                if kind == "status":
                    code, message = reply[1], reply[2]
                    return self._json(code, {"error": {
                        "message": message, "type": "invalid_request_error",
                        "code": KEY_ERROR.get(str(code), "error")}})
                cid = fake._new_id("chatcmpl")
                if not stream:
                    return self._json(200, self._whole(cid, reply))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                if kind == "tool":
                    self._chunk(cid, {"role": "assistant", "tool_calls": [{
                        "index": 0, "id": fake._new_id("call"), "type": "function",
                        "function": {"name": reply[1], "arguments": json.dumps(reply[2])}}]})
                    self._finish(cid, "tool_calls")
                    return
                words = _words(reply[-1])
                if kind == "slow":
                    self._chunk(cid, {"role": "assistant", "content": words[0]})
                    words = words[1:]
                    deadline = time.monotonic() + float(reply[1])
                    while time.monotonic() < deadline:
                        if fake._closing.wait(0.2):
                            return
                        self.wfile.write(b": keepalive\n\n")   # raises once the client left
                        self.wfile.flush()
                for i, word in enumerate(words):
                    delta = {"content": word}
                    if i == 0 and kind != "slow":
                        delta["role"] = "assistant"
                    self._chunk(cid, delta)
                self._finish(cid, "stop")
                fake._update(index, completed=True)

            def _whole(self, cid, reply):
                message = {"role": "assistant", "content": None}
                finish = "stop"
                if reply[0] == "tool":
                    message["tool_calls"] = [{"id": fake._new_id("call"), "type": "function",
                                              "function": {"name": reply[1],
                                                           "arguments": json.dumps(reply[2])}}]
                    finish = "tool_calls"
                else:
                    message["content"] = reply[-1]
                return {"id": cid, "object": "chat.completion", "created": int(time.time()),
                        "model": fake.model, "choices": [{"index": 0, "message": message,
                                                          "finish_reason": finish}],
                        "usage": {"prompt_tokens": 11, "completion_tokens": 3,
                                  "total_tokens": 14}}

            def _send(self, payload):
                self.wfile.write(b"data: " + json.dumps(payload).encode() + b"\n\n")
                self.wfile.flush()

            def _chunk(self, cid, delta, finish=None):
                self._send({"id": cid, "object": "chat.completion.chunk",
                            "created": int(time.time()), "model": fake.model,
                            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]})

            def _finish(self, cid, reason):
                self._chunk(cid, {}, reason)
                self._send({"id": cid, "object": "chat.completion.chunk",
                            "created": int(time.time()), "model": fake.model, "choices": [],
                            "usage": {"prompt_tokens": 11, "completion_tokens": 3,
                                      "total_tokens": 14}})
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

        return Handler
