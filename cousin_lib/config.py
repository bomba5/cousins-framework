"""Configuration seam: identity, ports, and operator profile.

Everything operator-specific or instance-specific is configuration; the
framework is code. A cousin's identity lives in <home>/cousin.toml. The
operator is optional by design - the null profile is "no operator", never
a defaulted human being.
"""
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


class MissingConfigError(Exception):
    """Required configuration is absent. Fail loud: a silent fallback here
    becomes somebody's home directory or somebody's name in a log."""


# The effort levels an agent command's {effort} placeholder may render;
# the memory scopes a cousin may declare; the model catalogue the
# console's spawn dialog offers when config/harness.toml [agent] names
# none. The catalogue is a convenience list for a dialog, not a
# default that ever reaches an agent: a {model} placeholder with no
# value configured anywhere is a spawn error (cousin_lib.spawn).
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
MEMORY_SCOPES = ("private", "shared")
# Retired scope names, still read so an older cousin.toml keeps working:
# "both" behaved exactly like "shared" (may nominate to the shared tier)
# and every cousin always keeps its private memory, so the name promised
# a difference that never existed.
RETIRED_SCOPES = {"both": "shared"}


def normalize_scope(value):
    """A scope as stored today: a retired name maps to its successor."""
    return RETIRED_SCOPES.get(value, value)
# The spawn dialog's catalogue when config/harness.toml [agent] sets no
# `models`. Each id was accepted by the agent CLI; the [1m] ids are the
# 1M-context variants. The first entry is the fallback default.
DEFAULT_MODELS = ("claude-opus-5", "claude-opus-5[1m]", "claude-fable-5-1",
                  "claude-sonnet-5", "claude-sonnet-5[1m]",
                  "claude-haiku-4-5-20251001")


def _check_effort(value, where):
    if value is not None and value not in EFFORT_LEVELS:
        raise MissingConfigError(
            "%s must be one of %s, got %r"
            % (where, ", ".join(EFFORT_LEVELS), value))
    return value


@dataclass
class CousinConfig:
    home: Path
    slug: str
    name: str
    chat_port: int | None
    operator_name: str | None
    chat_host: str | None = None
    peer_visible: bool = True
    tmux_session: str = ""
    memory_scope: str = "private"
    heartbeat_seconds: int = 3600
    type: str = "cousin"
    flip_at: str | None = None
    proactive_recall: bool = True
    recall_keyword_only: bool = False
    model: str | None = None
    effort: str | None = None

    @classmethod
    def load(cls, home):
        home = Path(home)
        toml_path = home / "cousin.toml"
        if not toml_path.is_file():
            raise MissingConfigError("no cousin.toml under %s" % home)
        data = tomllib.loads(toml_path.read_text())
        cousin = data.get("cousin", {})
        slug = cousin.get("slug")
        if not slug:
            raise MissingConfigError("cousin.slug missing in %s" % toml_path)
        runtime = data.get("runtime", {})
        model = runtime.get("model")
        effort = _check_effort(runtime.get("effort"),
                               "runtime.effort in %s" % toml_path)
        return cls(
            home=home,
            slug=slug,
            name=cousin.get("name", slug.capitalize()),
            chat_port=data.get("chat", {}).get("port"),
            operator_name=data.get("operator", {}).get("name"),
            chat_host=data.get("chat", {}).get("host"),
            peer_visible=bool(cousin.get("peer_visible", True)),
            tmux_session=data.get("chat", {}).get("tmux_session", slug),
            memory_scope=normalize_scope(
                data.get("memory", {}).get("scope", "private")),
            heartbeat_seconds=int(
                data.get("heartbeat", {})
                .get("context_beat_seconds", 3600)),
            type=cousin.get("type", "cousin"),
            flip_at=data.get("lifecycle", {}).get("flip_at"),
            proactive_recall=bool(
                data.get("memory", {}).get("proactive_recall", True)),
            recall_keyword_only=bool(
                data.get("memory", {}).get("recall_keyword_only", False)),
            model=str(model) if model is not None else None,
            effort=effort,
        )

    @classmethod
    def from_env(cls):
        home = os.environ.get("COUSIN_HOME")
        if not home:
            raise MissingConfigError(
                "COUSIN_HOME is not set; every cousin process runs with its "
                "home in the environment"
            )
        return cls.load(home)

    def require_chat_port(self):
        if self.chat_port is None:
            raise MissingConfigError(
                "chat.port missing in %s" % (self.home / "cousin.toml")
            )
        return self.chat_port


