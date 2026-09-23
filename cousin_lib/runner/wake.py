"""The wake socket: a producer pokes, the runner stops sleeping.

The inbox row is the message; the poke is only a nudge so the runner
does not have to poll. A poke that finds nobody listening returns
False and the row waits in the inbox for the next start. A runner that
cannot bind the socket polls the inbox instead (`listen`): it answers
on the same cadence, only without the early wake.
"""
import select
import socket
import time
from pathlib import Path


def socket_path(home):
    return Path(home) / "run" / "runner.sock"


def poke(home):
    path = socket_path(home)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        s.sendto(b"1", str(path))
        return True
    except OSError:
        return False
    finally:
        s.close()


class Listener:
    def __init__(self, home):
        self.path = socket_path(home)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            self.sock.bind(str(self.path))
            self.sock.setblocking(False)
        except OSError:
            self.sock.close()
            raise

    def wait(self, timeout):
        ready, _, _ = select.select([self.sock], [], [], timeout)
        if not ready:
            return False
        # Drain every pending poke: one wake is enough.
        while True:
            try:
                self.sock.recv(64)
            except BlockingIOError:
                break
        return True

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
