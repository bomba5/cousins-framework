"""Console routes for WP-A, agent and cousin settings.

A cousin's [agent] table, per lane, and the rest of its cousin.toml that
the console edits. Each kind of key has one write path:

- [agent] (the keys the cousin's runner reads, per lane): through
  spawn.persist_agent_values, the path the model and effort routes use
  too: agent_settings.validate (the runner's own checks, per key and for
  the table as a whole), an sdk model's validating turn in a child
  process (run here as a long operation), then agent_settings.apply (one
  atomic write whose hook re-checks the parsed table).
- the other keys (COUSIN_FIELDS: the name, peer_visible, the [memory]
  recall and review keys, [lifecycle] flip_at and [agent]
  commit_attribution, which every lane reads, the tmux one included):
  toml_edit.write_keys, with a validate hook that runs the same check on
  the parsed result before the rename.

[chat] port/host/tmux_session and the [session] hooks are shown, never
written: a changed port breaks every peer, and a hook is a shell command.

Routes:
  GET  /api/cousins/<slug>/agent      agent_settings.describe, the hold, env_allow's base
  POST /api/cousins/<slug>/agent      {"changes": {key: value|null}}
  GET  /api/cousins/<slug>/settings   the COUSIN_FIELDS and the read-only rows
  POST /api/cousins/<slug>/settings   {"changes": {"table.key": value|null}}

The install-wide [agent] defaults are WP-F's (routes_system.py)."""
from __future__ import annotations

import re
import tomllib

from cousin_lib.console import router
from cousin_lib.console.app import HttpError

OP_KIND = "agent-settings"
NAME_MAX_CHARS = 64

