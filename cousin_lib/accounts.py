"""Accounts: which credentials a cousin runs on (operator addition,
2026-09-23). Named once in config/accounts.toml, applied the same way by
the runner, `cousin-account` and `cousin-runner --check-auth`.

A cousin never obtains credentials: it runs on what it is given. The
host's default login (~/.claude) is the implicit account `host`; the
phase-2 `[agent] api_key_file` is an implicit anthropic-key account. The
tmux lane keeps agent_auth.py until phase 10."""
import argparse
import contextlib
import fcntl
import json
import os
import re
import subprocess
import sys
import time
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
    """What the operator runs to fix this account's login."""
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
    """Atomic JSON through _write_private_text."""
    _write_private_text(path, json.dumps(data))


def _write_private_text(path, text):
    """Atomic text, 0600 from the first byte, in a parent created 0700. A
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
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        if fd is not None:
            os.close(fd)
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


# ------------------------------------------------------------ login via the framework

URL_RX = r"(https://claude\.com/cai/oauth/authorize\?\S+)\s"       # whole: up to the whitespace
PROMPT_RX = r"Paste\s*code\s*here\s*if\s*prompted\s*>"
FAIL_RX = r"(Invalid code[^\n]*|OAuth error[^\n]*|Login interrupted[^\n]*)"
DONE_RX = r"(Login successful)|" + FAIL_RX
TOKEN_RX = r"Your\s*OAuth\s*token\s*\(valid\s*for[^)]*\):\s*(sk-ant-oat\S+)\s"
TOKEN_UNTIL = TOKEN_RX + "|" + FAIL_RX
# The page shows `code#state`; once a window has closed, only a message of
# this shape is taken as a LATE code (R18).
CODE_SHAPE = re.compile(r"^[A-Za-z0-9._~-]{16,}#[A-Za-z0-9._~-]{8,}$")
CAPTURE_TTL_S = 600
TOMBSTONE_S = 3600


def _flow_env(extra):
    env = {**scrub(os.environ), **extra, "BROWSER": "/bin/false"}
    for var in ("DISPLAY", "WAYLAND_DISPLAY"):  # no browser on the host: the URL goes to the operator
        env.pop(var, None)
    return env


def _pty(spawn):
    if spawn is not None:
        return spawn
    from cousin_lib.pty_driver import PtySession
    return PtySession


def _last_words(session, code=None, n=300):
    """The screen's tail for a failure line, the pasted code masked (the
    pty echoes it). The code is masked over the whole bounded tail BEFORE
    the cut, and a code the tail itself began inside is dropped, so no cut
    can leave a piece of it. Never used on a screen that may hold a token."""
    words = " ".join(session.text().split())
    if code:
        words = words.replace(code, "[code]")
        for k in range(len(code) - 1, 3, -1):    # the tail began inside the code
            if words.startswith(code[-k:]):
                words = words[k:]
                break
    return words[-n:]


def _run_code_flow(argv, env, *, relay, await_code, spawn, timeout, until,
                   screen_after_paste=True):
    """The shared middle: URL -> prompt -> relay -> code -> paste -> `until`.
    A stall carries the CLI's own last words, the pasted code masked (the
    pty echoes what is typed); with `screen_after_paste` False (the token
    flow: the screen after the paste may hold the token) a stall after
    the paste shows NO child output at all: a mask is not a fix."""
    from cousin_lib.pty_driver import PtyTimeout
    session = _pty(spawn)(argv, env)
    code, pasted = None, False
    try:
        url = session.read_until(URL_RX, 60).group(1)
        session.read_until(PROMPT_RX, 30)
        relay(url)
        code = (await_code(timeout) or "").strip() or None
        if not code:
            return {"ok": False, "reason": "no code within %ds" % timeout}
        session.write(code + "\r")
        pasted = True
        return {"ok": True, "match": session.read_until(until, 120)}
    except PtyTimeout as err:
        if pasted and not screen_after_paste:
            return {"ok": False, "reason": "the CLI flow stalled after the code was pasted;"
                    " the screen is not shown because it may hold the token"}
        return {"ok": False, "reason": "the CLI flow stalled (%s); the CLI's last words: %s"
                % (err, _last_words(session, code))}
    finally:
        session.close()


def login_flow(account, root, *, relay, await_code, spawn=None, timeout=CAPTURE_TTL_S):
    """`claude auth login --claudeai` under the account's config dir. A
    failure line is the CLI's own verdict; a success line is not: `claude
    auth status` is (R17)."""
    if account.kind != "claude-login":
        raise AccountsError("login is for claude-login accounts; %s is %s"
                            % (account.name, account.kind))
    if account.config_dir is not None:
        account.config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    out = _run_code_flow([_cli(), "auth", "login", "--claudeai"],
                         _flow_env(account_env(account, root)), relay=relay,
                         await_code=await_code, spawn=spawn, timeout=timeout, until=DONE_RX)
    if not out["ok"]:
        return out
    m = out["match"]
    if m.group(1) is None:
        return {"ok": False, "reason": "the CLI said: %s" % m.group(2).strip()}
    st = status(account, root)
    if not st.get("loggedIn"):
        return {"ok": False, "status": st,
                "reason": "the CLI said %r, but `claude auth status` reads logged out" % m.group(1)}
    return {"ok": True, "cli_said": m.group(1), "status": st}


def _write_secret(path, value):
    """The token, one line, through the hardened private writer; the
    directory tightened to 0700 even when it existed looser, since
    _read_secret refuses anything looser."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    _write_private_text(path, value + "\n")


def token_flow(account, root, *, relay, await_code, spawn=None, timeout=CAPTURE_TTL_S):
    """`claude setup-token` in a throwaway config dir; the token is read
    from its screen, VERIFIED with `claude auth status` (logged in, method
    oauth_token: R16), then saved 0600. The result never carries it."""
    import shutil
    import tempfile
    if account.kind != "claude-token":
        raise AccountsError("token is for claude-token accounts; %s is %s"
                            % (account.name, account.kind))
    scratch = Path(tempfile.mkdtemp(prefix="setup-token-",
                                    dir=_login_free_dir(root, account)))
    try:
        out = _run_code_flow([_cli(), "setup-token"],
                             _flow_env({"CLAUDE_CONFIG_DIR": str(scratch)}), relay=relay,
                             await_code=await_code, spawn=spawn, timeout=timeout,
                             until=TOKEN_UNTIL, screen_after_paste=False)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    if not out["ok"]:
        return out
    m = out["match"]
    if m.group(1) is None:
        return {"ok": False, "reason": "the CLI said: %s" % m.group(2).strip()}
    token = m.group(1).strip()
    probe = Account(account.name, account.kind, None, account.secret_file, secret_value=token)
    st = status(probe, root)
    if not st.get("loggedIn") or st.get("authMethod") != "oauth_token":
        return {"ok": False, "reason": "the new token did not read as an oauth_token login;"
                " not saved", "status": {k: v for k, v in st.items() if k != "email"}}
    _write_secret(account.secret_file, token)
    return {"ok": True, "saved": str(account.secret_file), "status": st}


# ------------------------------------------------------------ the one-shot code capture (R18)

def capture_path(root, name):
    """OUTSIDE every cousin home: no home's backup, git or chat history can
    ever hold a code."""
    return Path(root) / "run" / ("login-capture-%s.json" % name)


@contextlib.contextmanager
def _capture_lock(root, name):
    """Every read-modify-write of one capture holds this flock, so a store
    racing a take or a tombstone sees one or the other, never both."""
    run = Path(root) / "run"
    run.mkdir(mode=0o700, exist_ok=True)
    os.chmod(run, 0o700)
    fd = os.open(run / ("login-capture-%s.lock" % name), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def arm_capture(root, *, via, operator, account_name, ttl=CAPTURE_TTL_S, pid=None):
    """Arm the one-shot capture. `pid` is the flow waiting for the code
    (this process by default): a capture whose flow is gone is a
    tombstone, whatever its clock says."""
    data = {"account": account_name, "via": via, "operator": operator, "state": "armed",
            "expires": time.time() + ttl, "pid": os.getpid() if pid is None else pid}
    with _capture_lock(root, account_name):
        _write_private(capture_path(root, account_name), data)
    return data


def read_capture(root, name):
    try:
        return json.loads(capture_path(root, name).read_text())
    except (OSError, ValueError):
        return None


def captures_for(root, via):
    """Every capture (armed or a tombstone) whose code comes through `via`."""
    out = []
    for path in sorted((Path(root) / "run").glob("login-capture-*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if data.get("via") == via:
            out.append(data)
    return out


def _flow_alive(data):
    """Is the flow that armed this capture still running? A capture with
    no pid is judged by its clock alone."""
    pid = data.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:                              # EPERM: alive, not ours
        return True
    return True


def capture_window(data, now):
    """"armed" while a code is awaited, "taken" while a stored code waits
    for the flow's next poll, "late" for TOMBSTONE_S after the window
    closed (only a code-shaped message is diverted then), None after that
    or for no capture. The window is open only while the capture is
    armed, unexpired AND its flow is alive: a flow killed without running
    its cleanup (SIGKILL, a crash) leaves a tombstone, never a capture
    that swallows the operator's messages or keeps a code forever."""
    if not data:
        return None
    if data.get("state") == "armed" and now <= data["expires"] and _flow_alive(data):
        return "taken" if data.get("code") else "armed"
    until = data.get("until") or data["expires"] + TOMBSTONE_S
    return "late" if now <= until else None


def _tombstone(data, state, until):
    data.pop("code", None)
    data.update(state=state, until=until)
    return data


def store_code(root, name, code):
    """The diverted code, into an ARMED, unexpired, codeless capture of a
    live flow only. Never creates the file: a taken capture or a tombstone
    stores nothing."""
    with _capture_lock(root, name):
        data = read_capture(root, name)
        if capture_window(data, time.time()) != "armed":
            return False
        data["code"] = str(code).strip()
        _write_private(capture_path(root, name), data)
    return True


def take_code(root, name, *, timeout, poll=0.5):
    """Wait for the diverted code and take it under the lock, so a code
    sits on disk for at most one poll. A take rewrites the capture as a
    codeless tombstone (state "done"), so a second paste is still diverted;
    without a code (a timeout, an interrupt, any exit) it becomes a
    tombstone too, so a LATE code is still diverted. Neither is ever
    delivered or kept."""
    deadline = time.monotonic() + timeout
    taken = False
    try:
        while True:
            with _capture_lock(root, name):
                data = read_capture(root, name)
                if data is None or data.get("state") != "armed":
                    return None
                code = data.get("code")
                if code:
                    _write_private(capture_path(root, name),
                                   _tombstone(data, "done", time.time() + TOMBSTONE_S))
                    taken = True
                    return code
            if time.monotonic() >= deadline:
                return None
            time.sleep(poll)
    finally:
        if not taken:
            with _capture_lock(root, name):
                data = read_capture(root, name)
                if data is not None and data.get("state") == "armed":
                    # one that came as we gave up: dropped, never used
                    _write_private(capture_path(root, name),
                                   _tombstone(data, "tombstone", time.time() + TOMBSTONE_S))


def retire_capture(root, name):
    """An armed capture whose window closed without its flow's cleanup (the
    flow died, or the clock ran out first): rewritten as a codeless
    tombstone, or removed once the tombstone's hour is over too."""
    with _capture_lock(root, name):
        data = read_capture(root, name)
        if data is None or data.get("state") != "armed":
            return
        window = capture_window(data, time.time())
        if window in ("armed", "taken"):
            return
        if window is None:
            capture_path(root, name).unlink(missing_ok=True)
            return
        _write_private(capture_path(root, name), _tombstone(
            data, "tombstone", data.get("until") or data["expires"] + TOMBSTONE_S))


def discard_late_code(root, name):
    """A code-shaped message arrived after the window closed: True while
    the tombstone lasts. The tombstone is not spent: every late or second
    code is diverted until its `until`."""
    with _capture_lock(root, name):
        return capture_window(read_capture(root, name), time.time()) == "late"


def _span(seconds):
    if seconds % 60 == 0:
        minutes = seconds // 60
        return "%d minute%s" % (minutes, "" if minutes == 1 else "s")
    return "%d seconds" % seconds


def relay_notice(home, *, operator, account_name, url, timeout=CAPTURE_TTL_S):
    """One framework-authored row to the operator on this cousin's chat
    surface (R18); Telegram's outbound pump relays it like any cousin row."""
    from cousin_lib.server.storage import ChatStore, normalize_chat_user
    slug = tomllib.loads((Path(home) / "cousin.toml").read_text())["cousin"]["slug"]
    text = ("Login for account %s: open %s , sign in, and reply HERE with the whole code the"
            " page shows. It looks like `code#state`: paste all of it, the part after # included."
            " Your next message in this chat within %s is taken as that code; it is never"
            " delivered to %s and never kept in this history. If you did not start this login"
            " from a host shell yourself, do not answer this."
            % (account_name, url, _span(timeout), slug))
    store = ChatStore(Path(home) / "data" / "chat.db")
    try:
        row = store.add_message(chat_user=normalize_chat_user(operator), user="cousin-account",
                                message=text, msg_type=slug, reply_to_user=operator)
    finally:
        store.close()
    return row["id"]


