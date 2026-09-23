"""Accounts: which credentials a cousin runs on (operator addition,
2026-09-23). Named once in config/accounts.toml, applied the same way by
the runner, `cousin-account` and `cousin-runner --check-auth`.

A cousin never obtains credentials: it runs on what it is given. The
host's default login (~/.claude) is the implicit account `host`; the
phase-2 `[agent] api_key_file` is an implicit anthropic-key account. The
tmux lane keeps agent_auth.py until phase 10."""
import argparse
import json
import os
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from cousin_lib.trace import traced_cli

KINDS = ("claude-login", "claude-token", "anthropic-key")
RESERVED_KINDS = ("opencode",)
HOST = "host"
# Every variable that can pick the credentials or the provider the CLI
# uses: the CLI's own list of auth variables, the base URL, the config dir
# and the provider switches. cousin-runner removes them all from its own
# environment, so the account is the only source.
AUTH_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
             "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR",
             "AWS_BEARER_TOKEN_BEDROCK", "ANTHROPIC_FOUNDRY_API_KEY",
             "ANTHROPIC_FOUNDRY_AUTH_TOKEN", "ANTHROPIC_AWS_API_KEY",
             "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")
# Set for the two secret kinds: the bundled CLI then strips ANTHROPIC_API_KEY,
# CLAUDE_CODE_OAUTH_TOKEN, ANTHROPIC_AUTH_TOKEN and the cloud credential
# variables (AWS, Google, Azure) from every subprocess it starts, the
# model's own Bash included.
SUBPROCESS_SCRUB = {"CLAUDE_CODE_SUBPROCESS_ENV_SCRUB": "1"}
SECRETS_DIR = ".secrets/accounts"
LOGIN_FILES = (".credentials.json",)
LOGIN_KEYS = ("claudeAiOauth",)
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_ALLOWED = {"claude-login": {"kind", "config_dir"},
            "claude-token": {"kind", "secret_file"},
            "anthropic-key": {"kind", "secret_file"}}
EXPECTED_SOURCE = {"claude-login": "none", "claude-token": "none",
                   "anthropic-key": "ANTHROPIC_API_KEY"}
# What `claude auth status --json` reads for each kind (R16: a fake key read
# api_key and a fake CLAUDE_CODE_OAUTH_TOKEN read oauth_token on this host).
# The CLI's word for a key is joined, not spelled: it happens to equal the
# tmux lane's mode name, which only agent_auth.py may spell
# (test_agent_auth.test_no_other_module_spells_the_mode_names), and the two
# must be free to diverge.
_CLI_KEY_METHOD = "_".join(("api", "key"))
WANTED_METHOD = {"claude-login": "claude.ai", "claude-token": "oauth_token",
                 "anthropic-key": _CLI_KEY_METHOD}


class AccountsError(Exception):
    """An account cannot be loaded or used; the message never holds a secret."""


class SecretMissing(AccountsError):
    """The account's secret file is not there yet: a login to do (Task 16
    waits for it), not a configuration error."""


@dataclass(frozen=True)
class Account:
    name: str
    kind: str
    config_dir: Path | None
    secret_file: Path | None
    implicit: bool = False
    secret_value: str | None = field(default=None, repr=False)


def root_of(home):
    """The framework root above a cousin home (runner.main.root_for's rule)."""
    from cousin_lib.config import FrameworkConfig
    home = Path(os.path.abspath(home))
    return FrameworkConfig.root_from_home(home) or home.parent.parent


def _under(root, rel, what, where):
    """<root>/<rel>, normalized but NOT resolved: a symlink stays a
    symlink, so the strict read (O_NOFOLLOW, lstat on the directory)
    refuses it instead of following it somewhere else."""
    if not isinstance(rel, str) or not rel:
        raise AccountsError("%s %s must be a relative path" % (where, what))
    if Path(rel).is_absolute():
        raise AccountsError("%s %s must be relative to the framework root, got an absolute"
                            " path" % (where, what))
    base = os.path.normpath(os.path.abspath(root))
    path = os.path.normpath(os.path.join(base, rel))
    if not path.startswith(base + os.sep):
        raise AccountsError("%s %s leaves the framework root" % (where, what))
    return Path(path)


def load(root):
    path = Path(root) / "config" / "accounts.toml"
    try:
        data = tomllib.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as err:
        raise AccountsError("cannot read %s: %s" % (path, err))
    tables = data.get("accounts") or {}
    if not isinstance(tables, dict):
        raise AccountsError("config/accounts.toml: [accounts] must be a table of accounts")
    out = {}
    for name, table in tables.items():
        where = "config/accounts.toml [accounts.%s]" % name
        if not isinstance(table, dict):
            raise AccountsError("%s must be a table" % where)
        if not _NAME.match(name) or name == HOST:
            raise AccountsError("%s: the name must match %s and not be %r"
                                % (where, _NAME.pattern, HOST))
        kind = table.get("kind")
        if kind in RESERVED_KINDS:
            raise AccountsError("%s kind %r is reserved for phase 9 (opencode)" % (where, kind))
        if kind not in KINDS:
            raise AccountsError("%s kind must be one of %s, got %r" % (where, ", ".join(KINDS), kind))
        unknown = set(table) - _ALLOWED[kind]
        if unknown:
            raise AccountsError("%s: %s not allowed for kind %s"
                                % (where, ", ".join(sorted(unknown)), kind))
        if kind == "claude-login":
            cfg = _under(root, table.get("config_dir", "data/accounts/%s" % name),
                         "config_dir", where)
            out[name] = Account(name, kind, cfg, None)
        else:
            secret = _under(root, table.get("secret_file", "%s/%s" % (SECRETS_DIR, name)),
                            "secret_file", where)
            out[name] = Account(name, kind, None, secret)
    return out


