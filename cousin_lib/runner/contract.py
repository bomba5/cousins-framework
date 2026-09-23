"""The framework contract (spec, "The prompt is composed, not copied").

What a cousin must know about the framework, GENERATED from the one tool
registry and the release, instead of copied into every home's identity
file. It sits in the byte-stable system prompt (phase 0 finding 3: one
changed byte re-creates the whole appended block), so it is a pure
function of (registry, MAJOR.MINOR): no clock, no path, no slug, no
count of anything that grows. A PATCH release never changes a schema,
so it never changes a byte here."""
import re

from cousin_lib.mcp_server import SERVER_NAME

_VERSION = re.compile(r"^(\d+)\.(\d+)")

HEADER = """# Framework contract (generated for framework {version})

You are a cousin: a persistent agent whose agent loop the framework
runs. This section is generated from the framework's tool registry and
changes only with a release or a registry edit. Your authored identity
follows it. Where your identity text describes how the framework works
(how to reply, which command to run, how a session ends) and this
contract says otherwise, this contract is right: identity files were
written for an older framework and are not always updated with it."""

STATIC = """## How messages reach you

Every user message is an envelope. Its first line is
`[<thread>] <source> from <sender> at <time>`, where the thread is one of
operator:<name>, person:<name>, peer:<slug>, meeting:<id>, loop:<name>,
schedule or system. A block headed
`--- context (not the sender's words) ---` is recall from your memory,
added by the framework; it is never the sender speaking.

## How you answer

Your plain text, thinking and tool calls go to your reasoning stream,
which the operator can watch. Only the `reply` tool writes the chat
surface. With one live thread, `reply` answers it; with two (a message
folded into your running turn), name the thread. A peer is answered
with `send`, never with `reply`. A meeting line is answered with the
`meeting` tool on your turn only.

## Memory

Memory writes default to your own conclusion (L3). What the operator
told you is operator-stated: `remember` with `level=operator` and a
`cite` saying where it was said. An unverified guess is
`level=hypothesis`. Recall arrives on its own; search when you need
more.

## Generations

Your session ends at a rollover: on context pressure, or at the
operator's daily cadence. When a system message asks for your handoff,
call `handoff` exactly once, with `position`, `next_action` and
`status`, plus `active_threads` and `learned` when you have them. The
framework writes STATUS.md's open loops, your thread list, your new
memories and the handoff file from that one call, in that order. The
next generation starts from a digest of that state as its first
message. A new generation is not announced; the work continues."""


def major_minor(version):
    m = _VERSION.match(str(version or ""))
    return "%s.%s" % m.groups() if m else "unknown"


def _tool_block(definition, registry):
    name = definition["name"]
    lines = ["- `mcp__%s__%s`: %s" % (SERVER_NAME, name,
                                        " ".join(definition["description"].split()))]
    tool = registry["tools"].get(name)
    if tool is not None and tool.get("commands"):
        commands = set(tool["commands"])
        if tool.get("kind") == "job":
            commands |= {"status", "result"}
        lines.append("  Commands: %s." % ", ".join(sorted(commands)))
    return "\n".join(lines)


def render(registry, version):
    """The contract for this registry at this release. Same inputs, same bytes."""
    from cousin_lib.runner.tools import tool_definitions
    blocks = [_tool_block(d, registry) for d in tool_definitions(registry)]
    text = "\n\n".join([HEADER.format(version=major_minor(version)), STATIC,
                        "## Your tools\n\n" + "\n".join(blocks)])
    return text.rstrip("\n") + "\n"
