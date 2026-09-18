"""The agent launcher: what start_cousin puts in front of the agent
command in the tmux session.

    <python> .../cousin_lib/agent_launch.py --home H --root R -- <agent argv>

It reads the cousin's auth mode (cousin_lib.agent_auth) at exec time,
builds the agent's environment for that mode (in api_key mode the key
is read from the cousin's key file here, so it never appears in any
argv, tmux's included) and execs the agent in its place. A refused
mode prints the reason and exits 78 without starting anything.

Run by path, not with -m: the tmux server's environment may not carry
the PYTHONPATH of whoever asked for the start, so the package's parent
directory is put on sys.path from this file's own location.
"""
import os
import sys
from pathlib import Path

if __package__ in (None, ""):
    # Run as a script: sys.path[0] is cousin_lib/ itself, whose module
    # names (trace, config, ...) would shadow the standard library.
    sys.path[0] = str(Path(__file__).resolve().parents[1])

from cousin_lib.agent_auth import AuthError, agent_env  # noqa: E402

EX_CONFIG = 78


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    home = root = None
    while argv and argv[0] != "--":
        flag = argv.pop(0)
        if flag in ("--home", "--root") and argv:
            value = argv.pop(0)
            if flag == "--home":
                home = value
            else:
                root = value
        else:
            print("agent-launch: unknown argument %r" % flag,
                  file=sys.stderr)
            return 2
    if argv:
        argv.pop(0)
    if not home or not root or not argv:
        print("agent-launch: usage: --home H --root R -- <agent argv>",
              file=sys.stderr)
        return 2
    try:
        env = agent_env(home, root, os.environ)
    except AuthError as err:
        print("agent-launch: refused to start the agent: %s" % err,
              file=sys.stderr)
        return EX_CONFIG
    try:
        os.execvpe(argv[0], argv, env)
    except OSError as err:
        print("agent-launch: cannot run %r: %s" % (argv[0], err),
              file=sys.stderr)
        return 127


if __name__ == "__main__":
    sys.exit(main())
