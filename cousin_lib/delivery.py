"""Delivery: the one way anything reaches a cousin.

docs/design/agent-loop-runner.md is the contract. Every producer (chat,
reactions, chat hooks, loops, schedules, meetings, the flip, a pending
boot) hands an `Item` to `deliver()`. The item names its thread, so a
reply can be routed back to where the turn came from, and its source,
so a backend can decide how to present it and what to do with it when
it arrives mid-turn.

This phase has one backend, tmux, which renders an item to exactly the
line that producer typed before this module existed. The runner's
inbox is the second backend, selected by `[agent] runner`.

Outcomes are three and only three. When the framework cannot tell
whether a cousin received something it says `queued` or `failed`,
never `delivered`: unknown is a result, fine is a claim.
"""
from dataclasses import dataclass
from pathlib import Path

THREAD_KINDS = ("operator", "person", "peer", "meeting", "loop",
                "schedule", "system")
_BARE_KINDS = ("schedule", "system")
SOURCES = ("chat", "reaction", "hook", "loop", "schedule", "meeting",
           "flip", "boot", "propose", "interrupt")
# The sender names the framework writes itself: chat_hooks.HOOK_SENDER
# (threaded on `system` as a hook), the runner's own items, the fallback
# for a missing sender and the bare thread kinds. peer_inbound refuses
# them for a sender from outside the install (review round 2, N1).
FRAMEWORK_SENDERS = ("fw-hook", "runner", "framework", "unknown") + _BARE_KINDS
DELIVERED, QUEUED, FAILED = "delivered", "queued", "failed"

# The `[agent] runner` values that put a cousin on the runner lane: the
# one list (backend_for, spawn, the supervisor, the flip and the lifecycle
# read it), so a new runner kind is added here once.
RUNNER_KINDS = ("sdk", "fake", "opencode", "tmux")


class DeliveryError(ValueError):
    pass


def thread_id(kind, key=""):
    """`<kind>:<key>`, or the bare kind for the two that take no key."""
    if kind not in THREAD_KINDS:
        raise DeliveryError("unknown thread kind %r (one of %s)"
                            % (kind, ", ".join(THREAD_KINDS)))
    if kind in _BARE_KINDS:
        if key:
            raise DeliveryError("thread kind %r takes no key" % kind)
        return kind
    if not key:
        raise DeliveryError("thread kind %r needs a key" % kind)
    return "%s:%s" % (kind, key)


def parse_thread(value):
    """(kind, key) of a thread id; raises DeliveryError on a bad one.
    Only the first colon separates: a key may contain colons."""
    kind, _, key = str(value).partition(":")
    thread_id(kind, key)
    return kind, key


@dataclass(frozen=True)
class Item:
    thread_id: str
    source: str
    body: str
    sender: str = ""
    attachments: tuple = ()
    context: str = ""
    message_id: int | None = None

    def __post_init__(self):
        parse_thread(self.thread_id)
        if self.source not in SOURCES:
            raise DeliveryError("unknown source %r (one of %s)"
                                % (self.source, ", ".join(SOURCES)))
        object.__setattr__(self, "attachments", tuple(self.attachments))


class TmuxBackend:
    """Types an item into the cousin's tmux session as the line that
    producer typed before this module existed. Rendering is pure;
    `send` owns the injector."""

    def render(self, home, item, *, now=None):
        if item.source in ("chat", "hook"):
            from cousin_lib.server.injection import compose_delivery
            message = item.body
            if item.context:
                message = message + " " + item.context
            return compose_delivery(
                item.sender, message,
                marker_path=Path(home) / "data" / ".last-user-msg",
                attachments=item.attachments, now=now)
        if item.source == "schedule":
            return "[cousin-schedule] %s" % item.body
        return item.body

    def send(self, home, item, *, wait=True, **opts):
        """Render and type. `opts` are TmuxInjector's keyword arguments
        (tmux_bin, socket, settle, verify_delay, log, root, ...)."""
        if item.source == "interrupt":
            # an interrupt is a runner's inbox row; typed into a pane it
            # would be a message. A tmux cousin is stopped in its pane.
            return FAILED
        from cousin_lib.config import CousinConfig, MissingConfigError
        from cousin_lib.server.injection import TmuxInjector
        try:
            session = CousinConfig.load(home).tmux_session
        except (MissingConfigError, OSError):
            return FAILED
        text = self.render(home, item)
        injector = TmuxInjector(session, **opts)
        if not wait:
            injector.inject_async(text)
            return QUEUED
        return DELIVERED if injector.inject(text) else FAILED


