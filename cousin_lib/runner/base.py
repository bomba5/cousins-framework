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
    "chat": 3,        # the peer case; operator/person chat is 1, below
    "reaction": 1,
    "hook": 1,
    "boot": 1,
    "meeting": 2,
    "schedule": 4,
    "loop": 5,
}


def priority(source, thread_id):
    kind, _ = parse_thread(thread_id)
    if source == "chat" and kind in ("operator", "person"):
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
