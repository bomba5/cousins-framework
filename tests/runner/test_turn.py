"""Turn: what reply routes by."""
import threading
import unittest

from cousin_lib.runner.turn import Turn
from tests._hermetic import HermeticCase


def _row(i, thread, sender=""):
    return {"id": i, "thread_id": thread, "sender": sender, "source": "chat"}


class TestTurn(HermeticCase):
    def test_begin_add_end(self):
        t = Turn()
        self.assertFalse(t.active); self.assertEqual(t.threads, ())
        t.begin(_row(1, "operator:priya", "Priya"))
        self.assertTrue(t.active); self.assertEqual(t.origin, "operator:priya")
        t.add(_row(2, "peer:testa", "Testa"))
        t.add(_row(3, "operator:priya", "Priya"))          # same thread, no duplicate
        self.assertEqual(t.threads, ("operator:priya", "peer:testa"))
        self.assertEqual(t.inbox_ids, (1, 2, 3))
        self.assertEqual(t.senders["peer:testa"], "Testa")
        t.end()
        self.assertFalse(t.active); self.assertEqual(t.threads, ()); self.assertIsNone(t.origin)

    def test_add_outside_a_turn_is_refused(self):
        with self.assertRaises(RuntimeError):
            Turn().add(_row(1, "operator:priya"))

    def test_thread_safe_snapshot(self):
        t = Turn(); t.begin(_row(0, "operator:priya"))
        def adder():
            for i in range(1, 200):
                t.add(_row(i, "peer:p%d" % i))
        th = threading.Thread(target=adder); th.start()
        for _ in range(50):
            snap = t.threads          # never raises mid-mutation
            self.assertIsInstance(snap, tuple)
        th.join()
        self.assertEqual(len(t.threads), 200)


    def test_snapshot_reads_active_and_threads_together(self):
        t = Turn()
        self.assertEqual(t.snapshot(), (False, ()))
        t.begin(_row(1, "operator:priya", "Priya"))
        t.add(_row(2, "person:sam", "Sam"))
        self.assertEqual(t.snapshot(), (True, ("operator:priya", "person:sam")))
        t.end()
        self.assertEqual(t.snapshot(), (False, ()))


if __name__ == "__main__":
    unittest.main()
