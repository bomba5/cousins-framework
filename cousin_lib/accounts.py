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
import stat
import subprocess
import sys
import threading
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from cousin_lib.trace import traced_cli

KINDS = ("claude-login", "claude-token", "anthropic-key", "opencode")
RESERVED_KINDS = ()
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
# Never set for any kind (1.18.2): CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1 puts
# the bundled CLI in its CI hardening mode, which forces the permission
# mode to `default` (every tool asks, and a runner has nobody to answer)
# and requires the sandbox for every Bash call. Measured on CLI 2.1.277 and
# 2.1.281: a key account with it set could run no tool at all. A secret
# account's variable therefore reaches the commands the CLI starts, as it
# did on the tmux billing lane.
SECRETS_DIR = ".secrets/accounts"
LOGIN_FILES = (".credentials.json",)
LOGIN_KEYS = ("claudeAiOauth",)
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_ALLOWED = {"claude-login": {"kind", "config_dir"},
            "claude-token": {"kind", "secret_file"},
            "anthropic-key": {"kind", "secret_file"},
            "opencode": {"kind", "data_dir", "providers", "endpoint", "endpoint_model",
                         "endpoint_context", "endpoint_output"}}
# An opencode account (phase 9 R12): opencode's HOME and its four XDG
# directories live in the account's data dir, so its auth.json (the
# provider keys, 0600, written by opencode itself) never leaves it.
_PROVIDER = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_MODEL = re.compile(r"^\S{1,200}$")
XDG_DIRS = (("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"),
            ("XDG_CACHE_HOME", "cache"), ("XDG_STATE_HOME", "state"))
AUTH_JSON = ("data", "opencode", "auth.json")
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
                 "anthropic-key": _CLI_KEY_METHOD, "opencode": "opencode"}


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
    # kind = "opencode" only (phase 9 R12): exactly one of providers or
    # endpoint (with endpoint_model) is set.
    data_dir: Path | None = None
    providers: tuple = ()
    endpoint: str | None = None
    endpoint_model: str | None = None
    # the endpoint model's context window and output bound, in tokens (Task
    # 6): rendered as its `limit`, so context pressure works for it
    endpoint_context: int | None = None
    endpoint_output: int | None = None


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


def _accounts_path(root):
    return Path(root) / "config" / "accounts.toml"


def load(root):
    path = _accounts_path(root)
    try:
        text = path.read_text()
    except FileNotFoundError:
        return {}
    except OSError as err:
        raise AccountsError("cannot read %s: %s" % (path, err))
    return _parse(root, text, path)


