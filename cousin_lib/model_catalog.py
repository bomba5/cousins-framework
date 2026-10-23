"""Which models an account can run, asked of the account's own source and
cached (docs/configuration.md, "data/models/").

One list per lane the account runs on:
- a Claude account (the host's login, claude-login, claude-token,
  anthropic-key), sdk lane: the agent CLI's own model list, from the answer
  it gives when a session connects (runner/sdk.cli_models): no prompt is
  sent, nothing is billed
- openrouter: on the sdk lane OpenRouter's public model list, its
  anthropic/ models; on the opencode lane opencode's own list for the
  openrouter provider
- opencode with providers: `opencode models <provider>` in the account's
  data dir; with an endpoint, the endpoint's /models
The opencode lane refuses a model that names Claude or Anthropic, so its
lists never hold one.

The lists are cached in data/models/<account>.json, and a read never
fetches. A fetch runs on the operator's word (the console's refresh,
`cousin-account models --refresh`) or when the console finds a cache older
than a day; always in a child process whose environment has no Claude
variable. A failed fetch keeps the last lists and says why.
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from cousin_lib import accounts, atomic

MAX_AGE = 24 * 3600
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
FETCH_TIMEOUT = 60
CHILD_TIMEOUT = 180
CLAUDE_KINDS = ("claude-login", "claude-token", "anthropic-key")
_REFRESH_LOCK = threading.Lock()


class CatalogError(Exception):
    """A list could not be fetched; the message never holds a secret."""


def cache_path(root, name):
    return Path(root) / "data" / "models" / ("%s.json" % name)


def read(root, name):
    """The cached catalogue of account `name`: {"account", "kind", "lanes":
    {lane: [model ids]}, "fetched_at" (the last good fetch, epoch seconds),
    "checked_at" (the last try), "error"}; None when there is none."""
    try:
        data = json.loads(cache_path(root, name).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def catalogue_lane(runner_kind):
    """Which of an account's lists a runner kind reads: opencode's own, or
    the agent CLI's (sdk; the tmux pane runs the same CLI)."""
    return "opencode" if runner_kind == "opencode" else "sdk"


def models_for(root, name, lane):
    """The cached model ids of account `name` on `lane`; [] when none."""
    lanes = (read(root, name) or {}).get("lanes")
    ids = lanes.get(lane) if isinstance(lanes, dict) else None
    return [m for m in ids if isinstance(m, str)] if isinstance(ids, list) else []


def summary(root, name):
    """For a list of accounts: {"lanes": {lane: count}, "fetched_at",
    "checked_at", "error"}; None when the account was never fetched."""
    data = read(root, name)
    if data is None:
        return None
    lanes = data.get("lanes") if isinstance(data.get("lanes"), dict) else {}
    return {"lanes": {lane: len(ids) for lane, ids in lanes.items() if isinstance(ids, list)},
            "fetched_at": data.get("fetched_at"), "checked_at": data.get("checked_at"),
            "error": data.get("error")}


def stale(root, name, now=None):
    """True when the last try (a good fetch or not) is older than MAX_AGE."""
    checked = (read(root, name) or {}).get("checked_at")
    now = time.time() if now is None else now
    return not isinstance(checked, (int, float)) or now - checked > MAX_AGE


# ---- the sources ----------------------------------------------------------

def _get_json(url, *, http=None):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with (http or urllib.request.urlopen)(req, timeout=FETCH_TIMEOUT) as resp:
            return json.loads(resp.read(16 << 20))
    except urllib.error.HTTPError as err:
        raise CatalogError("%s answered HTTP %d" % (url, err.code))
    except (OSError, ValueError) as err:
        raise CatalogError("cannot read %s (%s)" % (url, type(err).__name__))


def _ids(items):
    """The `id` of each object in an OpenAI-style list, in order."""
    return [m["id"] for m in items if isinstance(m, dict) and isinstance(m.get("id"), str)]


def _unique(ids):
    out = []
    for m in ids:
        if m not in out:
            out.append(m)
    return out


def claude_ids(models):
    """The model ids in the agent CLI's list: each entry's resolved model
    (an alias such as the default resolves to one), first seen first."""
    return _unique(str(m.get("resolvedModel") or m.get("value"))
                   for m in models if isinstance(m, dict)
                   and (m.get("resolvedModel") or m.get("value")))