class FrameworkConfig:
    """An install's shared layout. The filesystem is the registry: a cousin
    exists iff cousins/<slug>/cousin.toml exists under the root. No service
    has to be running for the fleet to be enumerable."""

    def __init__(self, root):
        # Absolute from the start: the root is written into files the
        # agent reads from the cousin home (.mcp.json, hook commands),
        # where a relative root such as "." names the wrong directory.
        # abspath, not resolve: a symlinked checkout keeps its name.
        self.root = Path(os.path.abspath(root))

    @classmethod
    def from_env(cls):
        """The root from the environment alone: FRAMEWORK_ROOT, else the
        root a COUSIN_HOME names (see root_from_home). Library code and
        daemons use this; a person's command uses resolve()."""
        root = os.environ.get("FRAMEWORK_ROOT") or cls._root_from_env_home()
        if not root:
            raise MissingConfigError(
                "FRAMEWORK_ROOT is not set and COUSIN_HOME does not name a"
                " cousin home under a root; the framework root locates the"
                " cousin registry and shared configuration"
            )
        return cls(root)

    @classmethod
    def resolve(cls, flag_value=None, *, cwd_fallback=False):
        """The one root-discovery rule every entry point uses: an
        explicit --root flag wins, else FRAMEWORK_ROOT, else the root
        COUSIN_HOME names (a cousin's own shell may carry only its
        home; see root_from_home), else - for a command a person types
        (cwd_fallback) - the working directory when it is a checkout
        (see looks_like_checkout), else a loud error naming the
        channels. Sharing it is what keeps two commands from
        disagreeing about how to be told the same fact - a
        disagreement an adopter finds by failing, not by --help.
        Library code never passes cwd_fallback: a daemon's working
        directory is not a statement about which install it serves."""
        root = (flag_value or os.environ.get("FRAMEWORK_ROOT")
                or cls._root_from_env_home())
        if not root and cwd_fallback and cls.looks_like_checkout(
                os.getcwd()):
            root = os.getcwd()
        if not root:
            raise MissingConfigError(
                "no framework root; pass --root <checkout>, set "
                "FRAMEWORK_ROOT (or COUSIN_HOME to a cousin home under "
                "the root), or run from inside the checkout. The "
                "root locates the cousin registry and config/ "
                "(typically the checkout itself)."
            )
        return cls(root)

    @staticmethod
    def root_from_home(home):
        """The root a cousin home implies, or None. Homes live at
        <root>/cousins/<slug>; the grandparent counts as the root only
        when it holds a config/ directory, so an arbitrary directory
        that happens to be called cousins/ names nothing."""
        if not home:
            return None
        home = Path(os.path.abspath(home))
        root = home.parent.parent
        if home.parent.name == "cousins" and (root / "config").is_dir():
            return root
        return None

    @classmethod
    def _root_from_env_home(cls):
        return cls.root_from_home(os.environ.get("COUSIN_HOME"))

    @staticmethod
    def looks_like_checkout(directory):
        """A directory holding templates/cousin-CLAUDE.template.md and a
        config/ directory: the shape of a framework checkout."""
        d = Path(directory)
        return ((d / "templates" / "cousin-CLAUDE.template.md").is_file()
                and (d / "config").is_dir())

    def list_cousins(self):
        base = self.root / "cousins"
        rows = []
        if not base.is_dir():
            return rows
        for entry in sorted(base.iterdir()):
            if (entry / "cousin.toml").is_file():
                rows.append(CousinConfig.load(entry))
        return rows

    def agent_defaults(self):
        """config/harness.toml [agent]; see agent_config."""
        return agent_config(self.root)


def _read_harness_toml(root):
    path = Path(root) / "config" / "harness.toml"
    if not path.exists():
        return None
    try:
        return tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as err:
        raise MissingConfigError("config/harness.toml is unusable: %s" % err)


DEFAULT_FLIP_AT = "04:00"
_NEVER = ("never", "off", "none", "no", "")


def parse_flip_at(value, where):
    """(hour, minute) from "HH:MM". MissingConfigError on anything else,
    because a flip time that does not parse silently means no flip."""
    try:
        hour, minute = (int(part) for part in str(value).split(":"))
    except (TypeError, ValueError):
        raise MissingConfigError(
            "%s must be a time as HH:MM, got %r" % (where, value))
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise MissingConfigError(
            "%s must be a time as HH:MM, got %r" % (where, value))
    return hour, minute


def default_flip_at(root):
    """The install's daily flip time for cousins that do not set one:
    config/harness.toml `default_flip_at`, else DEFAULT_FLIP_AT. The
    built-in default is deliberate - a cousin nobody configured still
    flips, because the failure mode of not flipping is invisible."""
    data = _read_harness_toml(root) or {}
    value = data.get("default_flip_at")
    if value is None:
        return DEFAULT_FLIP_AT
    if str(value).strip().lower() in _NEVER:
        return None
    parse_flip_at(value, "config/harness.toml default_flip_at")
    return str(value)


