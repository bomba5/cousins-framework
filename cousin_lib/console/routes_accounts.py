"""Console routes for accounts (docs/reference/console-api.md,
"Accounts"): the accounts list and a per-row status (no model call), the
validated accounts.toml writer (accounts.write_entry), the write-only
keys, the claude-login / claude-token flows and the opencode OAuth method
as long operations, and a cousin's check-auth / validate.

The credential rule: this is the operator's console, so the operator
entering a credential here is fine; nothing in the framework obtains one
on its own. A flow runs only when a logged-in console user starts it, the
sign-in URL is shown to that operator (GET .../flow), and the code the
page shows is posted to its own write-only route (POST .../code), never
into a chat: one code, taken once, while the flow waits for it, and
never stored, logged, echoed or broadcast. The routes that take or mint
a credential refuse when the console itself runs inside a cousin (the
guardrail cousin-account applies; accounts._inside_cousin_ancestry).

A login is the account's long operation, not a cousin's: it runs through
console/longop.py under the key "account:<name>" (a cousin slug never
holds a colon), so it has longop's one-at-a-time rule, its stages and
its `cousin-op` events (slug "account:<name>"), and its state is served
at GET /api/accounts/<name>/op. check-auth and validate are the cousin's
own long operation (GET /api/cousins/<slug>/op).

Test seams on req.server.state (never set by a request):
`accounts.pty` (the pty factory the flows spawn with),
`accounts.opencode_bin`, and `accounts.check_auth_command` (the argv
prefix that replaces `python -m cousin_lib.runner.main`)."""
from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import tomllib
from pathlib import Path

from cousin_lib import accounts
from cousin_lib.console import longop, router
from cousin_lib.console.app import HttpError

FLOW_PREFIX = "account:"
MIN_TIMEOUT_S, MAX_TIMEOUT_S = 30, 1800
CHECK_TIMEOUT_S = 60
VALIDATE_TIMEOUT_S = 90 + 60          # sdk.validate_account's own 90 s, plus the start
LINE_MAX = 400
RUNNER_MODULE = "cousin_lib.runner.main"
# What a status answer may carry: never the email or the organisation the
# CLI reports, never anything read from a secret.
STATUS_KEYS = ("loggedIn", "authMethod", "providers", "missing", "endpoint", "error")


# ---- helpers ----------------------------------------------------------------

def flow_key(name):
    return FLOW_PREFIX + name


def _root(req):
    return Path(req.server.root)


def _check_name(name):
    if not isinstance(name, str) or not accounts._NAME.match(name):
        raise HttpError(400, "bad account name")
    return name


def _known(req):
    try:
        return accounts.load(_root(req))
    except accounts.AccountsError as err:
        raise HttpError(400, str(err))


def _host():
    return accounts.Account(accounts.HOST, "claude-login", None, None, implicit=True)


def _account(req, name):
    """The named account (host is the implicit one), else 404."""
    _check_name(name)
    if name == accounts.HOST:
        return _host()
    account = _known(req).get(name)
    if account is None:
        raise HttpError(404, "no account %s in config/accounts.toml" % name)
    return account


def _operator_only():
    """cousin-account's guardrail, kept for the console: a process started
    inside a cousin (COUSIN_HOME or COUSIN_SLUG in its environment or an
    ancestor's) never takes or mints a credential."""
    if os.environ.get("COUSIN_HOME") or os.environ.get("COUSIN_SLUG"):
        raise HttpError(403, "operator-run only: this console runs inside a cousin, and a"
                             " cousin never obtains credentials")
    ancestor = accounts._inside_cousin_ancestry()
    if ancestor:
        raise HttpError(403, "operator-run only: this console was started inside a cousin"
                             " (ancestor process %d); a cousin never obtains credentials"
                             % ancestor)


def _require_user(req):
    """The credential routes want a person on the other end: a console
    login, even on a console with no users configured (where every other
    route is open)."""
    if req.user is None:
        raise HttpError(403, "a logged-in console user is required for accounts and their"
                             " credentials: add one with `cousin-console adduser <name>`,"
                             " then log in")
    return req.user


def _owner(req):
    """The session a flow is bound to: a digest of its token, never the token."""
    token = req.session_token or ""
    return hashlib.sha256(token.encode()).hexdigest() if token else None