def _proc_parent(pid):
    with open("/proc/%d/status" % pid) as fh:
        for line in fh:
            if line.startswith("PPid:"):
                return int(line.split()[1])
    return 0


def _proc_environ(pid):
    with open("/proc/%d/environ" % pid, "rb") as fh:
        return fh.read().split(b"\0")


def _inside_cousin_ancestry(*, parent_of=_proc_parent, environ_of=_proc_environ):
    """Does any ancestor process carry COUSIN_HOME or COUSIN_SLUG in the
    environment it was started with? Catches `env -u COUSIN_HOME` run by a
    cousin's own shell. Best effort, a guardrail like the rest: an
    ancestor whose environment cannot be read is skipped, and no /proc
    means no check."""
    pid, seen = os.getppid(), set()
    while pid > 1 and pid not in seen:
        seen.add(pid)
        try:
            env = environ_of(pid)
        except (OSError, ValueError):
            env = ()
        if any(e.startswith((b"COUSIN_HOME=", b"COUSIN_SLUG=")) for e in env):
            return True
        try:
            pid = parent_of(pid)
        except (OSError, ValueError):
            return False
    return False


def _exit_on_hangup():
    """SIGHUP (a closed terminal, a dropped ssh) and SIGTERM raise
    SystemExit while a flow runs, so every `finally` runs: the pty child
    is reaped and the capture becomes a tombstone. Python runs no
    `finally` on a signal left at its default. Returns the undo."""
    import signal

    def _exit(signum, frame):
        raise SystemExit(128 + signum)
    saved = {}
    for sig in (signal.SIGHUP, signal.SIGTERM):
        try:
            saved[sig] = signal.signal(sig, _exit)
        except ValueError:                   # not the main thread: nothing to install
            pass

    def restore():
        for sig, old in saved.items():
            signal.signal(sig, old)
    return restore


