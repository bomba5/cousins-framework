"""opencode's v1 HTTP API, for OpencodeRunner (phase 9 R1, R3). Stdlib only.

OpencodeServer   one `opencode serve` child on loopback with a fresh
                 basic-auth password per start, in its own process group;
                 start() waits (bounded) for GET /global/health, stop()
                 sends SIGTERM to the group, then SIGKILL.
OpencodeClient   the v1 routes the runner uses: session, prompt_async,
                 abort, messages, permission reply, config, mcp. Every
                 call is one bounded request; a failure is OpencodeError
                 with the HTTP status (None when nothing answered) and an
                 excerpt of the body.
EventReader      a thread reading GET /event: each `data:` frame becomes a
                 dict passed to on_event. It reconnects with backoff when
                 the stream drops and never raises into its caller. Every
                 (re)connection starts with a `server.connected` event: a
                 consumer that sees a second one knows it may have missed
                 events in between.

Measured on 1.18.31 (the phase 9 survey): a prompt is only accepted by
prompt_async (204); its progress, its end (`session.idle`) and its
failure (`session.error`) arrive on the event stream.
"""
import base64
import http.client
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import threading
import time
from collections import deque
from urllib.parse import quote, urlsplit

from cousin_lib import accounts

USERNAME = "opencode"
HOST = "127.0.0.1"
# Set on every start, over anything inherited (R3, R5).
FIXED_ENV = {"OPENCODE_DISABLE_AUTOUPDATE": "1", "OPENCODE_DISABLE_SHARE": "1",
             "OPENCODE_DISABLE_CLAUDE_CODE": "1", "OPENCODE_DISABLE_PROJECT_CONFIG": "1"}
# Inherited variables that would undo what the runner sets: a username the
# client does not send, and config sources merged over the rendered file.
DROPPED_ENV = ("OPENCODE_SERVER_USERNAME", "OPENCODE_CONFIG_CONTENT", "OPENCODE_CONFIG_DIR")
EXCERPT = 300


class OpencodeError(Exception):
    """A failed call or start. `status` is the HTTP status, None when no
    response arrived; `body` an excerpt of the response body."""

    def __init__(self, message, *, status=None, body=""):
        super().__init__(message)
        self.status = status
        self.body = body


def _basic(password):
    return "Basic " + base64.b64encode(("%s:%s" % (USERNAME, password)).encode()).decode()


def _excerpt(text):
    text = " ".join(text.split())
    return text if len(text) <= EXCERPT else text[:EXCERPT] + "..."