def _login_running(req, name):
    """409 while a login runs on the account: its entry and its key stay put."""
    if longop.op_running(req.server, flow_key(name)):
        raise HttpError(409, "a login runs on account %s: wait for it or cancel it" % name,
                        busy=True)


def audit_path(root):
    return Path(root) / "data" / "accounts" / "audit.jsonl"


def _audit(req, kind, account, **extra):
    """Append one row to data/accounts/audit.jsonl: who (the console user)
    did what to which account. Never a value: no key, token, code or URL."""
    from datetime import datetime, timezone
    row = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "kind": kind,
           "actor": req.user, "account": account}
    row.update({k: v for k, v in extra.items() if v is not None})
    path = audit_path(_root(req))
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(path, "a") as fh:
        fh.write(json.dumps(row) + "\n")


def _under_rel(root, path, rel):
    base = os.path.realpath(os.path.join(root, rel))
    return os.path.realpath(path).startswith(base + os.sep)


def _private_secrets_dir(root):
    """<root>/.secrets, a directory of ours, 0700: made so when missing,
    tightened when it exists looser. A symlink or a stranger's directory
    is refused (os.makedirs would leave an intermediate at the umask)."""
    import stat as _stat
    path = Path(root) / ".secrets"
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        path.mkdir(mode=0o700)
        st = os.lstat(path)
    if _stat.S_ISLNK(st.st_mode) or not _stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise HttpError(400, ".secrets/ is not a directory of this user: fix it by hand")
    if _stat.S_IMODE(st.st_mode) != 0o700:
        os.chmod(path, 0o700)
    return path


def _rel(root, path):
    if path is None:
        return None
    try:
        return os.path.relpath(path, root)
    except ValueError:
        return str(path)


def _where(root, account):
    if account.name == accounts.HOST and account.implicit:
        return "~/.claude (the host's default login)"
    path = {"claude-login": account.config_dir, "opencode": account.data_dir}.get(
        account.kind, account.secret_file)
    return _rel(root, path)


def _lanes(account):
    from cousin_lib import agent_settings
    out = []
    for kind in agent_settings.kinds():
        try:
            accounts.check_lane(account, kind)
        except accounts.AccountsError:
            continue
        out.append(kind)
    return out


def _users(root):
    """{account name: [(slug, lane)]} for every runner cousin, from its
    cousin.toml alone (agent_settings.summary: `host` when it names none)."""
    from cousin_lib import agent_settings
    out = {}
    for toml in sorted((Path(root) / "cousins").glob("*/cousin.toml")):
        home = toml.parent
        summary = agent_settings.summary(home)
        if summary.get("account"):
            out.setdefault(summary["account"], []).append((home.name, summary["lane"]))
    return out


