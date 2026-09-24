"""A stand-in cousin-supervisor for the tests of its clients: a thread
that answers the control protocol (one JSON line in, one JSON line out,
per connection) on `<root>/run/supervisor.sock`, records every request,
and can write the `run/supervisor.json` snapshot the console reads
(holding `run/supervisor.lock`, as a live supervisor does, so that
supervisor.snapshot() believes it)."""
import fcntl
import json
import os
import socket
import threading
from pathlib import Path


def _default_answer(req):
    op = req.get("op")
    name = "runner:%s" % req.get("slug")
    if op == "start":
        return {"ok": True, "name": name, "state": "running"}
    if op == "stop":
        return {"ok": True, "name": name,
                "state": "stopping" if req.get("wait") is False else "stopped"}
    if op == "status":
        return {"ok": True, "pid": os.getpid(), "started": "2026-01-01T00:00:00+00:00",
                "children": {}}
    return {"ok": False, "error": "unknown op %r" % op}


class StubSupervisor:
    """`answers` maps an op to a dict, or to a callable(request) -> dict;
    an op it does not name gets the real supervisor's success shape."""

    def __init__(self, root, answers=None):
        self.root = Path(root)
        self.answers = dict(answers or {})
        self.requests = []
        self._lock_fd = None
        run = self.root / "run"
        run.mkdir(mode=0o700, exist_ok=True)
        self.path = run / "supervisor.sock"
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(str(self.path))
        self._sock.listen(8)
        self._sock.settimeout(0.1)
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True,
                                        name="stub-supervisor")

    def start(self):
        self._thread.start()
        return self

    def close(self):
        self._done.set()
        if self._thread.ident is not None:
            self._thread.join(5)
        self._sock.close()
        self.path.unlink(missing_ok=True)
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None

    def ops(self):
        return [(r.get("op"), r.get("slug")) for r in self.requests]

    def write_snapshot(self, children, pid=None, live=True):
        """The snapshot file; `live` also takes the supervisor's lock (a
        stale file is one no supervisor holds the lock behind)."""
        body = {"ok": True, "pid": os.getpid() if pid is None else pid,
                "started": "2026-01-01T00:00:00+00:00", "children": children}
        (self.root / "run" / "supervisor.json").write_text(json.dumps(body))
        if live and self._lock_fd is None:
            self._lock_fd = os.open(self.root / "run" / "supervisor.lock",
                                    os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX)

    def _serve(self):
        while not self._done.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with conn:
                conn.settimeout(5)
                data = b""
                try:
                    while b"\n" not in data:
                        chunk = conn.recv(65536)
                        if not chunk:
                            break
                        data += chunk
                    req = json.loads(data.split(b"\n", 1)[0])
                except (OSError, ValueError):
                    continue
                self.requests.append(req)
                answer = self.answers.get(req.get("op"))
                if callable(answer):
                    answer = answer(req)
                if answer is None:
                    answer = _default_answer(req)
                try:
                    conn.sendall((json.dumps(answer) + "\n").encode())
                except OSError:
                    pass


def runner_home(root, slug, runner="fake", extra=""):
    """A minimal runner-lane cousin home (`[agent] runner`)."""
    home = Path(root) / "cousins" / slug
    for sub in ("data", "memory", "run"):
        (home / sub).mkdir(parents=True, exist_ok=True)
    (home / "cousin.toml").write_text(
        '[cousin]\nslug = "%s"\nname = "%s"\n\n[chat]\ntmux_session = "%s"\n'
        '\n[agent]\nrunner = "%s"\n%s' % (slug, slug.capitalize(), slug, runner, extra))
    return home
