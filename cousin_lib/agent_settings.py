"""cousin.toml [agent] per runner lane: which keys a lane reads, what each
may hold, and the checks the runner itself makes, run before anything is
written. The console's settings routes and the spawn path read this one
table instead of keeping lists of their own.

The lanes are delivery.RUNNER_KINDS, read at call time and never copied
here: a cousin whose [agent] runner is not one of them is on the tmux
lane ("tmux-legacy"), whose model and effort are [runtime]'s and which
has no [agent] settings. Each key names the lanes that read it; a kind
added to RUNNER_KINDS later (phase 11's `tmux`) gets the keys that name
it plus the ones every lane reads, so its slot below (`env_allow`) is
live the day the kind is.

The checks are the runner's own, reused: runner/main.effort_of,
accounts.load / check_lane / refuse_claude_name, the [agent.sessions]
parser (runner/sessions.parse_map), opencode.shell_env and the opencode
runner's model checks, spawn.check_runtime_value for an sdk model's
shape. Nothing here spends a model turn: an sdk model that parses may
still be refused by the API at the next start.

- describe(home, root): the effective settings and the allowed values
  per key for this cousin's lane, plus what is wrong with them now.
- validate(home, root, changes): the changes normalized, or
  SettingsError with a reason per key; nothing written.
- apply(home, root, changes): validate, then one atomic write
  (console/toml_edit.write_keys) whose hook re-checks the parsed result.
- check_new(root, agent): the [agent] table a new cousin is spawned with.
- summary(home): lane, account, autoStart for the fleet row."""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

from cousin_lib import accounts, delivery

TMUX_LEGACY = "tmux-legacy"
ALL = "*"
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# key -> spec. lanes: the kinds that read the key (ALL: every runner kind).
# readonly: shown, never written here. restart: every [agent] key is read
# when the runner starts, so every change applies at the next start.
SCHEMA = {
    "runner": {"type": "choice", "lanes": ALL, "readonly": True,
               "hint": "the lane; switching it is a migration (cousin-migrate)"},
    "account": {"type": "account", "lanes": ALL, "default": accounts.HOST,
                "hint": "the credentials the runner uses (config/accounts.toml)"},
    "auto_start": {"type": "bool", "lanes": ALL, "default": True,
                   "hint": "the supervisor starts this runner with itself"},
    "model": {"type": "model", "lanes": ("sdk", "opencode"),
              "hint": "sdk: a model name; opencode: \"<provider>/<model>\""},
    "effort": {"type": "effort", "lanes": ("sdk",)},
    "rollover_at_percent": {"type": "percent", "lanes": ("sdk", "opencode"),
                            "min": 1, "max": 100,
                            "hint": "context use at which the session rolls over"},
    "sessions": {"type": "sessions", "lanes": ("sdk",),
                 "hint": "thread kinds with a side session of their own"},
    "small_model": {"type": "model", "lanes": ("opencode",),
                    "hint": "\"<provider>/<model>\"; the model when unset"},
    "opencode_models_fetch": {"type": "bool", "lanes": ("opencode",), "default": True},
    "shell_env": {"type": "env_list", "lanes": ("opencode",), "default": [],
                  "hint": "variables passed to the model's shell; never a credential"},
    "opencode_bin": {"type": "str", "lanes": ("opencode",), "readonly": True,
                     "hint": "a binary the runner executes: edit cousin.toml by hand"},
    "api_key_file": {"type": "str", "lanes": ("sdk", "fake"), "readonly": True,
                     "deprecated": True, "only_when_set": True,
                     "hint": "deprecated: move the key to an account"},
    # Phase 11 slot: the tmux kind's keys, live once `tmux` is a RUNNER_KIND.
    "env_allow": {"type": "env_list", "lanes": ("tmux",), "default": [],
                  "hint": "variables the agent may inherit; the hard deny still wins"},
}


class SettingsError(ValueError):
    """Refused settings: `errors` maps each key to its reason."""

    def __init__(self, errors):
        self.errors = dict(errors)
        super().__init__("; ".join("%s: %s" % kv for kv in self.errors.items()))


def kinds():
    return list(delivery.RUNNER_KINDS)


def lane_of(agent):
    runner = (agent or {}).get("runner")
    return runner if runner in delivery.RUNNER_KINDS else TMUX_LEGACY


def lane_keys(lane):
    """The [agent] keys `lane` reads, in schema order; none on tmux-legacy."""
    if lane not in delivery.RUNNER_KINDS:
        return []
    return [k for k, spec in SCHEMA.items()
            if spec["lanes"] == ALL or lane in spec["lanes"]]


