"""OpencodeRunner: a cousin on opencode instead of the Claude Agent SDK,
behind the same Runner protocol, inbox and event stream.

The runner owns one `opencode serve` child (opencode_http.OpencodeServer)
on loopback with a per-run password, its HOME and XDG directories in the
account's data dir, working in the cousin's home, started with an
ALLOWLISTED environment and a config the runner renders at every start:
opencode's own hosted provider disabled unless the account names it,
the model named, the
plugin pack, and one MCP server, the runner's own (mcp_http.McpHttpServer),
so every framework tool runs in this process against the live Turn,
plus one `local` MCP server per framework plugin the cousin enables that
declares [mcp] (cousin_lib/plugins.py; the home's .mcp.json is not read
on this kind).
Before the first prompt it checks that opencode reports that MCP server
`connected`; a runner whose model would have no framework tools refuses
to run turns.

The loop is FakeRunner's shape: claim one row, run one turn, close every
row with a result, route every failure to an outcome. A turn is one
`prompt_async` (204; the turn itself arrives on GET /event) and ends at
the first `session.idle` that follows a prompt the runner sent and whose
user message opencode announced: an idle with nothing outstanding
is ignored (opencode doubles the idle pair after an abort or a failure).
While the run is busy, operator, person and peer chat is sent at once
and opencode folds it into the same run (measured), so it closes at
the same idle; meeting, loop and schedule rows wait for their own turn
(`base.FOLDED_KINDS`). An abort drops a prompt
queued behind the running one, so every row sent into an aborted run
that the model never started is requeued. A `session.error` classifies
the turn: an auth error puts the rows back and waits for the login,
an abort is an interruption, anything else fails the turn.

The policy veto lives in the plugin pack: the runner renders
policy.toml as `<data dir>/cousin-policy.json`, names it to opencode in
COUSIN_POLICY_FILE, and refuses to run turns until opencode's config lists
the plugin AND the plugin acknowledged this start's file (opencode lists a
plugin whether or not it loaded). Recording, subagent jobs and checkpoints
come from SSE, here: a tool part's `running` state carries its
arguments, and the runner hands the recording library the SDK lane's hook
payloads, the tool named in the SDK form (`bash` -> `Bash`, `cousin_reply`
-> `mcp__cousin__reply`), the one form policy.toml and the recorder use.

Stdlib and cousin_lib only."""
import fcntl
import hashlib
import json
import os
import queue
import re
import threading
import time
import tomllib
import uuid
from datetime import datetime, timezone
from pathlib import Path

from cousin_lib import accounts, boot, recording, session, usage
from cousin_lib.delivery import DELIVERED, FAILED, QUEUED, Item
from cousin_lib.runner import auth, checkpoints, envelope, opencode_guard, opencode_http
from cousin_lib.runner import tools, wake
from cousin_lib.runner import policy as _policy
from cousin_lib.runner import rollover as _rollover
from cousin_lib.runner.base import INTERRUPT, NO_TURN, Receipt, RunnerError, folds_into_turn
from cousin_lib.runner.inbox import Inbox
from cousin_lib.runner.opencode_http import (EventReader, OpencodeClient, OpencodeError,
                                             OpencodeServer)
from cousin_lib.runner.policy import Policy
from cousin_lib.runner.state import StateMachine
from cousin_lib.runner.stream import EventStream
from cousin_lib.runner.turn import Turn

LANE = "opencode"
CHECKOUT = Path(__file__).resolve().parents[2]
# The plugin pack: the checkout's, as hooks/ is.
PLUGIN = CHECKOUT / "plugins" / "opencode" / "cousin-policy.js"
CONFIG_NAME = "opencode.runner.json"
SCHEMA = "https://opencode.ai/config.json"
# The provider id a local endpoint account is rendered under: its model is
# `local/<endpoint_model>`.
ENDPOINT_PROVIDER = "local"
# The only variables the server inherits from the runner's environment
# (plus LC_*): opencode reads provider keys (*_API_KEY), config sources
# (OPENCODE_CONFIG_CONTENT, OPENCODE_CONFIG_DIR) and switches
# (OPENCODE_*) from its environment, so nothing else passes. The CA
# bundle paths are not secrets and an https provider may need them.
ENV_ALLOW = ("PATH", "LANG", "LANGUAGE", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR")
HEALTH_TIMEOUT_S = 60.0         # a fresh HOME measured 13.6 s
BRIDGE_PORT = 3456              # the bridge guard refuses a URL on it
THINKING_CHARS = 8000           # SdkRunner's bound on a thinking block
TEXT_CHARS = 2000               # a tool result's, a user echo's bound
# opencode installs this package from npm into its config dir whenever any
# plugin is configured, and loads no plugin (answers no /event) until the
# install ends: 71 s and a failure offline, npm egress online (measured on
# 1.18.31). The pack imports nothing, so the runner marks it
# present (seed_plugin_dependency) and opencode installs nothing.
PLUGIN_DEPENDENCY = "@opencode-ai/plugin"
# The plugin pack's policy file, its acknowledgement and how the plugin
# finds the file. Both files live in the account's data dir.
POLICY_NAME = "cousin-policy.json"
POLICY_ACK = "cousin-policy.ack.json"
POLICY_ENV = "COUSIN_POLICY_FILE"
# The model's shell: opencode merges the plugin's
# `shell.env` answer over the server's environment, so what the shell must
# not inherit is set empty (XDG says empty is unset), never deleted. HOME is
# the cousin's home; SHELL_PASS and `[agent] shell_env` come from the runner's
# own environment.
SHELL_BLANK = ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME",
               "OPENCODE_SERVER_PASSWORD", "OPENCODE_CONFIG", POLICY_ENV)
SHELL_PASS = ("USER", "LOGNAME")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# A name shaped like a credential is never passed to the model's shell:
# accounts.credential_name, shared with the tmux pane.
TURN_GUARD_RETRY_S = 0.5            # one more try of the per-turn checks before giving up
ACCOUNT_HOLDER = "account.holder"   # the home of the runner holding the account (its flock)
ACCOUNT_TAKE_S = 1.0                # a restarting runner's predecessor may still be exiting
PLUGIN_TIMEOUT_S = 30.0         # GET /config (the instance's bootstrap), then the plugin's word
DENIED = "denied by policy: "   # what the plugin's throw reads as, to the model
# opencode's built-in tools in the SDK lane's names: the ONE form policy.toml
# and the recorder use on both lanes. A tool opencode has and the SDK lane
# has not (apply_patch, question, invalid) keeps its own name.
SDK_NAMES = {"bash": "Bash", "read": "Read", "write": "Write", "edit": "Edit",
             "glob": "Glob", "grep": "Grep", "task": "Agent", "webfetch": "WebFetch",
             "websearch": "WebSearch", "todowrite": "TodoWrite", "skill": "Skill"}
OWN_PREFIX = "cousin_"
# The largest output opencode asks of any model (its OUTPUT_TOKEN_MAX, 1.18.31)
OPENCODE_OUTPUT_MAX = 32000
# What may sit in opencode's global config dir (<XDG_CONFIG_HOME>/opencode),
# exactly what a clean start measured on 1.18.31: opencode's own .gitignore
# and the plugin library seed_plugin_dependency writes (node_modules and a
# package-lock.json whose root names PLUGIN_DEPENDENCY only). opencode MERGES
# opencode.json(c) there over the rendered config and LOADS plugin(s)/ and
# tool(s)/ (code in the server, which GET /config does not list), so the dir
# is an allowlist: anything else there refuses the start.
CONFIG_DIR_OWN = frozenset((".gitignore", "node_modules", "package-lock.json"))


def tool_name(name):
    """What opencode calls the registry's tool `name`."""
    return "%s%s" % (OWN_PREFIX, name)


def sdk_tool_name(name):
    """opencode's tool name in the SDK lane's form: `bash` -> `Bash`,
    `cousin_reply` -> `mcp__cousin__reply`; any other name is its own."""
    name = str(name or "")
    if name in SDK_NAMES:
        return SDK_NAMES[name]
    if name.startswith(OWN_PREFIX):
        return _policy.OWN_TOOL_PREFIX + name[len(OWN_PREFIX):]
    return name


def render_policy(policy, *, nonce, ack, shell_env=None):
    """policy.toml as the plugin reads it: the same lists, each
    pattern with the reason Policy.decide gives, the name table, this
    start's nonce and acknowledgement path, and what the plugin's
    `shell.env` hook sets for the model's shell (`shell_env`)."""
    return {
        "version": 1, "nonce": nonce, "ack": str(ack), "file": _policy.FILE,
        "source": policy.source,
        "deny_tools": list(policy.deny_tools),
        "deny_bash_patterns": [{"source": rx.pattern,
                                "reason": "%s: deny_bash_patterns %r matches"
                                          % (_policy.FILE, rx.pattern)}
                               for rx in policy.deny_bash_patterns],
        "ask": list(policy.ask),
        "own_tool_prefix": _policy.OWN_TOOL_PREFIX,
        "names": dict(SDK_NAMES),
        "prefixes": {OWN_PREFIX: _policy.OWN_TOOL_PREFIX},
        "shell_env": dict(shell_env or {}),
    }


def shell_env(home, agent, environ=None):
    """What the model's shell gets over the server's environment: HOME the
    cousin's home, SHELL_BLANK empty, and SHELL_PASS plus `[agent]
    shell_env` (a list of variable names) from `environ` where set. A name
    the runner owns or a credential is refused, naming the key (exit 2)."""
    environ = os.environ if environ is None else environ
    named = agent.get("shell_env", [])
    if not isinstance(named, list) or not all(isinstance(n, str) for n in named):
        raise RunnerError("cousin.toml [agent] shell_env must be a list of variable names")
    for name in named:
        if (not _ENV_NAME.match(name) or name == "HOME" or name.startswith(("XDG_", "OPENCODE_"))
                or accounts.credential_name(name)):
            raise RunnerError("cousin.toml [agent] shell_env names %r: HOME, XDG_*, OPENCODE_*"
                              " and credentials are the runner's to set, never passed to the"
                              " model's shell" % name)
    env = {"HOME": str(home)}
    for name in SHELL_PASS + tuple(named):
        if name in environ:
            env[name] = environ[name]
    env.update({name: "" for name in SHELL_BLANK})
    return env


