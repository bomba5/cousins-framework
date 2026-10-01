"""The tmux kind's in-pane launcher.

The pane's command is `exec env -i <env_base> <python> <this file> --home H
[--fresh] -- claude ...`: tmux's own environment is rebuilt by the login
shell, so the allowlist is applied INSIDE the pane, by `env -i`, and this
launcher then

  - reads the cousin's account (accounts.for_cousin) and adds its
    variables (a named login's CLAUDE_CONFIG_DIR; `host` adds none);
    `claude-token` and `anthropic-key` accounts are refused;
  - adds CLAUDE_CODE_DISABLE_AUTO_MEMORY=1 and DISABLE_AUTOUPDATER=1, and
    COUSIN_PANE_PID: its own pid, which the exec chain makes the pane's and
    the CLI's (the pane hook writes only for that CLI, tmux_hook.from_pane);
  - on a fresh start (--fresh) appends `--append-system-prompt-file` with
    the path of data/run/tmux-context.md: the CLI reads the block
    from that private file, so it never passes through tmux's parser and
    never sits on an argv, which every local user can read;
  - applies the hard deny again (DENY_PREFIXES), except the variables it
    set itself, and refuses the print-mode flags;
  - execs the CLI.

No secret is ever on an argv: the account's values go to the environment
only, and a key or token account never gets this far."""
import argparse
import os
import sys
from pathlib import Path

from cousin_lib.runner.tmux_hook import PANE_PID_VAR
from cousin_lib.runner.tmux_pane import DENY_PREFIXES, denied  # the hard deny, in one place

BASE_ENV = ("HOME", "PATH", "USER", "LOGNAME", "SHELL", "SSH_AUTH_SOCK", "XDG_RUNTIME_DIR",
            "DBUS_SESSION_BUS_ADDRESS", "LANG", "LOCALE_ARCHIVE", "TZ", "COLORTERM", "TMPDIR")
            # plus every LC_*; TERM comes from tmux