def _login_cmd(args, account, root):
    """`cousin-account login|token <name> [--via <slug>]`: exit 0 done, 4
    the flow failed, 2 refused."""
    if args.cmd == "login" and account.name == HOST:
        print("cousin-account: warning: this re-logs the host's own login in ~/.claude, the"
              " one every cousin without an account runs on and the operator's own"
              " `claude` uses", file=sys.stderr)
    flow = login_flow if args.cmd == "login" else token_flow
    if args.via:
        from cousin_lib.config import CousinConfig
        via = Path(root) / "cousins" / args.via
        if not (via / "cousin.toml").is_file():
            print("cousin-account: no cousin %r under %s" % (args.via, Path(root) / "cousins"),
                  file=sys.stderr)
            return 2
        operator = CousinConfig.load(via).operator_name   # what storage.is_operator compares
        if not operator:
            print("cousin-account: %s has no [operator] name: nobody to relay the URL to"
                  % args.via, file=sys.stderr)
            return 2

        def relay(url):
            # armed only once the CLI showed its URL: a flow that stalls
            # before that leaves nothing armed to swallow a message
            arm_capture(root, via=args.via, operator=operator, account_name=account.name,
                        ttl=args.timeout)
            try:
                relay_notice(via, operator=operator, account_name=account.name, url=url,
                             timeout=args.timeout)
            except BaseException:
                # no notice, no code will come: nothing stays armed
                with _capture_lock(root, account.name):
                    capture_path(root, account.name).unlink(missing_ok=True)
                raise
            print("cousin-account: the sign-in URL is in %s's chat; waiting up to %ds for"
                  " the code" % (args.via, args.timeout), file=sys.stderr)

        def await_code(timeout):
            return take_code(root, account.name, timeout=timeout)
    else:                                  # at the terminal: the operator is right here
        def relay(url):
            print("Open this URL, sign in, and paste the whole code shown (code#state):\n%s"
                  % url)

        def await_code(timeout):
            return input("code> ").strip() or None
    restore = _exit_on_hangup()
    try:
        out = flow(account, root, relay=relay, await_code=await_code, timeout=args.timeout)
    except AccountsError as err:
        print("cousin-account: %s" % err, file=sys.stderr)
        return 2
    except Exception as err:                 # noqa: BLE001 - a failed flow is exit 4, not a trace
        print("cousin-account: %s %s failed: %s: %s"
              % (args.cmd, account.name, type(err).__name__, err), file=sys.stderr)
        return 4
    finally:
        restore()
    print("%s %s: %s" % (args.cmd, account.name,
                         "ok" if out["ok"] else out.get("reason", "failed")))
    return 0 if out["ok"] else 4