class InboxBackend:
    """The runner's inbox: put a row, poke the socket, report `queued`.
    `delivered` is claimed only when `wait=True` and the runner marks
    the row done inside the timeout; a silent runner is `queued`,
    never `delivered` and never `failed`, because nothing is known.

    A put that fails (the store cannot be opened or written) is
    `failed`: nothing was kept. Once the put succeeded the row is
    durable, so anything that goes wrong after it (the poke, reading the
    outcome back) is `queued`, the row waiting for the runner, never
    `failed`: a producer that retried on it would deliver twice."""

    def send(self, home, item, *, wait=True, timeout=30.0, **opts):
        import sqlite3
        import time
        from cousin_lib.runner import wake
        from cousin_lib.runner.inbox import Inbox
        try:
            inbox = Inbox(home)
            inbox_id = inbox.put(item)
        except (OSError, sqlite3.Error):
            return FAILED
        try:
            wake.poke(home)
            if not wait:
                return QUEUED
            deadline = time.monotonic() + float(timeout)
            while time.monotonic() < deadline:
                row = inbox.get(inbox_id)
                if row and row["state"] == "done":
                    return DELIVERED if row["outcome"] == DELIVERED else FAILED
                time.sleep(0.05)
        except (OSError, sqlite3.Error):
            pass
        return QUEUED


def _runner_kind(home):
    """`[agent] runner`, or None. A cousin.toml that is missing or does
    not parse is None, so delivery stays on tmux: the conservative
    reading, and not a silent one, because cousin-runner refuses the
    same file with rc 2 and says why."""
    import tomllib
    try:
        data = tomllib.loads((Path(home) / "cousin.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return None
    return (data.get("agent") or {}).get("runner")


def backend_for(home):
    """tmux unless cousin.toml [agent] runner names a runner."""
    if _runner_kind(home) in RUNNER_KINDS:
        return InboxBackend()
    return TmuxBackend()


def deliver(home, item, *, wait=True, backend=None, **backend_opts):
    """Hand one item to a cousin. Returns DELIVERED, QUEUED or FAILED
    and never raises for a delivery problem: the caller's work (a
    stored message, a due schedule) must not be lost to a transport."""
    backend = backend if backend is not None else backend_for(home)
    return backend.send(home, item, wait=wait, **backend_opts)


def accepted(outcome, home):
    """A producer's acceptance test. `delivered` always; `queued` when the
    cousin is on the runner (the row is durable: retrying would deliver
    twice); never `failed`."""
    if outcome == DELIVERED:
        return True
    if outcome == QUEUED:
        return isinstance(backend_for(home), InboxBackend)
    return False


def is_alive(home, *, fallback=None):
    """Liveness for producers. A runner cousin is alive when a runner
    holds its lock; a tmux cousin answers through the caller's own
    check (`fallback`), because this module never touches tmux."""
    if isinstance(backend_for(home), InboxBackend):
        from cousin_lib.runner.main import is_running
        return is_running(home)
    return bool(fallback()) if fallback is not None else False


def thread_for_chat(config, user, *, cousins=None):
    """The thread a chat sender belongs to. `cousins` is the registry
    (objects with .slug and .name); read from the install when None,
    and an unreadable registry degrades to `person:`, never raises."""
    from cousin_lib.server.storage import normalize_chat_user
    who = normalize_chat_user(user)
    operator = getattr(config, "operator_name", None)
    if operator and who == normalize_chat_user(operator):
        return thread_id("operator", operator)
    if cousins is None:
        try:
            from cousin_lib.config import FrameworkConfig
            root = FrameworkConfig.root_from_home(config.home)
            cousins = FrameworkConfig(root).list_cousins()
        except Exception:  # noqa: BLE001 - a thread id must never fail a send
            cousins = []
    for cousin in cousins:
        if who in (normalize_chat_user(cousin.slug),
                   normalize_chat_user(cousin.name)):
            return thread_id("peer", cousin.slug)
    return thread_id("person", str(user).strip())