def endpoint_limit(account):
    """The endpoint model's `limit` block, or None when the account names no
    `endpoint_context`: the output defaults to a quarter of the context,
    capped at opencode's own maximum (opencode compacts at context minus
    output, so an output near the context would compact every turn)."""
    context = getattr(account, "endpoint_context", None)
    if not context:
        return None
    output = getattr(account, "endpoint_output", None) or min(context // 4, OPENCODE_OUTPUT_MAX)
    return {"context": context, "output": output}


def _agent_table(home):
    try:
        return tomllib.loads((Path(home) / "cousin.toml").read_text()).get("agent") or {}
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def opencode_bin(agent, environ=None):
    """[agent] opencode_bin, else COUSIN_OPENCODE_BIN (the image sets it),
    else `opencode` on PATH."""
    environ = os.environ if environ is None else environ
    return str(agent.get("opencode_bin") or environ.get("COUSIN_OPENCODE_BIN") or "opencode")


def split_model(model, what="model"):
    """(provider, model id) of `<provider>/<model>`; RunnerError otherwise."""
    provider, _, name = str(model or "").partition("/")
    if not provider or not name or any(c.isspace() for c in str(model)):
        raise RunnerError("cousin.toml [agent] %s must be \"<provider>/<model>\", got %r"
                          % (what, model))
    return provider, name


def plugin_entry(plugin=PLUGIN):
    """The plugin pack as a config `plugin` entry: a file URL (a bare name
    would be taken for an npm package and installed at boot)."""
    return Path(plugin).as_uri()


def local_mcp(server):
    """A stdio server ({command, args, env}) as an opencode `local` MCP entry."""
    return {"type": "local", "command": [server["command"]] + list(server.get("args") or []),
            "environment": dict(server.get("env") or {}), "enabled": True}


def render_config(account, *, model, small_model, mcp_url, mcp_token, plugin=PLUGIN,
                  servers=None):
    """The config the runner renders. opencode merges other sources
    over it (its global config dir, `$HOME/.opencode`, a managed config):
    `foreign_config_sources` refuses those the runner can see, and the
    effective config is checked after the server starts
    (`check_effective_config`). opencode's own hosted service (Zen, its
    free models among them) stays disabled unless the account names
    `opencode` in its providers: nothing reaches it by default. `servers`
    ({name: {command, args, env}}: the cousin's plugin MCP servers) are
    added beside `cousin` as `local` entries."""
    config = {
        "$schema": SCHEMA,
        "model": model,
        "small_model": small_model,
        "disabled_providers": [] if "opencode" in account.providers else ["opencode"],
        "autoupdate": False,
        "share": "disabled",
        "permission": {"*": "allow"},           # never an interactive ask
        "plugin": [plugin_entry(plugin)],
        "mcp": {"cousin": {"type": "remote", "url": mcp_url,
                           "headers": {"Authorization": "Bearer %s" % mcp_token}}},
    }
    for name, server in (servers or {}).items():
        config["mcp"][name] = local_mcp(server)
    if account.providers:
        config["enabled_providers"] = list(account.providers)
    if account.endpoint:
        model = {"name": account.endpoint_model}
        limit = endpoint_limit(account)
        if limit:
            model["limit"] = limit
        config["provider"] = {ENDPOINT_PROVIDER: {
            "npm": "@ai-sdk/openai-compatible", "name": "local endpoint (%s)" % account.name,
            "options": {"baseURL": account.endpoint},
            "models": {account.endpoint_model: model}}}
    return config


def server_env(account, root, *, home, environ=None, models_fetch=True):
    """The server's environment: the allowlist, the account's HOME and XDG
    directories, and the cousin's own two variables (the model's shell runs
    the `cousin-*` commands). OpencodeServer adds the password, the config
    path and the fixed switches."""
    environ = os.environ if environ is None else environ
    env = {k: v for k, v in environ.items() if k in ENV_ALLOW or k.startswith("LC_")}
    env.update(accounts.account_env(account, root))
    env["COUSIN_HOME"] = str(home)
    env["FRAMEWORK_ROOT"] = str(root)
    env[POLICY_ENV] = str(Path(account.data_dir) / POLICY_NAME)      # the plugin's
    if not models_fetch:
        env["OPENCODE_DISABLE_MODELS_FETCH"] = "1"
    return env


def check_model(account, model, what="model"):
    """`model` is one this opencode account can run: not named for Claude,
    "<provider>/<model>", on the account's endpoint or one of
    its providers. RunnerError naming the key. The runner checks both of its
    models at construction; the console checks a model change."""
    try:
        accounts.refuse_claude_name("cousin.toml [agent] %s" % what, model)
    except accounts.AccountsError as err:
        raise RunnerError(str(err))
    provider, _ = split_model(model, what)
    if account.endpoint and provider != ENDPOINT_PROVIDER:
        raise RunnerError("cousin.toml [agent] %s %r: account %s is a local endpoint,"
                          " rendered as provider %r: name \"%s/%s\""
                          % (what, model, account.name, ENDPOINT_PROVIDER,
                             ENDPOINT_PROVIDER, account.endpoint_model))
    if account.providers and provider not in account.providers:
        raise RunnerError("cousin.toml [agent] %s %r: account %s holds keys for %s only"
                          % (what, model, account.name, ", ".join(account.providers)))


def foreign_config_sources(account, root, home):
    """The config sources opencode would merge or load over the rendered
    file: anything in its global config dir but CONFIG_DIR_OWN, and
    `<data_dir>/.opencode` (the account's HOME; measured merged on 1.18.31),
    and `<home>/.opencode` (the server's cwd; measured NOT merged under
    OPENCODE_DISABLE_PROJECT_CONFIG, refused all the same: a leftover from
    running opencode by hand). A list of paths, sorted."""
    env = accounts.account_env(account, root)
    config_dir = Path(env["XDG_CONFIG_HOME"]) / "opencode"
    try:
        names = os.listdir(config_dir)
    except (FileNotFoundError, NotADirectoryError):
        names = []
    except OSError:                 # unreadable (chmod 000): what is in it cannot be known
        return [config_dir]
    found = [config_dir / n for n in names if n not in CONFIG_DIR_OWN]
    lock = config_dir / "package-lock.json"
    if "package-lock.json" in names and _lock_names_others(lock):
        found.append(lock)
    found += [p for p in (Path(env["HOME"]) / ".opencode", Path(home) / ".opencode")
              if os.path.lexists(p)]
    return sorted(found)


def _lock_names_others(path):
    """The lock is not the seed's: unreadable, or its root names a package
    other than PLUGIN_DEPENDENCY (names: `foreign_config_sources` says which)."""
    try:
        lock = json.loads(path.read_text())
        deps = lock["packages"][""].get("dependencies") or {}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return "unreadable"
    others = sorted(set(deps) - {PLUGIN_DEPENDENCY}) if isinstance(deps, dict) else ["?"]
    return ", ".join(others)


def check_effective_config(config, env, *, account, model, small_model, plugin=PLUGIN,
                           servers=()):
    """opencode's effective config (GET /config, every source merged) holds
    what the runner rendered and nothing more: the bridge guard over all of
    it, exactly the policy plugin, exactly the cousin MCP server and the
    plugin servers the runner rendered (`servers`, their names), providers
    within the account's, the named models. RunnerError naming what, never
    a value (a value may be a key)."""
    def refuse(what):
        raise RunnerError("opencode's effective config %s: another config source was merged"
                          " over the one the runner rendered, so no turn runs" % what)
    try:
        opencode_guard.refuse_bridge(config, env)
    except opencode_guard.BridgeRefused as err:
        refuse("fails the bridge guard (%s)" % err.reason)
    names = [p if isinstance(p, str) else (p[0] if isinstance(p, list) and p else None)
             for p in config.get("plugin") or []]
    if names != [plugin_entry(plugin)]:
        refuse("lists plugins %s, not only the policy plugin" % names)
    mcp = sorted(config.get("mcp") or {})
    if mcp != sorted({"cousin", *servers}):
        refuse("lists MCP servers %s, not only the cousin's%s"
               % (mcp, " and its plugins' %s" % sorted(servers) if servers else ""))
    allowed = set(account.providers or ()) | ({ENDPOINT_PROVIDER} if account.endpoint else set())
    extra = sorted(set(config.get("provider") or {}) - allowed)
    if extra:
        refuse("configures providers %s beyond the account's %s" % (extra, sorted(allowed)))
    for key, want in (("model", model), ("small_model", small_model)):
        if config.get(key) != want:
            refuse("names %s %r, not the %r cousin.toml names" % (key, config.get(key), want))


def seed_plugin_dependency(config_dir):
    """Make opencode's own check (`Npm.install` in 1.18.31: skip a config
    dir that has `node_modules` and a package-lock.json whose root lists
    every dependency) find PLUGIN_DEPENDENCY installed. An existing lock is
    merged, never replaced; one that already names it is left alone. (The
    runner refuses a lock whose root names any other package before it
    seeds: foreign_config_sources.)"""
    config_dir = Path(config_dir)
    (config_dir / "node_modules").mkdir(parents=True, exist_ok=True, mode=0o700)
    path = config_dir / "package-lock.json"
    try:
        lock = json.loads(path.read_text())
    except (OSError, ValueError):
        lock = None
    lock = lock if isinstance(lock, dict) else {}
    packages = lock.get("packages") if isinstance(lock.get("packages"), dict) else {}
    root = packages.get("") if isinstance(packages.get(""), dict) else {}
    deps = root.get("dependencies") if isinstance(root.get("dependencies"), dict) else {}
    if PLUGIN_DEPENDENCY in deps and path.exists():
        return
    lock["packages"], packages[""], root["dependencies"] = packages, root, deps
    deps[PLUGIN_DEPENDENCY] = "*"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(lock, indent=2))
    tmp.replace(path)


def classify(error):
    """(kind, detail) of a session.error: "auth" (an APIError 401/403,
    a ProviderAuthError), "aborted" (MessageAbortedError) or "failed"."""
    error = error if isinstance(error, dict) else {}
    name = str(error.get("name") or "UnknownError")
    data = error.get("data") if isinstance(error.get("data"), dict) else {}
    detail = ("%s: %s" % (name, data.get("message") or "")).strip()[:300]
    if name == "ProviderAuthError" or (name == "APIError" and data.get("statusCode") in (401, 403)):
        status = data.get("statusCode")
        return "auth", ("HTTP %s %s" % (status, data.get("message") or "") if status else detail)[:300]
    if name == "MessageAbortedError":
        return "aborted", detail
    return "failed", detail


def usage_of(messages):
    """usage.record's shape (the SDK's ResultMessage keys) from opencode's
    per-message token counts, summed over `messages`. opencode splits the
    provider's figures: `input` is the uncached input, `output` leaves out
    `reasoning`, `cache.write` is the cache creation; the reasoning goes
    back into output_tokens, as the provider bills it."""
    out = dict.fromkeys(usage.USAGE_KEYS, 0)

    def n(value):
        return int(value) if isinstance(value, (int, float)) \
            and not isinstance(value, bool) and value > 0 else 0
    for tokens in messages:
        cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
        out["input_tokens"] += n(tokens.get("input"))
        out["output_tokens"] += n(tokens.get("output")) + n(tokens.get("reasoning"))
        out["cache_read_input_tokens"] += n(cache.get("read"))
        out["cache_creation_input_tokens"] += n(cache.get("write"))
    return out


