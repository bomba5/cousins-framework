"""The configuration 2.0.0 removes with the tmux lane (phase 10, ruling
P10-2): one table, read twice. On 1.x, `cousin-migrate plan` warns about
every entry a cousin or the install still carries, with the line to
follow, so the operator can clean up before upgrading. In 2.0.0 (phase
10b) the same table makes a load fail loudly, never ignore silently.

`scan(root, home=None)` returns [{"where", "key", "line"}]: `where` is
the file (cousin.toml, config/harness.toml, config/agent-cmd), `key` the
table and key as a person writes it, `line` what to do about it."""
import tomllib
from pathlib import Path

RUNNER = ("[agent] runner", "2.0.0 runs every cousin on the SDK runner, and a cousin with no"
          " `runner`, or `runner = \"tmux\"`, is refused: move it first (`cousin-migrate plan"
          " <slug>`, then `apply`; docs/migrating.md)")

COUSIN_KEYS = (
    ("chat", "port", "[chat] port", "the per-cousin chat server is gone in 2.0.0 (the console serves"
     " chat, peers reach the inbox directly): delete the line when you upgrade"),
    ("chat", "host", "[chat] host", "the per-cousin chat server is gone in 2.0.0: delete the line"
     " when you upgrade"),
    ("chat", "tmux_session", "[chat] tmux_session", "there is no tmux session in 2.0.0: delete"
     " the line when you upgrade"),
)

HARNESS_KEYS = (
    ("attention_patterns", "the tmux pane it matched is gone in 2.0.0: delete it"),
    ("busy_patterns", "the tmux pane it matched is gone in 2.0.0: delete it"),
    ("input_mode", "nothing types into a pane in 2.0.0: delete it"),
    ("transcripts_dir", "2.0.0 reads the runner's session store, not the harness transcripts:"
     " delete it"),
    ("mcp_logs_dir", "the harness's MCP logs are not read in 2.0.0: delete it"),
    ("settings_file", "2.0.0 writes no harness settings: delete it"),
)

AGENT_CMD = ("config/agent-cmd", "the tmux lane's agent command line is not used in 2.0.0 (the"
             " runner starts the agent; accounts pick the credentials): delete the file")


def _toml(path):
    try:
        return tomllib.loads(Path(path).read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return None


def scan(root, home=None):
    """What 2.0.0 will reject, in `home`'s cousin.toml (when given) and in
    the install under `root`. An unreadable file shows nothing here: its
    own reader reports it."""
    out = []
    if home is not None:
        data = _toml(Path(home) / "cousin.toml")
        if data is not None:
            agent = data.get("agent") if isinstance(data.get("agent"), dict) else {}
            if agent.get("runner") in (None, "tmux"):
                out.append({"where": "cousin.toml", "key": RUNNER[0], "line": RUNNER[1]})
            for table, key, name, line in COUSIN_KEYS:
                section = data.get(table)
                if isinstance(section, dict) and key in section:
                    out.append({"where": "cousin.toml", "key": name, "line": line})
    harness = _toml(Path(root) / "config" / "harness.toml")
    for key, line in HARNESS_KEYS:
        if isinstance(harness, dict) and key in harness:
            out.append({"where": "config/harness.toml", "key": key, "line": line})
    if (Path(root) / "config" / "agent-cmd").exists():
        out.append({"where": AGENT_CMD[0], "key": AGENT_CMD[0], "line": AGENT_CMD[1]})
    return out