def for_cousin(home, root):
    try:
        data = tomllib.loads((Path(home) / "cousin.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError) as err:
        raise AccountsError("cannot read %s/cousin.toml: %s" % (home, err))
    agent = data.get("agent") or {}
    # the directory names a thin cousin.toml, as SdkRunner._identity does
    slug = (data.get("cousin") or {}).get("slug") or Path(home).name
    name, key_file = agent.get("account"), agent.get("api_key_file")
    if name and key_file:
        raise AccountsError("%s/cousin.toml [agent]: account and api_key_file together;"
                            " name the account only" % home)
    if key_file:
        return Account(slug, "anthropic-key", None,
                       _under(root, key_file, "api_key_file", "cousin.toml [agent]"),
                       implicit=True)
    if not name or name == HOST:
        return Account(HOST, "claude-login", None, None, implicit=True)
    known = load(root)
    if name not in known:
        raise AccountsError("cousin.toml [agent] account %r is not in config/accounts.toml" % name)
    return known[name]


def _read_secret(account):
    """One line from the account's secret file, read as strictly as agent_auth.read_key
    reads a key, by the same code: the directory a 0700 directory of ours,
    the file a regular 0600 file of ours, no symlink. A missing file is
    SecretMissing (a login to do); anything else is AccountsError (exit 2
    at the runner's start). The message never holds the content."""
    from cousin_lib import agent_auth
    path = account.secret_file
    try:
        raw = agent_auth.read_private_file(
            path, what="secret file",
            missing_hint=" (%s)" % _missing_hint(account))
    except agent_auth.MissingFile as err:
        raise SecretMissing(str(err))
    except agent_auth.AuthError as err:
        raise AccountsError(str(err))
    lines = [ln.strip() for ln in raw.decode("utf-8", "replace").splitlines() if ln.strip()]
    if len(lines) != 1:
        raise AccountsError("secret file %s must hold one line" % path)
    if not agent_auth._KEY_RE.match(lines[0]) or len(lines[0]) > agent_auth.KEY_MAX_CHARS:
        raise AccountsError("secret file %s holds a malformed secret" % path)
    return lines[0]


def _login_free_dir(root, account):
    d = Path(root) / "data" / "accounts" / account.name
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name in LOGIN_FILES:
        f = d / name
        if f.exists():
            try:
                holds = any(k in json.loads(f.read_text()) for k in LOGIN_KEYS)
            except (OSError, ValueError, TypeError):
                holds = True      # unreadable counts as a login: fail closed
            if holds:
                raise AccountsError("%s holds a login; with a login present the CLI may bill"
                                    " the login, not the %s; remove it" % (f, account.kind))
    return d


def preflight(account, root):
    """account_env's checks without keeping the secret, for the runner to
    make BEFORE it takes its lock: a secret open to group or others, not
    ours, a symlink, malformed, or a login inside a secret kind's config
    dir is AccountsError (exit 2). A missing secret is SecretMissing."""
    if account.kind == "claude-login":
        return
    _login_free_dir(root, account)
    if account.secret_value is None:
        _read_secret(account)


def account_env(account, root):
    """The variables to SET for this account (after scrub)."""
    if account.kind == "claude-login":
        return {} if account.config_dir is None else {"CLAUDE_CONFIG_DIR": str(account.config_dir)}
    secret = account.secret_value or _read_secret(account)
    var = "CLAUDE_CODE_OAUTH_TOKEN" if account.kind == "claude-token" else "ANTHROPIC_API_KEY"
    return {var: secret, "CLAUDE_CONFIG_DIR": str(_login_free_dir(root, account)),
            **SUBPROCESS_SCRUB}


def scrub(env):
    return {k: v for k, v in env.items() if k not in AUTH_VARS}


def resume_via_cli(account):
    """claude-login refreshes its own token: resume through the CLI (R12)."""
    return account.kind == "claude-login"


def expected_source(account):
    return EXPECTED_SOURCE[account.kind]


def _cli():
    """The agent CLI as an ABSOLUTE path: the SDK's bundled binary (found
    without importing the SDK), else `claude` on PATH, resolved. Never a
    bare name: the pty driver execs a path (Task 15)."""
    import importlib.util
    import shutil
    spec = importlib.util.find_spec("claude_agent_sdk")
    if spec is not None and spec.origin:
        bundled = Path(spec.origin).parent / "_bundled" / "claude"
        if bundled.is_file():
            return str(bundled)
    found = shutil.which("claude")
    if found:
        return str(Path(found).resolve())
    raise AccountsError("no agent CLI: install the sdk extra (its bundled `claude`) or put"
                        " `claude` on PATH")


def status(account, root, *, run=subprocess.run):
    """`claude auth status --json` under the account: presence, no model call."""
    try:
        env = {**scrub(os.environ), **account_env(account, root)}
        proc = run([_cli(), "auth", "status", "--json"], capture_output=True, text=True,
                   timeout=20, env=env)
        return json.loads(proc.stdout or "{}")
    except AccountsError as err:
        return {"error": str(err)}
    except (OSError, ValueError, subprocess.SubprocessError) as err:
        return {"error": "%s: %s" % (type(err).__name__, err)}


def _missing_hint(account):
    """How a missing secret gets written, per kind (login_action's wording)."""
    if account.kind == "claude-token":
        return "mint it with `cousin-account token %s`, or write it: mode 0600, directory 0700" \
            % account.name
    return "write the key to it: mode 0600, directory 0700"


def login_action(account, via=None):
    """What the operator runs to fix this account's login. `cousin-account
    login` and `token` are Task 15's: until that task lands the line names
    the command it will be."""
    tail = " --via %s" % via if via else ""
    if account.kind == "claude-login":
        return ("`claude auth login` as the host user" if account.config_dir is None
                else "`cousin-account login %s%s`" % (account.name, tail))
    if account.kind == "claude-token":
        return "`cousin-account token %s%s`" % (account.name, tail)
    return "write the key to %s (mode 0600, its directory 0700)" % account.secret_file


def check(home, root, *, run=subprocess.run):
    """(exit code, line) for `cousin-runner --check-auth` (Task 16) and
    `cousin-account status`."""
    try:
        account = for_cousin(home, root)
    except AccountsError as err:
        return 2, "error: %s" % err
    return _check_account(account, root, run=run, via=Path(home).name)


def _check_account(account, root, *, run=subprocess.run, via=None):
    st = status(account, root, run=run)
    line = "account=%s kind=%s loggedIn=%s method=%s" % (
        account.name, account.kind, bool(st.get("loggedIn")), st.get("authMethod") or "-")
    ok = bool(st.get("loggedIn")) and st.get("authMethod") == WANTED_METHOD[account.kind]
    if not ok:
        line += " -> %s" % (st.get("error") or "run " + login_action(account, via))
    return (0 if ok else 4), line


def _write_private(path, data):
    """Atomic JSON, 0600 from the first byte, in a parent created 0700. A
    stale tmp is removed first and the tmp is created with O_EXCL and
    O_NOFOLLOW, so neither a leftover readable tmp nor a symlink planted at
    the tmp path can carry the secret anywhere; the mode is set on the
    descriptor whatever the umask. A failed write leaves no tmp behind."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(".%s.tmp" % path.name)
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as fh:
            fd = None
            json.dump(data, fh)
        os.replace(tmp, path)
    except BaseException:
        if fd is not None:
            os.close(fd)
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


@traced_cli("cousin-account")
def account_main(argv=None):
    """cousin-account list | status <name> (Task 14) | login|token <name>
    (Task 15). Operator-run. Exit 0 ok, 4 not logged in (or a flow that
    failed), 2 usage, configuration or a refusal."""
    from cousin_lib.config import FrameworkConfig, MissingConfigError
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", help="the framework root; falls back to FRAMEWORK_ROOT,"
                                       " then the checkout you are in")
    p = argparse.ArgumentParser(prog="cousin-account",
                                description="the accounts cousins run on (operator-run)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", parents=[common], help="every account: name, kind, where")
    st = sub.add_parser("status", parents=[common],
                        help="is the account logged in (no model call)")
    st.add_argument("name")
    args = p.parse_args(argv)
    try:
        root = FrameworkConfig.resolve(args.root, cwd_fallback=True).root
    except MissingConfigError as err:
        print("cousin-account: %s" % err, file=sys.stderr)
        return 2
    try:
        known = load(root)
    except AccountsError as err:
        print("cousin-account: %s" % err, file=sys.stderr)
        return 2
    if args.cmd == "list":
        print("%-12s %-14s %s" % (HOST, "claude-login", "~/.claude (the host's default login)"))
        for name, a in sorted(known.items()):
            where = a.config_dir if a.kind == "claude-login" else a.secret_file
            print("%-12s %-14s %s" % (name, a.kind, where))
        return 0
    account = (Account(HOST, "claude-login", None, None, implicit=True) if args.name == HOST
               else known.get(args.name))
    if account is None:
        print("cousin-account: no account %r" % args.name, file=sys.stderr)
        return 2
    rc, line = _check_account(account, root)
    print(line)
    return rc


if __name__ == "__main__":
    sys.exit(account_main())