def _thinking_payload(text):
    payload = {"length": len(text), "text": text[:THINKING_CHARS]}
    if len(text) > THINKING_CHARS:
        payload["truncated"] = True
    return payload


def _default_server_factory(*, argv0, cwd, env, config_path, timeout):
    return OpencodeServer(argv0, cwd=cwd, env=env, config_path=config_path, timeout=timeout)


class _Sent:
    """One prompt sent into the run: its row (None for the runner's own),
    its text, whether opencode announced it (`echoed`, with its user
    message id) and whether the model started answering it."""
    __slots__ = ("row", "text", "echoed", "message_id", "started", "closed")

    def __init__(self, row, text):
        self.row, self.text = row, text
        self.echoed, self.message_id, self.started, self.closed = False, None, False, False


class _Run:
    """What one runner turn has seen: the prompts it sent, the messages
    that answer them, and how it ended."""

    def __init__(self):
        self.sent = []
        self.user_ids = {}          # user message id -> _Sent
        self.assistant_ids = set()  # assistant messages answering this turn's prompts
        self.costs = {}
        self.tokens = None          # the LAST answer's tokens: the context's size
        self.spent = {}             # assistant message id -> its tokens, the newest copy
        self.recorded = (dict.fromkeys(usage.USAGE_KEYS, 0), 0.0)   # at the last result
        self.error = None           # (kind, detail) seen after an echo
        self.pending_error = None   # seen before any echo: stale, or a prompt never stored
        self.pending_at = None
        self.emitted = set()
        self.open_parts = {}        # part id -> [kind, text]: streaming, not yet complete
        self.last_event = time.monotonic()
        self.over = False
        self.results = 0
        self.fold = True            # the handoff turn folds nothing
        self.quiet = False          # nor does it write a result: no inbox row is its
        self.deadline = None

    def unclosed(self):
        return [s for s in self.sent if not s.closed]


