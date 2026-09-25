"""The wake socket: a producer pokes, the runner stops sleeping.

The inbox row is the message; the poke is only a nudge so the runner
does not have to poll. A poke that finds nobody listening returns
False and the row waits in the inbox for the next start. A runner that
cannot bind the socket polls the inbox instead (`listen`): it answers
on the same cadence, only without the early wake.

The boundary is the uid (phase 11 R19): the socket is 0600 in a `run/`
created 0700, and on Linux every datagram carries its sender's
credentials (SO_PASSCRED), so one from another uid (root, which the
mode does not stop) wakes nothing. What a datagram says is kept in
`Listener.messages` for a caller that wants it (the tmux kind's pane
hooks send a small JSON); it is only ever a wake, never an instruction.
"""
import os
import select
import socket
import struct
import time
from pathlib import Path

MAX_DATAGRAM = 4096        # a poke is b"1"; a pane hook's JSON is well under this
SEND_S = 0.5               # a full queue is a runner already awake: never block a producer on it
_UCRED = struct.Struct("iII")      # struct ucred: pid, uid, gid
_PASSCRED = getattr(socket, "SO_PASSCRED", None)
_CRED_SPACE = socket.CMSG_SPACE(_UCRED.size) if _PASSCRED is not None else 0


def socket_path(home):
    return Path(home) / "run" / "runner.sock"


def send(home, payload):
    """One datagram to the runner's socket: True when it was queued,
    False when nobody listens (or the queue stayed full for SEND_S)."""
    path = socket_path(home)
    s = None
    try:
        # made inside the try: poke runs from a signal handler (begin_stop),
        # where a raise (EMFILE) would land in whatever the thread was doing
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        s.settimeout(SEND_S)
        s.sendto(payload, str(path))
        return True
    except OSError:
        return False
    finally:
        if s is not None:
            s.close()


def poke(home):
    return send(home, b"1")


def peer_allowed(ancdata, *, required, uid=None):
    """True when a datagram's sender runs as this uid. With SO_PASSCRED on
    (`required`), Linux attaches the sender's credentials to every
    datagram, so none attached is refused too; a platform without them
    has only the socket's 0600 mode, and a datagram there is allowed."""
    uid = os.getuid() if uid is None else uid
    for level, kind, data in ancdata:
        if level == socket.SOL_SOCKET and kind == getattr(socket, "SCM_CREDENTIALS", None) \
                and len(data) >= _UCRED.size:
            return _UCRED.unpack(data[:_UCRED.size])[1] == uid
    return not required


class Listener:
    def __init__(self, home):
        self.path = socket_path(home)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.messages = []     # what the last wait drained, from this uid only
        self.refused = 0       # datagrams from another uid, dropped
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self._creds = False
        try:
            if _PASSCRED is not None:
                self.sock.setsockopt(socket.SOL_SOCKET, _PASSCRED, 1)
                self._creds = True
            self.sock.bind(str(self.path))
            os.chmod(self.path, 0o600)
            self.sock.setblocking(False)
        except OSError:
            self.sock.close()
            raise

    def wait(self, timeout):
        """True when a datagram from this uid arrived within `timeout`;
        `messages` then holds every one drained (one wake is enough)."""
        self.messages = []
        ready, _, _ = select.select([self.sock], [], [], timeout)
        if not ready:
            return False
        while True:
            try:
                data, anc, _flags, _addr = self.sock.recvmsg(MAX_DATAGRAM, _CRED_SPACE)
            except (BlockingIOError, InterruptedError):
                break
            if peer_allowed(anc, required=self._creds):
                self.messages.append(data)
            else:
                self.refused += 1
        return bool(self.messages)

    def close(self):
        try:
            self.sock.close()
        finally:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class Poller:
    """The doorbell's stand-in when the socket cannot be bound: `wait`
    sleeps the timeout and reports no poke, so the caller claims from the
    inbox on its own cadence."""
    path = None
    messages = ()

    def wait(self, timeout):
        time.sleep(timeout)
        return False

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def listen(home, on_error):
    """A Listener, or a Poller when the socket cannot be bound (a `run`
    that is not a directory, a path too long for AF_UNIX, a permission).
    `on_error` gets one line naming the socket: a runner that lost its
    doorbell says so, and keeps working."""
    try:
        return Listener(home)
    except OSError as err:
        on_error("wake socket %s unavailable (%s: %s); polling the inbox instead"
                 % (socket_path(home), type(err).__name__, err))
        return Poller()