def _free_port():
    s = socket.socket()
    try:
        s.bind((HOST, 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _split(url):
    parts = urlsplit(url)
    return parts.hostname or HOST, parts.port or 80, parts.path.rstrip("/")


def _model(model):
    """`"<provider>/<model>"` (split at the first slash) or a dict, as the
    prompt body's {providerID, modelID}."""
    if isinstance(model, dict):
        return {"providerID": model["providerID"], "modelID": model["modelID"]}
    provider, _, name = str(model).partition("/")
    if not provider or not name:
        raise ValueError("model must be <provider>/<model>, got %r" % (model,))
    return {"providerID": provider, "modelID": name}


class OpencodeServer:
    """`<argv0> serve --hostname 127.0.0.1 --port <free>` in `cwd`, with
    `env` scrubbed of every Claude credential (accounts.scrub) plus
    OPENCODE_SERVER_PASSWORD, OPENCODE_CONFIG=`config_path` and FIXED_ENV.
    The caller's `env` carries HOME and the XDG directories (R3)."""

    def __init__(self, argv0, *, cwd, env, config_path, timeout=30):
        self.argv0 = str(argv0)
        self.cwd = str(cwd)
        self.env = dict(env)
        self.config_path = str(config_path)
        self.timeout = timeout
        self.password = None
        self.port = None
        self.url = None
        self.proc = None
        self._output = deque(maxlen=200)
        self._pump = None

    @property
    def pid(self):
        return self.proc.pid if self.proc is not None else None

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def output(self):
        """The last lines the child printed (stdout and stderr)."""
        return "\n".join(self._output)

    def child_env(self):
        env = accounts.scrub(self.env)
        for name in DROPPED_ENV:
            env.pop(name, None)
        env.update(FIXED_ENV)
        env["OPENCODE_SERVER_PASSWORD"] = self.password
        env["OPENCODE_CONFIG"] = self.config_path
        return env

    def _binary(self, env):
        if os.sep in self.argv0:
            found = self.argv0 if os.access(self.argv0, os.X_OK) else None
        else:
            found = shutil.which(self.argv0, path=env.get("PATH", os.defpath))
        if not found:
            raise OpencodeError("opencode binary not found or not executable: %s" % self.argv0)
        return found

    def start(self):
        if self.alive():
            return self
        self.password = secrets.token_urlsafe(24)
        self.port = _free_port()
        self.url = "http://%s:%d" % (HOST, self.port)
        env = self.child_env()
        argv = [self._binary(env), "serve", "--hostname", HOST, "--port", str(self.port)]
        self._output.clear()
        try:
            self.proc = subprocess.Popen(argv, cwd=self.cwd, env=env, stdin=subprocess.DEVNULL,
                                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         start_new_session=True)
        except OSError as err:
            raise OpencodeError("cannot start %s: %s" % (argv[0], err)) from err
        self._pump = threading.Thread(target=self._read_output, args=(self.proc.stdout,),
                                      name="opencode-serve-output", daemon=True)
        self._pump.start()
        try:
            self._wait_healthy()
        except BaseException:
            self.stop()
            raise
        return self

    def _read_output(self, stream):
        try:
            for line in stream:
                self._output.append(line.decode("utf-8", "replace").rstrip("\n"))
        except (OSError, ValueError):
            pass

    def _wait_healthy(self):
        client = OpencodeClient(self.url, self.password)
        deadline = time.monotonic() + self.timeout
        while True:
            if self.proc.poll() is not None:
                if self._pump is not None:
                    self._pump.join(1)
                raise OpencodeError("opencode serve exited with %s before it was healthy: %s"
                                    % (self.proc.returncode, _excerpt(self.output())))
            left = deadline - time.monotonic()
            if left <= 0:
                raise OpencodeError("opencode serve was not healthy within %ss: %s"
                                    % (self.timeout, _excerpt(self.output())))
            try:
                client.request("GET", "/global/health", timeout=max(0.1, min(2.0, left)))
                return
            except OpencodeError as err:
                if err.status == 401:
                    raise OpencodeError("opencode serve refused its own password") from err
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))

    def stop(self, timeout=5):
        """SIGTERM to the child's process group, SIGKILL after `timeout`
        seconds; then SIGKILL to anything left in the group. Idempotent;
        returns the exit status (None when it never started)."""
        proc = self.proc
        if proc is None:
            return None
        if proc.poll() is None:
            self._signal(proc, signal.SIGTERM)
            try:
                proc.wait(timeout)
            except subprocess.TimeoutExpired:
                self._signal(proc, signal.SIGKILL)
                try:
                    proc.wait(5)
                except subprocess.TimeoutExpired:
                    pass
        self._signal(proc, signal.SIGKILL)       # what it started and left behind
        if self._pump is not None:
            self._pump.join(1)
        if proc.stdout is not None and not (self._pump and self._pump.is_alive()):
            proc.stdout.close()
        return proc.returncode

    @staticmethod
    def _signal(proc, sig):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
        return False