FORBIDDEN = ("-p", "--print", "--output-format", "--input-format", "--strict-mcp-config")
SWITCHES = {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1", "DISABLE_AUTOUPDATER": "1"}
CONTEXT = ("data", "run", "tmux-context.md")
SESSION_FLAGS = ("--session-id", "--resume")    # a fresh session's id, or the one to resume
REFUSED_KINDS = ("claude-token", "anthropic-key")
EXIT_NOTE = ("data", "run", "tmux-launch-exit.txt")   # the last refusal, for the runner (its pane dies)


def env_base(shell_env, *, env_allow=()):
    """The pane's `env -i` allowlist from the runner's environment: BASE_ENV,
    every LC_*, and the names `[agent] env_allow` lists; then the hard deny
    (tmux_pane.denied: CLAUDE*, ANTHROPIC*, an auth variable or a
    credential-shaped name), which beats env_allow."""
    names = set(BASE_ENV) | set(env_allow)
    out = {k: v for k, v in shell_env.items() if k in names or k.startswith("LC_")}
    return {k: v for k, v in out.items() if not denied(k)}


def env_allow_of(agent):
    """`[agent] env_allow` as a tuple of variable names (default none): a list
    of names, never one the hard deny takes (it would be dropped anyway, so
    naming one is a mistake to say). ValueError naming the key."""
    names = agent.get("env_allow", [])
    if not isinstance(names, list) or not all(
            isinstance(n, str) and n and (n[0].isalpha() or n[0] == "_")
            and all(c.isalnum() or c == "_" for c in n) for n in names):
        raise ValueError("cousin.toml [agent] env_allow must be a list of variable names")
    refused = [n for n in names if denied(n)]
    if refused:
        raise ValueError("cousin.toml [agent] env_allow names %s: CLAUDE*, ANTHROPIC* and"
                         " credentials never reach the pane (the hard deny)"
                         % ", ".join(refused))
    return tuple(names)


def argv(*, home, session, model=None, effort=None, fresh, launcher=None):
    """The pane's command after `env -i <base>`: this launcher (by default
    this file), run by absolute path, then the CLI's argv. `session` is
    (flag, id), TmuxRunner's shape: "--session-id" mints the session,
    "--resume" resumes it. `fresh` asks the launcher to append the context
    block. A model or an effort left unset is the CLI's own default."""
    flag, session_id = session
    if flag not in SESSION_FLAGS:
        raise ValueError("session flag must be one of %s, got %r"
                         % (", ".join(SESSION_FLAGS), flag))
    launcher = Path(launcher) if launcher is not None else Path(__file__)
    cmd = [sys.executable, str(launcher.resolve()), "--home", str(home)]
    if fresh:
        cmd.append("--fresh")
    cmd += ["--", "claude"]
    if model:
        cmd += ["--model", str(model)]
    if effort:
        cmd += ["--effort", str(effort)]
    cmd += [flag, str(session_id), "--dangerously-skip-permissions",
            "--setting-sources", "project,local"]
    return cmd


def _say(message):
    print("tmux-launch: %s" % message, file=sys.stderr, flush=True)


def _refuse(home, message):
    """Say it, and leave it in the home: the pane dies with this process,
    so the runner reads why from EXIT_NOTE when it gives up."""
    _say(message)
    try:
        path = Path(home).joinpath(*EXIT_NOTE)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("tmux-launch: %s\n" % message)
    except OSError:
        pass
    return 2


def main(argv=None):
    """Exec the CLI with the account, the switches and the deny applied;
    2 (never an exec) on a refused account or argv."""
    from cousin_lib import accounts
    from cousin_lib.config import FrameworkConfig
    args = list(sys.argv[1:] if argv is None else argv)
    if "--" not in args:
        _say("usage: tmux_launch --home H [--fresh] -- claude ...")
        return 2
    at = args.index("--")
    parser = argparse.ArgumentParser(prog="tmux_launch", add_help=False)
    parser.add_argument("--home", required=True)
    parser.add_argument("--fresh", action="store_true")
    opts = parser.parse_args(args[:at])
    cli = args[at + 1:]
    if not cli or any(a in FORBIDDEN or a.startswith(("--output-format=", "--input-format="))
                      for a in cli):
        _say("refused: the tmux kind runs the interactive CLI, never %s"
             % ", ".join(FORBIDDEN))
        return 2
    home = Path(opts.home)
    root = FrameworkConfig.root_from_home(home)
    try:
        account = accounts.for_cousin(home, root)
    except accounts.AccountsError as err:
        return _refuse(home, str(err))
    if account.kind in REFUSED_KINDS:
        return _refuse(home, "the tmux kind runs on a subscription login (host or a claude-login"
                       " account); %s accounts are refused until a login-free config dir is shown"
                       " to start with no menu (onboarding is skippable by"
                       " seeding, but a token's login screen is not measured)" % account.kind)
    try:
        own = accounts.account_env(account, root)
    except accounts.AccountsError as err:
        return _refuse(home, str(err))
    env = dict(os.environ)
    env.update(own)
    env.update(SWITCHES)
    env[PANE_PID_VAR] = str(os.getpid())      # exec keeps it: the CLI's pid
    keep = set(own) | set(SWITCHES) | {PANE_PID_VAR}
    env = {k: v for k, v in env.items() if k in keep or not denied(k)}
    if opts.fresh:
        context = Path(os.path.abspath(home.joinpath(*CONTEXT)))
        try:
            with open(context):
                pass
        except OSError as err:
            return _refuse(home, "fresh start without its context block %s: %s"
                           % (context, err))
        cli += ["--append-system-prompt-file", str(context)]
    try:
        home.joinpath(*EXIT_NOTE).unlink()     # an older refusal is not this start's
    except OSError:
        pass
    os.execvpe(cli[0], cli, env)
    return 0   # not reached


if __name__ == "__main__":
    sys.exit(main())
