"""Delivery: the one way anything reaches a cousin.

docs/design/agent-loop-runner.md is the contract. Every producer (chat,
reactions, chat hooks, loops, schedules, meetings, the flip, a pending
boot) hands an `Item` to `deliver()`. The item names its thread, so a
reply can be routed back to where the turn came from, and its source,
so a backend can decide how to present it and what to do with it when
it arrives mid-turn.

This phase has one backend, tmux, which renders an item to exactly the
line that producer typed before this module existed. The runner's
inbox becomes the second one.

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
           "flip", "boot")
DELIVERED, QUEUED, FAILED = "delivered", "queued", "failed"


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


def backend_for(home):
    """The delivery backend for a cousin. tmux is the only one yet; the
    runner's inbox arrives with `[agent] runner` in cousin.toml."""
    return TmuxBackend()


def deliver(home, item, *, wait=True, backend=None, **backend_opts):
    """Hand one item to a cousin. Returns DELIVERED, QUEUED or FAILED
    and never raises for a delivery problem: the caller's work (a
    stored message, a due schedule) must not be lost to a transport."""
    backend = backend if backend is not None else backend_for(home)
    return backend.send(home, item, wait=wait, **backend_opts)


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
