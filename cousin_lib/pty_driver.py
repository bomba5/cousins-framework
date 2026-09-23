"""A small pty driver for the CLI's interactive login flows (Task 15).

`claude auth login` and `claude setup-token` want a terminal. This runs
one in a pty whose window size is set in the CHILD before exec, so the
first screen already sees it (an authorize URL wider than the terminal
would be wrapped), and a reader thread drains the pty continuously into a
bounded tail of CLEANED text: `setup-token` redraws its screen the whole
time it waits for a code, and a pty nobody reads fills up and blocks the
CLI. `clean()` turns cursor-forward moves (how the full-screen interface
draws spaces) back into spaces and drops every other escape, OSC 8
hyperlinks included; `Cleaner` does the same for a stream and holds back
an escape a read boundary cut, so each byte is cleaned once and
`read_until` never re-cleans the whole output."""
import codecs
import fcntl
import os
import pty
import re
import select
import signal
import struct
import termios
import threading
import time

TAIL_CHARS = 64 * 1024
PENDING_MAX = 4096
_FWD = re.compile(r"\x1b\[(\d*)C")
_CSI = re.compile(r"\x1b\[[0-9;?<>=]*[ -/]*[@-~]")
_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_ESC = re.compile(r"\x1b[()=>78][0-9A-Za-z]?")
_PARTIAL = re.compile(r"\x1b(?:\[[0-9;?<>=]*[ -/]*|[()])?")


class PtyTimeout(Exception):
    pass


def clean(raw):
    text = _FWD.sub(lambda m: " " * int(m.group(1) or 1), raw)
    text = _OSC.sub("", text)
    text = _CSI.sub("", text)
    text = _ESC.sub("", text)
    return text.replace("\r", "")


def _split(raw):
    """(complete, pending): `pending` is a trailing escape not yet ended."""
    osc = raw.rfind("\x1b]")
    if osc != -1 and not _OSC.match(raw, osc):
        return raw[:osc], raw[osc:]
    esc = raw.rfind("\x1b")
    if esc != -1 and _PARTIAL.fullmatch(raw, esc):
        return raw[:esc], raw[esc:]
    return raw, ""


class Cleaner:
    """clean() for a stream: feed raw text, get clean text back; an escape
    cut by a read boundary waits for the rest (at most PENDING_MAX chars,
    then it is let through as it is)."""

    def __init__(self):
        self.pending = ""

    def feed(self, raw):
        text, self.pending = _split(self.pending + raw)
        if len(self.pending) > PENDING_MAX:
            text, self.pending = text + self.pending, ""
        return clean(text)


class PtySession:
    def __init__(self, argv, env, *, cols=2000, rows=50):
        if not argv or not os.path.isabs(argv[0]):
            raise ValueError("PtySession runs an absolute program path, got %r"
                             % (argv[0] if argv else None))
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        self.pid, self.fd = pty.fork()
        if self.pid == 0:                        # the child: the size first, then the CLI
            try:
                fcntl.ioctl(0, termios.TIOCSWINSZ, winsize)
                os.execve(argv[0], list(argv), dict(env))
            finally:
                os._exit(127)
        self._cond = threading.Condition()
        self._cleaner = Cleaner()
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._text, self._base, self._pos = "", 0, 0      # tail, its offset, the consumed mark
        self._eof = self._closed = False
        self._reader = threading.Thread(target=self._drain, name="pty-reader", daemon=True)
        self._reader.start()

    def _drain(self):
        while True:
            try:
                ready, _, _ = select.select([self.fd], [], [], 0.2)
                data = os.read(self.fd, 65536) if ready else None
            except OSError:                      # EIO: the child closed its side
                data = b""
            if data is None:
                continue
            with self._cond:
                if data:
                    self._text += self._cleaner.feed(self._decoder.decode(data))
                    over = len(self._text) - TAIL_CHARS
                    if over > 0:
                        self._text, self._base = self._text[over:], self._base + over
                else:
                    self._eof = True
                self._cond.notify_all()
            if not data:
                return

    def text(self):
        with self._cond:
            return self._text

    def read_until(self, pattern, timeout):
        """The first match of `pattern` after the previous match; the text
        up to its end is consumed. PtyTimeout when it does not come within
        `timeout` or the CLI ends first."""
        rx = re.compile(pattern)
        deadline = time.monotonic() + float(timeout)
        with self._cond:
            while True:
                m = rx.search(self._text, max(0, self._pos - self._base))
                if m:
                    self._pos = self._base + m.end()
                    return m
                if self._eof:
                    raise PtyTimeout("the process ended before %r" % pattern)
                left = deadline - time.monotonic()
                if left <= 0:
                    raise PtyTimeout("no %r within %.0fs" % (pattern, timeout))
                self._cond.wait(min(left, 0.5))

    def write(self, text):
        os.write(self.fd, text.encode())

    def close(self, timeout=5):
        if self._closed:
            return None
        self._closed = True
        deadline = time.monotonic() + timeout
        rc = None
        while time.monotonic() < deadline:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            if pid:
                rc = os.waitstatus_to_exitcode(status)
                break
            time.sleep(0.05)
        if rc is None:
            try:
                os.kill(self.pid, signal.SIGKILL)
                os.waitpid(self.pid, 0)
            except OSError:
                pass
        self._reader.join(timeout=2)             # EIO once the child is gone ends it
        try:
            os.close(self.fd)
        except OSError:
            pass
        return rc