# "table.key" -> spec. restart: whether the running cousin needs a start
# to see a change (the chat server and the runner load these at start;
# the loops daemon and the review gate read the file each time).
COUSIN_FIELDS = {
    "cousin.name": {"type": "name", "restart": True,
                    "hint": "display name: cards, chat, the prompt"},
    "cousin.peer_visible": {"type": "bool", "default": True, "restart": False,
                            "hint": "false takes it out of every peer list, and it sees no peers"},
    "memory.proactive_recall": {"type": "bool", "default": True, "restart": True,
                                "hint": "recall lines added to incoming messages"},
    "memory.recall_keyword_only": {"type": "bool", "default": False, "restart": True,
                                   "hint": "keyword-only hits in recall lines when no embedding"
                                           " service is configured"},
    "memory.review_batch": {"type": "count", "default": 3, "restart": False,
                            "hint": "entries past which new memory is held for review"},
    "memory.review_model": {"type": "model", "default": None, "restart": False,
                            "hint": "the model the review gate asks (sdk lane); unset is the"
                                    " cousin's own"},
    "lifecycle.flip_at": {"type": "flip_at", "default": None, "restart": False,
                          "hint": "\"HH:MM\" daily flip, \"never\", or unset for the install"
                                  " default"},
    "agent.commit_attribution": {"type": "bool", "default": None, "restart": True,
                                 "hint": "the harness's own Co-Authored-By / \"Generated with\""
                                         " lines on commits and PRs; unset is the install"
                                         " default"},
}
READONLY_FIELDS = {
    "chat.port": "changing it breaks every peer that reaches this cousin",
    "chat.host": "changing it breaks every peer that reaches this cousin",
    "chat.tmux_session": "the session peers and the console type into",
    "session.start_hooks": "shell commands: edit cousin.toml by hand",
    "session.end_hooks": "shell commands: edit cousin.toml by hand",
}
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _read(home):
    try:
        return tomllib.loads((home / "cousin.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _lookup(data, dotted):
    table, key = dotted.split(".", 1)
    section = data.get(table)
    if isinstance(section, dict) and key in section:
        return True, section[key]
    return False, None


def check_field(name, value, root=None):
    """The value normalized for COUSIN_FIELDS[name] (None removes the key,
    where the field may be unset), or ValueError with the reason."""
    from cousin_lib import config as fwconfig
    if name in READONLY_FIELDS:
        raise ValueError("read-only here: %s" % READONLY_FIELDS[name])
    spec = COUSIN_FIELDS.get(name)
    if spec is None:
        raise ValueError("not a setting this panel writes")
    kind = spec["type"]
    if value is None:
        if kind == "name":
            raise ValueError("a name is required")
        return None
    if kind == "bool":
        if not isinstance(value, bool):
            raise ValueError("must be true or false")
        return value
    if kind == "count":
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("must be a whole number, 0 or more")
        return value
    if kind == "name":
        if not isinstance(value, str) or not value.strip():
            raise ValueError("a name is required")
        if value != value.strip():
            raise ValueError("no leading or trailing spaces")
        if len(value) > NAME_MAX_CHARS:
            raise ValueError("at most %d characters" % NAME_MAX_CHARS)
        if _CONTROL.search(value):
            raise ValueError("no control characters")
        return value
    if kind == "model":
        from cousin_lib import spawn
        try:
            spawn.check_runtime_value("model", value)
        except spawn.SpawnError:
            raise ValueError("one word of letters, digits and ._:/+-[]")
        return value
    if kind == "flip_at":
        if not isinstance(value, str):
            raise ValueError("must be \"HH:MM\" or \"never\"")
        if value == "never":
            return value
        try:
            fwconfig.parse_flip_at(value, "lifecycle.flip_at")
        except fwconfig.MissingConfigError:
            raise ValueError("must be \"HH:MM\" or \"never\"")
        return value
    raise ValueError("unsupported")


def _install_attribution(root):
    """{value, source} of config/harness.toml [agent] commit_attribution."""
    from cousin_lib import config as fwconfig
    try:
        data = tomllib.loads((root / "config" / "harness.toml").read_text())
    except OSError:
        data = {}
    except tomllib.TOMLDecodeError as err:
        return {"value": None, "source": "config/harness.toml does not parse: %s" % err}
    agent = data.get("agent") if isinstance(data.get("agent"), dict) else {}
    if "commit_attribution" in agent:
        try:
            return {"value": fwconfig.commit_attribution(root),
                    "source": "config/harness.toml [agent]"}
        except fwconfig.MissingConfigError as err:
            return {"value": None, "source": str(err)}
    return {"value": True, "source": "built-in default (the harness's stock attribution)"}


def describe_settings(home, root):
    """{fields: {name: {value, set, default, type, restart, hint, ...}},
    readonly: {name: value}, errors: {name: reason}} for the cousin."""
    from cousin_lib import config as fwconfig
    data = _read(home)
    fields, errors = {}, {}
    for name, spec in COUSIN_FIELDS.items():
        present, value = _lookup(data, name)
        default = spec.get("default")
        if name == "cousin.name":
            default = str(_lookup(data, "cousin.slug")[1] or home.name).capitalize()
        row = {"value": value if present else default, "set": present,
               "default": default, "type": spec["type"], "restart": spec["restart"],
               "hint": spec["hint"]}
        if present:
            try:
                check_field(name, value)
            except ValueError as err:
                errors[name] = str(err)
        fields[name] = row
    try:
        install_flip = fwconfig.default_flip_at(root)
    except fwconfig.MissingConfigError as err:
        install_flip, errors["lifecycle.flip_at"] = None, str(err)
    flip = fields["lifecycle.flip_at"]
    flip["install"] = install_flip
    try:
        flip["effective"] = fwconfig.flip_time(fwconfig.CousinConfig.load(home), root)
    except (fwconfig.MissingConfigError, tomllib.TOMLDecodeError) as err:
        flip["effective"] = None
        errors.setdefault("lifecycle.flip_at", str(err))
    attribution = fields["agent.commit_attribution"]
    attribution["install"] = _install_attribution(root)
    own = attribution["value"]
    attribution["effective"] = own if isinstance(own, bool) else attribution["install"]["value"]
    readonly = {}
    for name in READONLY_FIELDS:
        present, value = _lookup(data, name)
        if name.startswith("session."):
            value = value if present else []
        readonly[name] = value
    readonly["chat.tmux_session"] = readonly["chat.tmux_session"] or data.get(
        "cousin", {}).get("slug")
    return {"fields": fields, "readonly": readonly, "readonly_why": dict(READONLY_FIELDS),
            "errors": errors}


def _settings_kind(home):
    """The harness settings kind of this cousin: "tmux" for the tmux kind,
    None for tmux-legacy, and "runner" for every other runner kind (the
    SDK runner renders commit_attribution into its options at start)."""
    from cousin_lib import agent_settings
    lane = agent_settings.lane_of(_read(home).get("agent") or {})
    if lane == "tmux":
        return "tmux"
    if lane == agent_settings.TMUX_LEGACY:
        return None
    return "runner"


def write_settings(home, root, changes):
    """Validate every change, then one write through toml_edit.write_keys
    whose hook checks the parsed result the same way (and commit_attribution
    as config.commit_attribution reads it). A value the file already holds
    is not a change. Returns (changed names, note); _Refused with a reason
    per name, and nothing written, on a refusal. A commit_attribution
    change on a tmux lane also rewrites the cousin's harness settings,
    where that lane reads it."""
    from cousin_lib import agent_settings
    from cousin_lib import config as fwconfig
    from cousin_lib.console import toml_edit
    errors, out = {}, {}
    for name, value in changes.items():
        try:
            out[name] = check_field(name, value, root)
        except ValueError as err:
            errors[name] = str(err)
    if errors:
        raise _Refused(errors)
    data = _read(home)
    changed = [n for n, v in out.items() if _lookup(data, n) != ((v is not None), v)]
    if not changed:
        return [], None
    writes = [tuple(n.split(".", 1)) + (out[n],) for n in changed]

    def hook(parsed):
        bad = {}
        for name in changed:
            present, value = _lookup(parsed, name)
            if present:
                try:
                    check_field(name, value, root)
                except ValueError as err:
                    bad[name] = str(err)
        agent = parsed.get("agent") or {}
        # [agent] as a whole is checked only when this write touches it: a
        # stale key there (an account since removed) must not block a rename
        if any(name.startswith("agent.") for name in changed):
            try:
                fwconfig.commit_attribution(root, agent)
            except fwconfig.MissingConfigError as err:
                bad.setdefault("agent.commit_attribution", str(err))
            if agent_settings.lane_of(agent) != agent_settings.TMUX_LEGACY:
                # a runner cousin's [agent] as the runner reads it (the SCHEMA
                # knows commit_attribution), the check agent_settings.apply runs
                for key, reason in agent_settings.check_table(root, agent, home).items():
                    bad.setdefault("agent." + key, reason)
        if bad:
            raise _Refused(bad)
    try:
        toml_edit.write_keys(home, writes, validate=hook)
    except (TypeError, ValueError) as err:
        if isinstance(err, _Refused):
            raise
        raise _Refused({"changes": str(err)})
    note = None
    if "agent.commit_attribution" in changed:
        note = sync_harness_settings(home, root, {n: out[n] for n in changed})
    return changed, note


class HarnessFailed(Exception):
    """cousin.toml was written, the harness settings file was not, for a
    reason apply_project_settings does not word itself."""

    def __init__(self, message, written):
        super().__init__(message)
        self.written = written


def _attribution_overrides(home, want):
    """The keys of <home>/.claude/settings.json that the operator set
    himself (not harness_settings' own) and that say the opposite of
    `want`: those win over commit_attribution."""
    import json
    from cousin_lib import harness_settings
    try:
        data = json.loads(harness_settings.settings_path(home).read_text())
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    owned = harness_settings._read_owned_attribution(home)
    off = {"includeCoAuthoredBy": False, "attribution": dict(harness_settings.ATTRIBUTION_OFF)}
    return [key for key, value in off.items()
            if key in data and key not in owned and (data[key] == value) != (not want)]


def sync_harness_settings(home, root, written):
    """After a commit_attribution change: on a tmux lane (tmux-legacy or the
    tmux kind), which reads it from <home>/.claude/settings.json, rewrite that
    file with the function cousin-spawn --repair-settings runs; an SDK-family
    runner reads cousin.toml at start and needs nothing. The note to show:
    what was done, which of the operator's own keys win, or why the file was
    left (a SettingsError: its own words). Anything else is HarnessFailed,
    which carries `written`, the cousin.toml values already on disk."""
    if _settings_kind(home) == "runner":
        return None
    from cousin_lib import config as fwconfig
    from cousin_lib import harness_settings, spawn
    try:
        harness_settings.apply_project_settings(home, root=root, kind=spawn._settings_kind(home))
    except harness_settings.SettingsError as err:
        return ("cousin.toml was written, the harness settings were not: %s;"
                " run cousin-spawn --repair-settings %s" % (err, home.name))
    except Exception as err:  # noqa: BLE001 - worded for the operator, the toml stays
        raise HarnessFailed("cousin.toml was written (%s); the harness settings"
                            " (.claude/settings.json) were not: %s: %s"
                            % (", ".join("%s = %s" % (k, json_value(v))
                                         for k, v in written.items()),
                               type(err).__name__, err), written)
    note = "the harness settings (.claude/settings.json) were updated too"
    try:
        want = fwconfig.commit_attribution(root, _read(home).get("agent") or {})
    except fwconfig.MissingConfigError:
        return note
    wins = _attribution_overrides(home, want)
    if wins:
        note += ("; your own %s in .claude/settings.json %s and win%s: attribution stays %s"
                 % (" and ".join(wins), "are" if len(wins) > 1 else "is",
                    "" if len(wins) > 1 else "s", "on" if not want else "off"))
    return note


def json_value(value):
    import json
    return json.dumps(value)


class _Refused(ValueError):
    def __init__(self, errors):
        self.errors = dict(errors)
        super().__init__("; ".join("%s: %s" % kv for kv in self.errors.items()))


def describe_agent(home, root):
    """agent_settings.describe, plus the hold (<home>/run/held) and, on a
    lane that reads env_allow, the pane's base allowlist and hard deny."""
    from cousin_lib import agent_settings, supervisor
    out = agent_settings.describe(home, root)
    out["held"] = supervisor.is_held(home)
    row = out["settings"].get("env_allow")
    if row is not None:
        from cousin_lib.runner.tmux_launch import BASE_ENV, DENY_PREFIXES
        row["base"] = list(BASE_ENV) + ["LC_*"]
        row["deny_prefixes"] = list(DENY_PREFIXES)
    return out


def _needs_turn(home, changes):
    """Whether these [agent] changes carry an sdk model the cousin does not
    already have: the one change that spends a validating turn."""
    from cousin_lib import agent_settings
    agent = _read(home).get("agent") or {}
    model = changes.get("model")
    return (agent_settings.lane_of(agent) == agent_settings.TURN_LANE
            and isinstance(model, str) and model and agent.get("model") != model)


def _changes_of(req):
    changes = req.body.get("changes")
    if not isinstance(changes, dict) or not changes:
        raise HttpError(400, "changes must be an object of at least one key")
    return changes


def _refresh(server):
    from cousin_lib.console.routes_fleet import fleet_rows
    server.emit("cousins-refresh", fleet_rows(server))


def _after_write(home, root, changes, changed):
    """The harness step a written commit_attribution needs; its note."""
    if "commit_attribution" not in changed:
        return None
    return sync_harness_settings(home, root, {"agent.commit_attribution":
                                              changes.get("commit_attribution")})


def change_agent(server, slug, home, changes, *, answer):
    """A runner cousin's [agent] change, the one route logic behind POST
    /api/cousins/<slug>/agent and the fleet's /model and /effort: checked
    at once (400 with a reason per key), then spawn.persist_agent_values
    held exclusively on the cousin (409 while anything else runs on it).
    A new sdk model spends a validating turn, so it runs as the cousin's
    `agent-settings` long operation (202 {"op"}); its result is {changed,
    restart_required} or {ok: false, error, errors: {key: reason}}.
    `answer(changed, note)` shapes the 200 body."""
    from cousin_lib import agent_settings, spawn
    from cousin_lib.console import longop
    root = server.root
    try:
        # the runner's own checks, answered at once; the write path runs
        # them again on the file as it then is
        agent_settings.validate(home, root, changes)
    except agent_settings.SettingsError as err:
        raise HttpError(400, str(err), errors=err.errors)
    if _needs_turn(home, changes):
        def work(op):
            op.stage("validating turn", "running",
                     "one smallest turn with model %s on the account being written"
                     % changes["model"])
            try:
                changed = spawn.persist_agent_values(home, changes, root=root)
                note = _after_write(home, root, changes, changed)
            except agent_settings.SettingsError as err:
                return {"ok": False, "error": str(err), "errors": err.errors}
            except (spawn.SpawnError, HarnessFailed, ValueError, OSError) as err:
                return {"ok": False, "error": str(err), "errors": {"changes": str(err)}}
            op.stage("validating turn", "done")
            op.stage("write", "done", ", ".join(changed) or "nothing changed")
            if changed:
                _refresh(server)
            out = {"ok": True, "changed": changed, "restart_required": bool(changed)}
            if note:
                out["note"] = note
            return out
        return longop.start_response(server, slug, OP_KIND, work,
                                     params={"keys": sorted(changes)})
    try:
        hold = longop.exclusive(server, slug, OP_KIND)
    except longop.Busy as err:
        raise HttpError(409, str(err), busy=True)
    with hold:
        try:
            changed = spawn.persist_agent_values(home, changes, root=root)
            note = _after_write(home, root, changes, changed)
        except agent_settings.SettingsError as err:
            raise HttpError(400, str(err), errors=err.errors)
        except spawn.SpawnError as err:
            raise HttpError(400, str(err))
        except HarnessFailed as err:
            raise HttpError(500, str(err), written=err.written)
    if changed:
        _refresh(server)
    return 200, answer(changed, note)


def register():
    from cousin_lib.console import longop
    from cousin_lib.console._common import cousin_home

    @router.route("GET", "/api/cousins/{slug}/agent")
    def get_agent(req, slug):
        home = cousin_home(req.server, slug)
        return 200, {"ok": True, "slug": slug, **describe_agent(home, req.server.root)}

    @router.route("POST", "/api/cousins/{slug}/agent")
    def set_agent(req, slug):
        server = req.server
        home = cousin_home(server, slug)
        changes = _changes_of(req)

        def answer(changed, note):
            out = {"ok": True, "slug": slug, "changed": changed,
                   "restart_required": bool(changed),
                   "agent": describe_agent(home, server.root)}
            if note:
                out["note"] = note
            return out
        return change_agent(server, slug, home, changes, answer=answer)

    @router.route("GET", "/api/cousins/{slug}/settings")
    def get_settings(req, slug):
        home = cousin_home(req.server, slug)
        return 200, {"ok": True, "slug": slug, **describe_settings(home, req.server.root)}

    @router.route("POST", "/api/cousins/{slug}/settings")
    def set_settings(req, slug):
        server = req.server
        home = cousin_home(server, slug)
        changes = _changes_of(req)
        try:
            hold = longop.exclusive(server, slug, "cousin settings")
        except longop.Busy as err:
            raise HttpError(409, str(err), busy=True)
        with hold:
            try:
                changed, note = write_settings(home, server.root, changes)
            except _Refused as err:
                raise HttpError(400, str(err), errors=err.errors)
            except HarnessFailed as err:
                _refresh(server)
                raise HttpError(500, str(err), written=err.written)
        if changed:
            _refresh(server)
        out = {"ok": True, "slug": slug, "changed": changed,
               "restart_required": any(COUSIN_FIELDS[n]["restart"] for n in changed),
               "settings": describe_settings(home, server.root)}
        if note:
            out["note"] = note
        return 200, out


register()