def flip_time(config, root=None):
    """When this cousin flips daily, as "HH:MM", or None when it never
    does. Its own [lifecycle] flip_at wins, "never" is the explicit
    opt-out, a worker never flips, and anything else takes the install
    default. Resolving it here rather than reading config.flip_at is
    what makes the default reach a cousin whose file predates it."""
    if config.type == "worker":
        return None
    own = config.flip_at
    if own is not None:
        if str(own).strip().lower() in _NEVER:
            return None
        parse_flip_at(own, "lifecycle.flip_at in %s/cousin.toml"
                           % config.home)
        return str(own)
    if root is None:
        root = FrameworkConfig.root_from_home(config.home)
    return default_flip_at(root)


def agent_config(root):
    """config/harness.toml [agent]: the install-wide `default_model` and
    `default_effort` an agent command's {model} and {effort}
    placeholders fall back to when the cousin's [runtime] sets none,
    and `models`, the catalogue the console's spawn dialog offers.
    Absent file or table: both defaults None, the built-in catalogue.
    A default_effort outside the levels or a models value that is not
    a list of strings is loud, like the rest of the file."""
    data = _read_harness_toml(root) or {}
    agent = data.get("agent") or {}
    if not isinstance(agent, dict):
        raise MissingConfigError(
            "config/harness.toml [agent] must be a table")
    models = agent.get("models")
    if models is None:
        models = list(DEFAULT_MODELS)
    elif (not isinstance(models, list)
          or not all(isinstance(m, str) and m for m in models)):
        raise MissingConfigError(
            "config/harness.toml [agent] models must be a list of"
            " model names, got %r" % (models,))
    model = agent.get("default_model")
    return {
        "default_model": str(model) if model is not None else None,
        "default_effort": _check_effort(
            agent.get("default_effort"),
            "config/harness.toml [agent] default_effort"),
        "models": list(models),
    }


def harness_config(root):
    """config/harness.toml: where the agent harness keeps this install's
    session transcripts and its own auto-memory directory, and the
    transcript size (flip_when_transcript_mb) past which the loops
    daemon requests a flip, and settings_file, the harness's own
    settings JSON that `cousin-mcp approve` edits. Absent: None
    (transcript mining, the harness memory collection, the size guard
    and scripted MCP approval are all off).
    Unparsable, or a threshold that is not a positive number: loud,
    because it was promised. Path values are templates; expand them
    per cousin with expand_harness_path. attention_patterns: pane text
    that means the agent is waiting on a human (a login menu), which
    the console flags on a running cousin's row; absent, []."""
    data = _read_harness_toml(root)
    if data is None:
        return None
    threshold = data.get("flip_when_transcript_mb")
    if threshold is not None and (
            isinstance(threshold, bool)
            or not isinstance(threshold, (int, float))
            or threshold <= 0):
        raise MissingConfigError(
            "config/harness.toml flip_when_transcript_mb must be a"
            " positive number of megabytes, got %r" % (threshold,))
    patterns = data.get("attention_patterns", [])
    if (not isinstance(patterns, list)
            or not all(isinstance(p, str) and p for p in patterns)):
        raise MissingConfigError(
            "config/harness.toml attention_patterns must be a list of"
            " non-empty strings, got %r" % (patterns,))
    return {"transcripts_dir": data.get("transcripts_dir"),
            "auto_memory_dir": data.get("auto_memory_dir"),
            "flip_when_transcript_mb": threshold,
            "settings_file": data.get("settings_file"),
            "attention_patterns": list(patterns)}


def expand_harness_path(template, home):
    """Expand a harness.toml path template for one cousin home. {home}
    is the home verbatim; {home_encoded} is the harness's project-dir
    encoding of it: every '/' becomes '-', so /a/b -> -a-b. A leading
    ~ is the user's home, as the shell would read it."""
    home = Path(home)
    encoded = str(home).replace("/", "-")
    return Path(template.replace("{home_encoded}", encoded)
                        .replace("{home}", str(home))).expanduser()


def read_session_id(home):
    """The session id persisted in a cousin's `cousin.toml` [runtime],
    or "" when none was ever written (a hand-made cousin, or a first
    flip). Written by the flip AFTER the respawn, so a reader inside a
    just-started session can beat the write and see the previous
    generation's id."""
    try:
        data = tomllib.loads((Path(home) / "cousin.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return ""
    return str((data.get("runtime") or {}).get("session_id") or "")
