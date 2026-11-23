"""A runner cousin's own secret variables: cousin.toml `[agent] env_secrets`.

    [agent]
    env_secrets = ["LINEAR_API_KEY"]

Each name's value is the file <home>/.secrets/env/<NAME>: one line of
printable characters with no spaces (the key rule, agent_auth._KEY_RE and
KEY_MAX_CHARS), a regular file of this user with no group or other bits,
in a directory of this user with none either (agent_auth.read_private_file,
the reader every secret file goes through). The console writes it
(console/secrets.write_secret_file); a hand-written one must pass the same
reader.

The sdk runner reads every named file at its start (runner/main.runner_for)
and refuses to start, exit 2, when one is missing or unreadable: a cousin
told to have a variable never runs without it. The values go to the
agent CLI's environment (the `env` option, beside the account's
variables), so a `${NAME}` in the home's .mcp.json reaches the server it
names, and to the environment the runner checks .mcp.json against and
lists the servers with (mcp_config.load, served.list_one). They never
reach the supervisor's environment, another cousin's runner or an argv.
The agent's own commands inherit the CLI's environment, so the model can
read them, as it can read the files: this keeps a secret out of
.mcp.json and out of every other cousin, not away from its own cousin.

A name the framework sets itself is refused (`reserved`): an account
variable (accounts.AUTH_VARS), CLAUDE* and ANTHROPIC*, the framework's
own (COUSIN_*, FRAMEWORK_ROOT, PLUGIN_DIR), the process basics (PATH,
HOME and the rest of tmux_launch.BASE_ENV, LC_*) and the ones that load
code into a process the CLI starts (LD_*, DYLD_*, PYTHON*, NODE_OPTIONS,
NODE_PATH, BASH_ENV, ENV). Errors name the variable and the file, never a
value."""
from __future__ import annotations

import re
from pathlib import Path

from cousin_lib import accounts, agent_auth

DIR = (".secrets", "env")
KEY = "env_secrets"
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
RESERVED_PREFIXES = ("CLAUDE", "ANTHROPIC", "COUSIN_", "LC_", "LD_", "DYLD_", "PYTHON")
RESERVED_NAMES = ("FRAMEWORK_ROOT", "PLUGIN_DIR", "NODE_OPTIONS", "NODE_PATH", "BASH_ENV", "ENV",
                  "HOME", "PATH", "USER", "LOGNAME", "SHELL",
                  "SSH_AUTH_SOCK", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "LANG",
                  "LOCALE_ARCHIVE", "TZ", "COLORTERM", "TMPDIR", "TERM", "PWD")


class EnvSecretError(ValueError):
    """A name or a value file the runner would refuse; names the file, never the value."""


def reserved(name):
    """Why the framework keeps `name` for itself, or None."""
    if name in accounts.AUTH_VARS:
        return "an account variable"
    if name.startswith(RESERVED_PREFIXES) or name in RESERVED_NAMES:
        return "a variable the framework or the process sets itself, or one that loads code"
    return None


def names_of(agent):
    """`[agent] env_secrets` as a tuple of variable names (default none).
    EnvSecretError for a value that is not a list of names, a reserved
    name, or a name listed twice."""
    names = (agent or {}).get(KEY, [])
    if not isinstance(names, list) or not all(isinstance(n, str) and _NAME.match(n)
                                              for n in names):
        raise EnvSecretError("cousin.toml [agent] %s must be a list of variable names" % KEY)
    for name in names:
        why = reserved(name)
        if why:
            raise EnvSecretError("cousin.toml [agent] %s names %s: %s, never one of a"
                                 " cousin's secrets" % (KEY, name, why))
    if len(set(names)) != len(names):
        raise EnvSecretError("cousin.toml [agent] %s names a variable twice" % KEY)
    return tuple(names)


def path(home, name):
    """<home>/.secrets/env/<NAME>, absolute. EnvSecretError for a bad name."""
    if not isinstance(name, str) or not _NAME.match(name):
        raise EnvSecretError("%r is not a variable name" % (name,))
    return Path(home).absolute().joinpath(*DIR, name)


def read(home, name):
    """One variable's value, through the runner's private-file reader."""
    file = path(home, name)
    try:
        raw = agent_auth.read_private_file(
            file, what="value for %s" % name,
            missing_hint=" (write it in the console's agent settings, or put one line"
                         " in that file, mode 600)")
    except agent_auth.AuthError as err:      # MissingFile is one
        raise EnvSecretError(str(err))
    try:
        value = raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise EnvSecretError("%s is not UTF-8 text" % file)
    if not value:
        raise EnvSecretError("%s is empty" % file)
    if len(value) > agent_auth.KEY_MAX_CHARS or not agent_auth._KEY_RE.match(value):
        raise EnvSecretError("%s must hold one line of at most %d printable characters"
                             " with no spaces" % (file, agent_auth.KEY_MAX_CHARS))
    return value


def load(home, agent):
    """{NAME: value} for every name `[agent] env_secrets` lists, in its
    order; EnvSecretError on the first name or file the runner refuses."""
    return {name: read(home, name) for name in names_of(agent)}


def state(home, names):
    """{NAME: {set, last4, error}} for the console: what may be shown
    about each value file (console/secrets.secret_state), never a value."""
    from cousin_lib.console.secrets import secret_state
    out = {}
    for name in names:
        try:
            file = path(home, name)
        except EnvSecretError as err:
            out[name] = {"set": False, "last4": None, "error": str(err)}
            continue
        out[name] = secret_state(file)
    return out
