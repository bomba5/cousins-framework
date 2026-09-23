"""Auth on the SDK lane (Task 16): a login that is missing, expired or
revoked, and an account its billing stopped, are detected inside the turn,
kept apart from a rate limit, said where the operator looks, and picked
up again without a restart and without a turn spent per retry. The
credentials are the account's (Task 14); the runner never obtains any."""
import hashlib
import json
import socket
from datetime import datetime, timezone
from pathlib import Path

# The bundled CLI's wording (strings of claude_agent_sdk/_bundled/claude,
# 2.1.277): the fallback when no typed signal says it (a connect that
# raises; an error result with neither AssistantMessage.error nor
# api_error_status).
AUTH_PATTERNS = ("Not logged in", "Please run /login", "Invalid API key",
                 "API key is invalid", "OAuth access token is invalid",
                 "OAuth token revoked", "Session expired", "Invalid bearer token",
                 "authentication_failed")
AUTH_ERROR = "authentication_failed"
BILLING_ERROR = "billing_error"
LOGIN = "login_required"
BILLING = "billing"
BACKOFF_BASE_S = 1.0
BACKOFF_CAP_S = 300.0
LOGIN_FILE = "data/login-required.json"


def is_auth_text(text):
    return any(p in str(text or "") for p in AUTH_PATTERNS)


def assistant_signal(error, text=""):
    """The primary signal: the SDK's typed AssistantMessage.error. A
    logged-out session connects fine and says it here, in the turn."""
    if error == AUTH_ERROR:
        return {"reason": LOGIN, "detail": (text or AUTH_ERROR)[:300]}
    if error == BILLING_ERROR:
        return {"reason": BILLING, "detail": (text or BILLING_ERROR)[:300]}
    return None      # rate_limit is Task 12's; invalid_request, server_error, unknown: a failed turn


def retry_signal(data):
    """An `api_retry` system message: a 401 is a credential the CLI's
    retries will not fix, and the first attempt already says so."""
    data = data or {}
    if data.get("error_status") == 401 or data.get("error") == AUTH_ERROR:
        return {"reason": LOGIN, "detail": "HTTP %s %s on attempt %s" % (
            data.get("error_status"), data.get("error") or "", data.get("attempt"))}
    return None


def result_signal(is_error, api_error_status, result, errors):
    """The second signal: the error result. A 429 is never auth."""
    if not is_error or api_error_status == 429:
        return None
    if api_error_status == 401:
        return {"reason": LOGIN, "detail": "HTTP 401 %s" % str(result or "")[:280]}
    for text in [result, *(errors or ())]:
        if is_auth_text(text):
            return {"reason": LOGIN, "detail": str(text)[:300]}
    return None


def backoff_s(attempt):
    return min(BACKOFF_BASE_S * (2 ** int(attempt)), BACKOFF_CAP_S)


def credential_mark(account, root):
    """What changes when the operator fixes this account: the mtime and a
    hash of its credentials file (a login: `<config_dir>/.credentials.json`,
    the host's `~/.claude/.credentials.json`) or of its secret file (a
    token or a key). Hashed in memory, never stored. ("missing",) for a
    file that is not there; None for a key handed over in memory (no file
    to watch: the manual retry or a restart moves it)."""
    if account.kind == "claude-login":
        path = Path(account.config_dir or Path.home() / ".claude") / ".credentials.json"
    elif account.secret_value is not None or account.secret_file is None:
        return None
    else:
        path = Path(account.secret_file)
    try:
        return (path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
    except OSError:
        return ("missing",)


def host_label(root):
    """R14: config/harness.toml host_label, else the hostname."""
    from cousin_lib.config import MissingConfigError, harness_config
    try:
        label = (harness_config(root) or {}).get("host_label")
    except MissingConfigError:       # a broken harness.toml must not hide the login message
        label = None
    return str(label or socket.gethostname())


def billing_action(account, home):
    return ("check the billing (the plan or the credits) behind account %s (%s); then delete"
            " %s to retry" % (account.name, account.kind, Path(home) / LOGIN_FILE))


def _path(home):
    return Path(home) / LOGIN_FILE


def read_login_required(home):
    try:
        return json.loads(_path(home).read_text())
    except (OSError, ValueError):
        return None


def write_login_required(home, *, host, account, kind, reason, detail, action):
    prev = read_login_required(home) or {}
    data = {"host": host, "account": account, "kind": kind, "reason": reason,
            "detail": str(detail)[:300], "action": action,
            "since": prev.get("since") or datetime.now(timezone.utc).isoformat(timespec="seconds")}
    path = _path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(path)
    return data


def clear_login_required(home):
    try:
        _path(home).unlink()
        return True
    except FileNotFoundError:
        return False
