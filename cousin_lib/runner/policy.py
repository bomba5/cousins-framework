"""Operator policy as code: <home>/policy.toml (spec, "Hooks in-process",
`can_use_tool`). Enforced by a PreToolUse hook with no matcher, because
under bypassPermissions the CLI never consults can_use_tool. Missing:
allow all and say so once. Malformed: fail closed at start, loudly.

The hook fires for every tool call of the session, a subagent's own
calls included (the hook input then carries `agent_id`), so a deny
reaches inside a subagent's turn as well.

`deny_bash_patterns` is matched against the `command` string of any
tool whose input carries one (Bash, PowerShell, Monitor, any other); it
never sees a command a tool builds on the far side (an MCP server that
shells out, an editor tool's own file writes). The patterns are a
guardrail against a known command line, not a sandbox: the same effect
can be spelled another way. Deny the tool itself with `deny_tools` when
the risk is the tool, not the shape of one command.

The file lives in the home it governs, so the model can rewrite it; a
rewrite takes effect at the next start, never mid-session. An operator
who needs it immutable makes it read-only to the cousin's user or owns
it."""
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from cousin_lib.runner.base import RunnerError

FILE = "policy.toml"
KEYS = ("deny_tools", "deny_bash_patterns", "ask", "outbound_filter")


class PolicyError(RunnerError):
    pass


def _str_list(data, key):
    value = data.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise PolicyError("%s: %s must be a list of strings" % (FILE, key))
    return tuple(value)


@dataclass(frozen=True)
class Policy:
    deny_tools: tuple = ()
    deny_bash_patterns: tuple = ()      # compiled regexes
    ask: tuple = ()
    outbound_filter: bool = True
    source: str = "none"

    @classmethod
    def load(cls, home):
        path = Path(home) / FILE
        if not path.exists():
            return cls()
        try:
            data = tomllib.loads(path.read_text())
        except (OSError, tomllib.TOMLDecodeError) as err:
            raise PolicyError("%s: cannot read: %s" % (FILE, err))
        unknown = set(data) - set(KEYS)
        if unknown:
            raise PolicyError("%s: unknown keys %s" % (FILE, ", ".join(sorted(unknown))))
        patterns = []
        for p in _str_list(data, "deny_bash_patterns"):
            try:
                patterns.append(re.compile(p))
            except re.error as err:
                raise PolicyError("%s: deny_bash_patterns %r: %s" % (FILE, p, err))
        flag = data.get("outbound_filter", True)
        if not isinstance(flag, bool):
            raise PolicyError("%s: outbound_filter must be true or false" % FILE)
        return cls(deny_tools=_str_list(data, "deny_tools"), deny_bash_patterns=tuple(patterns),
                   ask=_str_list(data, "ask"), outbound_filter=flag, source=str(path))

    def _named(self, names, tool):
        tool = str(tool or "")
        for n in names:
            if n == tool or (n.endswith("*") and tool.startswith(n[:-1])):
                return n
        return None

    def decide(self, tool_name, tool_input):
        """`("allow", "")`, `("deny", reason)` or `("ask", reason)`:
        deny_tools first, then a string `command` in the tool's input
        (whatever the tool) against deny_bash_patterns, then ask.
        Anything not named is allowed. `tool_name` is normalised to a
        string first: a hook payload missing `tool_name` must not raise
        out of a gate and so turn into an allow."""
        tool_name = str(tool_name or "")
        hit = self._named(self.deny_tools, tool_name)
        if hit:
            return "deny", "%s: deny_tools lists %s" % (FILE, hit)
        command = tool_input.get("command") if isinstance(tool_input, dict) else None
        if isinstance(command, str):
            for rx in self.deny_bash_patterns:
                if rx.search(command):
                    return "deny", "%s: deny_bash_patterns %r matches" % (FILE, rx.pattern)
        hit = self._named(self.ask, tool_name)
        if hit:
            return "ask", "%s: ask lists %s" % (FILE, hit)
        return "allow", ""

    def describe(self):
        """One line for the stream at start."""
        if self.source == "none":
            return "no policy.toml: every tool allowed"
        return "%s: %d denied tools, %d bash patterns, %d ask, outbound_filter=%s" % (
            self.source, len(self.deny_tools), len(self.deny_bash_patterns), len(self.ask),
            self.outbound_filter)