def openrouter_sdk_ids(data):
    """OpenRouter's Claude models, the ones the sdk lane can run through its
    Anthropic-compatible endpoint: anthropic/<model>, without the variants
    (`:batch` and the like) the endpoint does not serve to an agent."""
    items = data.get("data") if isinstance(data, dict) else None
    return [m for m in _ids(items or []) if m.startswith("anthropic/") and ":" not in m]


def opencode_ids(output, provider):
    """`opencode models <provider>`'s lines: "<provider>/<model>" each, none
    that names Claude or Anthropic (the opencode lane refuses those)."""
    prefix = provider + "/"
    lines = (ln.strip() for ln in output.splitlines())
    return _unique(ln for ln in lines
                   if ln.startswith(prefix) and len(ln) > len(prefix) and " " not in ln
                   and not accounts.CLAUDE_NAME.search(ln))


def endpoint_ids(data, account):
    """A local endpoint's /models as opencode ids ("local/<model>"), its
    configured model first; none that names Claude or Anthropic."""
    from cousin_lib.runner.opencode import ENDPOINT_PROVIDER
    items = data.get("data") if isinstance(data, dict) else None
    ids = [account.endpoint_model] + _ids(items or [])
    return _unique("%s/%s" % (ENDPOINT_PROVIDER, m) for m in ids
                   if m and not accounts.CLAUDE_NAME.search(m))


def _opencode_list(root, account, provider, *, run=None):
    """`opencode models <provider>` with the account's data dir as its HOME
    and XDG dirs, and nothing else from this environment but what its own
    login child may inherit: its keys are the ones in that dir's auth.json."""
    accounts.preflight(account, root)        # an openrouter view's key, copied in
    env = {k: v for k, v in os.environ.items() if k in accounts.OPENCODE_PASS_ENV}
    env.update(accounts.account_env(account, root))
    env.update(accounts.OPENCODE_FLOW_VARS)
    try:
        proc = (run or subprocess.run)([accounts.opencode_bin(), "models", provider],
                                       capture_output=True, text=True,
                                       timeout=FETCH_TIMEOUT, env=env)
    except (OSError, subprocess.SubprocessError) as err:
        raise CatalogError("opencode models %s: %s" % (provider, type(err).__name__))
    ids = opencode_ids(proc.stdout or "", provider)
    if proc.returncode != 0 and not ids:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:] or ["no output"]
        raise CatalogError("opencode models %s exited %d: %s"
                           % (provider, proc.returncode, tail[0][:200]))
    return ids


def fetch(root, account, *, http=None, run=None, cli_models=None):
    """{lane: [model ids]} for `account`, asked of its sources now.
    CatalogError when nothing could be fetched; a lane that failed while
    another answered is left out, with the reason in "error"."""
    errors = []
    lanes = {}
    if account.kind in CLAUDE_KINDS:
        if cli_models is None:
            from cousin_lib.runner.sdk import cli_models
        try:
            lanes["sdk"] = claude_ids(cli_models(account, root))
        except Exception as err:        # the SDK's own errors are many kinds
            raise CatalogError("the agent CLI gave no model list: %s" % (str(err)[:300] or
                                                                          type(err).__name__))
    elif account.kind == "openrouter":
        try:
            lanes["sdk"] = openrouter_sdk_ids(_get_json(OPENROUTER_MODELS_URL, http=http))
        except CatalogError as err:
            errors.append("sdk: %s" % err)
        try:
            lanes["opencode"] = _opencode_list(root, accounts.for_lane(account, "opencode"),
                                               accounts.OPENROUTER_PROVIDER, run=run)
        except (CatalogError, accounts.AccountsError) as err:
            errors.append("opencode: %s" % err)
    elif account.kind == "opencode" and account.endpoint:
        lanes["opencode"] = endpoint_ids(
            _get_json(account.endpoint.rstrip("/") + "/models", http=http), account)
    elif account.kind == "opencode":
        ids = []
        for provider in account.providers:
            try:
                ids += _opencode_list(root, account, provider, run=run)
            except (CatalogError, accounts.AccountsError) as err:
                errors.append("%s: %s" % (provider, err))
        if ids:
            lanes["opencode"] = ids
    if not lanes:
        raise CatalogError("; ".join(errors) or "no source for kind %s" % account.kind)
    return lanes, "; ".join(errors) or None