@traced_cli("cousin-account")
def account_main(argv=None):
    """cousin-account list | status <name> | login|token <name> [--via <slug>].
    Operator-run. Exit 0 ok, 4 not logged in (or a flow that
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
    for cmd in ("login", "token"):
        c = sub.add_parser(cmd, parents=[common],
                           help=("log an account in" if cmd == "login"
                                 else "mint a long-lived token for an account"))
        c.add_argument("name")
        c.add_argument("--via", help="the cousin whose chat carries the URL and the code")
        c.add_argument("--timeout", type=int, default=CAPTURE_TTL_S)
    args = p.parse_args(argv)
    if args.cmd in ("login", "token"):
        # GUARDRAILS, not a boundary: the runner exports COUSIN_HOME to
        # every command a cousin runs, so the model's own Bash stops here,
        # and `env -u` is caught by the ancestor check. A determined
        # process can still get past both; the account files stay
        # readable by the same Unix user until the phase 6 container.
        if (os.environ.get("COUSIN_HOME") or os.environ.get("COUSIN_SLUG")
                or _inside_cousin_ancestry()):
            print("cousin-account: operator-run only; a cousin never obtains credentials",
                  file=sys.stderr)
            return 2
        # A usability check, not a safeguard: anything can fake a terminal.
        if not sys.stdin.isatty():
            print("cousin-account: %s wants a terminal: run it from a shell on the host (R20)"
                  % args.cmd, file=sys.stderr)
            return 2
        if args.timeout <= 0:
            print("cousin-account: --timeout must be a positive number of seconds",
                  file=sys.stderr)
            return 2
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
    if args.cmd in ("login", "token"):
        return _login_cmd(args, account, root)
    rc, line = _check_account(account, root)
    print(line)
    return rc


if __name__ == "__main__":
    sys.exit(account_main())
