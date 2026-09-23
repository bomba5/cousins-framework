"""The inbox: durable, thread-keyed, claimed in priority order."""
import multiprocessing
import sqlite3
import time
import unittest

from cousin_lib.delivery import Item
from cousin_lib.runner.inbox import Inbox
from tests._hermetic import HermeticCase
from tests.runner._home import temp_home


def _item(thread="operator:priya", source="chat", body="hello", **kw):
    return Item(thread_id=thread, source=source, body=body, **kw)


class InboxCase(HermeticCase):
    def setUp(self):
        super().setUp()
        self.home = temp_home(self)
        self.inbox = Inbox(self.home)


class TestPutAndClaim(InboxCase):
    def test_put_returns_a_growing_id_and_claim_is_oldest_first(self):
        a = self.inbox.put(_item(body="one"))
        b = self.inbox.put(_item(body="two"))
        self.assertLess(a, b)
        rows = self.inbox.claim(limit=2)
        self.assertEqual([r["body"] for r in rows], ["one", "two"])
        self.assertEqual(rows[0]["state"], "claimed")

    def test_claim_orders_by_source_priority_before_age(self):
        self.inbox.put(_item(thread="loop:heartbeat", source="loop", body="loop"))
        self.inbox.put(_item(thread="peer:testa", source="chat", body="peer"))
        self.inbox.put(_item(thread="meeting:7", source="meeting", body="meeting"))
        self.inbox.put(_item(thread="operator:priya", source="chat", body="op"))
        self.inbox.put(_item(thread="system", source="flip", body="flip"))
        rows = self.inbox.claim(limit=5)
        self.assertEqual([r["body"] for r in rows],
                         ["flip", "op", "meeting", "peer", "loop"])

    def test_a_claimed_row_is_not_claimed_twice(self):
        self.inbox.put(_item())
        first = self.inbox.claim()
        second = self.inbox.claim()
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])

    def test_pending_counts_queued_only(self):
        self.inbox.put(_item()); self.inbox.put(_item())
        self.assertEqual(self.inbox.pending(), 2)
        self.inbox.claim()
        self.assertEqual(self.inbox.pending(), 1)

    def test_attachments_and_context_round_trip(self):
        i = self.inbox.put(_item(attachments=("/tmp/a.png",), context="[recall] x",
                                 sender="Priya", message_id=42))
        row = self.inbox.get(i)
        self.assertEqual(row["attachments"], ["/tmp/a.png"])
        self.assertEqual(row["context"], "[recall] x")
        self.assertEqual(row["sender"], "Priya")
        self.assertEqual(row["message_id"], 42)


class TestDone(InboxCase):
    def test_done_records_outcome_and_is_idempotent(self):
        i = self.inbox.put(_item())
        self.inbox.claim()
        self.inbox.done(i, "delivered", "turn 1")
        self.inbox.done(i, "delivered", "turn 1")
        row = self.inbox.get(i)
        self.assertEqual((row["state"], row["outcome"], row["detail"]),
                         ("done", "delivered", "turn 1"))

    def test_one_result_closes_two_rows(self):
        # Finding 1: a message sent mid-turn is folded into the running
        # turn and ONE result closes both. done() per consumed row.
        a = self.inbox.put(_item(body="first"))
        b = self.inbox.put(_item(body="second, mid-turn"))
        claimed = self.inbox.claim(limit=2)
        for row in claimed:
            self.inbox.done(row["id"], "delivered", "result #1")
        self.assertEqual(self.inbox.get(a)["state"], "done")
        self.assertEqual(self.inbox.get(b)["state"], "done")
        self.assertEqual(self.inbox.pending(), 0)

    def test_requeue_resets_a_claimed_row_to_queued(self):
        i = self.inbox.put(_item())
        self.inbox.claim()
        self.inbox.requeue(i)
        row = self.inbox.get(i)
        self.assertEqual(row["state"], "queued")
        self.assertEqual(len(self.inbox.claim()), 1)


class TestUnfinished(InboxCase):
    def test_unfinished_counts_queued_and_claimed(self):
        self.inbox.put(_item())
        self.inbox.put(_item())
        claimed = self.inbox.claim()
        claimed_id = claimed[0]["id"]
        self.assertEqual(self.inbox.pending(), 1)
        self.assertEqual(self.inbox.unfinished(), 2)
        self.inbox.done(claimed_id, "delivered")
        self.assertEqual(self.inbox.unfinished(), 1)


class TestDurability(InboxCase):
    def test_requeue_stale_returns_a_row_claimed_by_a_dead_runner(self):
        i = self.inbox.put(_item())
        self.inbox.claim(claimant="pid:99999")
        self.assertEqual(self.inbox.requeue_stale(older_than_s=0.0), 1)
        self.assertEqual(self.inbox.get(i)["state"], "queued")
        self.assertEqual(len(self.inbox.claim()), 1)

    def test_requeue_stale_leaves_a_fresh_claim_alone(self):
        self.inbox.put(_item())
        self.inbox.claim()
        self.assertEqual(self.inbox.requeue_stale(older_than_s=3600.0), 0)

    def test_a_row_survives_process_death_between_put_and_claim(self):
        self.inbox.put(_item(body="survivor"))
        del self.inbox
        again = Inbox(self.home)
        self.assertEqual([r["body"] for r in again.claim()], ["survivor"])


def _claim_in_child(home_str, out):
    inbox = Inbox(home_str)
    rows = inbox.claim(limit=1, claimant="child")
    out.put([r["id"] for r in rows])


class TestTwoProcesses(InboxCase):
    def test_two_processes_never_claim_the_same_row(self):
        ids = [self.inbox.put(_item(body=str(n))) for n in range(20)]
        ctx = multiprocessing.get_context("fork")
        out = ctx.Queue()
        procs = [ctx.Process(target=_claim_in_child, args=(str(self.home), out))
                 for _ in range(8)]
        for p in procs: p.start()
        for p in procs: p.join(10)
        got = []
        while not out.empty():
            got.extend(out.get())
        self.assertEqual(len(got), 8)
        self.assertEqual(len(set(got)), 8, "a row was claimed twice")
        self.assertTrue(set(got) <= set(ids))


if __name__ == "__main__":
    unittest.main()