class OpencodeRunner:
    kind = "opencode"     # what runner/status.py reports (the `runner` event)
    # The contract items this runner DECLARES unsupported, and those a
    # plugin meets; read at class level by runner/contract_table.py
    UNSUPPORTED = ()           # measured: none (midturn_fold folds)
    PLUGIN_ITEMS = ()          # the plugin pack is the policy veto, not a contract item
    takes_interrupts = True
    poll_s = 0.2
    # how long a session.error with no announced prompt waits for one
    # (a prompt opencode never stored has no idle, as with "no providers")
    error_grace_s = 2.0
    # how long a stopping runner waits for its aborted turn's idle
    stop_grace_s = 2.0
    reader_backoff_s = 0.5
    # how often a runner waiting for a login reads its auth.json mark and
    # the login file between two looks
    login_poll_s = 1.0
    mcp_timeout_s = 10.0
    connect_timeout_s = 10.0
    plugin_timeout_s = PLUGIN_TIMEOUT_S

    def __init__(self, home, *, server_factory=None, account=None, model=None, small_model=None,
                 policy=None, registry=None, idle_timeout_s=600.0,
                 health_timeout_s=HEALTH_TIMEOUT_S, handoff_deadline_s=None, environ=None,
                 recorder=None):
        self.home = Path(os.path.abspath(home))
        from cousin_lib.runner.main import export_environment, root_for
        export_environment(self.home, overwrite=False)
        self.root = root_for(self.home)
        agent = _agent_table(self.home)
        try:
            if account is None:
                account = accounts.for_cousin(self.home, self.root)
            accounts.check_lane(account, LANE)
        except accounts.AccountsError as err:
            raise RunnerError(str(err))
        self.account = account
        # the model is named, never defaulted
        self.model = model or agent.get("model")
        if not self.model:
            raise RunnerError("%s/cousin.toml [agent] model is required for runner = \"opencode\""
                              " (\"<provider>/<model>\"): opencode would otherwise pick a model"
                              " from whatever provider it can reach" % self.home)
        self.small_model = small_model or agent.get("small_model") or self.model
        self.binary = opencode_bin(agent, environ)
        self.models_fetch = agent.get("opencode_models_fetch", True) is not False
        self._environ = environ
        self.shell_env = shell_env(self.home, agent, environ)
        self.server_factory = server_factory or _default_server_factory
        self.idle_timeout_s = float(idle_timeout_s)
        self.health_timeout_s = float(health_timeout_s)
        # The plugin MCP servers (cousin_lib/plugins.py), read once: an
        # edit lands at the next start, as on the sdk kind.
        from cousin_lib.runner import mcp_config
        self._plugin_mcp = mcp_config.add_plugins(mcp_config.Loaded(False), self.home,
                                                  self.root, environ=environ)
        self._plugin_mcp_said = False
        # the bridge guard at construction (runner_for, exit 2), on the
        # config as it will be rendered and the environment it will get
        self._render(mcp_url="http://127.0.0.1:0/mcp", mcp_token="-")
        self._check_models()
        self._refuse_foreign_config()
        self.session_id = "opencode-" + uuid.uuid4().hex[:8]    # the stream's, the claimant
        self.opencode_session = None                             # the server's session id
        self.inbox = Inbox(self.home)
        self.stream = EventStream(self.home, self.session_id)
        self.machine = StateMachine(on_change=self._on_state)
        self.turn = Turn()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self.policy = policy if policy is not None else Policy.load(self.home)
        self.registry = registry
        slug, name = self._identity()
        self.tool_context = tools.ToolContext(home=self.home, slug=slug, name=name,
                                              root=self.root, turn=self.turn,
                                              policy=self.policy, stream=self.stream,
                                              registry=registry)
        # The rollover: the handoff tool, reached over MCP, hands its
        # summary to the box; pressure is read after every good turn.
        self.handoff_deadline_s = float(handoff_deadline_s or _rollover.HANDOFF_DEADLINE_S)
        self.rollover_at_percent = float(agent.get("rollover_at_percent",
                                                   _rollover.ROLLOVER_AT_PERCENT))
        self.handoff_box = _rollover.HandoffBox()
        self.hysteresis = _rollover.Hysteresis()
        self.tool_context.on_handoff = self.handoff_box.set
        # the SDK lane's recording library, fed hook payloads from SSE
        self.recorder = recorder or (lambda payload: recording.handle(
            payload, self.home, self.root, slug=slug))
        self._policy_nonce = None
        self._limit = None            # (model, limit.context or None), read once per start
        # usage.record's client: this runner's own running cost, so a new
        # opencode server or session never resets what it diffs against
        self._usage_client = uuid.uuid4().hex
        self._usage_spent = 0.0
        self.fatal = None
        self._server = self._client = self._reader = self._mcp = None
        self._reader_stop = threading.Event()
        self._events = queue.Queue()
        self._torn = False
        self._teardown_lock = threading.Lock()
        self._system = None
        self._run = None
        self._roles = {}
        self._connects = 0
        self._gap = False
        self._live = False
        self._turn_seq = 0
        self._interrupt_asked = None
        self._interrupt_requested = False
        self._fallback_said = False
        self._login_blocked = False
        self._login_attempt = 0
        self._login_mark = None
        self._login_file_seen = False
        # a file left by an earlier runner clears on the first good result too
        self._restore_pending = auth.read_login_required(self.home) is not None
        self._account_fd = None
        self._server_env = None

    # -- construction helpers ------------------------------------------------
    def _identity(self):
        from cousin_lib.config import CousinConfig
        try:
            cfg = CousinConfig.load(self.home)
            return cfg.slug, cfg.name
        except Exception:  # noqa: BLE001 - a thin toml still runs; the dir names it
            return self.home.name, self.home.name.capitalize()

    def _render(self, *, mcp_url, mcp_token):
        """(config, env), refused when either names the bridge."""
        config = render_config(self.account, model=self.model, small_model=self.small_model,
                               mcp_url=mcp_url, mcp_token=mcp_token,
                               servers=self._plugin_servers())
        env = server_env(self.account, self.root, home=self.home, environ=self._environ,
                         models_fetch=self.models_fetch)
        try:
            opencode_guard.refuse_bridge(config, env)
        except opencode_guard.BridgeRefused as err:
            raise RunnerError(err.reason)
        return config, env

    def _plugin_servers(self):
        """{name: {command, args, env}} of the plugin servers to render."""
        return {row["name"]: {"command": cfg["command"], "args": cfg.get("args", []),
                              "env": cfg.get("env", {})}
                for row in self._plugin_mcp.listed
                for cfg in [self._plugin_mcp.servers[row["name"]]]}

    def _say_plugin_mcp(self):
        """The `mcp_config` event, once per runner, when a plugin was added
        or skipped (the sdk kind's event, `.mcp.json` never read here)."""
        if self._plugin_mcp.plugins and not self._plugin_mcp_said:
            self._plugin_mcp_said = True
            self.stream.append("mcp_config", dict(self._plugin_mcp.event(), file=None))

    def _refuse_foreign_config(self):
        """A config source opencode would merge over the
        rendered one refuses the start, named (the model could have written
        it: its shell's HOME is the account's data dir)."""
        found = foreign_config_sources(self.account, self.root, self.home)
        if found:
            named = []
            for p in found:
                others = _lock_names_others(p) if p.name == "package-lock.json" else ""
                named.append("%s (%s)" % (p, others if others == "unreadable"
                                          else "it names " + others) if others else str(p))
            raise RunnerError("%s would be merged into opencode's config over the one the"
                              " runner renders (a plugin, a tool, a provider or a model could"
                              " come back that way): remove %s"
                              % (", ".join(named), "it" if len(found) == 1 else "them"))

    def _check_models(self):
        """Both models well formed, on a provider this account reaches."""
        for what, model in (("model", self.model), ("small_model", self.small_model)):
            check_model(self.account, model, what)

    def _on_state(self, old, new, detail):
        self.stream.append("state", {"from": old, "to": new, "detail": detail})

    def _registry_fallback(self, payload):
        if not self._fallback_said:
            self._fallback_said = True
            self.stream.append("policy", payload)

    # -- Runner protocol -------------------------------------------------------
    def start(self):
        if self._thread is not None:
            return
        self._hold_account()
        self._thread = threading.Thread(target=self._main, name="opencode-runner", daemon=True)
        self._thread.start()

    def stop(self, *, timeout=30.0):
        if self.machine.state == "stopped":
            return
        self.interrupt()
        self._stop.set()
        wake.poke(self.home)
        if self._thread is not None:
            self._thread.join(timeout)
        self._teardown()
        self._release_account()
        self.turn.end()
        with self._lock:
            if self.machine.state != "stopped":
                self.machine.to("stopped")

    def state(self):
        return self.machine.state

    def enqueue(self, item):
        if not isinstance(item, Item):
            raise TypeError("enqueue() takes a delivery.Item")
        inbox_id = self.inbox.put(item)
        wake.poke(self.home)
        return Receipt(inbox_id=inbox_id, outcome=QUEUED)

    def interrupt(self):
        """Ask the loop to abort the turn running NOW: the request carries
        the turn's number, so a turn that ends before the loop acts on it
        never passes it to the next one."""
        with self._lock:
            if self.machine.state != "running" or not self._live:
                return False
            self._interrupt_asked = self._turn_seq
        return True

    def rollover(self, reason):
        """End this generation and start the next: one `flip` row
        (coalesced), claimed at the next turn boundary; waits for it."""
        return _rollover.request(self.inbox, self.home, reason, alive=self.worker_alive,
                                 timeout=self.handoff_deadline_s + _rollover.WAIT_SLACK_S)

    def _request_rollover(self, why):
        """Ask without waiting (pressure): the loop claims it next."""
        inbox_id, coalesced = _rollover.put_once(self.inbox, self.home, why)
        self.stream.append("rollover", {"phase": "coalesced" if coalesced else "requested",
                                        "reason": why, "inbox_id": inbox_id})

    def events(self, after=None):
        return self.stream.tail(after=after)

    def unsupported(self):
        return list(self.UNSUPPORTED)

    def plugin_items(self):
        """The contract items a plugin meets (none on this lane); optional in
        the Runner protocol, read by contract_table."""
        return list(self.PLUGIN_ITEMS)

    # -- CLI conveniences, NOT in the Runner protocol ------------------------
    def worker_alive(self):
        return self._thread is not None and self._thread.is_alive()

    def login_required(self):
        return self._login_blocked

    # -- start and teardown --------------------------------------------------
    def _hold_account(self):
        """One opencode account serves one cousin. Its
        data dir holds this start's rendered config (the MCP token), the
        policy file and its acknowledgement, and opencode's own store and
        session, so a second runner on it would overwrite the first's. An
        exclusive flock on the data dir, held until stop (the kernel drops
        it when the process dies); busy is a RunnerError, exit 2 at start,
        naming the cousin that holds it."""
        path = Path(self.account.data_dir)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        except OSError as err:
            raise RunnerError("cannot open the opencode account's data dir %s: %s" % (path, err))
        deadline = time.monotonic() + ACCOUNT_TAKE_S
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() < deadline:
                    time.sleep(0.02)
                    continue
                os.close(fd)
                try:
                    holder = (path / ACCOUNT_HOLDER).read_text().strip() or "another runner"
                except OSError:
                    holder = "another runner"
                raise RunnerError("opencode account %s (%s) is in use by the runner of %s: an"
                                  " opencode account serves one cousin (its data dir holds that"
                                  " cousin's config, MCP token, policy files and opencode's"
                                  " session store); give this cousin its own account in"
                                  " config/accounts.toml" % (self.account.name, path, holder))
        self._account_fd = fd
        tmp = path / (ACCOUNT_HOLDER + ".tmp")
        tmp.write_text(str(self.home) + "\n")
        tmp.replace(path / ACCOUNT_HOLDER)

    def _release_account(self):
        fd, self._account_fd = self._account_fd, None
        if fd is None:
            return
        holder = Path(self.account.data_dir) / ACCOUNT_HOLDER
        try:
            if holder.read_text().strip() == str(self.home):
                holder.unlink()
        except OSError:
            pass
        os.close(fd)

    def _fail_start(self, message):
        """The worker gives up (cousin-runner exits 3): `errored`, `fatal`,
        an `error` event. Queued rows stay queued."""
        self.fatal = message
        with self._lock:
            if self.machine.state in ("idle", "running"):
                self.machine.to("errored", message)
        self.stream.append("error", {"error": message, "fatal": True})

    def _start_mcp(self):
        from cousin_lib.runner.mcp_http import McpHttpServer
        self._mcp = McpHttpServer(self.tool_context, self.registry,
                                  on_fallback=self._registry_fallback)
        for _ in range(5):          # never the bridge's port: the guard would refuse it
            self._mcp.start()
            if not self._mcp.url.endswith(":%d/mcp" % BRIDGE_PORT):
                return
            self._mcp.stop()
        raise RunnerError("the MCP server could not get a port other than %d" % BRIDGE_PORT)

    def _write_config(self, config, name=CONFIG_NAME):
        path = Path(self.account.data_dir) / name
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(config, f, indent=2)
        os.chmod(tmp, 0o600)
        tmp.replace(path)
        return path

    def _write_policy(self):
        """The plugin's policy file for this start, a fresh nonce, and no
        acknowledgement left from an earlier start."""
        ack = Path(self.account.data_dir) / POLICY_ACK
        ack.unlink(missing_ok=True)
        self._policy_nonce = uuid.uuid4().hex
        return self._write_config(render_policy(self.policy, nonce=self._policy_nonce, ack=ack,
                                                shell_env=self.shell_env), POLICY_NAME)

    def _check_plugin_listed(self, env):
        """The veto's first half: opencode's config
        lists the plugin pack. This is also the instance's first request,
        on which opencode bootstraps the instance and loads its plugins, so
        it is bounded by `plugin_timeout_s` (without seed_plugin_dependency
        that bootstrap waited 24-47 s online for opencode's own npm install,
        and the event stream's 10 s connect bound failed first). The listing
        alone proves nothing: see `_await_plugin_ack`."""
        entry = plugin_entry(PLUGIN)
        try:
            effective = self._client.request("GET", "/config",
                                             timeout=self.plugin_timeout_s) or {}
        except OpencodeError as err:
            raise RunnerError("GET /config: %s: the policy plugin cannot be checked, so no turn"
                              " runs" % err)
        listed = effective.get("plugin") or []
        names = [p if isinstance(p, str) else (p[0] if isinstance(p, list) and p else None)
                 for p in listed]
        if entry not in names:
            raise RunnerError("opencode's config does not list the policy plugin %s (it lists %s):"
                              " policy.toml would not apply, so no turn runs"
                              % (entry, names or "none"))
        check_effective_config(effective, env, account=self.account, model=self.model,
                               small_model=self.small_model, servers=self._plugin_servers())

    def _await_plugin_ack(self):
        """The veto's second half: the plugin acknowledged THIS start's
        policy file (its nonce). opencode lists a configured plugin whether
        or not it loaded (measured on 1.18.31: a missing file, a syntax error
        and an init that throws are all listed, and a call they would have
        denied runs), so only the plugin's own word proves it is in force."""
        entry = plugin_entry(PLUGIN)
        path = Path(self.account.data_dir) / POLICY_ACK
        deadline = time.monotonic() + self.plugin_timeout_s
        ack = None
        while True:
            try:
                ack = json.loads(path.read_text())
            except (OSError, ValueError):
                ack = None
            if isinstance(ack, dict) and ack.get("nonce") == self._policy_nonce:
                break
            if time.monotonic() >= deadline or self._stop.is_set():
                raise RunnerError("the policy plugin did not load: opencode lists %s but no"
                                  " acknowledgement of this start's %s arrived within %.0fs:"
                                  " policy.toml would not apply, so no turn runs"
                                  % (entry, POLICY_NAME, self.plugin_timeout_s))
            time.sleep(0.1)
        if ack.get("fatal"):
            raise RunnerError("the policy plugin cannot apply the policy (%s): it would deny"
                              " every call, so no turn runs" % str(ack["fatal"])[:300])
        errors = ack.get("errors") if isinstance(ack.get("errors"), list) else []
        self.stream.append("system", {"subtype": "policy_plugin", "plugin": entry,
                                      "deny_tools": ack.get("deny_tools"),
                                      "deny_bash_patterns": ack.get("deny_bash_patterns"),
                                      "ask": ack.get("ask"), "errors": errors})
        for e in errors:
            e = e if isinstance(e, dict) else {}
            self.stream.append("error", {"error": "%s: deny_bash_patterns %r is not a valid"
                                         " JavaScript RegExp (%s): on opencode every command is"
                                         " denied until it is rewritten"
                                         % (_policy.FILE, e.get("source"), e.get("error"))})

    def _boot(self):
        """MCP server, config, guard, server, event reader, MCP check.
        True when turns may run; otherwise the runner gave up."""
        from cousin_lib.runner import prompt
        try:
            self._start_mcp()
            self._say_plugin_mcp()
            config, env = self._render(mcp_url=self._mcp.url, mcp_token=self._mcp.token)
            self._server_env = env
            self._refuse_foreign_config()
            self._reap_leftover()
            path = self._write_config(config)
            seed_plugin_dependency(Path(env["XDG_CONFIG_HOME"]) / "opencode")
            self._write_policy()
            self._system = prompt.compose_system_prompt(self.home, root=self.root,
                                                        registry=self._mcp.registry,
                                                        tool_name=tool_name,
                                                        runner="the opencode runner",
                                                        other_servers=None)
            if self._stop.is_set():
                return False
            self._server = self.server_factory(argv0=self.binary, cwd=self.home, env=env,
                                               config_path=path, timeout=self.health_timeout_s)
            self._server.start()
            self._record_server()
            if self._stop.is_set():
                return False
            self._client = OpencodeClient(self._server.url, self._server.password)
            self._limit = None
            self._check_plugin_listed(env)
            self._reader = EventReader(self._server.url, self._server.password, self._events.put,
                                       stop_event=self._reader_stop,
                                       backoff=self.reader_backoff_s)
            self._reader.start()
            if not self._reader.connected.wait(self.connect_timeout_s):
                raise RunnerError("no event stream from opencode within %.0fs: %s"
                                  % (self.connect_timeout_s, self._reader.last_error))
            self._check_mcp()
            self._await_plugin_ack()
            self._refuse_foreign_config()         # written while the server started
        except Exception as exc:  # noqa: BLE001 - a runner that cannot start says why
            if not self._stop.is_set():
                self._fail_start("opencode start: %s: %s" % (type(exc).__name__, exc))
            return False
        return True

    def _forget_server(self):
        """The pidfile goes once this runner's own server is stopped (never
        another's: the file names a pid)."""
        try:
            record = json.loads(self._pidfile().read_text())
        except (OSError, ValueError):
            return
        if isinstance(record, dict) and record.get("pid") == getattr(self._server, "pid", None):
            self._pidfile().unlink(missing_ok=True)

    def _guard_turn(self, row):
        """The start's checks again before every turn (the rollover's
        handoff and digest turns included): auth.json (preflight), the
        config sources opencode would merge, and the effective config
        (PATCH /global/config reloads plugins and providers live, measured
        on 1.18.31). It catches a change still in place when a turn starts;
        it bounds nothing the model does from its shell (a change made and
        undone inside a turn, a detached process prompting the server
        between turns). The container is the containment. A failure is
        tried once more after TURN_GUARD_RETRY_S (a timeout, a read caught
        mid-write), then puts the row back and the runner gives up; any
        exception is a failure, never a dead worker holding a claim."""
        err = None
        for attempt in range(2):
            try:
                accounts.preflight(self.account, self.root)
                self._refuse_foreign_config()
                effective = self._client.request("GET", "/config",
                                                 timeout=self.plugin_timeout_s) or {}
                check_effective_config(effective, self._server_env, account=self.account,
                                       model=self.model, small_model=self.small_model,
                                       servers=self._plugin_servers())
                return True
            except Exception as exc:  # noqa: BLE001 - a failed check is a refusal, never a crash
                err = exc
                if attempt == 0 and not self._stop.wait(TURN_GUARD_RETRY_S):
                    continue
                break
        try:
            self.inbox.requeue(row["id"])
        except Exception:  # noqa: BLE001 - the stale-claim sweep at the next start
            pass
        self._fail_start("opencode turn guard: %s" % (err if isinstance(
            err, (accounts.AccountsError, RunnerError, OpencodeError))
            else "%s: %s" % (type(err).__name__, err)))
        return False

    def _pidfile(self):
        return Path(self.account.data_dir) / opencode_http.PIDFILE

    def _reap_leftover(self):
        """Kill the server an earlier runner on this
        account left behind (it was SIGKILLed before its teardown, or its
        death signal was not delivered), so two servers never share the
        account's store and session."""
        pids = opencode_http.reap_leftover(self._pidfile())
        if pids:
            self.stream.append("system", {"subtype": "opencode_leftover", "pids": pids,
                                          "detail": "killed what an earlier runner's opencode"
                                                    " serve left running"})

    def _record_server(self):
        pid = getattr(self._server, "pid", None)
        if pid:
            opencode_http.write_pidfile(self._pidfile(), pid,
                                        marker=getattr(self._server, "marker", None))

    def _check_mcp(self):
        """opencode must report the runner's MCP server `connected` before
        the first prompt: without it the model has no framework
        tools. `needs_auth` is a token the server refused: no retry heals it."""
        deadline = time.monotonic() + self.mcp_timeout_s
        status = None
        while True:
            try:
                entry = (self._client.mcp_status() or {}).get("cousin") or {}
                status = entry.get("status")
            except OpencodeError as err:
                entry, status = {}, "unreadable: %s" % err
            if status == "connected":
                self.stream.append("system", {"subtype": "mcp", "status": status})
                return
            if status in ("needs_auth", "needs_client_registration") \
                    or time.monotonic() >= deadline or self._stop.is_set():
                raise RunnerError("opencode reports the cousin MCP server %s%s: the model would"
                                  " have no framework tools, so no turn runs"
                                  % (status or "absent",
                                     " (%s)" % entry["error"] if entry.get("error") else ""))
            time.sleep(0.2)

    def _teardown(self):
        """Reader, server, MCP server; bounded, idempotent, from any thread."""
        with self._teardown_lock:
            if self._torn:
                return
            self._torn = True
        self._reader_stop.set()
        if self._server is not None:
            try:
                self._server.stop(timeout=5)
            except Exception as exc:  # noqa: BLE001 - a dying child must not mask the stop
                self.stream.append("error", {"error": "stopping opencode: %s: %s"
                                             % (type(exc).__name__, exc)})
            self._forget_server()
        if self._mcp is not None:
            self._mcp.stop()
        if self._reader is not None:
            self._reader.join(2.0)

    # -- the session -----------------------------------------------------------
    def _session_path(self):
        return self.home / "data" / "runner-session.json"

    def _read_session_file(self):
        try:
            d = json.loads(self._session_path().read_text())
        except (OSError, ValueError):
            return {}
        return d if isinstance(d, dict) else {}

    def _save_session(self, session_id):
        path = self._session_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"session_id": session_id, "lane": LANE,
                                   "generation": boot.read_generation(self.home),
                                   "updated": time.time()}))
        tmp.replace(path)

    def _has_state(self):
        return (self.home / "STATUS.md").exists() or (self.home / "data" / "handoff.md").exists()

    def _open_session(self):
        """Resume the session on file when it is an opencode one opencode
        still holds; otherwise a new session, with the state digest as its
        first message when there is state to carry (SdkRunner's rule)."""
        on_file = self._read_session_file()
        saved = on_file.get("session_id") if on_file.get("lane") == LANE else None
        if saved:
            try:
                self._client.session(saved)      # held or not; never the whole history
            except OpencodeError as err:
                self.stream.append("system", {"subtype": "resume_failed", "session_id": saved,
                                              "error": str(err)})
            else:
                self.opencode_session = saved
                self.stream.append("system", {"subtype": "resumed", "session_id": saved})
                return
        self._new_session()
        self._start_fresh(with_digest=bool(on_file.get("session_id")) or self._has_state())

    def _new_session(self, generation=None):
        if generation is None:
            generation = boot.read_generation(self.home)
        created = self._client.create_session("%s generation %d" % (self.tool_context.slug,
                                                                    generation))
        self.opencode_session = created["id"]
        try:
            self._save_session(self.opencode_session)
        except Exception as exc:  # noqa: BLE001 - the file must never fail a start
            self.stream.append("error", {"error": "runner-session.json: %s: %s"
                                         % (type(exc).__name__, exc)})
        self.stream.append("system", {"subtype": "session", "session_id": self.opencode_session})

    def _start_fresh(self, *, with_digest):
        # a new session is a generation start, moved or not (SdkRunner._start_fresh)
        try:
            boot.mark_generation_start(self.home)
        except Exception as exc:  # noqa: BLE001 - a record; the session runs on
            self.stream.append("error", {"error": "generation start: %s: %s"
                                         % (type(exc).__name__, exc)})
        try:
            session.run_phase(self.home, "start")
        except Exception as exc:  # noqa: BLE001 - the session runs on without its hooks
            self.stream.append("error", {"error": "start hooks: %s: %s"
                                         % (type(exc).__name__, exc)})
        self.stream.append("system", {"subtype": "fresh", "digest": with_digest})
        if not with_digest or self._stop.is_set():
            return
        digest_id = self._put_digest(boot.read_generation(self.home))
        first = self.inbox.claim_id(digest_id, claimant=self.session_id) if digest_id else None
        if first is not None:
            try:
                self._turn(first)
            except Exception as exc:  # noqa: BLE001 - the loop's own guard, outside the loop
                self._recover_from(exc)

    def _put_digest(self, generation):
        """The digest row's id; a degraded digest when it cannot be built."""
        from cousin_lib.runner import prompt
        slug = self.tool_context.slug
        try:
            try:
                text = prompt.state_digest(self.home, root=self.root, slug=slug,
                                           generation=generation)["text"]
            except Exception as exc:  # noqa: BLE001 - degraded, never none
                text = _rollover.degraded_digest(self.home, slug=slug, generation=generation,
                                                 error="%s: %s" % (type(exc).__name__, exc))
            return self.inbox.put(Item(thread_id="system", source="boot", body=text,
                                       sender="runner"))
        except Exception as exc:  # noqa: BLE001 - the session runs on without one
            self.stream.append("error", {"error": "the digest row could not be stored: %s: %s"
                                         % (type(exc).__name__, exc)})
            return None

    # -- the loop -------------------------------------------------------------
    def _main(self):
        try:
            if not self._boot():
                return
            try:
                self._open_session()
            except Exception as exc:  # noqa: BLE001 - no session, no turn
                self._fail_start("opencode session: %s: %s" % (type(exc).__name__, exc))
                return
            with wake.listen(self.home, self._wake_error) as listener:
                while not self._stop.is_set() and self.fatal is None:
                    self._pump()
                    if self._server_gone():
                        break
                    if self._login_blocked:
                        self._await_login()
                        continue
                    try:
                        rows = self.inbox.claim(limit=1, claimant=self.session_id)
                    except Exception as exc:  # noqa: BLE001 - a store failure is never silence
                        self.stream.append("error", {"error": "inbox: %s: %s"
                                                     % (type(exc).__name__, exc)})
                        time.sleep(0.2)
                        continue
                    if not rows:
                        listener.wait(self.poll_s)
                        continue
                    row = rows[0]
                    if row["source"] == INTERRUPT:
                        self.inbox.done(row["id"], FAILED, NO_TURN)
                        continue
                    if not self._guard_turn(row):
                        break
                    try:
                        if row["source"] == "flip":
                            self._rollover_row(row)
                        else:
                            self._turn(row)
                    except Exception as exc:  # noqa: BLE001 - never a silent death
                        self._recover_from(exc)
        finally:
            self._teardown()

    def _wake_error(self, message):
        self.stream.append("error", {"error": message})

    def _server_gone(self):
        if self._server is None or self._server.alive():
            return False
        output = getattr(self._server, "output", lambda: "")() or ""
        self._fail_start("opencode serve exited: %s" % output[-300:])
        return True

    def _recover_from(self, exc):
        message = "%s: %s" % (type(exc).__name__, exc)
        with self._lock:
            if self.machine.state in ("idle", "running", "rolling_over"):
                self.machine.to("errored", message)
        self.turn.end()
        self.stream.append("error", {"error": message})
        with self._lock:
            self._live = False
            self._run = None
            if self.machine.state == "errored" and not self._login_blocked:
                self.machine.to("idle", "recovered")

    # -- the rollover -----------------------------------------------------------
    def _rollover_row(self, row):
        """At a turn boundary: the handoff (asked in the old session), end
        hooks, archive, a NEW opencode session, THEN the generation, start
        hooks and the digest as the new session's first message. Before the
        new session exists a failure keeps the old one (errored -> idle, the
        row failed); after it nothing fails the rollover (SdkRunner's
        rules). The old session stays in opencode's store."""
        reason = row["body"] or "rollover"
        old = self.opencode_session
        generation = boot.read_generation(self.home)
        with self._lock:
            if self.machine.state != "idle":     # stop() won the race: the row waits
                self.inbox.requeue(row["id"])
                return
            self.machine.to("rolling_over", reason.splitlines()[0][:120])
        self.stream.append("rollover", {"phase": "start", "reason": reason, "session_id": old})
        try:
            handoff = self._ask_handoff(reason)
            if handoff == "stopped":
                self.inbox.requeue(row["id"])      # the next start finishes this rollover
                self.stream.append("rollover", {"phase": "requeued", "reason": reason})
                return
            if handoff == "login_required":
                self.inbox.requeue(row["id"])      # first after the fix (priority 0)
                self.stream.append("rollover", {"phase": "postponed", "reason": reason,
                                                "why": auth.LOGIN})
                return
            session.run_phase(self.home, "end")
            _rollover.archive_generation(self.home, generation)
            self._new_session(generation + 1)
        except Exception as exc:  # noqa: BLE001 - a failed rollover must not wedge the runner
            message = "%s: %s" % (type(exc).__name__, exc)
            self.hysteresis.rolled_over()          # no re-request every turn over the threshold
            self.opencode_session = old
            with self._lock:
                if self.machine.state == "rolling_over":
                    self.machine.to("errored", "rollover failed: " + message)
            detail = {"reason": reason, "error": message,
                      "generation": boot.read_generation(self.home),
                      "old_session": old, "new_session": None}
            self.inbox.done(row["id"], FAILED, json.dumps(detail))
            self._close_duplicates(row, FAILED, detail)
            self.stream.append("rollover", dict(detail, phase="failed"))
            with self._lock:
                if self.machine.state == "errored" and not self._login_blocked:
                    self.machine.to("idle", "recovered")
            return
        # the point of no return: degrade, never fail
        problems = []
        try:
            generation = boot.bump_generation(self.home)
        except Exception as exc:  # noqa: BLE001 - named in the detail
            problems.append("generation not moved: %s: %s" % (type(exc).__name__, exc))
        self.hysteresis.rolled_over()
        try:
            session.run_phase(self.home, "start")
        except Exception as exc:  # noqa: BLE001 - named in the detail
            problems.append("start hooks: %s: %s" % (type(exc).__name__, exc))
        digest_id = self._put_digest(generation)
        detail = {"reason": reason, "handoff": handoff, "generation": generation,
                  "old_session": old, "new_session": self.opencode_session,
                  "digest": "built" if digest_id else "none"}
        if problems:
            detail["problems"] = problems
        with self._lock:
            if self.machine.state == "rolling_over":
                self.machine.to("idle", "rolled over")
        self.inbox.done(row["id"], DELIVERED, json.dumps(detail))
        self._close_duplicates(row, DELIVERED, detail)
        self.stream.append("rollover", dict(detail, phase="done"))
        if digest_id is None or self._stop.is_set():
            return          # a stored digest row stays queued (durable): the next start runs it
        first = self.inbox.claim_id(digest_id, claimant=self.session_id)
        if first is not None and self._guard_turn(first):
            self._turn(first)

    def _ask_handoff(self, reason):
        """One prompt into the dying session asking for the handoff; read to
        its idle, bounded by the handoff deadline. 'clean' when the handoff
        tool answered, 'emergency' when not (the file is then written from
        the session's tail), 'stopped', or 'login_required'."""
        self.handoff_box.summary = None
        run = _Run()
        run.fold, run.quiet = False, True
        run.deadline = time.monotonic() + self.handoff_deadline_s
        with self._lock:
            self._turn_seq += 1
            self._interrupt_requested = False
            self._roles = {}
            self._run = run
            self._live = True
        try:
            self._send(run, None, _rollover.handoff_request_text(reason))
            self._drive(run)
        except Exception as exc:  # noqa: BLE001 - an emergency handoff, never a wedge
            if run.error is None:
                run.error = ("failed", "%s: %s" % (type(exc).__name__, exc))
        finally:
            with self._lock:
                self._live = False
                self._run = None
        if run.error is not None and run.error[0] == "auth" \
                and self.handoff_box.summary is None:
            return "login_required"     # the login is the problem, not the model: postpone
        if self._stop.is_set():
            return "stopped"
        if self.handoff_box.summary is not None:
            self.stream.append("rollover", {"phase": "handoff", "handoff": "clean"})
            return "clean"
        why = run.error[1] if run.error else "the model finished its turn without calling handoff"
        _rollover.write_emergency_handoff(self.home, name=self.tool_context.name, reason=why,
                                          tail=self._session_tail())
        self.stream.append("rollover", {"phase": "handoff", "handoff": "emergency", "why": why})
        return "emergency"

    def _session_tail(self, chars=2000):
        """The session's last words from opencode's own store: the emergency
        handoff's observed activity."""
        try:
            messages = self._client.messages(self.opencode_session) or []
        except OpencodeError as err:
            return "(the session could not be read: %s)" % err
        lines = []
        for m in messages:
            role = (m.get("info") or {}).get("role") or "?"
            for part in m.get("parts") or []:
                if part.get("type") == "text" and part.get("text"):
                    lines.append("%s: %s" % (role, part["text"]))
        return "\n".join(lines)[-chars:]

    def _close_duplicates(self, row, outcome, detail):
        """A plain duplicate `flip` row gets this rollover's answer; a
        bequest never. The close is guarded on the body read here."""
        for other in self.inbox.open_rows("flip"):
            if other["id"] != row["id"] and other["state"] == "queued" \
                    and not _rollover.is_bequest(other["body"]) \
                    and self.inbox.done_if_queued(other["id"], outcome,
                                                  json.dumps(dict(detail, coalesced_into=row["id"])),
                                                  body=other["body"]):
                self.stream.append("rollover", {"phase": "coalesced", "inbox_id": other["id"],
                                                "into": row["id"]})

    def _context_limit(self):
        """The model's limit.context from GET /config/providers, read once
        per start; None when opencode reports none (pressure is then off,
        said once). The providers' keys in that answer never leave here."""
        if self._limit is not None:
            return self._limit[1]
        provider, model = split_model(self.model)
        limit = None
        try:
            for entry in (self._client.request("GET", "/config/providers") or {}).get(
                    "providers") or []:
                if entry.get("id") == provider:
                    limit = ((entry.get("models") or {}).get(model) or {}).get(
                        "limit", {}).get("context")
        except OpencodeError:
            limit = None
        if not isinstance(limit, (int, float)) or limit <= 0:
            limit = None
            self.stream.append("system", {"subtype": "pressure_off", "model": self.model,
                                          "why": "opencode reports no context limit for it"})
        self._limit = (self.model, limit)
        return limit

    def _pressure(self, run):
        """After a good turn: the last answer's tokens against the limit,
        held back by the hysteresis after a rollover."""
        try:
            limit = self._context_limit()
            tokens = run.tokens or {}
            if not limit or not tokens:
                return
            cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
            used = sum(float(x or 0) for x in (tokens.get("input"), tokens.get("output"),
                                               cache.get("read"), cache.get("write")))
            measured = {"percentage": 100.0 * used / limit, "totalTokens": used,
                        "maxTokens": limit}
            if self.hysteresis.allow(measured, self.rollover_at_percent) \
                    and _rollover.pressure_due(measured, self.rollover_at_percent):
                self._request_rollover("context pressure %d%%" % int(measured["percentage"]))
        except Exception as exc:  # noqa: BLE001 - the trigger must never fail a turn
            self.stream.append("error", {"error": "rollover trigger: %s: %s"
                                         % (type(exc).__name__, exc)})

    # -- events ----------------------------------------------------------------
    def _pump(self):
        """Handle every event already read, without waiting."""
        while True:
            try:
                event = self._events.get_nowait()
            except queue.Empty:
                return
            self._on_event(event)

    def _on_event(self, ev):
        kind = ev.get("type")
        p = ev.get("properties") if isinstance(ev.get("properties"), dict) else {}
        if kind == "server.connected":
            self._connects += 1
            if self._connects > 1:
                self._gap = True        # a reconnect: events may have been missed
            return
        if kind == "server.heartbeat":
            return
        info = p.get("info") if isinstance(p.get("info"), dict) else {}
        part = p.get("part") if isinstance(p.get("part"), dict) else {}
        sid = p.get("sessionID") or info.get("sessionID") or part.get("sessionID")
        if sid != self.opencode_session and not (kind == "session.error" and sid is None):
            return          # another session (a subagent's, say): not this turn's
        run = self._run
        if run is not None:
            run.last_event = time.monotonic()
        if kind == "message.updated":
            self._on_message(run, info)
        elif kind == "message.part.updated":
            self._on_part(run, part)
        elif kind == "message.part.delta":
            self._on_delta(run, p)
        elif kind == "session.error":
            self._on_error(run, p.get("error"))
        elif kind == "session.idle":
            self._on_idle(run)
        elif kind == "session.status":
            status = p.get("status") if isinstance(p.get("status"), dict) else {}
            if status.get("type") == "retry":
                self.stream.append("system", {"subtype": "retry", "attempt": status.get("attempt"),
                                              "message": status.get("message"),
                                              "next": status.get("next")})
        elif kind == "permission.asked":
            self._on_permission(p)
        elif kind == "session.compacted":
            self._on_compacted()

    def _on_message(self, run, info):
        mid, role = info.get("id"), info.get("role")
        if not mid:
            return
        self._roles[mid] = role
        if role != "assistant" or run is None:
            return
        sent = run.user_ids.get(info.get("parentID"))
        if sent is None:
            return
        sent.started = True
        run.assistant_ids.add(mid)
        if isinstance(info.get("cost"), (int, float)):
            run.costs[mid] = info["cost"]
        tokens = info.get("tokens")
        if isinstance(tokens, dict):
            run.spent[mid] = tokens     # opencode re-sends a message: per id, never twice
            if any(tokens.get(k) for k in ("input", "output", "total")):
                run.tokens = tokens

    def _on_part(self, run, part):
        if run is None or not self._live:
            return
        mid, kind, pid = part.get("messageID"), part.get("type"), part.get("id")
        if kind == "text" and self._roles.get(mid) == "user":
            self._on_echo(run, mid, part.get("text") or "")
            return
        if mid not in run.assistant_ids:
            return          # not an answer to this turn's prompts (a stale run's tail)
        if kind in ("text", "reasoning"):
            text = part.get("text") or ""
            if (part.get("time") or {}).get("end") is None:
                run.open_parts[pid] = [kind, text]
                return
            run.open_parts.pop(pid, None)
            self._emit_part(run, pid, kind, text)
        elif kind == "tool":
            self._on_tool(run, part)

    def _emit_part(self, run, pid, kind, text, partial=False):
        if pid in run.emitted:
            return
        run.emitted.add(pid)
        if kind == "reasoning":
            self.stream.append("thinking", _thinking_payload(text))
        else:
            payload = {"text": text}
            if partial:
                payload["partial"] = True
            self.stream.append("text", payload)

    def _on_delta(self, run, p):
        if run is None or p.get("field") != "text":
            return
        entry = run.open_parts.get(p.get("partID"))
        if entry is not None:
            entry[1] += str(p.get("delta") or "")

    def _on_tool(self, run, part):
        state = part.get("state") if isinstance(part.get("state"), dict) else {}
        status, call = state.get("status"), part.get("callID") or part.get("id")
        if status not in ("running", "completed", "error"):
            return
        if (call, "tool") not in run.emitted:
            run.emitted.add((call, "tool"))
            self.stream.append("tool", {"id": call, "name": part.get("tool"),
                                        "input": state.get("input") or {}})
            self._record("PreToolUse", part, state)
        if status in ("completed", "error") and (call, "result") not in run.emitted:
            run.emitted.add((call, "result"))
            text = state.get("output") if status == "completed" else state.get("error")
            self.stream.append("tool_result", {"tool_use_id": call,
                                               "is_error": status == "error",
                                               "text": str(text or "")[:TEXT_CHARS]})
            if status == "error" and str(text or "").startswith(DENIED):
                self.stream.append("policy", {"tool": sdk_tool_name(part.get("tool")),
                                              "decision": "deny",
                                              "reason": str(text)[len(DENIED):],
                                              "opencode_tool": part.get("tool")})
            self._record("PostToolUse" if status == "completed" else "PostToolUseFailure",
                         part, state)

    def _record(self, event, part, state):
        """One tool part as the SDK lane's hook payload, to the recording
        library (activity line, subagent job for `task`), with its arguments.
        A PreToolUse the policy denies is not recorded (hooks.recorder_for's
        rule: the call never ran). A recorder failure is a `hook` event."""
        name = sdk_tool_name(part.get("tool"))
        args = state.get("input") if isinstance(state.get("input"), dict) else {}
        if event == "PreToolUse" and self.policy.decide(name, args)[0] != "allow":
            return
        payload = {"hook_event_name": event, "tool_name": name, "tool_input": dict(args),
                   "tool_use_id": part.get("callID") or part.get("id"),
                   "session_id": self.opencode_session, "cwd": str(self.home),
                   "opencode_tool": part.get("tool")}
        if event == "PostToolUse":
            payload["tool_response"] = state.get("output")
        elif event == "PostToolUseFailure":
            payload["error"] = state.get("error")
            payload["is_interrupt"] = bool(self._interrupt_requested)
        try:
            self.recorder(payload)
        except Exception as exc:  # noqa: BLE001 - a recorder never fails the turn
            self.stream.append("hook", {"event": event, "error": "%s: %s"
                                        % (type(exc).__name__, exc)})

    def _checkpoint(self, kind, **extra):
        """The SDK lane's Stop (`session`) and PreCompact (`pre_compact`)
        checkpoint files; a failure is a `hook` event, never the turn's."""
        write = (checkpoints.write_session_checkpoint if kind == "session"
                 else checkpoints.write_pre_compact_checkpoint)
        try:
            path = write(self.home, slug=self.tool_context.slug)
        except Exception as exc:  # noqa: BLE001 - never the turn's failure
            self.stream.append("hook", {"event": kind, "error": "%s: %s"
                                        % (type(exc).__name__, exc)})
            return None
        self.stream.append("checkpoint", dict({"kind": kind, "path": str(path)}, **extra))
        return path

    def _on_compacted(self):
        """opencode compacted the session: the pre-compact checkpoint,
        then, as the SDK lane's PreCompact does, a rollover at the next turn
        boundary (a fresh generation with a handoff beats a summary)."""
        self._checkpoint("pre_compact", trigger="session.compacted")
        try:
            self._request_rollover("compacted")
        except Exception as exc:  # noqa: BLE001 - the trigger never fails a turn
            self.stream.append("error", {"error": "rollover trigger: %s: %s"
                                         % (type(exc).__name__, exc)})

    def _on_echo(self, run, mid, text):
        """opencode announced a user message: the prompt it stored."""
        match = next((s for s in run.sent if not s.echoed and s.text == text), None)
        if match is not None:
            match.echoed, match.message_id = True, mid
            run.user_ids[mid] = match
            run.pending_error = None        # an error before this was not this prompt's
        self.stream.append("user", {"text": text[:TEXT_CHARS],
                                    "echo_of": match.row["id"] if match and match.row else None})

    def _on_error(self, run, error):
        if run is None or not self._live:
            return
        kind = classify(error)
        if any(s.echoed and not s.closed for s in run.sent):
            if run.error is None:
                run.error = kind            # the first error decides
        else:
            run.pending_error, run.pending_at = kind, time.monotonic()

    def _on_idle(self, run):
        """The first idle after an announced prompt ends what it answered;
        an idle with nothing announced is ignored."""
        if run is None or not self._live or run.over:
            return
        if any(s.echoed and not s.closed for s in run.sent):
            self._settle(run)

    def _on_permission(self, p):
        """opencode's permission config is allow-all: an ask that still
        arrives is rejected at once, never left to hang the turn."""
        request = p.get("id")
        try:
            self._client.reply_permission(request, "reject",
                                          "the cousin's runner answers no interactive ask")
            outcome = "rejected"
        except OpencodeError as err:
            outcome = "reply failed: %s" % err
        self.stream.append("error", {"error": "opencode asked permission for %s %s: %s"
                                     % (p.get("permission"), p.get("patterns"), outcome),
                                     "permission": request})

    # -- turns -----------------------------------------------------------------
    def _row_item(self, row):
        return Item(thread_id=row["thread_id"], source=row["source"], body=row["body"],
                    sender=row["sender"], attachments=tuple(row["attachments"]),
                    context=row["context"], message_id=row["message_id"])

    def _send(self, run, row, text=None):
        """One prompt into the session: the composed prompt as
        `system`, the named model, the envelope text as the only part."""
        if text is None:
            text = envelope.render(self._row_item(row))
        sent = _Sent(row, text)
        run.sent.append(sent)
        try:
            self._client.prompt_async(self.opencode_session, text, system=self._system,
                                      model=self.model)
        except Exception:
            run.sent.remove(sent)
            raise
        return sent

    def _turn(self, first):
        """One runner turn: send `first`, fold operator/person/peer chat while it
        is live, and read events until every prompt sent is closed."""
        self._pump()                        # the last run's tail is not this turn's
        run = _Run()
        with self._lock:
            self._turn_seq += 1
            self._interrupt_requested = False
            self._roles = {}
            self._run = run
            self._live = True
            self.machine.to("running", "turn")
        self.turn.begin(first)
        self.stream.append("turn_start", {"inbox_ids": [first["id"]], "bodies": [first["body"]],
                                          "thread_id": first["thread_id"]})
        try:
            self._send(run, first)
        except Exception as exc:  # noqa: BLE001 - the prompt was not accepted
            self._unsent(run, first, exc)
            return False
        ok = self._drive(run)
        self._end_turn(run)
        self._checkpoint("session")             # the SDK lane's Stop hook
        if ok and not self._interrupt_requested and run.tokens:
            self._pressure(run)
        return ok

    def _unsent(self, run, row, exc):
        """prompt_async refused: a server gone takes the row back (the
        restart runs it); a refusal fails it, the runner recovers."""
        message = "%s: %s" % (type(exc).__name__, exc)
        requeue = self._server is not None and not self._server.alive()
        with self._lock:
            self.machine.to("errored", message)
        self.stream.append("error", {"error": message})
        try:
            self.stream.append("result", {"inbox_ids": [] if requeue else [row["id"]],
                                          "requeued": [row["id"]] if requeue else [],
                                          "interrupted": False, "is_error": True,
                                          "session_id": self.opencode_session, "usage": None,
                                          "total_cost_usd": None, "num_turns": 0,
                                          "error": message})
        finally:
            try:
                if requeue:                           # after its result, even when
                    self.inbox.requeue(row["id"])     # the append raises
                else:
                    self.inbox.done(row["id"], FAILED, message)
            finally:
                self._end_turn(run)

    def _end_turn(self, run):
        self.turn.end()
        with self._lock:
            self._live = False
            self._run = None
            if self.machine.state == "running":
                self.machine.to("idle", "turn done")
            elif self.machine.state == "errored" and not self._login_blocked \
                    and self.fatal is None:
                self.machine.to("idle", "recovered")

    def _drive(self, run):
        """Read events until the turn is over. Every `poll_s`, not every
        event: the interrupt rows, an interrupt asked in process, the fold,
        a reconnect's gap, the bounds."""
        stop_at, last = None, 0.0
        while not run.over:
            now = time.monotonic()
            if now - last >= self.poll_s:
                last = now
                if self._stop.is_set():
                    stop_at = stop_at or now + self.stop_grace_s
                    if not self._interrupt_requested:
                        self._abort(quiet=True)
                    if now >= stop_at:
                        self._settle(run, stopping=True)
                        break
                self._take_interrupts()
                if self._interrupt_asked == self._turn_seq and not self._interrupt_requested:
                    self._abort()
                self._fold(run)
                if self._gap:
                    self._gap = False
                    self._check_gap(run)
                    continue
                if self._bounds(run, now):
                    continue
            try:
                event = self._events.get(timeout=max(0.005, last + self.poll_s - time.monotonic()))
            except queue.Empty:
                continue
            self._on_event(event)
            drain_until = time.monotonic() + self.poll_s
            # every event already read before the next poll (bounded, so a
            # flood never starves the interrupt rows): a slow poll must not
            # leave one event per poll cycle
            while not run.over and time.monotonic() < drain_until:
                try:
                    event = self._events.get_nowait()
                except queue.Empty:
                    break
                self._on_event(event)
        return run.error is None or run.error[0] == "aborted"

    def _bounds(self, run, now):
        """A turn that cannot end by itself is settled failed: an error with
        no prompt announced (opencode never stored it: no idle follows), no
        event for `idle_timeout_s`, or the server gone. True when settled."""
        if run.pending_error is not None and now - run.pending_at >= self.error_grace_s:
            run.error, run.pending_error = run.pending_error, None
        elif run.deadline is not None and now >= run.deadline:
            run.error = ("failed", "handoff timeout (%.0fs)" % self.handoff_deadline_s)
            self._abort(quiet=True)
        elif now - run.last_event >= self.idle_timeout_s:
            run.error = ("failed", "no event from opencode for %.0fs" % self.idle_timeout_s)
            self._abort(quiet=True)
        elif self._server is not None and not self._server.alive():
            run.error = ("failed", "opencode serve exited during the turn")
        else:
            return False
        if run.sent:
            run.sent[0].echoed = True       # the turn's own row is what failed
        self._settle(run)
        return True

    def _abort(self, *, quiet=False):
        """POST abort for the live session. False when opencode refused it:
        said as an `error` event (unless `quiet`), and the turn runs on."""
        self._interrupt_requested = True
        try:
            self._client.abort(self.opencode_session)
            return True
        except OpencodeError as err:
            if not quiet:
                self._interrupt_requested = False
                self._interrupt_asked = None
                self.stream.append("error", {"error": "interrupt: %s" % err})
            return False

    def _take_interrupts(self):
        """Interrupt rows on their own path, every poll: the first
        aborts the run, and every one closes `delivered`; one opencode
        refused closes `failed` and the turn runs on."""
        if not self.takes_interrupts or not self._live:
            return
        for row in self.inbox.open_rows(INTERRUPT):
            if row["state"] != "queued" or \
                    self.inbox.claim_id(row["id"], claimant=self.session_id) is None:
                continue
            if self._interrupt_requested:
                self.inbox.done(row["id"], DELIVERED, "the live turn was already being interrupted")
            elif self._abort():
                self.inbox.done(row["id"], DELIVERED, "interrupted the live turn")
            else:
                self.inbox.done(row["id"], FAILED, "opencode refused the abort")

    def _fold(self, run):
        """Operator, person and peer chat that lands during the live turn
        is sent at once: opencode folds it into the running run.
        Anything else goes back to the queue (`base.FOLDED_KINDS`
        says why)."""
        if self._interrupt_requested or run.over or not run.fold:
            return
        rows = self.inbox.claim(limit=10, claimant=self.session_id)
        for i, row in enumerate(rows):
            if row["source"] == INTERRUPT or self._interrupt_requested \
                    or not folds_into_turn(row["source"], row["thread_id"]):
                self.inbox.requeue(row["id"])
                continue
            try:
                self._send(run, row)
            except Exception as exc:  # noqa: BLE001 - not sent: back to the queue
                for rest in rows[i:]:
                    self.inbox.requeue(rest["id"])
                self.stream.append("error", {"error": "fold: %s: %s"
                                             % (type(exc).__name__, exc)})
                return
            self.turn.add(row)

    def _check_gap(self, run):
        """A reconnected event stream may have missed the idle: read the
        session's messages and settle the turn when its last answer is
        complete."""
        try:
            messages = self._client.messages(self.opencode_session) or []
        except OpencodeError as err:
            self.stream.append("error", {"error": "after a reconnect: %s" % err})
            return
        for m in messages:
            info = m.get("info") or {}
            self._roles[info.get("id")] = info.get("role")
            if info.get("role") == "user":
                text = "".join(p.get("text") or "" for p in m.get("parts") or []
                               if p.get("type") == "text")
                match = next((s for s in run.sent if not s.echoed and s.text == text), None)
                if match is not None:
                    match.echoed, match.message_id = True, info.get("id")
                    run.user_ids[info.get("id")] = match
            else:
                self._on_message(run, info)
        last = (messages[-1].get("info") or {}) if messages else {}
        done = last.get("role") == "assistant" and (last.get("time") or {}).get("completed") \
            and (last.get("error") or last.get("finish") != "tool-calls")
        self.stream.append("system", {"subtype": "event_gap", "settled": bool(done)})
        if done and any(s.echoed and not s.closed for s in run.sent):
            if last.get("error") and run.error is None:
                run.error = classify(last["error"])
            self._settle(run)

    def _settle(self, run, *, stopping=False):
        """Close what this idle answered. Delivered: every announced prompt
        of a good run. Interrupted: the prompts the model received (the
        turn's first, and any it started); the rest go back, since an abort
        drops a queued prompt. Failed: the same split, failed. Auth:
        everything back, and the runner waits for the login."""
        kind, detail = run.error or (None, None)
        interrupted = self._interrupt_requested or kind == "aborted" or stopping
        first = run.sent[0] if run.sent else None
        closing, requeue = [], []
        for s in run.unclosed():
            received = s.echoed and (s.started or s is first)
            if kind == "auth" or stopping:
                requeue.append(s)
            elif interrupted or kind == "failed":
                (closing if received else requeue).append(s)
            elif s.echoed:
                closing.append(s)
        if kind == "failed":
            with self._lock:
                if self.machine.state == "running":
                    self.machine.to("errored", detail)
            self.stream.append("error", {"error": detail})
        outcome = FAILED if kind == "failed" else DELIVERED
        for pid, (part_kind, text) in list(run.open_parts.items()):
            if text:
                self._emit_part(run, pid, part_kind, text, partial=True)
        run.open_parts.clear()
        payload = {"inbox_ids": [s.row["id"] for s in closing if s.row is not None],
                   "requeued": [s.row["id"] for s in requeue if s.row is not None],
                   "interrupted": bool(interrupted), "is_error": kind in ("failed", "auth"),
                   "session_id": self.opencode_session,
                   "usage": usage_of(run.spent.values()) if run.spent else None,
                   "total_cost_usd": sum(run.costs.values()) if run.costs else None,
                   "num_turns": len(run.assistant_ids)}
        if kind in ("failed", "auth"):
            payload["error"] = detail
        if kind == "auth":
            payload.update(auth=auth.LOGIN, repeat_in_transcript=True)
        run.results += 1
        try:
            if not run.quiet:
                # the result first: whoever reads a row closed finds its
                # result; the rows close even when the append raises
                self.stream.append("result", payload)
        finally:
            for s in closing:
                s.closed = True
                if s.row is not None and s.row["id"] > 0:
                    self.inbox.done(s.row["id"], outcome, detail if kind == "failed"
                                    else "turn %s" % self.opencode_session)
            for s in requeue:
                s.closed = True
                if s.row is not None and s.row["id"] > 0:
                    self.inbox.requeue(s.row["id"])
        self._record_usage(run)
        run.over = not run.unclosed()
        if kind == "auth":
            self._login_required(detail)
        elif kind is None and not interrupted:
            self._note_good_result()

    def _record_usage(self, run):
        """The usage record of one result, as the SDK lane's: what the run's
        answers spent since its last result (the tokens the provider
        reported), at the cost opencode put on them (0 on a free model,
        an estimate as on the login lane), the cost passed as this
        runner's running total since usage.record diffs it per client. The
        handoff run is recorded without an event, as it writes no result. A
        failure is a `usage` event with the error, never a failed turn."""
        try:
            spent, cost = usage_of(run.spent.values()), sum(run.costs.values())
            tokens_before, cost_before = run.recorded
            run.recorded = (spent, max(cost, cost_before))
            self._usage_spent += max(0.0, cost - cost_before)
            row = usage.record(self.home, client_id=self._usage_client,
                               session_id=self.opencode_session or "", lane=LANE,
                               result={"usage": {k: max(0, v - tokens_before[k])
                                                 for k, v in spent.items()},
                                       "total_cost_usd": self._usage_spent,
                                       "session_id": self.opencode_session})
        except Exception as exc:  # noqa: BLE001 - usage must never fail a turn
            row = {"error": "%s: %s" % (type(exc).__name__, exc)}
        if run.quiet:
            return
        try:
            self.stream.append("usage", {k: row[k] for k in ("cost_usd", "estimate", "total",
                                                             "error") if k in row})
        except Exception:  # noqa: BLE001 - nor does its event
            pass

    # -- a login to do (the SDK lane's detection shape) ------------------------
    def _credential_mark(self):
        """What changes when the operator fixes this account: auth.json's
        mtime and hash (in memory only); None for a local endpoint."""
        if self.account.endpoint:
            return None
        path = Path(self.account.data_dir).joinpath(*accounts.AUTH_JSON)
        try:
            return (path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
        except OSError:
            return ("missing",)

    def _login_action(self):
        action = accounts.login_action(self.account, via=self.tool_context.slug)
        if self.account.endpoint:
            return "%s; then delete %s to retry" % (action, self.home / auth.LOGIN_FILE)
        return "run %s" % action

    def _login_required(self, detail):
        """`errored` (detail login_required), data/login-required.json, an
        `auth` event; the loop claims nothing until the fix. Never `fatal`."""
        with self._lock:
            if self.machine.state in ("idle", "running", "rolling_over"):
                self.machine.to("errored", auth.LOGIN)
        self._login_blocked = True
        self._restore_pending = True
        self._login_mark = self._credential_mark()
        fields = dict(host=auth.host_label(self.root), account=self.account.name,
                      kind=self.account.kind, reason=auth.LOGIN, detail=detail,
                      action=self._login_action())
        try:
            data = auth.write_login_required(self.home, **fields)
            self._login_file_seen = True
        except Exception as exc:  # noqa: BLE001 - the event and the wait still happen
            data = dict(fields, detail=str(detail)[:300],
                        since=datetime.now(timezone.utc).isoformat(timespec="seconds"))
            self._login_file_seen = False
            self.stream.append("error", {"error": "%s: %s: %s" % (
                auth.LOGIN_FILE, type(exc).__name__, exc)})
        self.stream.append("auth", {k: data[k] for k in ("account", "kind", "reason", "detail",
                                                          "action", "since", "host")})

    def _await_login(self):
        """Every 1, 2, 4 ... 300 s look: the account's auth.json changed, or
        the operator deleted data/login-required.json (the manual retry).
        Between two looks both are read every login_poll_s (a stat and a
        small hash), and a change ends the wait at once: a login finished
        mid-backoff is seen within a second. No turn is spent on a look;
        the next good result clears the file."""
        deadline = time.monotonic() + auth.backoff_s(self._login_attempt)
        poll_at = time.monotonic() + self.login_poll_s
        while not self._stop.is_set() and time.monotonic() < deadline:
            self._stop.wait(min(0.05, max(0.0, deadline - time.monotonic())))
            self._pump()
            if time.monotonic() < poll_at:
                continue
            poll_at = time.monotonic() + self.login_poll_s
            if ((self._login_file_seen and not (self.home / auth.LOGIN_FILE).exists())
                    or self._credential_mark() != self._login_mark):
                break
        if self._stop.is_set():
            return
        self._login_attempt += 1
        present = auth.read_login_required(self.home) is not None
        why = ("credentials changed" if self._credential_mark() != self._login_mark
               else "manual retry" if (self._login_file_seen and not present) else None)
        if why is None:
            return
        # opencode reads auth.json live, so the changed
        # file is held to the start's checks before any turn runs on it
        try:
            accounts.preflight(self.account, self.root)
        except accounts.AccountsError as err:
            self._fail_start("opencode login retry (%s): %s" % (why, err))
            return
        self._login_blocked = False
        with self._lock:
            if self.machine.state == "errored":
                self.machine.to("idle", "login retry: %s" % why)
        self.stream.append("auth", {"account": self.account.name, "retry": why})

    def _note_good_result(self):
        if not self._restore_pending:
            return
        self._restore_pending = False
        self._login_attempt = 0
        auth.clear_login_required(self.home)
        self.stream.append("auth", {"account": self.account.name, "restored": True})
