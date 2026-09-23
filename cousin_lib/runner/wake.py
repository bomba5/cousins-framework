"""The wake socket: a producer pokes, the runner stops sleeping.

The inbox row is the message; the poke is only a nudge so the runner
does not have to poll. A poke that finds nobody listening returns
False and the row waits in the inbox for the next start.
"""
import os
import select
import socket
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
        self.sock.bind(str(self.path))
        self.sock.setblocking(False)

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