def _raw_tables(root):
    try:
        data = tomllib.loads((Path(root) / "config" / "accounts.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    tables = data.get("accounts")
    return tables if isinstance(tables, dict) else {}


def _secret(root, account):
    """{set, last4, error} of a secret-file kind; None for the others. The
    tail is shown only for a file under .secrets/: a secret_file set by
    hand elsewhere under the root never has any of its bytes served."""
    if account.kind not in ("claude-token", "anthropic-key") or account.secret_file is None:
        return None
    from cousin_lib.console import secrets
    state = secrets.secret_state(account.secret_file)
    if not _under_rel(root, account.secret_file, ".secrets"):
        state = dict(state, last4=None)
    return state


def _row(root, account, users, tables):
    entry = tables.get(account.name) if not account.implicit else None
    return {"name": account.name, "kind": account.kind, "implicit": account.implicit,
            "where": _where(root, account), "lanes": _lanes(account),
            "cousins": [slug for slug, _lane in users.get(account.name, [])],
            "entry": dict(entry) if isinstance(entry, dict) else None,
            "secret": _secret(root, account)}


def _brief_status(st):
    return {k: st.get(k) for k in STATUS_KEYS if k in (st or {})}


def _status(root, account):
    st = accounts.status(account, root)
    method = st.get("authMethod")
    ok = bool(st.get("loggedIn")) and method == accounts.WANTED_METHOD[account.kind]
    missing = st.get("missing") or []
    out = {"ok": ok, "name": account.name, "kind": account.kind,
           "loggedIn": bool(st.get("loggedIn")), "method": method,
           "error": st.get("error"),
           "action": None if ok else accounts.login_action(
               account, provider=missing[0] if missing else None)}
    for key in ("providers", "missing", "endpoint"):
        if key in st:
            out[key] = st[key]
    return out


def _timeout(body):
    value = body.get("timeout", accounts.CAPTURE_TTL_S)
    if not isinstance(value, int) or isinstance(value, bool) \
            or not MIN_TIMEOUT_S <= value <= MAX_TIMEOUT_S:
        raise HttpError(400, "timeout must be a whole number of seconds from %d to %d"
                        % (MIN_TIMEOUT_S, MAX_TIMEOUT_S))
    return value


def _emit(req, name, what):
    req.server.emit("accounts-change", {"name": name, "what": what})


# ---- the one-shot code window -------------------------------------------------

class Flow:
    """A running login's state beside its long operation: the sign-in URL
    (and opencode's instruction line) shown to the operator, and the
    one-shot window the code is handed through. The window is armed once
    the CLI shows its URL; the first code offered while it is armed is
    taken, and the window is closed for good after that, after a timeout
    or a cancel, so a second or a late code is never delivered. The code
    lives in memory only, for as long as the flow's thread takes to pick
    it up.

    A flow belongs to the console session that started it (`owner`, a
    digest of its session token): only that session is shown the URL and
    may post the code or cancel. The pty session is held only while it is
    open (_TrackedSession drops it on close), so the screen text, which
    may hold a code or a minted token, is released with it, and a cancel
    never signals a pid the CLI no longer owns."""

    def __init__(self, name, kind, provider=None, owner=None):
        self.name, self.kind, self.provider, self.owner = name, kind, provider, owner
        self.url = self.instructions = None
        self.cancelled = False
        self.session = None
        self._cond = threading.Condition()
        self._state = "idle"                  # idle -> armed -> taken | closed
        self._code = None

    @property
    def awaiting(self):
        with self._cond:
            return self._state == "armed"

    def arm(self):
        with self._cond:
            if self._state == "idle" and not self.cancelled:
                self._state = "armed"

    def offer(self, code):
        with self._cond:
            if self._state != "armed":
                return False
            self._code, self._state = code, "taken"
            self._cond.notify_all()
            return True

    def wait(self, timeout):
        with self._cond:
            self._cond.wait_for(lambda: self._state != "armed", timeout)
            code, self._code = self._code, None
            self._state = "closed"
            return code

    def close(self):
        with self._cond:
            self._code, self._state = None, "closed"
            self.url = self.instructions = None
            self.session = None
            self._cond.notify_all()

    def attach(self, session):
        with self._cond:
            self.session = session

    def detach(self, session):
        with self._cond:
            if self.session is session:
                self.session = None

    def cancel(self):
        """Close the window and end the CLI's wait: SIGTERM to its pid,
        only while its session is open (under the lock the session's
        close takes first, so the child is not reaped yet and the pid is
        still the CLI's)."""
        self.cancelled = True
        with self._cond:
            session = self.session
            pid = getattr(session, "pid", None)
            if session is not None and isinstance(pid, int) and pid > 0:
                try:
                    os.kill(pid, signal.SIGTERM)    # the CLI's read ends: the flow returns
                except OSError:
                    pass
        self.close()


class _TrackedSession:
    """The pty session as the library sees it; its close() first drops the
    flow's reference (under the flow's lock), then closes the pty."""

    def __init__(self, session, flow):
        self._session, self._flow = session, flow
        self.pid = getattr(session, "pid", None)

    def read_until(self, pattern, timeout):
        return self._session.read_until(pattern, timeout)

    def write(self, text):
        return self._session.write(text)

    def text(self):
        return self._session.text()

    def close(self, *args, **kw):
        self._flow.detach(self)
        session, self._session = self._session, None
        return session.close(*args, **kw) if session is not None else None


def _flows(server):
    return server.state.setdefault("account_flows", {})


def _spawn_for(server, flow):
    """The pty factory the flow runs with, keeping the session so a cancel
    can end the CLI's wait."""
    base = server.state.get("accounts.pty")

    def spawn(argv, env):
        session = _TrackedSession(accounts._pty(base)(argv, env), flow)
        flow.attach(session)
        if flow.cancelled:
            flow.cancel()
        return session
    return spawn


def _start_flow(req, account, kind, run, *, provider=None, params=None):
    """Start `run(op, flow)` as the account's long operation: 202 {op}, or
    409 while one runs on the account. The flow is bound to the session
    that started it; who started it is audited."""
    flow = Flow(account.name, kind, provider, owner=_owner(req))

    def work(op):
        try:
            out = run(op, flow)
        except accounts.AccountsError as err:     # its message never holds a secret
            raise longop.OpError(str(err))
        finally:
            flow.close()
        if flow.cancelled:
            raise longop.OpError("cancelled by the operator")
        if not out.get("ok"):
            raise longop.OpError(out.get("reason") or "the login failed")
        return out
    answer = longop.start_response(req.server, flow_key(account.name), kind, work,
                                   params=dict(params or {}, account=account.name))
    _flows(req.server)[account.name] = flow
    _audit(req, "login-start", account.name, flow=kind, provider=provider)
    return answer


def _code_flow(req, account, fn, timeout):
    """claude-login (fn = login_flow) and claude-token (token_flow): the
    URL to the operator, the code from POST .../code."""
    root = _root(req)
    server = req.server

    def run(op, flow):
        op.stage("start", "running", "starting the CLI")

        def relay(url):
            flow.url = url
            flow.arm()
            op.stage("start", "done")
            op.stage("sign in", "running", "open the sign-in URL, sign in, and paste the whole"
                                           " code the page shows (code#state) here")

        def await_code(wait):
            code = flow.wait(wait)
            if code:
                op.stage("sign in", "done", "code received")
                op.stage("verify", "running", "claude auth status")
            return code
        out = fn(account, root, relay=relay, await_code=await_code,
                 spawn=_spawn_for(server, flow), timeout=timeout)
        if out.get("ok"):
            op.stage("verify", "done", "logged in")
            return {"ok": True, "status": _brief_status(out.get("status"))}
        return {"ok": False, "reason": out.get("reason")}
    return run


def _opencode_flow(req, account, provider, method, timeout):
    root = _root(req)
    server = req.server

    def run(op, flow):
        op.stage("start", "running", "starting opencode auth login")

        def relay(url, instructions):
            flow.url, flow.instructions = url, instructions or None
            op.stage("start", "done")
            op.stage("sign in", "running", "open the URL and sign in; opencode finishes the"
                                           " login by itself, nothing to paste back")
        out = accounts.opencode_login_flow(
            account, root, provider=provider, method=method, relay=relay,
            spawn=_spawn_for(server, flow), binary=server.state.get("accounts.opencode_bin"),
            timeout=timeout)
        if out.get("ok"):
            op.stage("sign in", "done", "%s is in the account's auth.json" % provider)
            return {"ok": True, "provider": provider, "status": _brief_status(out.get("status"))}
        return {"ok": False, "reason": out.get("reason")}
    return run


# ---- check-auth / validate ------------------------------------------------------

def _run_child(argv, timeout):
    """(rc, stdout, stderr) of cousin-runner's check, in a child process of
    its own: without any accounts.AUTH_VARS variable (the account is the
    only source), in a session of its own so a timeout kills it and the
    CLI it started."""
    env = {k: v for k, v in os.environ.items() if k not in accounts.AUTH_VARS}
    package_root = str(Path(__file__).resolve().parents[2])
    env["PYTHONPATH"] = os.pathsep.join(p for p in (package_root, env.get("PYTHONPATH")) if p)
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, encoding="utf-8", errors="replace",
                                env=env, start_new_session=True)
    except OSError as err:
        return 2, "", "cannot start the check: %s" % err
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        proc.communicate()
        return 4, "", "no answer within %ds" % timeout
    return proc.returncode, out, err


def _said(out, err, *, last):
    lines = [ln.strip() for ln in (out or "").splitlines() if ln.strip()]
    if lines:
        return (lines[-1] if last else lines[0])[:LINE_MAX]
    tail = [ln.strip() for ln in (err or "").splitlines() if ln.strip()]
    return (tail[-1] if tail else "no output")[:LINE_MAX]


def _check_auth_work(server, home, validate):
    """One child: `--check-auth`, or `--check-auth --validate`, which runs
    the status check first and the turn only when it passed (runner/main
    _check_auth), so the status is never checked twice. Its stdout is the
    status line, then the validate line when a turn ran."""
    prefix = list(server.state.get("accounts.check_auth_command")
                  or [sys.executable, "-m", RUNNER_MODULE])
    argv = prefix + ["--home", str(home), "--check-auth"] + (["--validate"] if validate else [])

    def work(op):
        op.stage("status", "running", "claude auth status under the account (no model call)"
                 + ("; then one smallest model turn" if validate else ""))
        rc, out, err = _run_child(argv, VALIDATE_TIMEOUT_S if validate else CHECK_TIMEOUT_S)
        lines = [ln.strip()[:LINE_MAX] for ln in (out or "").splitlines() if ln.strip()]
        status_lines = [ln for ln in lines if not ln.startswith("validate:")]
        turn_lines = [ln for ln in lines if ln.startswith("validate:")]
        line = status_lines[0] if status_lines else _said("", err, last=True)
        if not validate:
            if rc != 0:
                op.stage("status", "failed", line)
                raise longop.OpError(line)
            op.stage("status", "done", line)
            return {"ok": True, "line": line}
        if not turn_lines:
            # the status failed (or the check could not run): no turn was spent
            op.stage("status", "failed" if rc != 0 else "done", line)
            op.stage("one model turn", "skipped", "no turn spent")
            if rc != 0:
                raise longop.OpError(line)
            return {"ok": True, "line": line}
        op.stage("status", "done", line)
        vline = turn_lines[-1]
        if rc != 0:
            op.stage("one model turn", "failed", vline)
            raise longop.OpError(vline)
        op.stage("one model turn", "done", vline)
        return {"ok": True, "line": line, "validate": vline}
    return work


def _confine(root, account):
    """The console's writer keeps an entry's paths where the framework puts
    them: a secret file and an opencode data dir under .secrets/, a login's
    config dir under data/accounts/. (accounts.toml by hand may say
    otherwise; the console never writes that.)"""
    for key, path, rel in (("secret_file", account.secret_file, ".secrets"),
                           ("data_dir", account.data_dir, ".secrets"),
                           ("config_dir", account.config_dir, os.path.join("data", "accounts"))):
        if path is not None and not _under_rel(root, path, rel):
            raise accounts.AccountsError("%s must be under %s/ (the console writes nothing"
                                         " else; edit accounts.toml by hand for another place)"
                                         % (key, rel))


def _url_view(url):
    """A sign-in URL is served as a link only when it is https://."""
    return {"url": url, "url_is_https": isinstance(url, str) and url.startswith("https://")}


# ---- routes -----------------------------------------------------------------------

def register():
    from cousin_lib.console._common import cousin_home

    @router.route("GET", "/api/accounts")
    def list_accounts(req):
        root = _root(req)
        error = None
        try:
            known = accounts.load(root)
        except accounts.AccountsError as err:
            known, error = {}, str(err)
        users, tables = _users(root), _raw_tables(root)
        listed = [_host()] + [known[n] for n in sorted(known)]
        return 200, {"ok": True, "error": error,
                     "accounts": [_row(root, a, users, tables) for a in listed],
                     "kinds": list(accounts.KINDS),
                     "fields": {k: sorted(v - {"kind"}) for k, v in accounts._ALLOWED.items()},
                     "timeout": accounts.CAPTURE_TTL_S}

    @router.route("GET", "/api/accounts/{name}/status")
    def account_status(req, name):
        account = _account(req, name)
        running = req.server.state.setdefault("account_status_running", set())
        lock = req.server.state.setdefault("account_status_lock", threading.Lock())
        with lock:
            if name in running:
                raise HttpError(409, "a status check of account %s is already running" % name,
                                busy=True)
            running.add(name)
        try:
            return 200, _status(_root(req), account)
        finally:
            with lock:
                running.discard(name)

    def _lane_check(root, name):
        def check(account):
            users = _users(root).get(name, [])
            if account is None:
                if users:
                    raise accounts.AccountsError(
                        "account %s is in use by %s: move them to another account first"
                        % (name, ", ".join(slug for slug, _ in users)))
                return
            _confine(root, account)
            for slug, lane in users:
                try:
                    accounts.check_lane(account, lane)
                except accounts.AccountsError as err:
                    raise accounts.AccountsError("cousin %s runs on account %s: %s"
                                                 % (slug, name, err))
        return check

    def _write(req, name, entry, expect):
        root = _root(req)
        try:
            account = accounts.write_entry(root, name, entry, expect=expect,
                                           check=_lane_check(root, name))
        except accounts.AccountsError as err:
            text = str(err)
            status = 409 if ("already" in text or "in use by" in text) else \
                404 if text.startswith("no account") else 400
            raise HttpError(status, text)
        what = "removed" if entry is None else ("added" if expect == "absent" else "edited")
        _audit(req, "entry-" + what, name)
        _emit(req, name, what)
        return account

    @router.route("POST", "/api/accounts")
    def add_account(req):
        _require_user(req)
        name, entry = req.body.get("name"), req.body.get("entry")
        _check_name(name)
        if not isinstance(entry, dict):
            raise HttpError(400, "entry must be a table of the account's keys")
        account = _write(req, name, entry, "absent")
        users, tables = _users(_root(req)), _raw_tables(_root(req))
        return 201, {"ok": True, "account": _row(_root(req), account, users, tables)}

    @router.route("POST", "/api/accounts/{name}")
    def edit_account(req, name):
        _require_user(req)
        _check_name(name)
        if name == accounts.HOST:
            raise HttpError(400, "host is the host's own login: it has no entry to edit")
        entry = req.body.get("entry")
        if not isinstance(entry, dict):
            raise HttpError(400, "entry must be a table of the account's keys")
        _login_running(req, name)
        account = _write(req, name, entry, "present")
        users, tables = _users(_root(req)), _raw_tables(_root(req))
        return 200, {"ok": True, "account": _row(_root(req), account, users, tables)}

    @router.route("POST", "/api/accounts/{name}/remove")
    def remove_account(req, name):
        _require_user(req)
        _check_name(name)
        if req.body.get("confirm") != name:
            raise HttpError(400, "type the account's name in confirm to remove it")
        _login_running(req, name)
        _write(req, name, None, "present")
        return 200, {"ok": True, "removed": name}

    @router.route("POST", "/api/accounts/{name}/key")
    def set_key(req, name):
        _require_user(req)
        account = _account(req, name)
        _operator_only()
        _login_running(req, name)
        value = req.body.get("key")
        if not isinstance(value, str):
            raise HttpError(400, "key must be a string")
        root = _root(req)
        if account.kind == "opencode":
            provider = req.body.get("provider")
            try:
                accounts.store_api_key(account, provider, value)
            except accounts.AccountsError as err:
                raise HttpError(400, str(err))
            finally:
                del value
            _audit(req, "key", name, provider=provider)
            _emit(req, name, "key")
            return 200, {"ok": True, "name": name, "provider": provider,
                         "status": _status(root, account)}
        if account.kind == "claude-login":
            raise HttpError(400, "a claude-login account holds no key: log in instead"
                                 " (POST /api/accounts/%s/login)" % name)
        from cousin_lib.console import secrets
        path = os.path.abspath(account.secret_file)
        if not _under_rel(root, path, ".secrets"):
            raise HttpError(400, "account %s's secret file is outside .secrets/: write it by"
                                 " hand (mode 0600, its directory 0700)" % name)
        within = _private_secrets_dir(root)
        try:
            state = secrets.write_secret_file(path, value, within=within)
        except ValueError as err:
            raise HttpError(400, str(err))
        finally:
            del value
        _audit(req, "key", name)
        _emit(req, name, "key")
        return 200, {"ok": True, "name": name, "secret": state}

    @router.route("POST", "/api/accounts/{name}/login")
    def login(req, name):
        _require_user(req)
        account = _account(req, name)
        _operator_only()
        timeout = _timeout(req.body)
        if account.kind == "opencode":
            provider, method = req.body.get("provider"), req.body.get("method")
            if not isinstance(method, str) or not method.strip():
                raise HttpError(400, "an opencode OAuth login names its method (the label"
                                     " opencode lists); an API key goes to .../key")
            try:
                accounts.check_opencode_login(account, provider, method)
            except accounts.AccountsError as err:
                raise HttpError(400, str(err))
            return _start_flow(req, account, "opencode-login",
                               _opencode_flow(req, account, provider, method, timeout),
                               provider=provider,
                               params={"provider": provider, "method": method})
        if account.kind != "claude-login":
            raise HttpError(400, "login is for claude-login and opencode accounts; %s is %s%s"
                            % (name, account.kind, " (mint one with .../token)"
                               if account.kind == "claude-token" else ""))
        if account.implicit and req.body.get("confirm_host") is not True:
            raise HttpError(400, "this re-logs the host's own login in ~/.claude, the one every"
                                 " cousin without an account and the operator's own `claude`"
                                 " use: send confirm_host true to go on")
        return _start_flow(req, account, "login",
                           _code_flow(req, account, accounts.login_flow, timeout))

    @router.route("POST", "/api/accounts/{name}/token")
    def token(req, name):
        _require_user(req)
        account = _account(req, name)
        _operator_only()
        timeout = _timeout(req.body)
        if account.kind != "claude-token":
            raise HttpError(400, "token is for claude-token accounts; %s is %s"
                            % (name, account.kind))
        return _start_flow(req, account, "token",
                           _code_flow(req, account, accounts.token_flow, timeout))

    @router.route("GET", "/api/accounts/{name}/op")
    def account_op(req, name):
        _check_name(name)
        return 200, {"ok": True, "op": longop.status(req.server, flow_key(name))}

    def _own_flow(req, name):
        """(flow, running op) of the account, the flow None when there is
        none; 403 when it belongs to another console session."""
        op = longop.status(req.server, flow_key(name))
        flow = _flows(req.server).get(name)
        if flow is None or not op or op["status"] != "running":
            return None, op
        if flow.owner is None or flow.owner != _owner(req):
            raise HttpError(403, "this login was started from another console session; only"
                                 " that session sees its URL, sends its code or cancels it")
        return flow, op

    @router.route("GET", "/api/accounts/{name}/flow")
    def flow_state(req, name):
        _check_name(name)
        op = longop.status(req.server, flow_key(name))
        flow = _flows(req.server).get(name)
        live = bool(flow) and bool(op) and op["status"] == "running"
        mine = live and flow.owner is not None and flow.owner == _owner(req)
        url = flow.url if mine else None
        return 200, {"ok": True, "name": name, "op": op,
                     "kind": flow.kind if flow else None,
                     "provider": flow.provider if flow else None,
                     "mine": mine, **_url_view(url),
                     "instructions": flow.instructions if mine else None,
                     "awaiting_code": bool(mine and flow.awaiting)}

    @router.route("POST", "/api/accounts/{name}/code")
    def code(req, name):
        _check_name(name)
        _require_user(req)
        _operator_only()
        value = req.body.get("code")
        flow, _op = _own_flow(req, name)
        if flow is None or not flow.awaiting:
            raise HttpError(409, "no login on account %s waits for a code" % name)
        if not isinstance(value, str) or not accounts.CODE_SHAPE.match(value.strip()):
            raise HttpError(400, "that is not the whole code the page shows: it looks like"
                                 " code#state, the part after # included")
        taken = flow.offer(value.strip())
        del value
        if not taken:
            raise HttpError(409, "no login on account %s waits for a code" % name)
        return 200, {"ok": True, "taken": True}

    @router.route("POST", "/api/accounts/{name}/cancel")
    def cancel(req, name):
        _check_name(name)
        _require_user(req)
        flow, _op = _own_flow(req, name)
        if flow is None:
            raise HttpError(409, "no login runs on account %s" % name)
        flow.cancel()
        _audit(req, "login-cancel", name)
        return 200, {"ok": True, "cancelled": True}

    @router.route("POST", "/api/cousins/{slug}/check-auth")
    def check_auth(req, slug):
        from cousin_lib import agent_settings
        home = cousin_home(req.server, slug)
        validate = req.body.get("validate", False)
        if not isinstance(validate, bool):
            raise HttpError(400, "validate must be true or false")
        lane = agent_settings.summary(home)["lane"]
        if lane == agent_settings.TMUX_LEGACY:
            raise HttpError(400, "%s is a tmux cousin: its auth is the [runtime] auth mode,"
                                 " not an account" % slug)
        if validate and lane != "sdk":
            raise HttpError(400, "validate runs one turn on the sdk lane; the %s lane has no"
                                 " validating turn (check-auth alone reports its presence)"
                            % lane)
        return longop.start_response(req.server, slug, "validate" if validate else "check-auth",
                                     _check_auth_work(req.server, home, validate),
                                     params={"validate": validate})


register()
