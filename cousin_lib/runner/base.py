"""The runner contract every runner is written against.

docs/design/agent-loop-runner.md, "The runner". The Protocol below is
the locked interface from the master plan; `FakeRunner` and
`SdkRunner` implement it and one contract suite tests both.
"""
from dataclasses import dataclass
from typing import Iterator, Protocol

from cousin_lib.delivery import Item, parse_thread


class RunnerError(Exception):
    pass


@dataclass(frozen=True)
class Receipt:
    inbox_id: int
    outcome: str


# Lower claims first. Chat ranks by whom it is from: an operator or a
# person is streamed into a live turn, a peer waits (spec, "The store
# is the bus"). A flip's handoff goes ahead of everything.
SOURCE_PRIORITY = {
    "flip": 0,
    "interrupt": 0,   # phase 5: the out-of-process interrupt, ahead of everything
    "chat": 3,        # the peer case; operator/person chat is 1, below
    "reaction": 1,
    "hook": 1,
    "boot": 1,
    "meeting": 2,
    "schedule": 4,
    "loop": 5,
    "propose": 6,     # a memory proposal (extract.propose_turn) waits behind everything
}


FOLDED_KINDS = ("operator", "person")

# Phase 5: the interrupt a process without the runner object asks for
# (the console's interrupt route, and any process that enqueues one; not
# cousin-watch): an inbox row the fold hands back (requeue) rather than
# folding, and that SdkRunner._take_interrupts() takes during a live turn
# (gated by the class attribute `takes_interrupts`), closing it
# `delivered`; claimed at a turn boundary with no live turn running, it
# is closed `failed` with NO_TURN and never runs as a turn.
INTERRUPT = "interrupt"
NO_TURN = "no turn was running"


def folds_into_turn(source, thread_id):
    """True for an item a runner writes into the turn already running
    (operator or person chat), False for one that waits for the next
    turn. Every runner folds by this one rule, and only while the turn
    is live: never once an interrupt is requested, never after the
    turn's result was read (spec, "The store is the bus")."""
    kind, _ = parse_thread(thread_id)
    return source == "chat" and kind in FOLDED_KINDS


def priority(source, thread_id):
    if folds_into_turn(source, thread_id):
        return 1
    return SOURCE_PRIORITY.get(source, 3)


class Runner(Protocol):
    def start(self) -> None: ...
    def stop(self, *, timeout: float = 30.0) -> None: ...
    def state(self) -> str: ...
    def enqueue(self, item: Item) -> Receipt: ...
    def interrupt(self) -> bool: ...
    def rollover(self, reason: str) -> dict: ...
    def events(self, after: int | None = None) -> Iterator[dict]: ...
    def unsupported(self) -> list[str]: ...
    # Optional (P9, R19): plugin_items() -> list[str], the contract items a
    # plugin meets. Each shipped runner answers both from class attributes
    # UNSUPPORTED and PLUGIN_ITEMS, which runner/contract_table.py reads
    # without building a runner.
