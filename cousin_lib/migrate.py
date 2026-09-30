"""cousin-migrate: move one cousin from the legacy tmux lane to the SDK runner
(master plan phase 7 tasks 7-8; the runbook is docs/migrating.md).

Nothing here runs on its own. A merge, an upgrade or a boot never
migrates a cousin: only the operator's `cousin-migrate apply <slug>
--yes` does, one cousin at a time, and `cousin-migrate rollback <slug>
--yes` undoes it.

  plan      the checks and the steps; writes nothing (the default look).
            Its `mcp` line names the .mcp.json servers the runner will
            load (the file stays; `cousin` in it is skipped).
            The cousin must be RUNNING on the tmux lane: migrating a
            stopped cousin would start it (the supervisor starts every
            runner cousin), so it is started first or left alone.
  apply     the steps, in order, stopping at the first that fails:
              close    a clean stop of the tmux session (flip.close_session:
                       the handoff, the transcript mined, the generation bumped);
                       a handoff not written during the close is a warning
                       with its age (handoff_freshness)
              handover the tmux lane's transcript path(s) recorded in
                       data/previous-transcript.json (handover.py, #103): the
                       working conversation does not carry, and the runner's
                       first fresh session is handed the path. Never fails:
                       a transcript that cannot be found is recorded missing
              import   the agent CLI's own auto-memory folded in
                       (memory_import.apply: idempotent, a baseline first)
              toml     cousin.toml [agent] runner = "sdk", the account, and
                       what the tmux lane's [runtime] carries (#96: the
                       runner reads only [agent]): model and effort, and
                       for the key mode (agent_auth.MODE_API_KEY) an
                       anthropic-key account <slug>-key made from the
                       cousin's own key file
                       (an existing [agent] key wins); only once the tmux
                       session is still down
              start    the migration-day boot packet archived (the runner
                       boots on its own digest), the review gate's cursor
                       opened afresh (what the cousin wrote on the tmux lane
                       is not the gate's), the supervisor asked to start
                       the runner, and the cousin's chat server started (the
                       supervisor runs none: a peer's `cousin-chat send`,
                       an MCP send and a hive tell-home all reach the inbox
                       through it)
              verify   the runner child stays `running` and holds its lock
                       for STABLE_S, and the chat server answers /health for
                       the slug
            data/migration.json holds the prior cousin.toml, its exact
            bytes and mode, before the first step, every step's outcome,
            the handover record and the warnings
  rollback  undo exactly the steps that ran, and refuse what would be
            unsafe: a record already rolled back, inbox rows still
            waiting (nobody reads the inbox on the tmux lane) or an inbox
            that cannot be read (both unless --force), a runner that is not
            down after its stop. When `toml` ran: stop the runner, wait
            until it is down, put the saved bytes and mode back, have the
            supervisor rescan. The handover record (and a consumed one) is
            removed. A key account the migration made is
            removed with its secret when no other cousin names it (else
            kept, and why). Then a fresh boot packet (the cousin's state
            now, not the migration day's) and the tmux session started,
            since the cousin was running when `apply` began, unless it
            already runs; last, the supervisor's hold on the runner
            (`run/held`, which its stop wrote) is released: a tmux cousin
            carries none. A step that fails is recorded and reported.
  check     the exit criterion over the runner's own records since the
            migration (or --since): inbox rows that never reached done,
            tool calls with no recorded result, recorder hooks that failed,
            the runner's model, effort and account against the cousin's
            [runtime] (a MISMATCH is not ok), and whether the chat server
            answers
  tidy      (2.0.0, R7) the keys 2.0.0 no longer reads (removed_keys),
            removed from one cousin's cousin.toml (`tidy <slug>`) or from
            every cousin's and the install's config/harness.toml,
            config/hive.toml and config/agent-cmd (`tidy --all`). A plan
            by default: with --yes each file's prior bytes go beside it
            (<home>/data/cousin.toml.pre-2.0.0, config/<file>.pre-2.0.0,
            never over an earlier copy) and only the removed lines go
            (comments, order and line endings kept; a table left empty is
            dropped; a file the line remover cannot edit is left whole and
            named). A 1.x chat server still running for the cousin is
            stopped (SIGTERM), found by data/chat-server.pid or its [chat]
            port, and signalled only when its command line is a chat
            server for this home; the pid file is removed. A cousin with
            no runner kind is refused: tidy is not a conversion.

The supervisor interface assumed (phase 6, round 2 as its drafter stated
it, 9fcf52a): a stop through spawn.stop_cousin waits until the child is
down (up to 35 s) unless told otherwise; `start {slug}` clears the stop's
hold marker; `reload` never restarts a stopped child; snapshot()'s
children rows carry `state` (running, backoff, failing, stopped); a
runner's exit 5 (another runner holds the lock) reads `backoff`.

Every live action is a keyword argument (the tests inject all of them);
_live() gives the real ones."""
import argparse
import base64
import contextlib
import importlib.util
import json
import os
import re
import sqlite3
import sys
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path

from cousin_lib import removed_keys
from cousin_lib.delivery import RUNNER_KINDS, lane_refusal  # the one list of runner kinds (M6)

RECORD = "data/migration.json"
STEPS = ("close", "handover", "import", "toml", "start", "verify")
STALE_S = 3600.0          # an inbox row not done after this long is a lost message
TOOL_GRACE_S = 600.0      # a tool call this recent may still be running
VERIFY_S = 90.0           # how long verify waits for a stable runner
TRUST_WAIT_S = 600.0      # how long a kind switch's verify waits for the operator at the pane's trust dialog
STABLE_S = 10.0           # how long the runner must stay up to count as started
DOWN_S = 60.0             # how long rollback waits for a stopped runner to let go of its lock
PRE_RUNNER_BOOT = "data/pending-boot.pre-runner.json"


class MigrateError(Exception):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat()