class OpencodeClient:
    """The v1 routes (R1). Each call opens one connection, bounded by
    `timeout` seconds."""

    def __init__(self, url, password, *, timeout=10.0):
        self.url = url
        self.host, self.port, self.base = _split(url)
        self.timeout = timeout
        self._auth = _basic(password)

    def request(self, method, path, body=None, *, timeout=None):
        conn = http.client.HTTPConnection(self.host, self.port,
                                          timeout=self.timeout if timeout is None else timeout)
        headers = {"Authorization": self._auth, "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        try:
            conn.request(method, self.base + path, body=data, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
        except (OSError, http.client.HTTPException) as err:
            raise OpencodeError("%s %s: %s" % (method, path, str(err) or type(err).__name__)) \
                from err
        finally:
            conn.close()
        text = raw.decode("utf-8", "replace")
        if not 200 <= resp.status < 300:
            raise OpencodeError("%s %s: HTTP %d %s" % (method, path, resp.status, _excerpt(text)),
                                status=resp.status, body=_excerpt(text))
        if not text.strip():
            return None
        try:
            return json.loads(text)
        except ValueError as err:
            raise OpencodeError("%s %s: not JSON: %s" % (method, path, _excerpt(text)),
                                status=resp.status, body=_excerpt(text)) from err

    def health(self):
        return self.request("GET", "/global/health")

    def create_session(self, title=None):
        return self.request("POST", "/session", {"title": title} if title else {})

    def prompt_async(self, session_id, text, *, system=None, model=None, agent=None):
        """Queue one text prompt; 204, the turn itself arrives on /event."""
        body = {"parts": [{"type": "text", "text": text}]}
        if system is not None:
            body["system"] = system
        if model is not None:
            body["model"] = _model(model)
        if agent is not None:
            body["agent"] = agent
        self.request("POST", "/session/%s/prompt_async" % quote(session_id, safe=""), body)

    def abort(self, session_id):
        return self.request("POST", "/session/%s/abort" % quote(session_id, safe=""))

    def messages(self, session_id):
        return self.request("GET", "/session/%s/message" % quote(session_id, safe=""))

    def reply_permission(self, request_id, reply, message=None):
        body = {"reply": reply}
        if message is not None:
            body["message"] = message
        return self.request("POST", "/permission/%s/reply" % quote(request_id, safe=""), body)

    def config(self):
        return self.request("GET", "/config")

    def mcp_status(self):
        return self.request("GET", "/mcp")


def iter_sse(lines):
    """Server-sent events -> the JSON object of each complete `data:` frame.
    Comments, other fields, frames that are not a JSON object, and a last
    frame with no blank line after it are skipped."""
    data = []
    for line in lines:
        if isinstance(line, bytes):
            line = line.decode("utf-8", "replace")
        line = line.rstrip("\r\n")
        if not line:
            if data:
                try:
                    event = json.loads("\n".join(data))
                except ValueError:
                    event = None
                data = []
                if isinstance(event, dict):
                    yield event
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        if field == "data":
            data.append(value[1:] if value.startswith(" ") else value)


class EventReader(threading.Thread):
    """Reads GET /event until `stop_event` is set, calling `on_event(dict)`
    for every frame, in order, on this thread. A dropped, refused or silent
    stream (nothing for `read_timeout` seconds; the server sends a
    heartbeat about every 10 s) is reopened after a backoff that doubles
    from `backoff` to `max_backoff` and resets once a stream delivers.
    `connected` is set while a stream is open, `connects` counts the
    streams that delivered, `last_error` names the last failure. Setting
    `stop_event` closes the open stream at once."""

    def __init__(self, url, password, on_event, *, stop_event, backoff=0.5, max_backoff=5.0,
                 read_timeout=30.0, connect_timeout=5.0):
        super().__init__(name="opencode-events", daemon=True)
        self.host, self.port, self.base = _split(url)
        self.on_event = on_event
        self.stop_event = stop_event
        self.backoff = backoff
        self.max_backoff = max_backoff
        self.read_timeout = read_timeout
        self.connect_timeout = connect_timeout
        self.connects = 0
        self.connected = threading.Event()
        self.last_error = None
        self._auth = _basic(password)
        self._conn = None
        self._lock = threading.Lock()

    def run(self):
        threading.Thread(target=self._close_on_stop, name="opencode-events-stop",
                         daemon=True).start()
        delay = self.backoff
        while not self.stop_event.is_set():
            try:
                if self._stream():
                    delay = self.backoff
            except Exception as err:  # noqa: BLE001 - never raise into the caller
                self.last_error = "%s: %s" % (type(err).__name__, err)
            finally:
                self.connected.clear()
                self._close()
            if self.stop_event.wait(delay):
                break
            delay = min(delay * 2, self.max_backoff)

    def _stream(self):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=self.connect_timeout)
        with self._lock:
            self._conn = conn
        if self.stop_event.is_set():
            return False
        conn.request("GET", self.base + "/event",
                     headers={"Authorization": self._auth, "Accept": "text/event-stream"})
        resp = conn.getresponse()
        if resp.status != 200:
            self.last_error = "GET /event: HTTP %d" % resp.status
            return False
        if conn.sock is not None:
            conn.sock.settimeout(self.read_timeout)
        delivered = False
        for event in iter_sse(resp):
            if not delivered:
                delivered = True
                self.connects += 1
                self.connected.set()
            try:
                self.on_event(event)
            except Exception as err:  # noqa: BLE001 - a consumer's bug is not the stream's
                self.last_error = "on_event: %s: %s" % (type(err).__name__, err)
            if self.stop_event.is_set():
                break
        return delivered

    def _close_on_stop(self):
        self.stop_event.wait()
        self._close()

    def _close(self):
        with self._lock:
            conn, self._conn = self._conn, None
        if conn is None:
            return
        try:
            if conn.sock is not None:
                conn.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        conn.close()
