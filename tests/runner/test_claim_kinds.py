"""The inbox claims by thread kind: a side session takes only its
kinds, the primary everything but them, so a row runs in exactly one
session."""
import unittest

from cousin_lib.delivery import Item
from cousin_lib.runner.inbox import Inbox
from tests._hermetic import HermeticCase
from tests.runner._home import temp_home


def _put(inbox, thread, body, source="chat"):
    return inbox.put(Item(thread_id=thread, source=source, body=body))


class TestClaimKinds(HermeticCase):
    def setUp(self):
        super().setUp()
        self.inbox = Inbox(temp_home(self))

    def test_kinds_claims_only_rows_of_those_kinds_oldest_first(self):
        _put(self.inbox, "operator:priya", "op")
        _put(self.inbox, "peer:testa", "peer one")
        _put(self.inbox, "peer:sam", "peer two")
        rows = self.inbox.claim(limit=5, kinds=("peer",))
        self.assertEqual([r["body"] for r in rows], ["peer one", "peer two"])
        self.assertEqual(self.inbox.pending(), 1)

    def test_exclude_kinds_skips_them_whatever_their_priority(self):
        _put(self.inbox, "peer:testa", "peer")
        _put(self.inbox, "loop:heartbeat", "loop", source="loop")
        rows = self.inbox.claim(limit=5, exclude_kinds=("peer",))
        self.assertEqual([r["body"] for r in rows], ["loop"])
        self.assertEqual(self.inbox.claim(limit=5, kinds=("peer",))[0]["body"], "peer")

    def test_a_bare_kind_is_its_own_kind(self):
        _put(self.inbox, "system", "digest", source="boot")
        _put(self.inbox, "schedule", "due", source="schedule")
        self.assertEqual([r["body"] for r in self.inbox.claim(limit=5, exclude_kinds=("system",))],
                         ["due"])
        self.assertEqual([r["body"] for r in self.inbox.claim(limit=5, kinds=("system",))],
                         ["digest"])

    def test_only_the_first_colon_separates_the_kind(self):
        _put(self.inbox, "meeting:m:1", "meeting", source="meeting")
        _put(self.inbox, "person:sam:two", "person")
        self.assertEqual([r["body"] for r in self.inbox.claim(limit=5, kinds=("meeting",))],
                         ["meeting"])
        self.assertEqual([r["body"] for r in self.inbox.claim(limit=5, kinds=("person",))],
                         ["person"])

    def test_empty_kinds_claims_nothing(self):
        _put(self.inbox, "peer:testa", "peer")
        self.assertEqual(self.inbox.claim(limit=5, kinds=()), [])
        self.assertEqual(self.inbox.pending(), 1)

    def test_two_disjoint_claimers_split_the_queue_between_them(self):
        for thread, body in (("operator:priya", "a"), ("peer:testa", "b"),
                             ("person:mallory", "c"), ("peer:sam", "d")):
            _put(self.inbox, thread, body)
        primary = self.inbox.claim(limit=10, exclude_kinds=("peer",))
        side = self.inbox.claim(limit=10, kinds=("peer",))
        self.assertEqual(sorted(r["body"] for r in primary), ["a", "c"])
        self.assertEqual(sorted(r["body"] for r in side), ["b", "d"])
        self.assertEqual(self.inbox.pending(), 0)


if __name__ == "__main__":
    unittest.main()