def _write(root, name, data):
    path = cache_path(root, name)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic.write_text(path, json.dumps(data, indent=1), mode=0o600)


def update(root, account, *, now=None, **sources):
    """Fetch `account`'s lists and write the cache; the summary. A failed
    fetch keeps the last lists and records the error."""
    now = time.time() if now is None else now
    old = read(root, account.name) or {}
    data = {"account": account.name, "kind": account.kind, "checked_at": now}
    try:
        lanes, partial = fetch(root, account, **sources)
        data.update(lanes=lanes, fetched_at=now, error=partial)
    except (CatalogError, accounts.AccountsError) as err:
        data.update(lanes=old.get("lanes") or {}, fetched_at=old.get("fetched_at"),
                    error=str(err))
    _write(root, account.name, data)
    return summary(root, account.name)


def account_named(root, name):
    """The account a catalogue is kept for: the host's login, or an entry of
    config/accounts.toml (an [agent] api_key_file account belongs to one
    cousin and keeps none)."""
    if name == accounts.HOST:
        return accounts.Account(accounts.HOST, "claude-login", None, None, implicit=True)
    known = accounts.load(root)
    if name not in known:
        raise accounts.AccountsError("no account %r in config/accounts.toml" % name)
    return known[name]


# ---- the fetch, in a child --------------------------------------------------

def refresh(root, name, *, run=subprocess.run):
    """Run `python -m cousin_lib.model_catalog refresh <name>` in a child
    with no Claude variable in its environment (as cousin-runner
    --check-auth runs): the account's lists fetched and cached. The
    summary, or {"error": ...} when the child itself failed."""
    env = accounts.scrub(os.environ)
    try:
        proc = run([sys.executable, "-m", "cousin_lib.model_catalog", "refresh",
                    "--root", str(root), name], capture_output=True, text=True,
                   timeout=CHILD_TIMEOUT, env=env)
    except subprocess.TimeoutExpired:
        return {"error": "the model list fetch took longer than %ds" % CHILD_TIMEOUT}
    except OSError as err:
        return {"error": "cannot start the model list fetch: %s" % err.strerror}
    try:
        out = json.loads((proc.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        tail = (proc.stderr or "").strip().splitlines()[-1:] or ["no output"]
        return {"error": "the model list fetch failed: %s" % tail[0][:300]}
    return out if isinstance(out, dict) else {"error": "the model list fetch said nothing"}


def refresh_stale(root, names, running, *, now=None, start=None):
    """Start a background refresh (a thread running refresh()) for each of
    `names` whose cache is stale and that has none in `running` (a dict
    the caller keeps for the life of its process); the names started."""
    started = []
    with _REFRESH_LOCK:
        for name in dict.fromkeys(names):            # each once, in order
            thread = running.get(name)
            if (thread is not None and thread.is_alive()) or not stale(root, name, now):
                continue
            thread = threading.Thread(target=refresh, args=(root, name), daemon=True,
                                      name="models-%s" % name)
            running[name] = thread
            (start or threading.Thread.start)(thread)
            started.append(name)
    return started


def main(argv=None):
    """`python -m cousin_lib.model_catalog refresh --root <root> <account>`:
    the child refresh() starts. Prints one line of JSON (the summary)."""
    parser = argparse.ArgumentParser(prog="python -m cousin_lib.model_catalog")
    sub = parser.add_subparsers(dest="cmd", required=True)
    one = sub.add_parser("refresh")
    one.add_argument("--root", required=True)
    one.add_argument("name")
    args = parser.parse_args(argv)
    try:
        account = account_named(args.root, args.name)
    except accounts.AccountsError as err:
        print(json.dumps({"error": str(err)}))
        return 2
    out = update(args.root, account)
    print(json.dumps(out))
    return 0 if out and not (out.get("error") and not out.get("lanes")) else 1


if __name__ == "__main__":
    sys.exit(main())
