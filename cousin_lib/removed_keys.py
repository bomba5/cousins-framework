"""The configuration 2.0.0 removed with the legacy tmux lane: one
table, read in four places. A key on it does nothing any more: no runner
kind reads it. It is named, never fatal and never silently ignored: by
`cousin-runner` at start (one stderr line, and one `system` `config`
event per finding on the stream), by `cousin-supervisor status` (its
`config` block), by the console's fleet row (`removedKeys`) and by
`cousin-migrate plan|check`. `cousin-migrate tidy <slug>|--all --yes`
removes them. A cousin with no `[agent] runner` is not on this table: it
is refused outright (delivery.lane_refusal).

`scan(root, home=None)` returns [{"where", "key", "line"}]: `where` is
the file (cousin.toml, config/harness.toml, config/hive.toml,
config/agent-cmd), `key` the table and key as a person writes it, `line`
what to do about it. The cousin's own findings come first, then the
install's. Each table row is (path, key, line): `path` is the TOML path
of the key (a table's own path when the whole table goes), which
`cousin-migrate tidy` removes."""
import tomllib
from pathlib import Path

TIDY = "`cousin-migrate tidy` removes it"

_CHAT = ("the per-cousin chat server is gone in 2.0.0 (the console serves chat, peers reach"
         " the inbox directly): delete the line; %s" % TIDY)
_RUNTIME = ("the legacy tmux lane's; a runner reads only [agent] (model, effort, account):"
            " delete the line; %s" % TIDY)

COUSIN_KEYS = (
    (("chat", "port"), "[chat] port", _CHAT),
    (("chat", "host"), "[chat] host", _CHAT),
    (("chat", "tmux_session"), "[chat] tmux_session",
     "the tmux kind names its own session (tmux-<slug>): delete the line; %s" % TIDY),
    (("runtime", "model"), "[runtime] model", _RUNTIME),
    (("runtime", "effort"), "[runtime] effort", _RUNTIME),
    (("runtime", "session_id"), "[runtime] session_id",
     "the legacy tmux lane's session; a runner keeps its own in data/runner-session.json:"
     " delete the line; %s" % TIDY),
    (("runtime", "auth"), "[runtime] auth",
     "the auth-mode switch is gone; a runner authenticates through its [agent] account:"
     " delete the line; %s" % TIDY),
)

HARNESS_KEYS = (
    (("attention_patterns",), "attention_patterns",
     "the legacy pane it matched is gone (the tmux kind reads its own screen): delete it;"
     " %s" % TIDY),
    (("busy_patterns",), "busy_patterns",
     "the auth-mode switch that read it is gone: delete it; %s" % TIDY),
    (("input_mode",), "[input_mode]",
     "nothing types chat into a pane in 2.0.0: delete the table; %s" % TIDY),
    (("flip_when_transcript_mb",), "flip_when_transcript_mb",
     "the transcript-size guard read the legacy lane's session: delete it; %s" % TIDY),
    (("agent", "resume"), "[agent.resume]",
     "config/agent-cmd is gone, and a runner resumes its own session: delete the table;"
     " %s" % TIDY),
    (("auth", "api_key"), "[auth.api_key]",
     "the api_key auth mode is gone; an anthropic-key account in config/accounts.toml"
     " replaces it: delete the table; %s" % TIDY),
)

HIVE_KEYS = (
    (("home_chat_url",), "home_chat_url",
     "the per-cousin chat server it posted to is gone; set home_cousin (a node reaches it"
     " through the queen): delete it; %s" % TIDY),
)

AGENT_CMD = ("config/agent-cmd", "the legacy tmux lane's launch command; every runner kind"
             " starts its own agent: delete the file; `cousin-migrate tidy` moves it aside")

# where -> (the file under the root, or under the home for cousin.toml; its table)
FILES = (("config/harness.toml", HARNESS_KEYS), ("config/hive.toml", HIVE_KEYS))


def _toml(path):
    try:
        return tomllib.loads(Path(path).read_text())
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None


def has(data, path):
    """`path` (a tuple of keys) is present in the parsed `data`."""
    node = data
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return False
        node = node[key]
    return True


def findings(data, table, where):
    """The rows of `table` present in the parsed `data`, as findings."""
    if not isinstance(data, dict):
        return []
    return [{"where": where, "key": key, "line": line}
            for path, key, line in table if has(data, path)]


def scan_home(home):
    """The removed keys in `home`'s cousin.toml. An unreadable file shows
    nothing here: its own reader reports it."""
    return findings(_toml(Path(home) / "cousin.toml"), COUSIN_KEYS, "cousin.toml")


def scan_install(root):
    """The removed keys and files of the install under `root`."""
    root = Path(root)
    out = []
    for where, table in FILES:
        out += findings(_toml(root / where), table, where)
    if (root / AGENT_CMD[0]).exists():
        out.append({"where": AGENT_CMD[0], "key": AGENT_CMD[0], "line": AGENT_CMD[1]})
    return out


def scan(root, home=None):
    """What 2.0.0 no longer reads: `home`'s cousin.toml (when given), then
    the install under `root`."""
    return (scan_home(home) if home is not None else []) + scan_install(root)


def summary(found):
    """One line naming every finding: `<where> <key>` joined."""
    return ", ".join(f["key"] if f["key"] == f["where"] else "%s %s" % (f["where"], f["key"])
                     for f in found)
