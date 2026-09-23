"""poke wakes a listener; with nobody listening it is False, never an error."""
import threading
import time
import unittest

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


if __name__ == "__main__":
    unittest.main()