def _read_agent(home):
    try:
        data = tomllib.loads((Path(home) / "cousin.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    agent = data.get("agent")
    return agent if isinstance(agent, dict) else {}


def summary(home):
    """{lane, account, autoStart} for the fleet row, from cousin.toml alone
    (no accounts.toml read): the account a runner cousin names, `host`
    when it names none, null on the tmux lane."""
    agent = _read_agent(home)
    lane = lane_of(agent)
    if lane == TMUX_LEGACY:
        return {"lane": lane, "account": None, "autoStart": None}
    account = agent.get("account") or (None if agent.get("api_key_file") else accounts.HOST)
    return {"lane": lane, "account": account, "autoStart": agent.get("auto_start") is not False}


# ---- one value ----------------------------------------------------------

def _check_value(key, value, lane, home):
    """The value normalized, or ValueError with the reason. Cross-key
    checks (the account against the lane, an opencode model against its
    account) are _cross's."""
    from cousin_lib.runner.base import RunnerError
    kind = SCHEMA[key]["type"]
    if kind == "bool":
        if not isinstance(value, bool):
            raise ValueError("must be true or false")
        return value
    if kind == "percent":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("must be a number")
        lo, hi = SCHEMA[key]["min"], SCHEMA[key]["max"]
        if not lo <= value <= hi:
            raise ValueError("must be between %s and %s" % (lo, hi))
        return float(value)
    if kind == "effort":
        from cousin_lib.runner.main import effort_of
        try:
            return effort_of({"effort": value})
        except RunnerError as err:
            raise ValueError(str(err))
    if kind == "model":
        if not isinstance(value, str) or not value:
            raise ValueError("must be a model name")
        if lane == "opencode":
            from cousin_lib.runner import opencode, opencode_guard
            try:
                accounts.refuse_claude_name("cousin.toml [agent] %s" % key, value)
                opencode.split_model(value, key)
                # the runner's bridge guard (R13), on the value as it is rendered
                opencode_guard.refuse_bridge({key: value}, {})
            except opencode_guard.BridgeRefused as err:
                raise ValueError(err.reason)
            except (accounts.AccountsError, RunnerError) as err:
                raise ValueError(str(err))
            return value
        from cousin_lib import spawn
        try:
            spawn.check_runtime_value("model", value)
        except spawn.SpawnError as err:
            # the shape check is [runtime]'s; the key here is [agent]'s
            raise ValueError(str(err).replace("runtime.model", "agent.model"))
        return value
    if kind == "account":
        if not isinstance(value, str) or not value:
            raise ValueError("must be an account name")
        return value
    if kind == "sessions":
        from cousin_lib.runner import sessions
        try:
            sessions.parse_map(value)
        except RunnerError as err:
            raise ValueError(str(err))
        return dict(value)
    if kind == "env_list":
        if not isinstance(value, list) or not all(isinstance(n, str) for n in value):
            raise ValueError("must be a list of variable names")
        if key == "shell_env":
            from cousin_lib.runner import opencode
            try:
                opencode.shell_env(Path(home or "."), {"shell_env": value}, environ={})
            except RunnerError as err:
                raise ValueError(str(err))
        for name in value:
            if not _ENV_NAME.match(name):
                raise ValueError("%r is not a variable name" % name)
        return list(value)
    if not isinstance(value, str):
        raise ValueError("must be a string")
    return value


# ---- the table as a whole -----------------------------------------------

def _account(root, agent, home):
    """The Account the table names, as accounts.for_cousin resolves it."""
    name, key_file = agent.get("account"), agent.get("api_key_file")
    if name and key_file:
        raise accounts.AccountsError("account and api_key_file together; name the account only")
    if key_file:
        return accounts.Account(Path(home).name if home else "new", "anthropic-key", None,
                                accounts._under(root, key_file, "api_key_file",
                                                "cousin.toml [agent]"), implicit=True)
    if not name or name == accounts.HOST:
        return accounts.Account(accounts.HOST, "claude-login", None, None, implicit=True)
    known = accounts.load(root)
    if name not in known:
        raise accounts.AccountsError("account %r is not in config/accounts.toml (known: %s)"
                                     % (name, ", ".join(sorted(known)) or "none"))
    return known[name]


def _cross(root, agent, home=None):
    """{key: reason} for what the table cannot run with as a whole: the
    account on this lane (check_lane), the opencode lane's required model
    and its models against the account (the opencode runner's own
    _check_models), and every known key's value on its own."""
    from cousin_lib.runner.base import RunnerError
    errors = {}
    lane = lane_of(agent)
    if lane == TMUX_LEGACY:
        return errors
    for key in lane_keys(lane):
        if key in agent and not SCHEMA[key].get("readonly"):
            try:
                _check_value(key, agent[key], lane, home)
            except ValueError as err:
                errors[key] = str(err)
    try:
        account = _account(root, agent, home)
        accounts.check_lane(account, lane)
        # the runner's preflight before its lock (runner/main.account_for): a
        # secret open to others, not ours, a symlink or malformed is exit 2;
        # a missing one is a login to do, which the runner waits for
        try:
            accounts.preflight(account, root)
        except accounts.SecretMissing:
            pass
    except accounts.AccountsError as err:
        errors.setdefault("account", str(err))
        account = None
    if lane == "opencode":
        if not agent.get("model"):
            errors.setdefault("model", "[agent] model is required on the opencode lane"
                                       " (\"<provider>/<model>\")")
        elif account is not None and "model" not in errors and "small_model" not in errors:
            from types import SimpleNamespace
            from cousin_lib.runner.opencode import OpencodeRunner
            probe = SimpleNamespace(model=agent["model"],
                                    small_model=agent.get("small_model") or agent["model"],
                                    account=account)
            try:
                # the runner's own check, on the values as they would be read
                OpencodeRunner._check_models(probe)
            except RunnerError as err:
                text = str(err)     # "cousin.toml [agent] <key> ...": the key it names
                key = "small_model" if text.startswith("cousin.toml [agent] small_model") \
                    else "model"
                errors[key] = text
        if account is not None and "model" not in errors and "small_model" not in errors:
            from cousin_lib.runner import opencode, opencode_guard
            config = opencode.render_config(
                account, model=agent["model"],
                small_model=agent.get("small_model") or agent["model"],
                mcp_url="http://127.0.0.1:0/mcp", mcp_token="-")
            try:
                # the constructor's guard (OpencodeRunner._render) on the whole
                # config as it would be written; the environment is the
                # runner's own at start and is checked there
                opencode_guard.refuse_bridge(config, {})
            except opencode_guard.BridgeRefused as err:
                errors["model"] = err.reason
    return errors


# ---- the public surface -------------------------------------------------

def _choices(key, lane, root):
    if key == "runner":
        return kinds()
    if key == "effort":
        from cousin_lib.config import EFFORT_LEVELS
        return list(EFFORT_LEVELS)
    if key == "account":
        try:
            known = accounts.load(root)
        except accounts.AccountsError:
            known = {}
        out = [] if lane == "opencode" else [accounts.HOST]
        for name in sorted(known):
            try:
                accounts.check_lane(known[name], lane)
            except accounts.AccountsError:
                continue
            out.append(name)
        return out
    return None


def _suggestions(key, lane, root, agent):
    if key != "model":
        return None
    if lane == "sdk":
        from cousin_lib.config import DEFAULT_MODELS, MissingConfigError, agent_config
        try:
            return list(agent_config(root)["models"])
        except MissingConfigError:
            return list(DEFAULT_MODELS)
    if lane == "opencode":
        try:
            account = _account(root, agent, None)
        except accounts.AccountsError:
            return []
        if account.endpoint_model:
            from cousin_lib.runner.opencode import ENDPOINT_PROVIDER
            return ["%s/%s" % (ENDPOINT_PROVIDER, account.endpoint_model)]
        return ["%s/" % p for p in account.providers]
    return None


def model_rule(lane):
    """How `lane` takes [agent] model, for a form: {required, catalogue,
    hint}. `catalogue`: config/harness.toml's model list are valid
    suggestions (the sdk lane's Claude models; never on opencode, which
    refuses them). None when the lane reads no model."""
    if "model" not in lane_keys(lane):
        return None
    if lane == "opencode":
        return {"required": True, "catalogue": False,
                "hint": "required: \"<provider>/<model>\" on a provider the account holds"}
    return {"required": False, "catalogue": True,
            "hint": "a model name; blank is the runner's default"}


def _default(key, agent):
    if key == "rollover_at_percent":
        from cousin_lib.runner.rollover import ROLLOVER_AT_PERCENT
        return float(ROLLOVER_AT_PERCENT)
    if key == "small_model":
        return agent.get("model")
    if key == "sessions":
        from cousin_lib.runner import sessions
        return sessions.parse_map({})
    return SCHEMA[key].get("default")


def describe(home, root):
    """{lane, kinds, settings: {key: {value, set, default, type, readonly,
    choices?, suggestions?, hint, restart}}, errors: {key: reason}} for the
    cousin's lane. On tmux-legacy, settings is empty."""
    agent = _read_agent(home)
    lane = lane_of(agent)
    out = {"lane": lane, "kinds": kinds(), "settings": {}, "errors": {},
           "restart_required": True}
    if lane == TMUX_LEGACY:
        return out
    for key in lane_keys(lane):
        spec = SCHEMA[key]
        if spec.get("only_when_set") and key not in agent:
            continue
        default = _default(key, agent)
        value = agent.get(key, default)
        if key == "sessions":
            from cousin_lib.runner import sessions
            try:
                value = sessions.parse_map(agent.get("sessions") or {})
            except Exception:  # noqa: BLE001 - reported under errors
                value = default
        row = {"value": value, "set": key in agent, "default": default,
               "type": spec["type"], "readonly": bool(spec.get("readonly")),
               "hint": spec.get("hint", ""), "restart": True}
        if spec.get("deprecated"):
            row["deprecated"] = True
        if "min" in spec:
            row["min"], row["max"] = spec["min"], spec["max"]
        choices = _choices(key, lane, root)
        if choices is not None:
            row["choices"] = choices
        suggestions = _suggestions(key, lane, root, agent)
        if suggestions is not None:
            row["suggestions"] = suggestions
        if key == "sessions":
            from cousin_lib.delivery import THREAD_KINDS
            from cousin_lib.runner.sessions import ALWAYS_PRIMARY, OWN, PRIMARY
            row["kinds"] = list(THREAD_KINDS)
            row["always_primary"] = list(ALWAYS_PRIMARY)
            row["choices"] = [PRIMARY, OWN]
        out["settings"][key] = row
    out["errors"] = _cross(root, agent, home)
    return out


def _merge(agent, changes):
    merged = dict(agent)
    for key, value in changes.items():
        if value is None:
            merged.pop(key, None)
        elif key == "sessions":
            table = dict(agent.get("sessions") or {})
            table.update(value)
            merged["sessions"] = {k: v for k, v in table.items() if v != "primary"}
        else:
            merged[key] = value
    return merged


def validate(home, root, changes):
    """The changes normalized ({key: value}, None removes the key), or
    SettingsError. A key is refused when it is unknown, read-only, or not
    read by this cousin's lane; its value by the runner's own check; and
    the table the changes leave by _cross. Nothing is written."""
    if not isinstance(changes, dict) or not changes:
        raise SettingsError({"changes": "nothing to change"})
    agent = _read_agent(home)
    lane = lane_of(agent)
    if lane == TMUX_LEGACY:
        raise SettingsError({"runner": "a tmux-legacy cousin has no [agent] settings; its"
                                       " model and effort are [runtime]'s"})
    errors, out = {}, {}
    keys = lane_keys(lane)
    for key, value in changes.items():
        if key not in SCHEMA:
            errors[key] = "unknown [agent] key"
        elif SCHEMA[key].get("readonly"):
            errors[key] = "read-only here: %s" % SCHEMA[key].get("hint", "")
        elif key not in keys:
            lanes = SCHEMA[key]["lanes"]
            errors[key] = "not read on the %s lane (only %s)" % (lane, ", ".join(lanes))
        elif value is None:
            out[key] = None
        else:
            try:
                out[key] = _check_value(key, value, lane, home)
            except ValueError as err:
                errors[key] = str(err)
    if errors:
        raise SettingsError(errors)
    errors = _cross(root, _merge(agent, out), home)
    if errors:
        raise SettingsError(errors)
    return out


def apply(home, root, changes):
    """validate, then one atomic write of every change; the write's hook
    re-checks the parsed [agent] table before the rename. Returns
    describe() of the result."""
    from cousin_lib.console import toml_edit
    out = validate(home, root, changes)
    writes = []
    for key, value in out.items():
        if key == "sessions":
            for kind, mode in value.items():
                writes.append(("agent.sessions", kind, None if mode == "primary" else mode))
        else:
            writes.append(("agent", key, value))

    def hook(parsed):
        errors = _cross(root, parsed.get("agent") or {}, home)
        if errors:
            raise SettingsError(errors)
    toml_edit.write_keys(home, writes, validate=hook)
    return describe(home, root)


def check_new(root, agent):
    """The [agent] table a new cousin is spawned with ({runner, account?,
    model?, effort?, ...}), normalized, or SettingsError: every key read by
    the lane, each value and the table as a whole checked as validate
    does."""
    lane = lane_of(agent)
    if lane == TMUX_LEGACY:
        raise SettingsError({"runner": "must be one of %s" % ", ".join(kinds())})
    errors, out = {}, {}
    keys = lane_keys(lane)
    for key, value in agent.items():
        if key == "runner":
            out[key] = value
        elif key not in SCHEMA:
            errors[key] = "unknown [agent] key"
        elif key not in keys:
            errors[key] = "not read on the %s lane" % lane
        else:
            try:
                out[key] = _check_value(key, value, lane, None)
            except ValueError as err:
                errors[key] = str(err)
    if not errors:
        errors = _cross(root, out, None)
    if errors:
        raise SettingsError(errors)
    return out