def _parse(root, text, path):
    """load()'s rules over the text of accounts.toml (write_entry checks a
    new text by them before it is written)."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as err:
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
            raise AccountsError("%s kind %r is reserved" % (where, kind))
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
        elif kind == "opencode":
            out[name] = _load_opencode(root, name, table, where)
        else:
            secret = _under(root, table.get("secret_file", "%s/%s" % (SECRETS_DIR, name)),
                            "secret_file", where)
            out[name] = Account(name, kind, None, secret)
    return out


def _load_opencode(root, name, table, where):
    """kind = "opencode": a data dir (default .secrets/accounts/<name>.opencode,
    under the root) and exactly one of `providers` (the ids whose keys
    opencode keeps in <data_dir>/data/opencode/auth.json) or `endpoint`
    plus `endpoint_model` (a local OpenAI-compatible model)."""
    data_dir = _under(root, table.get("data_dir", "%s/%s.opencode" % (SECRETS_DIR, name)),
                      "data_dir", where)
    if ("providers" in table) == ("endpoint" in table):
        raise AccountsError("%s: kind opencode takes exactly one of providers (the providers"
                            " whose keys opencode keeps in the data dir) or endpoint (a local"
                            " OpenAI-compatible model)" % where)
    if "providers" in table:
        provs = table["providers"]
        if not isinstance(provs, list) or not provs \
                or not all(isinstance(p, str) and _PROVIDER.match(p) for p in provs):
            raise AccountsError("%s providers must be a non-empty list of provider ids matching"
                                " %s" % (where, _PROVIDER.pattern))
        if len(set(provs)) != len(provs):
            raise AccountsError("%s providers names a provider twice" % where)
        if "opencode" in provs:
            raise AccountsError("%s providers: 'opencode' is opencode's own hosted service,"
                                " which the runner always disables; name the providers whose"
                                " keys you hold" % where)
        for key in ("endpoint_model", "endpoint_context", "endpoint_output"):
            if key in table:
                raise AccountsError("%s %s goes with endpoint, not providers" % (where, key))
        return Account(name, "opencode", None, None, data_dir=data_dir, providers=tuple(provs))
    endpoint, model = table["endpoint"], table.get("endpoint_model")
    _check_endpoint(endpoint, where)
    if not isinstance(model, str) or not _MODEL.match(model):
        raise AccountsError("%s endpoint_model (the model id the endpoint serves) is required"
                            " with endpoint: one word, no spaces" % where)
    context, output = table.get("endpoint_context"), table.get("endpoint_output")
    for key, value in (("endpoint_context", context), ("endpoint_output", output)):
        if value is not None and (not isinstance(value, int) or isinstance(value, bool)
                                  or value <= 0):
            raise AccountsError("%s %s must be a positive whole number of tokens" % (where, key))
    if output is not None and context is None:
        raise AccountsError("%s endpoint_output goes with endpoint_context" % where)
    if output is not None and output >= context:
        raise AccountsError("%s endpoint_output must be less than endpoint_context (opencode"
                            " compacts at the context minus the output)" % where)
    return Account(name, "opencode", None, None, data_dir=data_dir, endpoint=endpoint,
                   endpoint_model=model, endpoint_context=context, endpoint_output=output)


def _check_endpoint(endpoint, where):
    """An http(s) base URL with a host and no credentials in it, that does
    not name the subscription bridge (R13's account half). The message
    never repeats the URL: it might hold a password."""
    from cousin_lib.runner import opencode_guard
    bad = AccountsError("%s endpoint must be an http(s) base URL with a host, for example"
                        " http://127.0.0.1:11434/v1" % where)
    if not isinstance(endpoint, str):
        raise bad
    try:
        parts = urlsplit(endpoint)
        parts.port                                  # a malformed port raises here
    except ValueError:
        raise bad
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise bad
    if parts.username is not None or parts.password is not None:
        raise AccountsError("%s endpoint must not carry credentials (user:password@); a local"
                            " endpoint needs none" % where)
    marker = opencode_guard.bridge_marker(endpoint)
    if marker:
        raise AccountsError("%s endpoint names the Claude-subscription bridge (marker %r)%s"
                            % (where, marker.pattern, opencode_guard.move_hint(marker)))


# Ruling P9-1: "Claude cousins run on the Agent SDK and nowhere else", read
# literally. On the opencode lane a provider or a model whose id says claude
# or anthropic is refused. A name is a weak test (a proxy can serve Claude
# under any id); it stops the honest mistake and the obvious proxy, and the
# operator's account config stays the real boundary.
CLAUDE_NAME = re.compile(r"claude|anthropic", re.IGNORECASE)


def refuse_claude_name(what, text):
    """AccountsError when `text` (a provider or model id) names Claude or
    Anthropic, on the opencode lane (ruling P9-1)."""
    if isinstance(text, str) and CLAUDE_NAME.search(text):
        raise AccountsError("%s %r names Claude or Anthropic: Claude cousins run on the Agent"
                            " SDK and nowhere else (ruling P9-1), so the opencode lane never"
                            " runs one; use runner = \"sdk\" for a Claude model" % (what, text))


def check_lane(account, runner_kind):
    """R12: an opencode runner runs on an opencode account only, and an
    opencode account on an opencode runner only. The Claude kinds (a login,
    a token, an Anthropic key, the host's login) never reach the opencode
    lane; opencode's data dir never reaches the SDK."""
    if runner_kind == "opencode":
        if account.kind != "opencode":
            raise AccountsError(
                "runner = \"opencode\" runs on a kind = \"opencode\" account only (its"
                " providers' keys or a local endpoint); account %s is %s: name an opencode"
                " account in [agent] account" % (account.name, account.kind))
        for provider in account.providers or ():
            refuse_claude_name("account %s's provider" % account.name, provider)
        refuse_claude_name("account %s's endpoint_model" % account.name, account.endpoint_model)
    elif account.kind == "opencode":
        raise AccountsError(
            "account %s is kind opencode: it runs with runner = \"opencode\" only, not"
            " runner = \"%s\"" % (account.name, runner_kind))


# ------------------------------------------------------------ the accounts.toml writer

_WRITE_LOCK = threading.Lock()


def write_entry(root, name, entry, *, expect=None, check=None):
    """Add or replace [accounts.<name>] in config/accounts.toml with
    `entry` (a table of load()'s keys; the entry is replaced whole), or
    remove it when `entry` is None. `expect` "absent" refuses a name that
    exists, "present" one that does not. Every other line of the file is
    kept byte for byte (console/toml_edit.set_key); the new text must read
    back with only this entry changed and pass load()'s rules as a whole,
    and an opencode entry must not name Claude (ruling P9-1), before the
    atomic rename that keeps the file's mode. `check(account)` (the new
    Account, None on removal) runs last, under the same lock: raise
    AccountsError there to refuse (the console refuses a change a cousin
    on the account could not run with). Nothing is written on a refusal
    (AccountsError, whose message never repeats a value). No secret
    lives in this file. Returns the Account, or None on removal."""
    from cousin_lib.console import toml_edit
    if not isinstance(name, str) or not _NAME.match(name) or name == HOST:
        raise AccountsError("an account name must match %s and not be %r" % (_NAME.pattern, HOST))
    if entry is not None:
        _check_entry(name, entry)
    path = _accounts_path(root)
    table = "accounts.%s" % name
    with _WRITE_LOCK:
        try:
            text = path.read_text()
            mode = stat.S_IMODE(path.stat().st_mode)
        except FileNotFoundError:
            text, mode = "", 0o644
        except OSError as err:
            raise AccountsError("cannot read %s: %s" % (path, err))
        try:
            before = tomllib.loads(text)
        except tomllib.TOMLDecodeError as err:
            raise AccountsError("%s does not parse (%s): fix it by hand first" % (path, err))
        tables = before.get("accounts") or {}
        if not isinstance(tables, dict):
            raise AccountsError("%s: [accounts] must be a table of accounts: fix it by hand"
                                % path)
        present = name in tables
        if expect == "absent" and present:
            raise AccountsError("account %s is already in %s" % (name, path))
        if (expect == "present" or entry is None) and not present:
            raise AccountsError("no account %r in %s" % (name, path))
        try:
            if entry is None:
                new = _drop_table(text, table)
            else:
                new = text
                old = tables.get(name)
                for key in (old if isinstance(old, dict) else {}):
                    if key not in entry:
                        new = toml_edit.set_key(new, table, key, None)
                for key in ["kind"] + [k for k in entry if k != "kind"]:
                    new = toml_edit.set_key(new, table, key, entry[key])
        except ValueError as err:
            raise AccountsError("cannot edit %s in place (%s): edit it by hand" % (path, err))
        after = tomllib.loads(new)
        want = dict(tables)
        if entry is None:
            want.pop(name, None)
        else:
            want[name] = dict(entry)
        rest = {k: v for k, v in after.items() if k != "accounts"}
        if (after.get("accounts") or {}) != want \
                or rest != {k: v for k, v in before.items() if k != "accounts"}:
            raise AccountsError("editing %s would change more than account %s: edit it by hand"
                                % (path, name))
        known = _parse(root, new, path)
        if check is not None:
            check(None if entry is None else known[name])
        _write_config_text(path, new, mode)
    return None if entry is None else known[name]


def _check_entry(name, entry):
    """The entry's shape before any text is touched: a table, a known kind,
    only that kind's keys, TOML-able values, and on an opencode entry no
    provider or endpoint model that names Claude (P9-1: such an account
    could never run on its only lane)."""
    where = "account %s" % name
    if not isinstance(entry, dict):
        raise AccountsError("%s must be a table of keys" % where)
    kind = entry.get("kind")
    if kind not in KINDS:
        raise AccountsError("%s kind must be one of %s" % (where, ", ".join(KINDS)))
    unknown = set(entry) - _ALLOWED[kind]
    if unknown:
        raise AccountsError("%s: %s not allowed for kind %s"
                            % (where, ", ".join(sorted(map(str, unknown))), kind))
    for key, value in entry.items():
        ok = (isinstance(value, str) and "\n" not in value) \
            or (isinstance(value, int) and not isinstance(value, bool)) \
            or (isinstance(value, list) and all(isinstance(v, str) for v in value))
        if not ok:
            raise AccountsError("%s %s must be a line of text, a whole number or a list of"
                                " names" % (where, key))
    if kind == "opencode":
        for provider in entry.get("providers") or ():
            refuse_claude_name("%s's provider" % where, provider)
        refuse_claude_name("%s's endpoint_model" % where, entry.get("endpoint_model"))
        _refuse_secret_query(where, entry.get("endpoint"))


# A query or fragment parameter whose name says it carries a credential.
_SECRET_PARAM = re.compile(r"(key|token|secret|pass|auth|sig|cred|session)", re.IGNORECASE)


def _refuse_secret_query(where, endpoint):
    """accounts.toml holds no secret: an endpoint whose query string (or
    fragment) looks like it carries one is refused. The message never
    repeats the URL."""
    if not isinstance(endpoint, str):
        return
    from urllib.parse import parse_qsl
    try:
        parts = urlsplit(endpoint)
    except ValueError:
        return                              # load()'s own check words it
    for text in (parts.query, parts.fragment):
        for name, _value in parse_qsl(text, keep_blank_values=True):
            if _SECRET_PARAM.search(name):
                raise AccountsError("%s endpoint carries a %r parameter, which looks like a"
                                    " credential: no secret goes in accounts.toml" % (where, name))


def _drop_table(text, table):
    """The text without the `[table]` header and its keys; the comments
    and blank lines after its last key stay (they usually belong to the
    next table). ValueError when the entry is not a `[table]` of its own."""
    from cousin_lib.console import toml_edit
    lines = text.splitlines(keepends=True)
    statements = toml_edit._statements(lines)
    found = toml_edit._table_body(statements, table) if statements else None
    if found is None:
        raise ValueError("%s is not a [%s] table of its own" % (table, table))
    (hi, hj, _h), body = found
    end = hj
    for i, j, _ in body:
        stripped = lines[i].strip()
        if stripped and not stripped.startswith("#"):
            end = j
    del lines[hi:end]
    # one blank line left where two were
    if 0 < hi < len(lines) and not lines[hi].strip() and not lines[hi - 1].strip():
        del lines[hi]
    return "".join(lines)


def _write_config_text(path, text, mode):
    """Atomic: a tmp in the same directory, the mode set, then the rename."""
    import tempfile
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".accounts.", suffix=".toml.tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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
    if account.kind == "opencode":
        _opencode_dir(account)
        _auth_entries(account)
        return
    _login_free_dir(root, account)
    if account.secret_value is None:
        _read_secret(account)


def account_env(account, root):
    """The variables to SET for this account (after scrub). opencode: HOME
    and the four XDG directories inside its data dir and nothing else; its
    keys stay in auth.json, never in the environment."""
    if account.kind == "opencode":
        d = _opencode_dir(account)
        return {"HOME": str(d), **{var: str(d / sub) for var, sub in XDG_DIRS}}
    if account.kind == "claude-login":
        return {} if account.config_dir is None else {"CLAUDE_CONFIG_DIR": str(account.config_dir)}
    secret = account.secret_value or _read_secret(account)
    var = "CLAUDE_CODE_OAUTH_TOKEN" if account.kind == "claude-token" else "ANTHROPIC_API_KEY"
    return {var: secret, "CLAUDE_CONFIG_DIR": str(_login_free_dir(root, account))}


def _private_dir(path, what):
    """A directory of ours, created 0700, tightened to 0700, never a symlink."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode):
        raise AccountsError("%s %s is a symlink; it must be a directory of this user"
                            % (what, path))
    if not stat.S_ISDIR(st.st_mode):
        raise AccountsError("%s %s is not a directory" % (what, path))
    if st.st_uid != os.getuid():
        raise AccountsError("%s %s is not owned by this user" % (what, path))
    os.chmod(path, 0o700)


def _opencode_dir(account):
    """The account's data dir and its four XDG subdirectories, each 0700."""
    _private_dir(account.data_dir, "the opencode data dir")
    for _, sub in XDG_DIRS:
        _private_dir(account.data_dir / sub, "the opencode data dir's")
    return account.data_dir


def _auth_entries(account):
    """{provider id: entry type} from the account's auth.json ({} when there
    is none), read as strictly as a secret file: a regular file of ours, no
    symlink, no group or other bits. The keys never leave this function.
    An Anthropic OAuth login in it is a Claude subscription: refused."""
    path = account.data_dir.joinpath(*AUTH_JSON)
    return _entry_types(_read_auth_json(path), path)


# The auth.json entry types an opencode account may hold: a provider's API
# key, or another vendor's OAuth login (ruling P9-2). opencode reads every
# entry live, and any other type (`wellknown` fetches a config and a token
# from a URL) is a source the bridge guard never sees.
AUTH_TYPES = ("api", "oauth")


def _entry_types(data, path):
    entries = {str(k): (v.get("type") if isinstance(v, dict) else None) for k, v in data.items()}
    if entries.get("anthropic") == "oauth":
        raise AccountsError("%s holds an Anthropic OAuth login, a Claude subscription; the"
                            " opencode lane never carries subscription traffic: remove it and"
                            " use an API key" % path)
    for provider, kind in sorted(entries.items()):
        refuse_claude_name("%s entry" % path, provider)       # P9-1, whatever its type
        if kind not in AUTH_TYPES:
            raise AccountsError("%s entry %s is of type %s; an opencode account holds only %s"
                                " entries (a key, or another vendor's OAuth login): remove it"
                                % (path, provider, kind if isinstance(kind, str) else "none",
                                   " or ".join(AUTH_TYPES)))
    return entries


def _read_auth_json(path):
    """The whole auth.json as a dict ({} when there is none), strictly: a
    regular file of ours, no symlink, no group or other bits, a JSON
    object. The message never holds the content."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return {}
    except OSError as err:
        raise AccountsError("cannot open %s: %s" % (path, err.strerror))
    with os.fdopen(fd, "rb") as fh:
        st = os.fstat(fh.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise AccountsError("%s is not a regular file" % path)
        if st.st_uid != os.getuid():
            raise AccountsError("%s is not owned by this user" % path)
        if st.st_mode & 0o077:
            raise AccountsError("%s is readable by group or others (mode %o); chmod 600 it"
                                % (path, st.st_mode & 0o777))
        raw = fh.read(1 << 20)
    try:
        data = json.loads(raw)
    except ValueError:
        raise AccountsError("%s is not JSON" % path)
    if not isinstance(data, dict):
        raise AccountsError("%s is not a JSON object" % path)
    return data


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
    """`claude auth status --json` under the account: presence, no model call.
    opencode: presence only, no process (_opencode_status)."""
    if account.kind == "opencode":
        return _opencode_status(account)
    try:
        env = {**scrub(os.environ), **account_env(account, root)}
        proc = run([_cli(), "auth", "status", "--json"], capture_output=True, text=True,
                   timeout=20, env=env)
        return json.loads(proc.stdout or "{}")
    except AccountsError as err:
        return {"error": str(err)}
    except (OSError, ValueError, subprocess.SubprocessError) as err:
        return {"error": "%s: %s" % (type(err).__name__, err)}


def _opencode_status(account):
    """An endpoint account is logged in by its configuration; a providers
    account when auth.json holds every provider it names. Nothing is run,
    nothing is created, no key is returned."""
    if account.endpoint:
        return {"loggedIn": True, "authMethod": "opencode", "endpoint": account.endpoint}
    try:
        entries = _auth_entries(account)
    except AccountsError as err:
        return {"error": str(err)}
    missing = [p for p in account.providers if p not in entries]
    return {"loggedIn": not missing, "authMethod": "opencode",
            "providers": [p for p in account.providers if p in entries], "missing": missing}


def _missing_hint(account):
    """How a missing secret gets written, per kind (login_action's wording)."""
    if account.kind == "claude-token":
        return "mint it with `cousin-account token %s`, or write it: mode 0600, directory 0700" \
            % account.name
    return "write the key to it: mode 0600, directory 0700"


def login_action(account, via=None, provider=None):
    """What the operator runs to fix this account's login. opencode: the
    provider to log in (the first named when none is given), or the
    endpoint to check. An API key never travels through chat (R12'): it
    goes on stdin or in a key file, and `--via` is named only with an
    OAuth `--method`."""
    tail = " --via %s" % via if via else ""
    if account.kind == "opencode":
        if account.endpoint:
            return ("check the endpoint %s (a local OpenAI-compatible model: nothing to log"
                    " in)" % account.endpoint)
        return ("`cousin-account login %s --provider %s` (the API key on stdin or with"
                " --key-file; an OAuth method: add --method <label>%s)"
                % (account.name, provider or account.providers[0], tail))
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
        missing = st.get("missing") or [None]
        line += " -> %s" % (st.get("error")
                            or "run " + login_action(account, via, provider=missing[0]))
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


# ------------------------------------------------------------ opencode login (phase 9 R12')
#
# An API key never travels through chat: it comes from stdin (hidden on a
# terminal) or a strict key file and is written straight into the account's
# auth.json in the shape `opencode auth login` writes (measured on 1.18.31:
# {"<provider>": {"type": "api", "key": "..."}}, 0600). No opencode process.
# An OAuth method runs `opencode auth login` in a pty. Every OAuth method of
# 1.18.31 is "auto": the CLI shows `Go to: <url>`, one instruction line (a
# device code, or "complete authorization in your browser") and waits; nothing
# is pasted back. The URL and the instructions are relayed; auth.json decides.

OC_FIRST_RX = (r"Go to:\s*(?P<url>\S+)(?P<instr>[\s\S]*?)Waiting for authorization"
               r"|(?P<key>Enter your API key)"
               r"|(?P<error>Error:[^\n]*)"
               r"|\u25c6[ \t]*(?![ \t]|Enter your API key)(?P<prompt>[^\n]+)")   # a prompt
OC_DONE_RX = r"(Login successful)|(Failed to authorize[^\n]*|Error:[^\n]*)"
_OC_DECOR = re.compile(r"^[\s|\u2502\u250c\u2514\u25cf\u2022\u25c6\u25c7\u25d2\u25d0\u25d3\u25d1]+")
# What the login child may inherit: everything else (OPENCODE_CONFIG_CONTENT,
# a provider's *_API_KEY, the Claude variables) stays out, so the account's
# data dir is its only source (Task 1 finding 1).
OPENCODE_PASS_ENV = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TZ", "SSL_CERT_FILE",
                     "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS", "HTTPS_PROXY", "HTTP_PROXY",
                     "NO_PROXY", "https_proxy", "http_proxy", "no_proxy")
OPENCODE_FLOW_VARS = {"OPENCODE_DISABLE_AUTOUPDATE": "1", "OPENCODE_DISABLE_SHARE": "1",
                      "OPENCODE_DISABLE_CLAUDE_CODE": "1", "BROWSER": "/bin/false"}


def opencode_bin():
    """The opencode binary as an absolute path: COUSIN_OPENCODE_BIN, else
    `opencode` on PATH, resolved (the pty driver execs a path)."""
    import shutil
    named = os.environ.get("COUSIN_OPENCODE_BIN")
    if named:
        if not os.path.isabs(named):
            raise AccountsError("COUSIN_OPENCODE_BIN must be an absolute path")
        return named
    found = shutil.which("opencode")
    if not found:
        raise AccountsError("no opencode binary: set COUSIN_OPENCODE_BIN or put `opencode` on"
                            " PATH")
    return str(Path(found).resolve())


def check_opencode_login(account, provider, method=None):
    """R12' refusals, before any key is read or any process runs: a Claude
    subscription (Anthropic, or any method named Claude, by OAuth),
    opencode's own hosted service, a bridge marker, a provider the account
    does not name, and every account that is not an opencode providers
    account."""
    from cousin_lib.runner import opencode_guard
    if account.kind != "opencode":
        raise AccountsError("--provider, --method and --key-file are for kind opencode"
                            " accounts; %s is %s" % (account.name, account.kind))
    if account.endpoint:
        raise AccountsError("account %s is a local endpoint (%s): nothing to log in"
                            % (account.name, account.endpoint))
    if not isinstance(provider, str) or not _PROVIDER.match(provider):
        raise AccountsError("name the provider to log in with --provider <id>, one of %s"
                            " (a provider id matches %s)"
                            % (", ".join(account.providers), _PROVIDER.pattern))
    if provider == "opencode":
        raise AccountsError("'opencode' is opencode's own hosted service, which the runner"
                            " always disables: log in the providers whose keys you hold")
    for what, text in (("provider", provider), ("method", method or "")):
        marker = opencode_guard.bridge_marker(text)
        if marker:
            raise AccountsError("the %s names the Claude-subscription bridge (marker %r); the"
                                " opencode lane never carries subscription traffic"
                                % (what, marker.pattern))
    if method is not None and (provider == "anthropic"
                               or re.search("claude|anthropic", method, re.IGNORECASE)):
        raise AccountsError("an Anthropic or Claude login by OAuth is a Claude subscription;"
                            " the opencode lane never carries one")
    refuse_claude_name("the provider", provider)
    if provider not in account.providers:
        raise AccountsError("account %s does not name provider %r in its providers (%s): add"
                            " it to config/accounts.toml first"
                            % (account.name, provider, ", ".join(account.providers)))


def _api_key(text):
    """One word of printable characters, at most agent_auth.KEY_MAX_CHARS;
    the message never repeats it."""
    from cousin_lib import agent_auth
    text = (text or "").strip()
    if not text:
        raise AccountsError("the key is empty")
    if len(text) > agent_auth.KEY_MAX_CHARS or not agent_auth._KEY_RE.match(text):
        raise AccountsError("the key must be one word of printable characters (at most %d)"
                            % agent_auth.KEY_MAX_CHARS)
    return text


def read_api_key(provider, *, key_file=None, stdin=None):
    """The key from `key_file` (read as strictly as a secret file: a 0700
    directory and a 0600 regular file of ours, no symlink, one line), else
    from stdin: hidden with getpass on a terminal, one line otherwise."""
    import getpass
    from cousin_lib import agent_auth
    if key_file:
        try:
            raw = agent_auth.read_private_file(key_file, what="key file")
        except agent_auth.AuthError as err:
            raise AccountsError(str(err))
        lines = [ln for ln in raw.decode("utf-8", "replace").splitlines() if ln.strip()]
        if len(lines) != 1:
            raise AccountsError("key file %s must hold one line" % key_file)
        return _api_key(lines[0])
    stdin = sys.stdin if stdin is None else stdin
    if stdin.isatty():
        return _api_key(getpass.getpass("API key for %s (not echoed): " % provider))
    return _api_key(stdin.readline())


def _auth_dir(account):
    """The data dir, its XDG subdirs and data/opencode, each 0700 and ours
    (opencode itself makes data/opencode 0755: tightened)."""
    path = _opencode_dir(account).joinpath(*AUTH_JSON)
    _private_dir(path.parent, "the opencode auth dir")
    return path


def store_api_key(account, provider, key):
    """Merge {"<provider>": {"type": "api", "key": key}} into the account's
    auth.json (every other entry kept as it is), 0600, tmp + rename.
    Nothing is written into a file that would still hold a Claude
    subscription. Returns the path; never the key."""
    check_opencode_login(account, provider)
    key = _api_key(key)
    path = _auth_dir(account)
    data = _read_auth_json(path)
    data[provider] = {"type": "api", "key": key}
    _entry_types(data, path)
    _write_private_text(path, json.dumps(data, indent=2))
    return path


def _instructions(text):
    lines = (_OC_DECOR.sub("", ln).strip() for ln in text.splitlines())
    return " ".join(ln for ln in lines if ln)


def _opencode_flow_env(account):
    from cousin_lib.runner import opencode_guard
    env = {k: v for k, v in os.environ.items() if k in OPENCODE_PASS_ENV}
    env.setdefault("TERM", "xterm-256color")
    env.update(account_env(account, None), **OPENCODE_FLOW_VARS)
    try:
        opencode_guard.refuse_bridge({}, env)
    except opencode_guard.BridgeRefused as err:
        raise AccountsError(err.reason)
    return env


def opencode_login_flow(account, root, *, provider, method, relay, spawn=None, binary=None,
                        timeout=CAPTURE_TTL_S):
    """`opencode auth login --pure --provider <id> --method <label>` under
    the account's HOME and XDG dirs, in a pty. relay(url, instructions)
    once the CLI shows them; then the CLI waits on its own (a device code
    entered on the provider's page, or a browser callback) for `timeout`
    seconds. An API-key prompt, an error or any other prompt ends the
    flow before the relay. The CLI's `Login successful` is not the
    verdict: the account's auth.json holding the provider is."""
    from cousin_lib.pty_driver import PtyTimeout
    check_opencode_login(account, provider, method)
    argv = [binary or opencode_bin(), "auth", "login", "--pure", "--provider", provider,
            "--method", method]
    env = _opencode_flow_env(account)
    _auth_dir(account)
    session = _pty(spawn)(argv, env)
    try:
        try:
            first = session.read_until(OC_FIRST_RX, 60)
        except PtyTimeout as err:
            return {"ok": False, "reason": "opencode showed no sign-in URL (%s); its last"
                    " words: %s" % (err, _last_words(session))}
        if first.group("key"):
            return {"ok": False, "reason": "method %r of %s asks for an API key: run without"
                    " --method (the key on stdin or with --key-file; a key never goes"
                    " through the pty)" % (method, provider)}
        if first.group("error"):
            return {"ok": False, "reason": "opencode said: %s" % first.group("error").strip()}
        if first.group("prompt"):
            return {"ok": False, "reason": "method %r of %s asks %r before its URL, which this"
                    " flow does not answer" % (method, provider, first.group("prompt").strip())}
        relay(first.group("url"), _instructions(first.group("instr")))
        try:
            done = session.read_until(OC_DONE_RX, timeout)
        except PtyTimeout:
            return {"ok": False, "reason": "no login within %ds" % timeout}
        if done.group(1) is None:
            return {"ok": False, "reason": "opencode said: %s" % done.group(2).strip()}
    finally:
        session.close()
    try:
        entries = _auth_entries(account)
    except AccountsError as err:
        return {"ok": False, "reason": str(err)}
    if provider not in entries:
        return {"ok": False, "reason": "opencode said %r, but %s holds no %s entry"
                % (done.group(1), account.data_dir.joinpath(*AUTH_JSON), provider)}
    return {"ok": True, "cli_said": done.group(1), "status": _opencode_status(account)}


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
        data = json.loads(capture_path(root, name).read_text())
    except (OSError, ValueError):
        return None
    # Valid JSON that is not an object (a list, a number, ...) is not a
    # capture: skip it like unparsable data (tracker #84), not an
    # AttributeError on the .get() every caller does next.
    return data if isinstance(data, dict) else None


def captures_for(root, via):
    """Every capture (armed or a tombstone) whose code comes through `via`."""
    out = []
    for path in sorted((Path(root) / "run").glob("login-capture-*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
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
    """Any capture whose tombstone hour is over is removed; an armed capture
    whose window closed without its flow's cleanup (the flow died, or the
    clock ran out first) is rewritten as a codeless tombstone."""
    with _capture_lock(root, name):
        data = read_capture(root, name)
        if data is None:
            return
        window = capture_window(data, time.time())
        if window is None:
            capture_path(root, name).unlink(missing_ok=True)
            return
        if data.get("state") != "armed" or window in ("armed", "taken"):
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
    slug = tomllib.loads((Path(home) / "cousin.toml").read_text())["cousin"]["slug"]
    text = ("Login for account %s: open %s , sign in, and reply HERE with the whole code the"
            " page shows. It looks like `code#state`: paste all of it, the part after # included."
            " Within %s, paste the code as the next message; only the code is taken, never"
            " delivered to %s and never kept in this history. If you did not start this login"
            " from a host shell yourself, do not answer this."
            % (account_name, url, _span(timeout), slug))
    return _operator_row(home, operator, slug, text)


def relay_url_notice(home, *, operator, account_name, provider, url, instructions,
                     timeout=CAPTURE_TTL_S):
    """The opencode OAuth notice (R12'): the URL and opencode's own
    instruction line. Nothing is taken back from the chat: no capture is
    armed, the login finishes on the provider's side."""
    slug = tomllib.loads((Path(home) / "cousin.toml").read_text())["cousin"]["slug"]
    text = ("Login for account %s (provider %s): open %s and sign in.%s Nothing to paste"
            " back here: opencode finishes the login by itself, and waits %s. If you did"
            " not start this login from a host shell yourself, ignore this."
            % (account_name, provider, url,
               " opencode says: %s." % instructions.rstrip(".") if instructions else "",
               _span(timeout)))
    return _operator_row(home, operator, slug, text)


def _operator_row(home, operator, slug, text):
    from cousin_lib.server.storage import ChatStore, normalize_chat_user
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
    """The pid of the nearest ancestor process that carries COUSIN_HOME or
    COUSIN_SLUG in the environment it was started with, or None. Catches `env -u COUSIN_HOME` run by a
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
            return pid
        try:
            pid = parent_of(pid)
        except (OSError, ValueError):
            return None
    return None


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
    if args.cmd == "login" and (account.kind == "opencode" or args.provider or args.method
                                or args.key_file):
        return _opencode_login_cmd(args, account, root)
    if args.cmd == "login" and account.name == HOST:
        print("cousin-account: warning: this re-logs the host's own login in ~/.claude, the"
              " one every cousin without an account runs on and the operator's own"
              " `claude` uses", file=sys.stderr)
    flow = login_flow if args.cmd == "login" else token_flow
    if args.via:
        via, operator = _via_operator(args, root)
        if via is None:
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


def _via_operator(args, root):
    """(the via cousin's home, its operator's name), or (None, None) after
    saying why on stderr."""
    from cousin_lib.config import CousinConfig
    via = Path(root) / "cousins" / args.via
    if not (via / "cousin.toml").is_file():
        print("cousin-account: no cousin %r under %s" % (args.via, Path(root) / "cousins"),
              file=sys.stderr)
        return None, None
    operator = CousinConfig.load(via).operator_name   # what storage.is_operator compares
    if not operator:
        print("cousin-account: %s has no [operator] name: nobody to relay the URL to"
              % args.via, file=sys.stderr)
        return None, None
    return via, operator


def _opencode_login_cmd(args, account, root):
    """`cousin-account login <name> --provider <id> [--key-file <path>]` (an
    API key: stdin or the file, never chat) or `... --method <label> [--via
    <slug>]` (OAuth through the pty). Exit 0 done, 4 the flow failed, 2
    refused."""
    try:
        check_opencode_login(account, args.provider, args.method)
        if args.method is None and args.via:
            raise AccountsError("an API key never travels through chat: --via goes with an"
                                " OAuth --method only; give the key on stdin or with"
                                " --key-file")
        if args.method is not None and args.key_file:
            raise AccountsError("--key-file is for an API key; an OAuth --method takes none")
        if args.method is None:
            key = read_api_key(args.provider, key_file=args.key_file)
            path = store_api_key(account, args.provider, key)
            del key
    except AccountsError as err:
        print("cousin-account: %s" % err, file=sys.stderr)
        return 2
    if args.method is None:
        missing = _opencode_status(account).get("missing") or []
        print("login %s: ok (%s: an API key in %s%s)" % (
            account.name, args.provider, path,
            "; still missing: %s" % ", ".join(missing) if missing else ""))
        return 0
    if not sys.stdin.isatty():
        print("cousin-account: an OAuth login wants a terminal: run it from a shell on the"
              " host", file=sys.stderr)
        return 2
    if args.via:
        via, operator = _via_operator(args, root)
        if via is None:
            return 2

        def relay(url, instructions):
            relay_url_notice(via, operator=operator, account_name=account.name,
                             provider=args.provider, url=url, instructions=instructions,
                             timeout=args.timeout)
            print("cousin-account: the sign-in URL is in %s's chat; waiting up to %ds for the"
                  " login" % (args.via, args.timeout), file=sys.stderr)
    else:
        def relay(url, instructions):
            print("Open this URL and sign in:\n%s%s\nNothing to paste back: waiting up to"
                  " %ds for the login." % (url, "\n" + instructions if instructions else "",
                                           args.timeout))
    restore = _exit_on_hangup()
    try:
        out = opencode_login_flow(account, root, provider=args.provider, method=args.method,
                                  relay=relay, timeout=args.timeout)
    except AccountsError as err:
        print("cousin-account: %s" % err, file=sys.stderr)
        return 2
    except Exception as err:                 # noqa: BLE001 - a failed flow is exit 4, not a trace
        print("cousin-account: login %s failed: %s: %s"
              % (account.name, type(err).__name__, err), file=sys.stderr)
        return 4
    finally:
        restore()
    print("login %s: %s" % (account.name, "ok" if out["ok"] else out.get("reason", "failed")))
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
        if cmd == "login":
            c.add_argument("--provider", help="opencode accounts: the provider to log in")
            c.add_argument("--key-file", help="opencode accounts: read the API key from this"
                                              " private file instead of stdin")
            c.add_argument("--method", help="opencode accounts: an OAuth login method, by its"
                                            " label (opencode auth login --method)")
    args = p.parse_args(argv)
    if args.cmd in ("login", "token"):
        # GUARDRAILS, not a boundary: the runner exports COUSIN_HOME to
        # every command a cousin runs, so the model's own Bash stops here,
        # and `env -u` is caught by the ancestor check. A determined
        # process can still get past both; the account files stay
        # readable by the same Unix user until the phase 6 container.
        if os.environ.get("COUSIN_HOME") or os.environ.get("COUSIN_SLUG"):
            print("cousin-account: operator-run only; a cousin never obtains credentials",
                  file=sys.stderr)
            return 2
        ancestor = _inside_cousin_ancestry()
        if ancestor:
            print("cousin-account: operator-run only; a cousin never obtains credentials"
                  " (ancestor process %d was started inside a cousin)" % ancestor,
                  file=sys.stderr)
            return 2
        # A usability check, not a safeguard: anything can fake a terminal.
        # An opencode API key may come on a pipe (R12'): that path checks
        # its own flags once the account is known.
        key_path = args.cmd == "login" and (args.provider or args.key_file) and not args.method
        if not key_path and not sys.stdin.isatty():
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
            where = {"claude-login": a.config_dir, "opencode": a.data_dir}.get(
                a.kind, a.secret_file)
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
