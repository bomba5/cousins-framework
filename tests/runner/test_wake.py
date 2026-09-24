"""poke wakes a listener; with nobody listening it is False, never an error."""
import os
import socket
import stat
import struct
import threading
import time
import unittest
import unittest.mock

from cousin_lib.runner import wake
from tests._hermetic import HermeticCase
from tests.runner._home import temp_home


class TestWake(HermeticCase):
    def setUp(self):
        super().setUp()
        self.home = temp_home(self)

    def test_poke_with_no_listener_is_false_not_an_error(self):
        self.assertFalse(wake.poke(self.home))

    def test_listener_wakes_within_500ms(self):
        with wake.Listener(self.home) as listener:
            woke = {}
            def waiter():
                t = time.monotonic()
                woke["ok"] = listener.wait(timeout=2.0)
                woke["ms"] = (time.monotonic() - t) * 1000
            th = threading.Thread(target=waiter); th.start()
            time.sleep(0.05)
            self.assertTrue(wake.poke(self.home))
            th.join(3)
            self.assertTrue(woke["ok"])
            self.assertLess(woke["ms"], 500)

    def test_wait_times_out_false_when_nobody_pokes(self):
        with wake.Listener(self.home) as listener:
            self.assertFalse(listener.wait(timeout=0.1))

    def test_a_stale_socket_file_is_replaced_at_listen(self):
        path = wake.socket_path(self.home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("stale")
        with wake.Listener(self.home) as listener:
            self.assertTrue(wake.poke(self.home))
            self.assertTrue(listener.wait(timeout=1.0))

    def test_a_poke_is_kept_as_its_message(self):
        with wake.Listener(self.home) as listener:
            self.assertTrue(wake.poke(self.home))
            self.assertTrue(wake.send(self.home, b'{"event": "Stop"}'))
            self.assertTrue(listener.wait(timeout=1.0))
            self.assertEqual(listener.messages, [b"1", b'{"event": "Stop"}'])
            self.assertFalse(listener.wait(timeout=0.05))
            self.assertEqual(listener.messages, [])

    def test_the_socket_is_0600_and_a_new_run_dir_0700(self):
        (self.home / "run").rmdir()
        with wake.Listener(self.home) as listener:
            self.assertEqual(stat.S_IMODE(os.stat(listener.path).st_mode), 0o600)
            self.assertEqual(stat.S_IMODE((self.home / "run").stat().st_mode), 0o700)


@unittest.skipUnless(hasattr(socket, "SO_PASSCRED"), "SCM_CREDENTIALS is Linux's")
class TestPeerUid(HermeticCase):
    """R19: the boundary is the uid; a datagram from another uid wakes nothing."""

    def creds(self, uid):
        return [(socket.SOL_SOCKET, socket.SCM_CREDENTIALS,
                 struct.pack("iII", 4242, uid, os.getgid()))]

    def test_our_own_uid_is_allowed(self):
        self.assertTrue(wake.peer_allowed(self.creds(os.getuid()), required=True))

    def test_a_foreign_uid_is_refused(self):
        self.assertFalse(wake.peer_allowed(self.creds(os.getuid() + 1), required=True))

    def test_missing_credentials_are_refused_where_the_kernel_gives_them(self):
        self.assertFalse(wake.peer_allowed([], required=True))
        self.assertTrue(wake.peer_allowed([], required=False))

    def test_a_refused_datagram_neither_wakes_nor_is_kept(self):
        home = temp_home(self)
        with wake.Listener(home) as listener:
            with unittest.mock.patch.object(wake, "peer_allowed", return_value=False):
                self.assertTrue(wake.poke(home))
                self.assertFalse(listener.wait(timeout=0.3))
            self.assertEqual(listener.messages, [])
            self.assertEqual(listener.refused, 1)


if __name__ == "__main__":
    unittest.main()