def _agent(home):
    try:
        data = tomllib.loads((Path(home) / "cousin.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError) as err:
        raise MigrateError("cannot read %s/cousin.toml: %s" % (home, err))
    agent = data.get("agent")
    return agent if isinstance(agent, dict) else {}


def read_record(home):
    try:
        data = json.loads((Path(home) / RECORD).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_record(home, rec):
    path = Path(home) / RECORD
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rec, indent=1) + "\n")
    os.replace(tmp, path)


# ------------------------------------------------------------ plan

def _check(name, ok, detail):
    return {"check": name, "ok": bool(ok), "detail": detail}


HANDOFF = "data/handoff.md"


def _age(seconds):
    seconds = max(0.0, float(seconds))
    if seconds < 120:
        return "%ds" % seconds
    if seconds < 7200:
        return "%dm" % (seconds // 60)
    if seconds < 172800:
        return "%.1fh" % (seconds / 3600)
    return "%.1fd" % (seconds / 86400)


def _handoff_mtime_ns(home):
    try:
        return (Path(home) / HANDOFF).stat().st_mtime_ns
    except OSError:
        return None


def handoff_age(home, now=None):
    """How old data/handoff.md is now, as text, or None when there is none."""
    try:
        mtime = (Path(home) / HANDOFF).stat().st_mtime
    except OSError:
        return None
    return _age((time.time() if now is None else now) - mtime)


def handoff_freshness(home, before_ns, stages=()):
    """(fresh, detail, warning or None) after the close: the runner starts
    from the handoff, so it must be one written during this close (its
    mtime moved past `before_ns`, the mtime before the close; None when
    there was no file). flip.close_session waits for exactly that, else
    writes an emergency handoff; a cousin whose session was already gone
    gets neither, which this catches."""
    after = _handoff_mtime_ns(home)
    if after is None:
        return False, "no %s after the close" % HANDOFF, (
            "no %s after the close: the runner starts from STATUS.md and memory alone"
            % HANDOFF)
    if before_ns is not None and after <= before_ns:
        age = handoff_age(home)
        return False, "%s not written during the close (%s old)" % (HANDOFF, age), (
            "%s was not written during the close: it is %s old, and the runner starts"
            " from it" % (HANDOFF, age))
    names = {st.get("stage"): st for st in stages or () if isinstance(st, dict)}
    if "emergency_handoff" in names:
        return True, "handoff written during the close (the framework's emergency one)", (
            "the handoff is the framework's emergency one: the cousin did not write it"
            " in time, so the runner starts from a degraded handoff")
    clean = (names.get("wait_handoff") or {}).get("wrote_clean")
    return True, "handoff written during the close%s" % (" (clean)" if clean else ""), None


# ------------------------------------------------------------ what [runtime] carries (#96)

CARRIED = ("model", "effort")      # [runtime] key -> the same [agent] key
KEY_SUFFIX = "-key"                # the per-cousin key account: <slug>-key


def _cousin_toml(home):
    try:
        return tomllib.loads((Path(home) / "cousin.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError) as err:
        raise MigrateError("cannot read %s/cousin.toml: %s" % (home, err))


def tmux_values(home, root):
    """{key: (value, source)} for model and effort: what the tmux lane
    runs the cousin on. [runtime] first; else config/harness.toml [agent]
    default_<key>, but only when config/agent-cmd renders the {<key>}
    placeholder (spawn.render_agent_cmd's rule). None when neither: the
    tmux lane ran the CLI's own default too."""
    from cousin_lib import flip
    from cousin_lib.config import MissingConfigError, agent_config
    runtime = _cousin_toml(home).get("runtime") or {}
    try:
        defaults = agent_config(root)
    except MissingConfigError as err:
        raise MigrateError(str(err))
    template = flip._read_agent_cmd_template(Path(root))
    out = {}
    for key in CARRIED:
        if runtime.get(key) is not None:
            out[key] = (str(runtime[key]), "[runtime] %s" % key)
        elif "{%s}" % key in template and defaults.get("default_%s" % key):
            out[key] = (defaults["default_%s" % key],
                        "config/harness.toml [agent] default_%s" % key)
        else:
            out[key] = (None, None)
    return out


def _key_env(root):
    """The variable the tmux lane's key file names (config/harness.toml
    [auth.api_key] key_env), else ANTHROPIC_API_KEY."""
    from cousin_lib import agent_auth
    cfg = agent_auth.api_key_config(root)
    return cfg["key_env"] if cfg else "ANTHROPIC_API_KEY"


def _cousin_key(home, root):
    """The key from the cousin's own <home>/.secrets/api-key.env, read by
    agent_auth.read_key (every check it makes). MigrateError naming the
    file and the problem, never the content."""
    from cousin_lib import agent_auth
    try:
        return agent_auth.read_key(home, _key_env(root))
    except agent_auth.AuthError as err:
        raise MigrateError(str(err))


def carry(home, root, account=None):
    """What `apply`'s toml step carries from the tmux lane's [runtime] into
    the runner lane's [agent] (the runner reads only [agent]):

      values   {key: value} to write: model, effort, account
      rows     [{key, action (carry, kept, create, reuse, none), detail}]
      create   the key account to make, {name, secret_file (root-relative),
               write_secret, add_table}, or None
      error    why the carry cannot be done (a blocker), or None

    An existing [agent] key wins and is reported, as does an explicit
    --account. `[runtime] auth` in the key mode (agent_auth.MODE_API_KEY)
    becomes the anthropic-key account <slug>-key, made from the cousin's
    own key file; a key file
    that is missing or malformed is an error, never the host login. The
    key itself is never in what this returns."""
    from cousin_lib import accounts, agent_auth
    home, root = Path(home), Path(root)
    agent = _agent(home)
    values, rows = {}, []
    out = {"values": values, "rows": rows, "create": None, "error": None}
    try:
        effective = tmux_values(home, root)
    except MigrateError as err:
        out["error"] = str(err)
        return out
    for key in CARRIED:
        value, source = effective[key]
        if agent.get(key) is not None:
            rows.append({"key": key, "action": "kept", "detail": "[agent] %s %r kept%s" % (
                key, agent[key], "" if value is None or value == agent[key] else
                " over %s %r" % (source, value))})
        elif value is not None:
            values[key] = value
            rows.append({"key": key, "action": "carry",
                         "detail": "%s %r -> [agent] %s" % (source, value, key)})
        else:
            rows.append({"key": key, "action": "none",
                         "detail": "no %s configured: the CLI's default, as on the tmux lane"
                                   % key})
    try:
        mode = agent_auth.read_mode(home)
    except agent_auth.AuthError as err:
        out["error"] = str(err)
        return out
    if mode != agent_auth.MODE_API_KEY:
        rows.append({"key": "account", "action": "none", "detail":
                     "[runtime] auth %r: %s" % (mode, "--account %s" % account if account else
                                                "[agent] account %r kept" % agent["account"]
                                                if agent.get("account") else "the host login")})
        return out
    auth = "[runtime] auth %r" % mode
    if account:
        rows.append({"key": "account", "action": "kept", "detail":
                     "--account %s given: %s not carried (the operator's choice)" % (account, auth)})
        return out
    for existing in ("account", "api_key_file"):
        if agent.get(existing):
            rows.append({"key": "account", "action": "kept", "detail":
                         "[agent] %s %r kept over %s" % (existing, agent[existing], auth)})
            return out
    slug = home.name
    name = slug + KEY_SUFFIX
    no_host = " (never the host login in its place)"
    if not accounts._NAME.match(name):
        out["error"] = ("%s: the account name %r does not match %s; add one to"
                        " config/accounts.toml and pass --account%s"
                        % (auth, name, accounts._NAME.pattern, no_host))
        return out
    try:
        key = _cousin_key(home, root)
        known = accounts.load(root)
    except (MigrateError, accounts.AccountsError) as err:
        out["error"] = "%s: %s%s" % (auth, err, no_host)
        return out
    rel = "%s/%s" % (accounts.SECRETS_DIR, name)
    create = {"name": name, "secret_file": rel, "write_secret": True, "add_table": True}
    if name in known:
        acct = known[name]
        if acct.kind != "anthropic-key":
            out["error"] = ("%s: config/accounts.toml already has %s of kind %s, not"
                            " anthropic-key%s" % (auth, name, acct.kind, no_host))
            return out
        create.update(add_table=False, secret_file=os.path.relpath(acct.secret_file, root))
        try:
            held = accounts._read_secret(acct)
        except accounts.SecretMissing:
            held = None
        except accounts.AccountsError as err:
            out["error"] = "%s: %s%s" % (auth, err, no_host)
            return out
        if held is not None and held != key:
            out["error"] = ("%s: account %s already holds a different key than %s%s"
                            % (auth, name, agent_auth.key_file(home), no_host))
            return out
        create["write_secret"] = held is None
    else:
        # no table, but a secret may already sit at the path: never
        # overwritten, and never removed by a rollback (not ours)
        try:
            held = accounts._read_secret(accounts.Account(
                name, "anthropic-key", None, root / rel, implicit=True))
        except accounts.SecretMissing:
            held = None
        except accounts.AccountsError as err:
            out["error"] = "%s: %s is already there and unusable: %s%s" % (
                auth, rel, err, no_host)
            return out
        if held is not None and held != key:
            out["error"] = ("%s: %s already holds a different key than %s%s"
                            % (auth, rel, agent_auth.key_file(home), no_host))
            return out
        create["write_secret"] = held is None
    values["account"] = name
    if create["add_table"] or create["write_secret"]:
        out["create"] = create
        rows.append({"key": "account", "action": "create", "detail":
                     "%s -> [agent] account %r, an anthropic-key account made from %s"
                     " (%s)" % (auth, name, agent_auth.key_file(home), "; ".join(
                         (["secret %s, 0600" % create["secret_file"]] if create["write_secret"]
                          else ["secret %s already holds this key: kept, not ours"
                                % create["secret_file"]])
                         + (["its table appended to config/accounts.toml"]
                            if create["add_table"] else [])))})
    else:
        rows.append({"key": "account", "action": "reuse", "detail":
                     "%s -> [agent] account %r (already in config/accounts.toml with this"
                     " key)" % (auth, name)})
    return out


def _appendix(text, name, secret_rel):
    """What appending [accounts.<name>] to `text` adds, exactly: the block,
    after one blank line when the file has content. The file's own bytes
    are never touched, so removing this suffix gives them back."""
    block = '[accounts.%s]\nkind = "anthropic-key"\nsecret_file = %s\n' % (
        name, json.dumps(secret_rel))
    if not text:
        return block
    return ("\n" if text.endswith("\n") else "\n\n") + block


def _write_accounts(root, text, mode):
    """config/accounts.toml atomically with `mode`; never a file that does
    not parse."""
    tomllib.loads(text)
    path = Path(root) / "config" / "accounts.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _append_account(root, name, text, appended, mode):
    """config/accounts.toml becomes `text` + `appended`; the result must
    load with the new account. (Read-modify-write with no lock: two
    migrations at the same instant could lose a table; parked.)"""
    from cousin_lib import accounts
    _write_accounts(root, text + appended, mode)
    if name not in accounts.load(root):
        raise MigrateError("config/accounts.toml does not load %s after the write" % name)


def make_account(home, root, create, record=lambda done: None):
    """The key account `carry` planned: the secret (the cousin's key, one
    line, 0600 in a 0700 directory, through accounts' private writer) and
    the config/accounts.toml table. `record(done)` is called BEFORE each
    sub-step (its state `planned`) and after it (`written`), so a rollback
    after a failure half-way removes exactly what exists. What it did,
    never the key."""
    from cousin_lib import accounts
    root = Path(root)
    done = {"name": create["name"], "secret_file": create["secret_file"], "secret": None,
            "table": None}
    if create["write_secret"]:
        done["secret"] = "planned"
        record(done)
        accounts._write_secret(root / create["secret_file"], _cousin_key(home, root))
        done["secret"] = "written"
        record(done)
    if create["add_table"]:
        path = root / "config" / "accounts.toml"
        try:
            text, mode, existed = path.read_text(), path.stat().st_mode & 0o7777, True
        except FileNotFoundError:
            text, mode, existed = "", 0o644, False
        appended = _appendix(text, create["name"], create["secret_file"])
        done["table"] = {"appended": appended, "created_file": not existed, "state": "planned"}
        record(done)
        _append_account(root, create["name"], text, appended, mode)
        done["table"]["state"] = "written"
        record(done)
    return done


def _users_of(root, name, but):
    """The cousins under <root>/cousins whose cousin.toml names account
    `name`, except `but`."""
    out = []
    for toml in sorted((Path(root) / "cousins").glob("*/cousin.toml")):
        if toml.parent.name == but:
            continue
        try:
            agent = tomllib.loads(toml.read_text()).get("agent") or {}
        except (OSError, tomllib.TOMLDecodeError):
            continue
        if agent.get("account") == name:
            out.append(toml.parent.name)
    return out


def drop_account(home, root, made):
    """Rollback's half of make_account, for what exists of it (a sub-step
    `planned` may or may not have happened). Kept, and why, when another
    cousin names the account. The table: exactly the bytes appended are
    taken out, the rest of the file byte for byte (a file the migration
    created and left empty is removed); kept when those bytes are no
    longer there. The secret: removed unless the table stayed or an
    account in config/accounts.toml still resolves to its path. The
    cousin's own key file is never touched."""
    from cousin_lib import accounts
    root = Path(root)
    name = made["name"]
    users = _users_of(root, name, Path(home).name)
    if users:
        return "account %s kept: %s use it" % (name, ", ".join(users))
    parts = []
    table = made.get("table")
    if table:
        path = root / "config" / "accounts.toml"
        try:
            text = path.read_text()
        except FileNotFoundError:
            text = None
        at = -1 if text is None else text.rfind(table["appended"])
        if at >= 0:
            new = text[:at] + text[at + len(table["appended"]):]
            if not new and table.get("created_file"):
                path.unlink()
            else:
                _write_accounts(root, new, path.stat().st_mode & 0o7777)
            parts.append("its table")
        elif table.get("state") == "written":
            return ("account %s kept: its table in config/accounts.toml changed since the"
                    " migration" % name)
    if made.get("secret") in ("planned", "written"):
        secret = root / made["secret_file"]
        try:
            holders = [a.name for a in accounts.load(root).values()
                       if a.secret_file is not None and Path(a.secret_file) == secret]
        except accounts.AccountsError as err:
            return "account %s: %s removed; its secret kept (%s)" % (
                name, " and ".join(parts) or "nothing", err)
        if holders:
            return "account %s: %s removed; its secret kept: %s use it" % (
                name, " and ".join(parts) or "nothing", ", ".join(holders))
        try:
            secret.unlink()
            parts.append("its secret")
        except FileNotFoundError:
            pass
    return "account %s: %s removed (no other cousin uses it)" % (
        name, " and ".join(parts) or "nothing")


NEVER_UNRUN = "a model the runner's CLI can't run is never written"


def runner_cli():
    """The CLI the runner's SDK starts, read without running anything: the
    SDK prefers its bundled binary, whose version it records. A model the
    bundled CLI is too old for fails every turn with an API 400 (#96)."""
    spec = importlib.util.find_spec("claude_agent_sdk")
    if spec is None or not spec.origin:
        return "no claude-agent-sdk installed: the runner has no CLI"
    try:
        from claude_agent_sdk._cli_version import __cli_version__ as cli
    except ImportError:
        cli = None
    try:
        from claude_agent_sdk._version import __version__ as sdk_version
    except ImportError:
        sdk_version = "?"
    if (Path(spec.origin).parent / "_bundled" / "claude").is_file():
        return "Claude Code %s (bundled with claude-agent-sdk %s)" % (cli or "?", sdk_version)
    return ("`claude` on PATH, version not read (claude-agent-sdk %s bundles none)"
            % sdk_version)


def _validate_account(home, root, name, moved):
    """The Account the runner will run on, for one validating turn before
    it exists: the key account `carry` will make holds the key in memory
    only (never written by a plan)."""
    from cousin_lib import accounts
    home, root = Path(home), Path(root)
    if moved["create"] is not None:
        return accounts.Account(name, "anthropic-key", None,
                                root / moved["create"]["secret_file"], implicit=True,
                                secret_value=_cousin_key(home, root))
    if name == accounts.HOST:
        return accounts.for_cousin(home, root)       # the host, or [agent] api_key_file
    return accounts.load(root)[name]


def _dir_note(name, moved):
    """What --validate leaves behind for a key account: the account's own
    config dir (accounts.account_env makes it; no secret goes there)."""
    if moved["create"] is None and "account" not in moved["values"]:
        return ""
    return (" (--validate makes data/accounts/%s, the account's login-free config dir;"
            " no secret is written there)" % name)


def _validate(validator, account, root, model, effort):
    """(ok, detail) of ONE smallest model turn on a throwaway client
    (sdk.validate_account): the model, effort and account the runner
    will run, the API's own words on a failure."""
    if validator is None:
        from cousin_lib.runner.sdk import validate_account as validator
    what = "model %s, effort %s, account %s" % (model or "the CLI's default",
                                                effort or "the CLI's default", account.name)
    try:
        rc, line = validator(account, root, model=model, effort=effort)
    except Exception as err:  # noqa: BLE001 - a validation that cannot run did not pass
        rc, line = 2, "validate: %s: %s" % (type(err).__name__, err)
    return rc == 0, "%s (%s)" % (line, what)


def plan(home, *, root, account=None, auth_check, supervisor_up, sdk_ok, tmux_alive,
         validate=False, validator=None, cli_version=None, **_unused):
    """{"slug", "checks": [...], "steps", "ready", "carry", "cli"}; writes
    nothing. A model to be written makes the plan not ready until
    `validate` ran one model turn with it and passed (NEVER_UNRUN)."""
    from cousin_lib import accounts, handover, memory_import
    home, root = Path(home), Path(root)
    checks = []
    agent = _agent(home)
    runner = agent.get("runner")
    lane_ok = runner not in RUNNER_KINDS
    if lane_ok:
        lane_detail = "on the tmux lane"
    elif runner == "tmux":
        lane_detail = ("[agent] runner = \"tmux\" is the tmux runner kind, not the legacy tmux"
                       " lane: it is already on the runner lane; switch kinds with --to sdk"
                       " (phase 11)")
    else:
        lane_detail = "already on the runner lane ([agent] runner = %r)" % runner
    checks.append(_check("lane", lane_ok, lane_detail))
    running = lane_ok and tmux_alive(home)
    if lane_ok:
        checks.append(_check("running", running, "its tmux session is up" if running else
                             "it is stopped: start it first (a migrated cousin runs; the"
                             " supervisor starts every runner cousin)"))
    rec = read_record(home)
    open_rec = rec is not None and rec.get("state") in ("applying", "failed", "migrated")
    checks.append(_check("record", not open_rec, "no migration in progress" if not open_rec else
                         "%s says %s: `cousin-migrate rollback` first" % (RECORD, rec.get("state"))))
    moved = carry(home, root, account)
    checks.append(_check("carry", moved["error"] is None, moved["error"] or "; ".join(
        r["detail"] for r in moved["rows"])))
    name = (account or moved["values"].get("account") or agent.get("account")
            or accounts.HOST)
    if moved["create"] is not None:
        checks.append(_check("account", True, (
            "account %r is made at apply from the cousin's key file; the auth check runs"
            " after it (`cousin-runner --home %s --check-auth`)" % (name, home))))
    else:
        try:
            known = accounts.load(root)
            if name != accounts.HOST and name not in known:
                raise accounts.AccountsError("account %r is not in config/accounts.toml" % name)
            code, line = auth_check(home, root, name)
            checks.append(_check("account", code == 0, line))
        except accounts.AccountsError as err:
            checks.append(_check("account", False, str(err)))
    cli = (cli_version or runner_cli)()
    checks.append(_check("cli", True, "the runner's CLI: %s" % cli))
    model = moved["values"].get("model") or agent.get("model")
    effort = moved["values"].get("effort") or agent.get("effort")
    blocked = [c["check"] for c in checks if not c["ok"]]
    notes = [handover.COST_LINE]
    age = handoff_age(home)
    notes.append("%s is %s old now; the close asks for a new one and waits for it"
                 % (HANDOFF, age) if age is not None else
                 "no %s yet; the close asks for one and waits for it" % HANDOFF)
    if validate and blocked:
        checks.append(_check("validate", False, "not run: fix %s first; %s"
                             % (", ".join(blocked), NEVER_UNRUN)))
    elif validate:
        try:
            ok, line = _validate(validator, _validate_account(home, root, name, moved), root,
                                 model, effort)
        except (MigrateError, accounts.AccountsError, KeyError) as err:
            ok, line = False, "validate: %s" % err
        checks.append(_check("validate", ok, "%s%s; %s" % (line, _dir_note(name, moved),
                                                          NEVER_UNRUN)))
    elif "model" in moved["values"] or "effort" in moved["values"]:
        checks.append(_check("validate", False, (
            "[agent] %s would be written unvalidated: run with --validate (one smallest"
            " model turn on %s)%s; %s" % (" and ".join(
                "%s %r" % (k, moved["values"][k]) for k in CARRIED if k in moved["values"]),
                cli, _dir_note(name, moved), NEVER_UNRUN))))
    up = supervisor_up(root)
    checks.append(_check("supervisor", up, "a cousin-supervisor answers for %s" % root if up else
                         "no cousin-supervisor runs for %s: start it (`cousin-supervisor run`,"
                         " or its unit)" % root))
    has_sdk = sdk_ok()
    checks.append(_check("sdk", has_sdk, "claude-agent-sdk is installed" if has_sdk else
                         "claude-agent-sdk is not installed: pip install -e '.[sdk]'"))
    try:
        rows = memory_import.plan(home, root=root)
        conflicts = [r["name"] for r in rows if r["action"] == "conflict"]
        todo = sum(r["action"] in ("import", "update") for r in rows)
        checks.append(_check("import", not conflicts,
                             "%d auto-memory file(s) to fold in" % todo if not conflicts else
                             "merge by hand first (`cousin-memory import-auto`): %s"
                             % ", ".join(conflicts)))
    except Exception as err:  # noqa: BLE001 - ManifestError, an unreadable source
        checks.append(_check("import", False, "%s: %s" % (type(err).__name__, err)))
    # the home's .mcp.json stays where it is; the runner reads it and skips
    # `cousin` (it serves its own in-process). Names only, never a blocker.
    from cousin_lib.runner import mcp_config
    try:
        checks.append(_check("mcp", True, mcp_config.describe(home)))
    except Exception as err:  # noqa: BLE001 - the runner treats it as not fatal too
        checks.append(_check("mcp", True, "%s not read: %s" % (mcp_config.FILE,
                                                                type(err).__name__)))
    return {"slug": home.name, "account": name, "checks": checks, "steps": list(STEPS),
            "ready": all(c["ok"] for c in checks), "carry": moved, "cli": cli,
            "notes": notes}


# ------------------------------------------------------------ apply

def set_agent_keys(text, values):
    """cousin.toml's text with [agent] `values` set, the rest untouched:
    a key replaced in the [agent] table's body, else added under its
    header, else an [agent] table appended. Line endings are kept."""
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = ['%s = %s' % (k, json.dumps(v)) for k, v in values.items()]
    header = re.search(r"(?m)^\[agent\][ \t]*\r?$", text)
    if header is None:
        return text.rstrip("\r\n") + nl + nl + "[agent]" + nl + nl.join(lines) + nl
    start = header.end()
    nxt = re.search(r"(?m)^\s*\[", text[start:])
    end = start + nxt.start() if nxt else len(text)
    body = text[start:end]
    for key, line in zip(values, lines):
        key_re = re.compile(r"(?m)^%s\s*=[^\r\n]*" % re.escape(key))
        if key_re.search(body):
            body = key_re.sub(lambda m: line, body, count=1)
        else:
            body = nl + line + body
    return text[:start] + body + text[end:]


def _write_toml(home, data, mode):
    """Write cousin.toml's bytes atomically with `mode`; never a file that
    does not parse."""
    tomllib.loads(data.decode("utf-8"))
    path = Path(home) / "cousin.toml"
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_bytes(data)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def apply(home, *, root, account=None, close, import_auto, start, verify, tmux_alive,
          validate=False, **checks):
    """Run the steps; the record, with state `migrated` or `failed`.
    MigrateError when the plan is not ready (nothing is written then):
    among others, a model to be written that `validate` did not pass in
    this very run (NEVER_UNRUN)."""
    from cousin_lib import accounts, handover
    home, root = Path(home), Path(root)
    p = plan(home, root=root, account=account, tmux_alive=tmux_alive, validate=validate,
             **checks)
    if not p["ready"]:
        raise MigrateError("not ready: %s" % "; ".join(
            c["detail"] for c in p["checks"] if not c["ok"]))
    path = home / "cousin.toml"
    prior = path.read_bytes()
    mode = path.stat().st_mode & 0o7777
    rec = {"slug": home.name, "state": "applying", "started_at": _now(), "account": p["account"],
           "was_running": True, "prior_toml_b64": base64.b64encode(prior).decode("ascii"),
           "prior_mode": mode, "steps": [], "warnings": [], "cli": p["cli"],
           "validated": next((c["detail"] for c in p["checks"] if c["check"] == "validate"),
                             None)}
    _write_record(home, rec)
    values = {"runner": "sdk"}
    # [runtime] model/effort/auth, carried (#96): the runner reads only [agent]
    for key in CARRIED:
        if key in p["carry"]["values"]:
            values[key] = p["carry"]["values"][key]
    if p["account"] != accounts.HOST:
        values["account"] = p["account"]

    def still_down():
        if tmux_alive(home):
            raise MigrateError("the tmux session is up again (a flip, a schedule or a"
                               " console start): stop it, then roll back and apply again")

    def run(step):
        if step == "close":
            before = _handoff_mtime_ns(home)
            out = close(home.name, root)
            if not out.get("ok"):
                return False, out.get("error") or "the clean stop failed"
            _fresh, detail, warning = handoff_freshness(home, before, out.get("stages"))
            if warning:
                rec["warnings"].append(warning)
            return True, "closed cleanly; %s" % detail
        if step == "handover":
            got = handover.record(home, root, ended_at=_now())
            rec["handover"] = got
            paths = [t["path"] for t in got["transcripts"]]
            return True, "%s: %s%s" % (handover.RECORD, ", ".join(paths) or "no transcript",
                                       "; missing: %s" % got["missing"] if got["missing"]
                                       else "")
        if step == "import":
            return True, json.dumps(import_auto(home, root))
        if step == "toml":
            still_down()
            made = ""
            if p["carry"]["create"] is not None:
                # before cousin.toml names it: a runner never starts on an
                # account that is not there yet
                def record(done):
                    rec["account_created"] = done
                    _write_record(home, rec)
                done = make_account(home, root, p["carry"]["create"], record)
                made = "; account %s made (%s)" % (done["name"], ", ".join(
                    (["secret %s, 0600" % done["secret_file"]] if done["secret"] else [])
                    + (["its table"] if done["table"] else [])))
            text = set_agent_keys(prior.decode("utf-8"), values)
            _write_toml(home, text.encode("utf-8"), mode)
            kept = [r["detail"] for r in p["carry"]["rows"] if r["action"] == "kept"]
            return True, "[agent] %s%s%s" % (
                ", ".join("%s = %r" % kv for kv in values.items()), made,
                "; " + "; ".join(kept) if kept else "")
        if step == "start":
            still_down()
            boot_file = home / "data" / "pending-boot.json"
            if boot_file.exists():
                os.replace(boot_file, home / PRE_RUNNER_BOOT)
            from cousin_lib import review_gate
            review_gate.begin(home, reset=True)
            start(home, root)
            return True, ("the supervisor was asked to start runner:%s and the chat server"
                          " was started" % home.name)
        out = verify(home, root)
        return bool(out.get("ok")), out.get("detail") or ""

    for step in STEPS:
        try:
            ok, detail = run(step)
        except Exception as err:  # noqa: BLE001 - a step's failure is recorded, then we stop
            ok, detail = False, "%s: %s" % (type(err).__name__, err)
        rec["steps"].append({"step": step, "ok": ok, "detail": detail, "at": _now()})
        if not ok:
            rec["state"] = "failed"
            _write_record(home, rec)
            return rec
        _write_record(home, rec)
    rec["state"], rec["migrated_at"] = "migrated", _now()
    _write_record(home, rec)
    return rec


def verify_runner(home, root, *, snapshot, alive, health, stable_s=STABLE_S, timeout=VERIFY_S,
                  sleep=time.sleep, clock=time.monotonic):
    """The default verify: the supervisor's `runner:<slug>` row reads
    `running` and the runner holds the cousin's lock, unbroken for
    `stable_s` (a runner that exits at once - a lock held by another
    runner, a missing secret, a broken policy - never passes), then the
    chat server answers /health for the slug."""
    name = "runner:%s" % Path(home).name
    deadline = clock() + timeout
    since, state = None, None
    while clock() < deadline:
        row = ((snapshot(root) or {}).get("children") or {}).get(name) or {}
        state = row.get("state")
        if state == "running" and alive(home):
            since = since if since is not None else clock()
            if clock() - since >= stable_s:
                break
        else:
            since = None
        sleep(1.0)
    else:
        return {"ok": False, "detail": "%s did not stay running for %ds within %ds (last: %s)"
                % (name, stable_s, timeout, state)}
    ok, detail = health(home)
    return {"ok": ok, "detail": ("%s running %ds; " % (name, stable_s)) + detail}


# ------------------------------------------------------------ rollback

def _inbox_rows(home):
    """[(state, outcome, created_at)] read-only; [] when there is no inbox;
    None when it cannot be read (locked, broken): never "nothing waits"."""
    path = Path(home) / "data" / "inbox.db"
    if not path.exists():
        return []
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=5)
        try:
            return conn.execute("SELECT state, outcome, created_at FROM inbox").fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def fresh_packet(home):
    """A boot packet of the cousin's state now, pending for the next tmux
    start (flip.close_session's tail): the rollback's tmux session must
    not boot on the migration day's packet."""
    from cousin_lib import boot, spawn
    home = Path(home)
    generation = boot.bump_generation(home)
    packet = boot.assemble(home.name, home, generation=generation)
    path = home / "data" / ("boot-packet-gen-%04d.md" % generation)
    path.write_text(packet["text"])
    spawn.pending_boot_path(home).write_text(json.dumps({
        "generation": generation, "packet": str(path), "written_at": _now()}))
    return generation



def _runner_session_files(home):
    """The runner lane's session state in a home: data/runner-session.json,
    each side session's data/runner-session-<kind>.json, and the restart
    mark (restart_note)."""
    data = Path(home) / "data"
    found = sorted(data.glob("runner-session*.json"))
    mark = data / "runner-restart.json"
    return found + ([mark] if mark.exists() else [])


def _remove_all(paths):
    gone = []
    for path in paths:
        try:
            path.unlink()
            gone.append(path.name)
        except FileNotFoundError:
            pass
    return gone

def rollback(home, *, root, stop, runner_alive, reload, start_tmux, tmux_alive, release,
             new_packet=fresh_packet, force=False, sleep=time.sleep, clock=time.monotonic,
             **_unused):
    """Back to the tmux lane, undoing only what `apply` did. Process
    actions follow what IS, not what was recorded: the runner is stopped
    only while the file still names the runner lane (it differs from the
    saved bytes) or a runner still holds the lock, and the file is
    restored only while it differs. A retry after a failed rollback
    therefore never stops the tmux session the first attempt restored
    (spawn.stop_cousin on a tmux-lane file is the tmux lane's stop), and
    the steps a failed attempt completed (recorded as `rollback_done`)
    are not run again. An interrupted `apply` can have written the file
    and not recorded it: the bytes decide."""
    home, root = Path(home), Path(root)
    rec = read_record(home)
    if rec is None or "prior_toml_b64" not in rec:
        raise MigrateError("no %s with the prior cousin.toml: nothing to roll back" % RECORD)
    if rec.get("state") == "rolled_back":
        raise MigrateError("already rolled back at %s; nothing to undo" % rec.get("rolled_back_at"))
    ran = {s["step"] for s in rec.get("steps", []) if s.get("ok")}
    prior = base64.b64decode(rec["prior_toml_b64"])
    differs = (home / "cousin.toml").read_bytes() != prior
    flipped = "toml" in ran or differs         # the runner lane existed at some point
    done = set(rec.get("rollback_done") or ())
    rows = _inbox_rows(home)
    if rows is None and not force:
        raise MigrateError("data/inbox.db cannot be read, so whether rows wait is unknown;"
                           " look at it, or pass --force")
    waiting = sum(1 for state, _o, _c in rows or () if state != "done")
    if waiting and not force:
        raise MigrateError("%d inbox row(s) still wait for the runner; on the tmux lane nobody"
                           " reads them. Let them finish, or pass --force" % waiting)
    steps = []

    def step(name, fn, detail=None, once=True):
        # `once`: a step a failed attempt completed is not run again; the
        # stop and the restore follow the state instead (once=False)
        if once and name in done:
            return None
        try:
            out = fn()
        except Exception as err:  # noqa: BLE001 - recorded, then the operator decides
            rec.setdefault("rollback_attempts", []).append(
                {"at": _now(), "failed": name, "error": "%s: %s" % (type(err).__name__, err),
                 "steps": steps})
            _write_record(home, rec)
            raise MigrateError("rollback step %s failed: %s: %s (recorded in %s)"
                               % (name, type(err).__name__, err, RECORD))
        steps.append({"step": name, "detail": detail(out) if detail else "done", "at": _now()})
        done.add(name)
        rec["rollback_done"] = sorted(done)
        _write_record(home, rec)
        return out

    if differs or runner_alive(home):
        step("stop", lambda: stop(home, root), json.dumps, once=False)
        deadline = clock() + DOWN_S
        while runner_alive(home) and clock() < deadline:
            sleep(1.0)
        if runner_alive(home):
            rec.setdefault("rollback_attempts", []).append(
                {"at": _now(), "refused": "the runner still holds its lock after %ds" % DOWN_S})
            _write_record(home, rec)
            raise MigrateError("the runner still holds its lock after %ds; nothing restored."
                               " Stop it (`cousin-supervisor stop %s`), then roll back again"
                               % (DOWN_S, home.name))
    if differs:
        step("restore", lambda: _write_toml(home, prior, int(rec["prior_mode"])),
             lambda _: "cousin.toml as it was, byte for byte", once=False)
    if rec.get("account_created"):
        step("account", lambda: drop_account(home, root, rec["account_created"]), str)
    if flipped:
        step("reload", lambda: reload(root))
    if "close" in ran or flipped:
        if flipped:
            step("packet", lambda: new_packet(home), lambda g: "generation %s" % g)
        if rec.get("was_running"):
            if tmux_alive(home):
                steps.append({"step": "start_tmux", "detail": "its tmux session is already up",
                              "at": _now()})
            else:
                step("start_tmux", lambda: start_tmux(home, root))
    else:
        steps.append({"step": "none", "detail": "close never ran: nothing was changed",
                      "at": _now()})
    from cousin_lib import handover
    if any((home / rel).exists() for rel in (handover.RECORD, handover.CONSUMED)):
        step("handover", lambda: handover.remove(home),
             lambda gone: "removed %s" % ", ".join(gone), once=False)
    # #107: what the runner lane kept of its session (the primary's and the
    # side sessions' records, the restart mark) goes with it: a
    # re-migration after this rollback starts a fresh session, with the
    # handover first, instead of resuming the old runner session
    lane_files = _runner_session_files(home)
    if lane_files:
        step("runner_session", lambda: _remove_all(lane_files),
             lambda gone: "removed %s" % ", ".join(gone), once=False)
    step("release", lambda: release(home),
         lambda _: "no runner hold left on the tmux cousin (run/held)")
    rec.update(state="rolled_back", rolled_back_at=_now(), rollback_steps=steps,
               waiting_at_rollback=waiting)
    _write_record(home, rec)
    return rec


# ------------------------------------------------------------ check

def _stamp(value):
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except ValueError:
        return None


def config_mismatches(home, root):
    """({"runner": ..., "tmux": ...}, [mismatch lines]): the model, effort
    and account the runner would run this cousin on ([agent], the only
    table it reads) against what its [runtime] runs it on on the tmux lane
    (tmux_values; `[runtime] auth` api_key means a key account). Only
    for a cousin on the runner lane: a tmux cousin has nothing to compare."""
    from cousin_lib import accounts, agent_auth
    home, root = Path(home), Path(root)
    try:
        agent = _agent(home)
    except MigrateError as err:
        return {}, [str(err)]
    if agent.get("runner") != "sdk":
        return {}, []
    wrong = []
    try:
        effective = tmux_values(home, root)
    except MigrateError as err:
        effective = {k: (None, None) for k in CARRIED}
        wrong.append(str(err))
    runner = {k: agent.get(k) for k in CARRIED}
    tmux = {k: effective[k][0] for k in CARRIED}
    for key in CARRIED:
        value, source = effective[key]
        if value is not None and runner[key] != value:
            wrong.append("%s: the runner runs %s, %s says %r" % (
                key, "%r" % runner[key] if runner[key] is not None else "the CLI's default",
                source, value))
    try:
        acct = accounts.for_cousin(home, root)
        runner.update(account=acct.name, kind=acct.kind)
    except accounts.AccountsError as err:
        acct = None
        wrong.append("account: %s" % err)
    try:
        mode = agent_auth.read_mode(home)
    except agent_auth.AuthError as err:
        mode = None
        wrong.append("auth: %s" % err)
    tmux["auth"] = mode
    if acct is not None and mode is not None:
        keyed = acct.kind == "anthropic-key"
        if mode == agent_auth.MODE_API_KEY and not keyed:
            wrong.append("account: [runtime] auth %r bills its API key, the runner bills"
                         " %s (%s)" % (mode, "the host login" if acct.name == accounts.HOST
                                       else "account %r" % acct.name, acct.kind))
        elif mode != agent_auth.MODE_API_KEY and keyed:
            wrong.append("account: [runtime] auth %r runs on a login, the runner bills the"
                         " API key of account %r" % (mode, acct.name))
    return {"runner": runner, "tmux": tmux}, wrong


def check(home, *, since=None, now=None, health=None, root=None, validate=False,
          validator=None, cli_version=None):
    """The exit criterion, from the runner's own records since `since`
    (epoch seconds; default: the migration's end, else everything):
    every inbox row reached done (`stale`: open longer than STALE_S),
    every tool call has a recorded result (`unrecorded`: none after
    TOOL_GRACE_S), no recorder hook failed (`hook_errors`, the jobs rows a
    failed recorder never wrote), an inbox that can be read, the runner's
    model, effort and account agree with the cousin's [runtime]
    (`mismatches`, config_mismatches), with `validate` one smallest model
    turn on the runner's model, effort and account (`validate`), and,
    with `health`, a chat server that answers. `cli` names the runner's
    CLI and its version."""
    from cousin_lib import accounts
    home = Path(home)
    root = Path(root) if root is not None else accounts.root_of(home)
    now = time.time() if now is None else now
    if since is None:
        rec = read_record(home) or {}
        since = _stamp(rec.get("migrated_at")) if rec.get("state") == "migrated" else None
    since = since or 0.0
    inbox = {"done": 0, "failed": 0, "open": 0, "stale": 0}
    rows = _inbox_rows(home)
    for state, outcome, created in rows or ():
        if float(created or 0.0) < since:
            continue
        if state == "done":
            inbox["done"] += 1
            inbox["failed"] += outcome == "failed"
        else:
            inbox["open"] += 1
            inbox["stale"] += (now - float(created or now)) > STALE_S
    calls, results, hook_errors = {}, set(), []
    for path in sorted((home / "data" / "stream").glob("*.jsonl")):
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            ts = float(ev.get("ts") or now)
            if ts < since:
                continue
            payload = ev.get("payload") or {}
            kind = ev.get("kind")
            if kind == "tool" and payload.get("id"):
                calls[payload["id"]] = ts
            elif kind == "tool_result" and payload.get("tool_use_id"):
                results.add(payload["tool_use_id"])
            elif kind == "hook" and payload.get("error") and \
                    str(payload.get("event") or "").startswith(("PreToolUse", "PostToolUse")):
                hook_errors.append("%s: %s" % (payload.get("event"), payload.get("error")))
    unrecorded = sorted(i for i, ts in calls.items()
                        if i not in results and now - ts > TOOL_GRACE_S)
    out = {"since": since, "inbox": inbox, "inbox_readable": rows is not None,
           "tool_calls": len(calls), "unrecorded": unrecorded, "hook_errors": hook_errors}
    out["config"], out["mismatches"] = config_mismatches(home, root)
    out["warnings"] = []
    try:
        tmux_kind = _agent(home).get("runner") == "tmux"
    except MigrateError:
        tmux_kind = False
    account, _err = _account(home, root) if tmux_kind else (None, None)
    if account is not None and account_mcp_servers(account):
        out["warnings"].append("the account's config holds MCP servers (%s): they load in"
                               " the pane and not in the SDK kind (P11-13)"
                               % ", ".join(account_mcp_servers(account)))
    out["removed"] = removed_keys.scan(root, home)   # named, never a reason for NOT ok (R7)
    switch = read_switch_record(home)       # a late acceptance reads `switched` here
    if switch is not None:
        out["switch"] = {k: switch.get(k) for k in ("state", "from", "to", "late")}
    out["cli"] = (cli_version or runner_cli)()
    ok = rows is not None and inbox["stale"] == 0 and not unrecorded and not hook_errors \
        and not out["mismatches"]
    if validate:
        try:
            agent = _agent(home)
            out["validate_ok"], out["validate"] = _validate(
                validator, accounts.for_cousin(home, root), root, agent.get("model"),
                agent.get("effort"))
        except (MigrateError, accounts.AccountsError) as err:
            out["validate_ok"], out["validate"] = False, "validate: %s" % err
        ok = ok and out["validate_ok"]
    if health is not None:
        out["chat_ok"], out["chat"] = health(home)
        ok = ok and out["chat_ok"]
    out["ok"] = ok
    return out


# ------------------------------------------------------------ the live actions

def chat_health(home):
    """(ok, detail): 2.0.0 runs no per-cousin chat server (R10): the
    console and the runner's inbox carry chat, so there is nothing to
    probe and nothing to fail."""
    return True, "none in 2.0.0 (the console and the inbox carry chat)"


def _live():
    from cousin_lib import accounts, delivery, flip, memory_import, spawn, supervisor
    from cousin_lib.config import CousinConfig

    def auth_check(home, root, name):
        acct = accounts.load(root).get(name) or accounts.Account(
            accounts.HOST, "claude-login", None, None, implicit=True)
        return accounts._check_account(acct, root, via=Path(home).name)

    def tmux_alive(home):
        return flip._session_alive(CousinConfig.load(home).tmux_session, "tmux", None)

    def import_auto(home, root):
        rows = memory_import.apply(home, root=root)
        counts = {}
        for r in rows:
            counts[r["action"]] = counts.get(r["action"], 0) + 1
        return counts

    def start(home, root):
        spawn.start_cousin(home, agent_cmd="", root=root)

    def reload(root):
        try:
            supervisor.request(root, "reload")
        except supervisor.SupervisorUnavailable:
            pass

    def validator(account, root, *, model=None, effort=None):
        from cousin_lib.runner import sdk
        return sdk.validate_account(account, root, model=model, effort=effort)

    return dict(
        auth_check=auth_check,
        validator=validator,
        supervisor_up=lambda root: supervisor.snapshot(root) is not None,
        sdk_ok=lambda: importlib.util.find_spec("claude_agent_sdk") is not None,
        tmux_alive=tmux_alive,
        close=lambda slug, root: flip.close_session(slug),
        import_auto=import_auto,
        start=start,
        verify=lambda home, root: verify_runner(home, root, snapshot=supervisor.snapshot,
                                                alive=delivery.is_alive, health=chat_health),
        stop=lambda home, root: spawn.stop_cousin(home, root=root),
        runner_alive=delivery.is_alive,
        release=supervisor.release,
        reload=reload,
        start_tmux=lambda home, root: spawn.start_cousin(
            home, agent_cmd=flip._read_agent_cmd_template(Path(root)), root=root))


# ------------------------------------------------------------ the kind switch
# Phase 11 (R17, I9, P11-11, P11-13): `--to sdk|tmux` moves a runner cousin
# between the two Claude kinds. The session id in data/runner-session.json is
# the continuity: the source stops at idle keeping it, the target resumes it.

SWITCH_RECORD = "data/kind-switch.json"
SWITCH_KINDS = ("sdk", "tmux")
SWITCH_STEPS = {"tmux": ("trust", "close", "toml", "start", "verify"),
                "sdk": ("close", "toml", "start", "verify")}


def read_switch_record(home):
    """The kind switch's record, or None; a failed verify the target
    outlived reads `switched` (_late_switch)."""
    try:
        data = json.loads((Path(home) / SWITCH_RECORD).read_text())
    except (OSError, ValueError):
        return None
    return _late_switch(home, data) if isinstance(data, dict) else None


def _write_switch_record(home, rec):
    path = Path(home) / SWITCH_RECORD
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(rec, indent=2))
    os.replace(tmp, path)


def _account(home, root):
    from cousin_lib import accounts
    try:
        return accounts.for_cousin(home, root), None
    except accounts.AccountsError as err:
        return None, str(err)


def _claude_json(account):
    """The account's config dir's .claude.json (the host login's is
    ~/.claude.json), as a dict; {} when absent or unreadable."""
    path = (Path(account.config_dir) / ".claude.json" if account.config_dir is not None
            else Path.home() / ".claude.json")
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def trust_recorded(home, account):
    """P11-11's fast path, never a gate: the config dir has recorded the
    trust dialog for this home (projects[<home>].hasTrustDialogAccepted,
    Z9). False proves nothing: every live CLI rewrites that file, and CLI
    2.1.282 recorded no entry for a home even after a hand-accepted dialog
    (the live proofs, 2026-09-25, finding 2)."""
    projects = _claude_json(account).get("projects")
    entry = projects.get(str(Path(home))) if isinstance(projects, dict) else None
    return isinstance(entry, dict) and entry.get("hasTrustDialogAccepted") is True


# The pane's one-time dialogs an operator answers (P11-11). The tmux runner
# types nothing into them; it writes data/login-required.json {kind: tmux,
# screen, ts} and emits `auth login_required` (TmuxRunner._screen_allows).
OPERATOR_DIALOGS = ("trust", "bypass")


def pane_hint(home, root):
    """Where the operator answers the pane: tmux on the framework's socket
    (TmuxRunner._make_pane's socket and session name), or the console's
    pane view where it shows that session (it resolves `[chat]
    tmux_session` on the console's own --tmux-socket)."""
    home = Path(home)
    return ("`tmux -S %s attach -t tmux-%s`, or the console's pane view where it shows"
            " that session" % (Path(root) / "run" / "tmux.sock", home.name))


def pane_dialog(home, since):
    """The operator dialog ("trust", "bypass") the tmux runner reports the
    pane showing since `since` (wall time), or None. A file older than
    `since` is another start's."""
    try:
        data = json.loads((Path(home) / "data" / "login-required.json").read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("kind") != "tmux":
        return None
    screen = data.get("screen")
    try:
        fresh = float(data.get("ts") or 0) >= since
    except (TypeError, ValueError):
        fresh = False
    return screen if screen in OPERATOR_DIALOGS and fresh else None


def _dialog_hint(screen, home, root):
    if screen == "trust":
        what = "the trust dialog: accept it (the cursor may start on \"No, exit\")"
    else:
        what = "the %s dialog: accept it" % screen
    return "%s in %s" % (what, pane_hint(home, root))


def wait_for_turn(home, started, *, root, since, what, timeout=VERIFY_S,
                  trust_timeout=TRUST_WAIT_S, say=None, clock=time.monotonic, sleep=time.sleep):
    """A kind switch's verify for the tmux kind: poll `started()` until it is
    true, for `timeout`. While the pane shows an operator dialog (the trust
    dialog on a home the account's CLI never trusted, P11-11) the wait is
    neither a failure nor a rollback: it is said once through `say`, and the
    deadline moves out to `trust_timeout` from the start. (ok, detail)."""
    begun = clock()
    deadline = begun + timeout
    seen = None
    while True:
        if started():
            return True, (what if seen is None else
                          "%s, after the operator accepted the %s dialog" % (what, seen))
        screen = pane_dialog(home, since)
        if screen is not None and seen is None:
            seen = screen
            deadline = max(deadline, begun + trust_timeout)
            if say is not None:
                say("waiting for the operator to accept the %s dialog in the pane (%s);"
                    " up to %d s" % (screen, pane_hint(home, root), trust_timeout))
        if clock() >= deadline:
            break
        sleep(1.0)
    detail = "no %s within %ds" % (what, round(clock() - begun))
    screen = pane_dialog(home, since) or seen
    if screen is not None:
        detail += "; the pane showed %s" % _dialog_hint(screen, home, root)
    return False, detail


def account_mcp_servers(account):
    """The account-level MCP servers (.claude.json mcpServers): they load in
    a tmux-kind pane and not in the SDK kind (P11-13)."""
    servers = _claude_json(account).get("mcpServers")
    return sorted(servers) if isinstance(servers, dict) else []


def _session_record(home):
    try:
        data = json.loads((Path(home) / "data" / "runner-session.json").read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _recorded_session(home):
    sid = _session_record(home).get("session_id")
    return sid if isinstance(sid, str) and sid else None


def switch_plan(home, *, root, to, supervisor_up, **_unused):
    """{"slug", "from", "to", "steps", "checks", "warnings", "ready"}; writes
    nothing."""
    home, root = Path(home), Path(root)
    checks, warnings = [], []
    try:
        current = _agent(home).get("runner")
    except MigrateError as err:
        current = None
        checks.append(_check("toml", False, str(err)))
    if to not in SWITCH_KINDS:
        checks.append(_check("to", False, "--to must be one of %s" % ", ".join(SWITCH_KINDS)))
        return {"slug": home.name, "from": current, "to": to, "steps": [], "checks": checks,
                "warnings": warnings, "ready": False}
    if current not in RUNNER_KINDS:
        checks.append(_check("kind", False, "%s is on the legacy tmux lane: the kind switch is"
                             " between runner kinds; migrate it first (cousin-migrate apply)"
                             % home.name))
    elif current == to:
        checks.append(_check("kind", False, "%s is already the %s kind" % (home.name, to)))
    elif current not in SWITCH_KINDS:
        checks.append(_check("kind", False, "%s runs runner = %r: the switch is between sdk and"
                             " tmux only" % (home.name, current)))
    else:
        checks.append(_check("kind", True, "%s -> %s" % (current, to)))
    account, err = _account(home, root)
    if account is None:
        checks.append(_check("account", False, err))
    elif to == "tmux" and account.kind in ("claude-token", "anthropic-key"):
        checks.append(_check("account", False, "the tmux kind runs on a subscription login;"
                             " %s accounts are refused until a login-free config dir is shown"
                             " to start with no menu (P11-6; A4: onboarding is skippable by"
                             " seeding, but a token's login screen is not measured)" % account.kind))
    else:
        checks.append(_check("account", True, "%s (%s)" % (account.name, account.kind)))
    sid = _recorded_session(home)
    # I5: "fresh" is a tmux rollover's new id whose CLI has not written the
    # session yet; the other kind would resume a session nothing holds
    fresh = sid is not None and _session_record(home).get("fresh") is True
    checks.append(_check("session", sid is not None and not fresh,
                         "session %s continues" % sid if sid and not fresh else
                         "session %s is a rollover in flight (\"fresh\" in"
                         " data/runner-session.json): nothing is written under it yet;"
                         " let the cousin take one turn first" % sid if fresh else
                         "no recorded session in data/runner-session.json: nothing to continue;"
                         " start the cousin once first"))
    checks.append(_check("supervisor", supervisor_up(root),
                         "the supervisor runs" if supervisor_up(root) else
                         "no cousin-supervisor: the switch stops and starts through it"))
    if to == "tmux" and account is not None:
        # never a gate (finding 2): the pane asks, the runner types nothing
        # into it, and verify waits for the operator (wait_for_turn)
        checks.append(_check("trust", True, _trust_detail(home, root, account)))
    if account is not None:
        servers = account_mcp_servers(account)
        if servers and to == "tmux":
            warnings.append("the account's config holds MCP servers (%s): they load in the"
                            " pane and not in the SDK kind (P11-13)" % ", ".join(servers))
    return {"slug": home.name, "from": current, "to": to, "steps": list(SWITCH_STEPS[to]),
            "checks": checks, "warnings": warnings, "ready": all(c["ok"] for c in checks),
            "removed": removed_keys.scan(root, home)}


def _trust_detail(home, root, account):
    if trust_recorded(home, account):
        return "the trust dialog is recorded for this home: the pane should not ask"
    return ("not known in advance: the pane asks once; the switch waits up to %d s for the"
            " operator to accept it in %s" % (TRUST_WAIT_S, pane_hint(home, root)))


NOTICE_RANK = -1         # ahead of every queued row, a flip or an interrupt (0) included


def _switch_notice(home, old, new):
    """R10's notice: one runner line, as the framework's start-up line (a
    system `boot` row), which is also the turn verify reads. Put before the
    target starts and ranked ahead of every queued row, so it is the first
    turn after the switch: a row queued before the switch is answered by a
    model that already knows its kind (live proofs 09-25, finding 4). The
    row's id."""
    from cousin_lib.delivery import Item
    from cousin_lib.runner.inbox import Inbox
    return Inbox(home).put(Item(thread_id="system", source="boot", sender="runner", body=(
        "[runner] this cousin moved from the %s kind to the %s kind; the session goes on."
        " Instructions written for the %s kind are superseded by this kind's contract."
        % (old, new, old))), rank=NOTICE_RANK)


def _drop_notice(home, rec):
    """A rollback's: the switch's notice, if nobody took it, would tell the
    restored kind it is the other one. Closed while queued, or claimed by a
    runner that died holding it (requeue_stale would hand it to the
    restored kind); a notice already done is left as it is."""
    from cousin_lib.delivery import FAILED
    from cousin_lib.runner.inbox import Inbox
    notice_id = rec.get("notice_id")
    if notice_id is None:
        return False
    return Inbox(home).done_if_open(notice_id, FAILED, "the kind switch was rolled back")


def _clear_tmux_login_flag(home):
    """A rollback's: data/login-required.json the tmux runner wrote (a
    trust dialog nobody accepted) would read LOGIN REQUIRED on the
    restored kind forever; its own kind's flag (auth.py's) is kept."""
    path = Path(home) / "data" / "login-required.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict) or data.get("kind") != "tmux":
        return False
    path.unlink(missing_ok=True)
    return True


def _late_switch(home, rec):
    """A switch whose verify failed while the target ran on (the operator
    accepted the trust dialog after the wait): the notice the target took
    shows the switch completed. The record then ends `switched`, written
    back; any other record is returned as it is."""
    from cousin_lib.delivery import DELIVERED
    from cousin_lib.runner.inbox import Inbox
    if rec.get("state") != "failed" or rec.get("failed") != "verify" \
            or rec.get("notice_id") is None:
        return rec
    try:
        row = Inbox(home).get(rec["notice_id"])
    except Exception:  # noqa: BLE001 - an unreadable inbox changes nothing
        return rec
    if not row or row["state"] != "done" or row["outcome"] != DELIVERED:
        return rec
    rec.update(state="switched", switched_at=_now(), switched_late_at=_now(),
               late=("the target took the switch's notice after verify gave up (%s)"
                     % row.get("detail")))
    _write_switch_record(home, rec)
    return rec


def switch_apply(home, *, root, to, close, start, verify, cursor_end, supervisor_up,
                 clock=time.time, **_unused):
    """Run the switch; the record, state `switched` or `failed`. MigrateError
    when the plan is not ready (nothing is changed then). The trust step
    gates nothing: for `--to tmux` the pane may ask once, and verify waits
    for the operator (wait_for_turn)."""
    from cousin_lib import harness_settings
    from cousin_lib.runner import extract
    home, root = Path(home), Path(root)
    p = switch_plan(home, root=root, to=to, supervisor_up=supervisor_up)
    if not p["ready"]:
        raise MigrateError("not ready: %s" % "; ".join(
            "%s: %s" % (c["check"], c["detail"]) for c in p["checks"] if not c["ok"]))
    path = home / "cousin.toml"
    prior = path.read_bytes()
    sid = _recorded_session(home)
    rec = {"state": "switching", "from": p["from"], "to": to, "session_id": sid,
           "started_at": _now(), "prior_toml_b64": base64.b64encode(prior).decode(),
           "prior_mode": path.stat().st_mode & 0o777, "steps": [],
           "warnings": list(p["warnings"])}

    def step(name, detail="done"):
        rec["steps"].append({"step": name, "detail": detail, "at": _now()})
        _write_switch_record(home, rec)

    _write_switch_record(home, rec)
    if to == "tmux":
        account, _err = _account(home, root)
        step("trust", _trust_detail(home, root, account))
    close(home, root)
    step("close", "the %s runner stopped at idle; session %s kept" % (p["from"], sid))
    text = set_agent_keys(prior.decode("utf-8"), {"runner": to})
    _write_toml(home, text.encode("utf-8"), rec["prior_mode"])
    if to == "tmux":
        settings_out = harness_settings.apply_project_settings(home, root=root, kind="tmux")
        rec["warnings"].extend(settings_out.get("warnings") or ())
    else:
        harness_settings.remove_kind_settings(home)
    step("toml", "[agent] runner = %r, the kind's settings %s"
         % (to, "written" if to == "tmux" else "removed"))
    # the mining cursor BEFORE the start (P11-9): the target's first turn end
    # mines from here, never from an offset in the other kind's record
    extract.set_cursor(home, sid, cursor_end(home, root, sid, to))
    step("cursor", "the mining cursor at the end of the %s kind's record" % to)
    # the notice before the start, ahead of the queue: the target's first turn
    rec["notice_id"] = _switch_notice(home, p["from"], to)
    step("notice", "inbox row %d, the first turn after the switch" % rec["notice_id"])
    since = clock()
    start(home, root)
    step("start", "the %s runner resumes %s" % (to, sid))
    ok, detail = verify(home, root, sid, to, since)
    if not ok:
        rec.update(state="failed", failed="verify", error=detail)
        step("verify", detail)
        raise MigrateError("verify: %s (roll back with --to %s, or look at it first)"
                           % (detail, p["from"]))
    step("verify", detail)
    rec.update(state="switched", switched_at=_now())
    _write_switch_record(home, rec)
    return rec


def switch_rollback(home, *, root, to, close, start, cursor_end, **_unused):
    """Back from a kind switch (R17, Task 11b), switched or failed: the
    runner stopped, cousin.toml restored byte for byte, the kind's settings
    as the restored kind wants them, the runner started again; the session
    id is never touched, so the restored kind resumes it. `to` must name
    the kind the switch came from."""
    from cousin_lib import harness_settings
    from cousin_lib.runner import extract
    home, root = Path(home), Path(root)
    rec = read_switch_record(home)
    if rec is None or "prior_toml_b64" not in rec:
        raise MigrateError("no kind switch recorded in %s: nothing to roll back" % SWITCH_RECORD)
    if rec.get("state") == "rolled_back":
        raise MigrateError("already rolled back at %s" % rec.get("rolled_back_at"))
    if to != rec.get("from"):
        raise MigrateError("the switch came from %s: roll back with --to %s"
                           % (rec.get("from"), rec.get("from")))
    steps = []

    def step(name, detail="done"):
        steps.append({"step": name, "detail": detail, "at": _now()})
        rec["rollback_steps"] = steps
        _write_switch_record(home, rec)

    close(home, root)
    step("close", "the %s runner stopped" % rec.get("to"))
    if _drop_notice(home, rec):
        step("notice", "the switch's notice, never taken, dropped")
    if _clear_tmux_login_flag(home):
        step("login", "the tmux pane's data/login-required.json cleared")
    _write_toml(home, base64.b64decode(rec["prior_toml_b64"]), int(rec["prior_mode"]))
    if to == "tmux":
        settings_out = harness_settings.apply_project_settings(home, root=root, kind="tmux")
        rec.setdefault("warnings", []).extend(settings_out.get("warnings") or ())
    else:
        harness_settings.remove_kind_settings(home)
    step("restore", "cousin.toml as it was, byte for byte; the %s kind's settings" % to)
    sid = rec.get("session_id")
    if sid:                                  # before the start, as the switch sets it
        extract.set_cursor(home, sid, cursor_end(home, root, sid, to))
        step("cursor", "the mining cursor at the end of the %s kind's record" % to)
    start(home, root)
    step("start", "the %s runner resumes %s" % (to, rec.get("session_id")))
    rec.update(state="rolled_back", rolled_back_at=_now())
    _write_switch_record(home, rec)
    return rec


def _switch_live():
    """The kind switch's live actions: the supervisor stops and starts the
    runner; verify reads the target kind's own record of the resumed session."""
    from cousin_lib import supervisor
    from cousin_lib.runner import transcript
    from cousin_lib.runner.session_store import SqliteSessionStore

    def close(home, root):
        answer = supervisor.request(root, "stop", slug=Path(home).name, wait=True,
                                    by="cousin-migrate", timeout=60.0)
        if not answer.get("ok"):
            raise MigrateError("the supervisor refused the stop: %s" % answer.get("error"))

    def start(home, root):
        answer = supervisor.request(root, "start", slug=Path(home).name)
        if not answer.get("ok"):
            raise MigrateError("the supervisor refused the start: %s" % answer.get("error"))

    def _transcript(home, root, sid):
        account, _err = _account(home, root)
        return transcript.locate(home, session_id=sid,
                                 config_dir=account.config_dir if account else None)

    def _turn_started(home, root, sid, since):
        entries, _ = transcript.read_from(_transcript(home, root, sid), 0)
        for e in entries:
            if e.kind == "turn_start":
                stamp = _stamp(str(e.raw.get("timestamp") or ""))
                if stamp is not None and stamp >= since:
                    return True
        return False

    def _session_init(home, sid, since):
        for path in sorted((Path(home) / "data" / "stream").glob("*.jsonl")):
            for line in path.read_text(errors="replace").splitlines():
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if (ev.get("kind") == "session_init" and float(ev.get("ts") or 0) >= since
                        and (ev.get("payload") or {}).get("session_id") == sid):
                    return True
        return False

    def say(line):
        print("  ..  %-10s %s" % ("verify", line), flush=True)

    def verify(home, root, sid, to, since, timeout=VERIFY_S):
        if to == "tmux":
            return wait_for_turn(home, lambda: _turn_started(home, root, sid, since), root=root,
                                 since=since, timeout=timeout, say=say,
                                 what="turn start under %s in the pane's transcript" % sid)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if _session_init(home, sid, since):
                return True, "the SDK's session_init names %s" % sid
            time.sleep(1.0)
        return False, "no session_init under %s within %ds" % (sid, timeout)

    def cursor_end(home, root, sid, to):
        if to == "tmux":
            try:
                return _transcript(home, root, sid).stat().st_size
            except OSError:
                return 0
        return SqliteSessionStore(home).entries_after(sid, 0)[1]

    return dict(close=close, start=start, verify=verify, cursor_end=cursor_end,
                supervisor_up=lambda root: supervisor.snapshot(root) is not None)


# ------------------------------------------------------------ tidy (2.0.0, R7)
# The keys 2.0.0 no longer reads are named everywhere an operator looks
# (removed_keys); `tidy` removes them. A line-based remover inside the
# named table, as set_agent_keys writes: comments, order and line endings
# survive, and the result is checked against the parsed file minus the
# removed keys before anything is written.

TIDY_SUFFIX = ".pre-2.0.0"
TIDY_TERM_S = 5.0          # how long a stopped chat server has to go
PID_FILE = "data/chat-server.pid"

_TABLE_RE = re.compile(r"^[ \t]*\[(?!\[)[ \t]*(.+?)[ \t]*\][ \t]*(#.*)?\r?\n?$")
_ARRAY_RE = re.compile(r"^[ \t]*\[\[[ \t]*(.+?)[ \t]*\]\][ \t]*(#.*)?\r?\n?$")
_KEY_RE = re.compile(r"""^[ \t]*((?:[A-Za-z0-9_-]+|"[^"\r\n]*"|'[^'\r\n]*')"""
                     r"""(?:[ \t]*\.[ \t]*(?:[A-Za-z0-9_-]+|"[^"\r\n]*"|'[^'\r\n]*'))*)[ \t]*=""")


class _Unedited(Exception):
    """The line remover cannot take these keys out of this file."""


def _key_path(raw):
    parts, cur, quote = [], "", None
    for ch in raw:
        if quote:
            if ch == quote:
                quote = None
            else:
                cur += ch
        elif ch in "\"'":
            quote = ch
        elif ch == ".":
            parts.append(cur.strip())
            cur = ""
        else:
            cur += ch
    parts.append(cur.strip())
    return tuple(parts)


def _blocks(text):
    """The file as [{"path", "header", "array", "items"}]: the top level,
    then one block per table header; items are ("blank"|"comment"|"stmt",
    lines, key path). A statement runs over as many lines as its value
    needs (it parses on its own once complete)."""
    lines = text.splitlines(keepends=True)
    blocks = [{"path": (), "header": None, "array": False, "items": []}]
    i = 0
    while i < len(lines):
        line = lines[i]
        bare = line.strip()
        if not bare or bare.startswith("#"):
            blocks[-1]["items"].append(("blank" if not bare else "comment", [line], None))
            i += 1
            continue
        if bare.startswith("["):
            m = _ARRAY_RE.match(line) or _TABLE_RE.match(line)
            if m is None:
                raise _Unedited("a table header it cannot read: %r" % bare)
            blocks.append({"path": _key_path(m.group(1)), "header": line,
                           "array": bare.startswith("[["), "items": []})
            i += 1
            continue
        m = _KEY_RE.match(line)
        if m is None:
            raise _Unedited("a line it cannot read: %r" % bare)
        j = i + 1
        while True:
            try:
                tomllib.loads("".join(lines[i:j]))
                break
            except tomllib.TOMLDecodeError:
                if j >= len(lines):
                    raise _Unedited("a value it cannot delimit: %r" % bare)
                j += 1
        blocks[-1]["items"].append(("stmt", lines[i:j], _key_path(m.group(1))))
        i = j
    return blocks


def _under(prefix, path):
    return len(path) >= len(prefix) and tuple(path[:len(prefix)]) == tuple(prefix)


def _without(data, paths):
    """`data` with every path in `paths` deleted, and each table that held
    one dropped when that left it empty."""
    import copy
    data = copy.deepcopy(data)
    for path in paths:
        chain = [data]
        for key in path[:-1]:
            nxt = chain[-1].get(key) if isinstance(chain[-1], dict) else None
            if not isinstance(nxt, dict):
                break
            chain.append(nxt)
        else:
            if path[-1] in chain[-1]:
                del chain[-1][path[-1]]
                for depth in range(len(chain) - 1, 0, -1):
                    if chain[depth]:
                        break
                    del chain[depth - 1][path[depth - 1]]
    return data


def strip_keys(text, paths):
    """`text` without the keys (or whole tables) at `paths`: their
    statement lines and table headers go, a table left with no statement
    goes with them, and every other byte stays. _Unedited when the result
    would not be exactly the parsed file minus those keys (an inline
    table holding one, say): the file is then edited by hand."""
    paths = [tuple(p) for p in paths]
    blocks = _blocks(text)
    touched = set()
    for b in blocks:
        if b["header"] is not None and any(_under(p, b["path"]) for p in paths):
            b["drop"] = True
            touched.update(b["path"][:k] for k in range(len(b["path"])))
            continue
        kept = []
        for kind, lines, key in b["items"]:
            full = b["path"] + key if kind == "stmt" else None
            if full is not None and any(_under(p, full) for p in paths):
                touched.update(full[:k] for k in range(len(full)))
                continue
            kept.append((kind, lines, key))
        b["items"] = kept
    for b in blocks:
        empty = b["header"] is not None and not b["array"] and b["path"] in touched \
            and not any(kind == "stmt" for kind, _l, _k in b["items"])
        if b.get("drop") or empty:
            b["header"] = None
            b["items"] = [item for item in b["items"] if item[0] == "comment"]
    new = "".join((b["header"] or "") + "".join("".join(lines) for _k, lines, _p in b["items"])
                  for b in blocks)
    try:
        ok = tomllib.loads(new) == _without(tomllib.loads(text), paths)
    except tomllib.TOMLDecodeError:
        ok = False
    if not ok:
        raise _Unedited("the keys are not on lines of their own")
    return new


def _prior_copy(path, into):
    """Copy `path`'s bytes and mode to `into`/<name>.pre-2.0.0 (or .1, .2,
    ...: never over an earlier copy); the copy's path."""
    dest = Path(into) / (path.name + TIDY_SUFFIX)
    n = 0
    while dest.exists():
        n += 1
        dest = Path(into) / ("%s%s.%d" % (path.name, TIDY_SUFFIX, n))
    dest.write_bytes(path.read_bytes())
    os.chmod(dest, path.stat().st_mode & 0o7777)
    return dest


def _tidy_file(path, table, where, into, yes, target):
    """Name `table`'s keys found in the TOML file at `path` and, with
    `yes`, remove them (the prior bytes copied into `into` first)."""
    try:
        text = path.read_bytes().decode("utf-8")
        data = tomllib.loads(text)
    except FileNotFoundError:
        return
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as err:
        target["errors"].append("%s does not read (%s): not tidied" % (where, err))
        return
    found = removed_keys.findings(data, table, where)
    if not found:
        return
    target["findings"] += found
    paths = [p for p, _k, _l in table if removed_keys.has(data, p)]
    try:
        new = strip_keys(text, paths)
    except _Unedited as err:
        target["errors"].append("%s: %s; edit it by hand (%s)"
                                % (where, err, removed_keys.summary(found)))
        return
    if not yes:
        return
    prior = _prior_copy(path, into)
    tmp = path.with_name(path.name + ".tidy.tmp")
    tmp.write_bytes(new.encode("utf-8"))
    os.chmod(tmp, path.stat().st_mode & 0o7777)
    os.replace(tmp, path)
    target["actions"].append("%s: %d removed (the prior bytes: %s)"
                             % (where, len(found), os.path.relpath(prior, into.parent)))


def _chat_server_of(pid, home):
    """`pid` is a 1.x chat server for `home`: spawn's own test (its command
    line carries a chat-server marker) and its `--home` names this home,
    so a reused pid, or another install's server on the same port, is
    never signalled. Without /proc, the pid file is trusted as spawn does."""
    from cousin_lib import spawn
    if not spawn._pid_is_chat_server(pid):
        return False
    try:
        args = [a.decode(errors="replace") for a in
                Path("/proc/%d/cmdline" % pid).read_bytes().split(b"\0")]
    except OSError:
        return not os.path.isdir("/proc")
    value = None
    for i, arg in enumerate(args):
        if arg == "--home" and i + 1 < len(args):
            value = args[i + 1]
            break
        if arg.startswith("--home="):
            value = arg.split("=", 1)[1]
            break
    if not value:
        return False
    if not os.path.isabs(value):
        try:
            value = os.path.join(os.readlink("/proc/%d/cwd" % pid), value)
        except OSError:
            return False
    return os.path.realpath(value) == os.path.realpath(home)


def _tidy_chat_server(home, data, yes, target, *, pid_alive, is_chat_server, port_pid, kill,
                      term_wait):
    """A 1.x chat server still running for `home`: by its pid file, else by
    the `[chat] port` it listens on (read before the key goes). Only a
    process `is_chat_server` accepts is signalled; the pid file goes."""
    import signal
    pid_file = Path(home) / PID_FILE
    pid, via = None, None
    try:
        pid, via = int(pid_file.read_text().strip()), PID_FILE
    except (OSError, ValueError):
        pid = None
    if pid is not None and not (pid_alive(pid) and is_chat_server(pid, home)):
        pid = None
    port = ((data or {}).get("chat") or {}).get("port")
    if pid is None and isinstance(port, int) and not isinstance(port, bool) and port > 0:
        found = port_pid(port)
        if found and pid_alive(found) and is_chat_server(found, home):
            pid, via = found, "[chat] port %d" % port
    if pid is not None:
        if not yes:
            target["actions"].append("chat server: pid %d (%s), a 1.x chat server for this"
                                     " cousin: stopped with --yes" % (pid, via))
        else:
            try:
                kill(pid, signal.SIGTERM)
                deadline = time.monotonic() + term_wait
                while pid_alive(pid) and time.monotonic() < deadline:
                    try:
                        os.waitpid(pid, os.WNOHANG)
                    except ChildProcessError:
                        pass
                    time.sleep(0.05)
                state = "stopped" if not pid_alive(pid) else \
                    "signalled, still up after %gs" % term_wait
            except ProcessLookupError:
                state = "already gone"
            except PermissionError:
                state = None
                target["errors"].append("chat server: pid %d (%s) is not ours to signal"
                                        % (pid, via))
            if state:
                target["actions"].append("chat server: pid %d (%s) %s" % (pid, via, state))
    if pid_file.exists():
        if not yes:
            if pid is None:
                target["actions"].append("%s names no running chat server: removed with --yes"
                                         % PID_FILE)
        else:
            pid_file.unlink(missing_ok=True)
            target["actions"].append("%s removed" % PID_FILE)


def _tidy_home(home, *, yes, seams):
    home = Path(home)
    target = {"target": home.name, "refused": None, "findings": [], "actions": [],
              "errors": []}
    try:
        data = tomllib.loads((home / "cousin.toml").read_text())
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        data = None
    kind = ((data or {}).get("agent") or {}).get("runner") if isinstance(data, dict) else None
    worker = isinstance(data, dict) and (data.get("cousin") or {}).get("type") == "worker"
    if data is None or (kind not in RUNNER_KINDS and not worker):
        target["refused"] = lane_refusal(home)       # tidy is not a conversion (R2)
        return target
    _tidy_chat_server(home, data, yes, target, **seams)
    _tidy_file(home / "cousin.toml", removed_keys.COUSIN_KEYS, "cousin.toml", home / "data",
               yes, target)
    return target


def _tidy_install(root, *, yes):
    root = Path(root)
    target = {"target": "install", "refused": None, "findings": [], "actions": [],
              "errors": []}
    for where, table in removed_keys.FILES:
        _tidy_file(root / where, table, where, root / "config", yes, target)
    cmd = root / removed_keys.AGENT_CMD[0]
    if cmd.exists():
        target["findings"].append({"where": removed_keys.AGENT_CMD[0],
                                   "key": removed_keys.AGENT_CMD[0],
                                   "line": removed_keys.AGENT_CMD[1]})
        if yes:
            dest = cmd.with_name(cmd.name + TIDY_SUFFIX)
            n = 0
            while dest.exists():
                n += 1
                dest = cmd.with_name("%s%s.%d" % (cmd.name, TIDY_SUFFIX, n))
            os.replace(cmd, dest)
            target["actions"].append("%s: moved to config/%s"
                                     % (removed_keys.AGENT_CMD[0], dest.name))
    return target


def tidy(root, homes, *, install=False, yes=False, pid_alive, is_chat_server, port_pid, kill,
         term_wait=TIDY_TERM_S):
    """[target] for each home in `homes`, then the install's config files
    when `install`: {"target" (slug or "install"), "refused" (a cousin
    with no runner kind: lane_refusal, nothing touched), "findings"
    ([{"where", "key", "line"}]), "actions" (done with `yes`, else what
    `yes` would do), "errors" (a file left whole, a process not ours)}.
    Nothing is written or signalled without `yes`."""
    seams = dict(pid_alive=pid_alive, is_chat_server=is_chat_server, port_pid=port_pid,
                 kill=kill, term_wait=term_wait)
    out = [_tidy_home(home, yes=yes, seams=seams) for home in homes]
    if install:
        out.append(_tidy_install(root, yes=yes))
    return out


def _tidy_live():
    from cousin_lib import spawn
    return dict(pid_alive=spawn._pid_alive, is_chat_server=_chat_server_of,
                port_pid=spawn._pid_bound_to_port, kill=os.kill)


def _tidy_cli(args, root):
    if args.all:
        base = root / "cousins"
        homes = sorted(e for e in base.iterdir() if (e / "cousin.toml").is_file()) \
            if base.is_dir() else []
    else:
        home = root / "cousins" / args.slug
        if not (home / "cousin.toml").exists():
            print("error: no cousin %r under %s" % (args.slug, root), file=sys.stderr)
            return 2
        homes = [home]
    targets = tidy(root, homes, install=args.all, yes=args.yes, **_tidy_live())
    if not args.all and targets[0]["refused"]:
        print("error: %s" % targets[0]["refused"], file=sys.stderr)
        return 2
    shown = bad = pending = False
    for t in targets:
        if t["refused"]:
            print("%s: refused: %s" % (t["target"], t["refused"]))
            shown = bad = True
            continue
        if not (t["findings"] or t["actions"] or t["errors"]):
            continue
        shown = True
        print("%s:" % t["target"])
        for f in t["findings"]:
            print("  %s: %s" % (f["key"] if f["key"] == f["where"] else
                                "%s %s" % (f["where"], f["key"]), f["line"]))
        for line in t["actions"]:
            print("  %s" % line)
        for line in t["errors"]:
            print("  NOT tidied: %s" % line)
        bad = bad or bool(t["errors"])
        pending = pending or bool(t["findings"] or t["actions"])
    if not shown:
        print("nothing to tidy")
        return 0
    if pending and not args.yes:
        print("plan only, nothing written: `cousin-migrate tidy %s --yes` removes them (each"
              " file's prior bytes go beside it, as <name>%s)"
              % ("--all" if args.all else args.slug, TIDY_SUFFIX))
    return 1 if bad else 0


# ------------------------------------------------------------ the CLI

def _print_removed(found, indent):
    """R7: one `warn 2.0.0` line per key 2.0.0 no longer reads."""
    for f in found or ():
        print("%swarn 2.0.0 %s: %s" % (indent, f["key"] if f["key"] == f["where"] else
                                       "%s %s" % (f["where"], f["key"]), f["line"]))


def _print_plan(p):
    for c in p["checks"]:
        if c["check"] == "carry" and c["ok"]:
            print("  ok  carry")
            for r in p["carry"]["rows"]:
                print("        %-7s %s" % (r["action"], r["detail"]))
            continue
        print("  %s %-10s %s" % ("ok " if c["ok"] else "NO ", c["check"], c["detail"]))
    for line in p.get("notes") or ():
        print("  note %s" % line)
    print("steps: %s" % " -> ".join(p["steps"]))
    print("%s: %s" % (p["slug"], "ready (run: cousin-migrate apply %s%s --yes)" % (
        p["slug"], " --validate" if any(c["check"] == "validate" for c in p["checks"]) else "")
        if p["ready"] else "not ready"))


def _switch_cli(args, home, root):
    live = _switch_live()
    try:
        if args.cmd == "plan":
            p = switch_plan(home, root=root, to=args.to, **live)
            print("%s: %s -> %s, steps %s" % (p["slug"], p["from"], p["to"], ", ".join(p["steps"])))
            for c in p["checks"]:
                print("  %s %-10s %s" % ("ok " if c["ok"] else "NO ", c["check"], c["detail"]))
            for w in p["warnings"]:
                print("  warn %s" % w)
            _print_removed(p.get("removed"), "  ")
            print("ready" if p["ready"] else "NOT ready")
            return 0 if p["ready"] else 1
        if args.cmd == "rollback":
            rec = switch_rollback(home, root=root, to=args.to, **live)
            for s in rec["rollback_steps"]:
                print("  ok  %-10s %s" % (s["step"], s["detail"]))
            for w in rec.get("warnings") or ():
                print("  warn %s" % w)
            print(rec["state"])
            return 0
        rec = switch_apply(home, root=root, to=args.to, **live)
    except MigrateError as err:
        print("error: %s" % err, file=sys.stderr)
        return 2
    for s in rec["steps"]:
        print("  ok  %-10s %s" % (s["step"], s["detail"]))
    for w in rec.get("warnings") or ():
        print("  warn %s" % w)
    print(rec["state"])
    return 0


def no_kind_line(home):
    """Why a plan, apply or rollback without --to is refused (row 72): a
    cousin with no runner gets delivery.lane_refusal; a runner cousin is
    told to name a kind."""
    from cousin_lib.delivery import _runner_kind
    kind = _runner_kind(home)
    if kind not in RUNNER_KINDS:
        return lane_refusal(home)
    return ("%s runs on %s: name a kind with --to (%s); 2.0.0 has no legacy lane to"
            " migrate from" % (Path(home).name, kind, ", ".join(SWITCH_KINDS)))


def migrate_main(argv=None):
    from cousin_lib.config import FrameworkConfig
    parser = argparse.ArgumentParser(
        prog="cousin-migrate",
        description="plan, apply and rollback --to switch a runner cousin between the sdk"
                    " and tmux kinds (without --to they are refused: 2.0.0 has no legacy"
                    " lane to migrate from); check measures a cousin; tidy removes the"
                    " keys 2.0.0 no longer reads")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "apply"):
        p = sub.add_parser(name)
        p.add_argument("slug")
        p.add_argument("--account", default=None,
                       help="the config/accounts.toml account it runs on (default: the host login)")
        p.add_argument("--validate", action="store_true",
                       help="one smallest model turn with the model, effort and account the"
                            " runner will run (needed when a model is carried: %s)" % NEVER_UNRUN)
        p.add_argument("--to", choices=SWITCH_KINDS, default=None,
                       help="switch a runner cousin between the sdk and tmux kinds (phase 11)")
        if name == "apply":
            p.add_argument("--yes", action="store_true", help="really run the steps")
    p = sub.add_parser("rollback")
    p.add_argument("slug")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--to", choices=SWITCH_KINDS, default=None,
                   help="roll a kind switch back to the kind it came from (phase 11)")
    p.add_argument("--force", action="store_true",
                   help="roll back with inbox rows waiting, or an inbox that cannot be read")
    p = sub.add_parser("check")
    p.add_argument("slug")
    p.add_argument("--since", default=None,
                   help="an ISO time (default: when the migration finished)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--validate", action="store_true",
                   help="one smallest model turn with the runner's model, effort and account")
    p = sub.add_parser("tidy", help="remove the keys 2.0.0 no longer reads (a plan without"
                                     " --yes) and stop a 1.x chat server still running")
    p.add_argument("slug", nargs="?", help="one cousin (its cousin.toml and chat server)")
    p.add_argument("--all", action="store_true",
                   help="every cousin, and the install's config/harness.toml, config/hive.toml"
                        " and config/agent-cmd")
    p.add_argument("--yes", action="store_true", help="really remove them")
    args = parser.parse_args(argv)
    if args.cmd == "tidy" and (args.slug is None) == (not args.all):
        parser.error("tidy takes a cousin's slug or --all, exactly one")
    root = FrameworkConfig.resolve().root
    if args.cmd == "tidy":
        return _tidy_cli(args, root)
    home = root / "cousins" / args.slug
    if not (home / "cousin.toml").exists():
        print("error: no cousin %r under %s" % (args.slug, root), file=sys.stderr)
        return 2
    if args.cmd == "check":
        since = _stamp(args.since) if args.since else None
        if args.since and since is None:
            print("error: --since %r is not an ISO time" % args.since, file=sys.stderr)
            return 2
        c = check(home, since=since, health=chat_health, root=root, validate=args.validate)
        if args.json:
            print(json.dumps(c, indent=1))
        else:
            if not c["inbox_readable"]:
                print("inbox: UNREADABLE (data/inbox.db)")
            print("inbox: %(done)d done (%(failed)d failed), %(open)d open, %(stale)d stale"
                  % c["inbox"])
            print("tool calls: %d, unrecorded: %s" % (c["tool_calls"],
                                                      ", ".join(c["unrecorded"]) or "none"))
            print("recorder hook errors: %s" % ("; ".join(c["hook_errors"]) or "none"))
            print("chat server: %s" % c["chat"])
            print("runner CLI: %s" % c["cli"])
            if c.get("switch"):
                sw = c["switch"]
                print("kind switch: %s -> %s, %s%s" % (sw["from"], sw["to"], sw["state"],
                                                       " (%s)" % sw["late"] if sw["late"] else ""))
            if "validate" in c:
                print("%s %s" % ("validate:" if c["validate_ok"] else "NOT VALID:", c["validate"]))
            if c["config"]:
                print("runner config: %s" % ", ".join(
                    "%s=%s" % kv for kv in c["config"]["runner"].items()))
            for line in c["mismatches"]:
                print("MISMATCH %s" % line)
            for line in c.get("warnings") or ():
                print("warn %s" % line)
            _print_removed(c.get("removed"), "")
            print("ok" if c["ok"] else "NOT ok")
        return 0 if c["ok"] else 1
    if not getattr(args, "to", None):
        # Row 72: 2.0.0 keeps no conversion from the legacy lane (O3), so a
        # plan, apply or rollback names a kind; the legacy migration below
        # is not reached.
        print("error: %s" % no_kind_line(home), file=sys.stderr)
        return 2
    if args.cmd in ("apply", "rollback") and not args.yes:
        print("error: %s changes a live cousin; run `cousin-migrate plan %s` first, then"
              " pass --yes" % (args.cmd, args.slug), file=sys.stderr)
        return 2
    if getattr(args, "to", None):
        return _switch_cli(args, home, root)
    live = _live()
    try:
        if args.cmd == "plan":
            p = plan(home, root=root, account=args.account, validate=args.validate, **live)
            _print_plan(p)
            return 0 if p["ready"] else 1
        if args.cmd == "apply":
            rec = apply(home, root=root, account=args.account, validate=args.validate, **live)
        else:
            rec = rollback(home, root=root, force=args.force, **live)
    except MigrateError as err:
        print("error: %s" % err, file=sys.stderr)
        return 2
    for s in rec.get("steps", []) if args.cmd == "apply" else rec.get("rollback_steps", []):
        print("  %s %-10s %s" % ("ok " if s.get("ok", True) else "NO ", s["step"], s["detail"]))
    if args.cmd == "apply":
        for line in rec.get("warnings") or ():
            print("  warn %s" % line)
        got = rec.get("handover")
        if got is not None:
            for t in got["transcripts"]:
                print("previous transcript (%s): %s" % (t["which"], t["path"]))
            if got["missing"]:
                print("previous transcript missing: %s" % got["missing"])
    print("%s: %s" % (args.slug, rec["state"]))
    if rec["state"] == "failed":
        print("undo with: cousin-migrate rollback %s --yes" % args.slug)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(migrate_main())
