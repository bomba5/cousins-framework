"""Console routes for WP-F, system and install config: the supervisor, schedules, console users, backup and the install config editors.

A package seam (app.py PACKAGE_ROUTE_MODULES): this module is the
package's own. Its routes are registered in register() below with
`@router.route(METHOD, "/api/...")`; per-server state lives on
req.server.state, never at module level. Long-running work goes through
console/longop.py and a pasted secret through console/secrets.py. The
browser side is system.jsx (index.html's package block).

What each part calls, never re-implements:

- the supervisor: supervisor.request (status, start, stop, reload) over
  its own socket. The console is never stopped from here (it would take
  this page with it; its own restart route is the way), the loops
  daemon only with `confirm: "loops"`, a runner cousin under the same
  exclusive mark the fleet's start and stop take, and a Telegram bridge
  not at all (it follows its runner).
- schedules: cousin_lib/schedule add, list_entries, cancel.
- console users: auth.Users. Passwords are write-only; the last user and
  the session's own user are never removed, and a reset of your own
  password goes through change-password, which asks for the old one.
- backup: backup.snapshot per cousin, each a LongOp (kind "backup") and a
  row in the jobs store, into a destination checked here: absolute,
  an existing writable directory, never inside the live root's cousins/,
  config/ or .secrets/.
- the install config: every TOML edit through toml_edit.write_file_keys,
  checked first by the file's own loader against the edited text
  (hive.hive_config, chat.load_external_peers, memory_search's embedding
  reader, config.agent_config / commit_attribution / harness_config); a
  key file or a peer token only ever through secrets.write_secret_file,
  at a path this module picks, so a path field is never free text. The
  JSON and Markdown files (outbound-filter.json, net-allowlist.json,
  law.md) are validated, backed up under data/config-backups/ and
  renamed into place, refused when they changed since they were read.
  agent-cmd and worker-cmd are shown, never written."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import os
import re
import tempfile
import threading
import time
import tomllib
import urllib.parse
from pathlib import Path

from cousin_lib.console import router  # noqa: F401 - the package's routes use it

# ---- shared helpers -------------------------------------------------------

USER_MAX_CHARS = 1024
_PEER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
PROMPT_MAX_CHARS = 8000
TEXT_MAX_CHARS = 256 * 1024
BACKUPS_KEPT = 20
MEDIA_KINDS = ("image", "voice", "video")
COMMAND_FILES = ("agent-cmd", "worker-cmd")
CONSOLE_RESTART_ROUTE = "/api/admin/restart/framework"


def _http_error():
    from cousin_lib.console.app import HttpError
    return HttpError


def _err(status, message, **extra):
    return _http_error()(status, message, **extra)


def _lock(server, name):
    return server.state.setdefault("system_lock_" + name, threading.Lock())


def _sha(data):
    return hashlib.sha256(data).hexdigest()[:16] if data is not None else ""


def _read_bytes(path):
    try:
        return Path(path).read_bytes()
    except FileNotFoundError:
        return None


# ---- the supervisor --------------------------------------------------------

def _child_kind(name):
    return name.split(":", 1)[0] if ":" in name else name


def _child_actions(name):
    kind = _child_kind(name)
    if kind == "console":
        return {"start": False, "stop": False, "restart": True,
                "why": "the console stops only through its own restart"}
    if kind == "loops":
        return {"start": True, "stop": True, "restart": False, "confirm": "loops",
                "why": "stopping the loops daemon stops every heartbeat, loop and schedule"}
    if kind == "runner":
        return {"start": True, "stop": True, "restart": False}
    return {"start": False, "stop": False, "restart": False,
            "why": "a bridge follows its runner cousin"}


def supervisor_status(server):
    from cousin_lib import supervisor
    supervised = bool(os.environ.get("COUSIN_SUPERVISED"))
    try:
        answer = supervisor.request(server.root, "status", timeout=3.0)
    except supervisor.SupervisorAbsent as err:
        return {"ok": True, "running": False, "supervised": supervised, "reason": str(err),
                "children": []}
    except supervisor.SupervisorUnavailable as err:
        return {"ok": True, "running": None, "supervised": supervised, "reason": str(err),
                "children": []}
    rows = []
    for name, row in sorted((answer.get("children") or {}).items()):
        if not isinstance(row, dict):
            continue
        rows.append(dict(row, name=name, kind=_child_kind(name), actions=_child_actions(name)))
    return {"ok": True, "running": True, "supervised": supervised, "pid": answer.get("pid"),
            "started": answer.get("started"), "children": rows}


def _supervisor_target(child):
    """(request args, runner slug or None) for a child name, or 400."""
    if not isinstance(child, str) or not child:
        raise _err(400, "child is required")
    if child in ("console", "loops"):
        return {"name": child}, None
    if child.startswith("runner:"):
        slug = child[len("runner:"):]
        return {"slug": slug}, slug
    raise _err(400, "%s cannot be started or stopped here: a bridge follows its runner"
               " cousin; a runner cousin is runner:<slug>" % child)


def _supervisor_call(server, op, timeout, **args):
    from cousin_lib import supervisor
    try:
        answer = supervisor.request(server.root, op, timeout=timeout, **args)
    except supervisor.SupervisorUnavailable as err:
        raise _err(503, str(err))
    if not answer.get("ok"):
        raise _err(409, answer.get("error") or "the supervisor refused", answer=answer)
    return answer


def _refresh_fleet(server):
    try:
        from cousin_lib.console.routes_fleet import fleet_rows
        server.emit("cousins-refresh", fleet_rows(server))
    except Exception:  # noqa: BLE001 - a refresh is a courtesy
        pass


def supervisor_child(req, op):
    from cousin_lib.console import longop
    from cousin_lib.console._common import cousin_home
    server = req.server
    child = req.body.get("child")
    if child == "console":
        raise _err(400, "the console is never started or stopped from its own page:"
                        " use its restart (%s)" % CONSOLE_RESTART_ROUTE,
                   restart_route=CONSOLE_RESTART_ROUTE)
    args, slug = _supervisor_target(child)
    if op == "stop":
        if child == "loops" and req.body.get("confirm") != "loops":
            raise _err(400, "stopping the loops daemon stops every heartbeat, loop and"
                            " scheduled prompt; confirm with confirm: \"loops\"")
        args.update(wait=False, by="console %s" % (req.user or "(no auth)"))
    if slug is None:
        return 200, _supervisor_call(server, op, 10.0, **args)
    cousin_home(server, slug)
    try:
        hold = longop.exclusive(server, slug, op)
    except longop.Busy as err:
        raise _err(409, str(err), busy=True)
    with hold:
        answer = _supervisor_call(server, op, 10.0, **args)
    _refresh_fleet(server)
    return 200, answer


# ---- schedules -------------------------------------------------------------

def _schedule_rows(rows):
    out = []
    for row in rows:
        out.append(dict(row, when=time.strftime("%Y-%m-%dT%H:%M:%S%z",
                                                time.localtime(row["target_ts"]))))
    return out


# ---- console users ---------------------------------------------------------

def _check_user(name, *, from_path=False):
    """A console user's name as `cousin-console adduser` takes it: any
    non-empty string (argv never holds a NUL), kept exactly as given. In a
    path it arrives percent-encoded."""
    if from_path and isinstance(name, str):
        name = urllib.parse.unquote(name)
    if not isinstance(name, str) or not name or "\x00" in name or len(name) > USER_MAX_CHARS:
        raise _err(400, "a user name is a non-empty string without NUL, at most %d"
                        " characters" % USER_MAX_CHARS)
    return name


def _check_password(value):
    from cousin_lib.console import auth
    if not isinstance(value, str) or len(value) < auth.MIN_PASSWORD_CHARS:
        raise _err(400, "the password must be at least %d characters"
                   % auth.MIN_PASSWORD_CHARS)
    if len(value) > 1024:
        raise _err(400, "the password is longer than 1024 characters")
    return value


def _users_or_error(server):
    from cousin_lib.console import auth
    try:
        return server.users.load(), server.users.configured()
    except auth.UsersFileError as err:
        raise _err(503, str(err))


# ---- backup ----------------------------------------------------------------

def _under(path, base):
    return path == base or path.startswith(base.rstrip(os.sep) + os.sep)


def _refuse_in_root(root, target):
    """ValueError when `target`, symlinks resolved, is the live root or
    inside it (its cousins/, config/ and .secrets/ named, being the worst)."""
    real = os.path.realpath(target)
    for sub in ("cousins", "config", ".secrets"):
        bad = os.path.realpath(os.path.join(str(root), sub))
        if _under(real, bad):
            raise ValueError("the destination may not be inside %s: a backup there"
                             " writes into the live install" % bad)
    base = os.path.realpath(str(root))
    if _under(real, base):
        raise ValueError("the destination may not be inside the install (%s): keep"
                         " backups off the live root" % base)
    return real


def check_backup_dest(root, dest, slugs=()):
    """The destination as a real path, or ValueError: absolute, an
    existing directory the console can write, and neither it nor any
    snapshot directory under it (<dest>/<slug>, each realpath'd) inside
    the live root at all (symlinks resolved)."""
    if not isinstance(dest, str) or not dest.strip() or "\x00" in dest:
        raise ValueError("the destination is required")
    if not os.path.isabs(dest):
        raise ValueError("the destination must be an absolute path")
    real = _refuse_in_root(root, dest)
    for slug in slugs:
        _refuse_in_root(root, os.path.join(real, slug))
    if not os.path.isdir(real):
        raise ValueError("%s is not an existing directory" % real)
    if not os.access(real, os.W_OK | os.X_OK):
        raise ValueError("%s is not writable by the console" % real)
    return real


def _owner_only(top):
    """Every directory under `top` 0700 and every file 0600; a symlink is
    left alone (never followed)."""
    os.chmod(top, 0o700)
    for base, dirs, files in os.walk(top):
        for name in dirs + files:
            path = os.path.join(base, name)
            if os.path.islink(path):
                continue
            os.chmod(path, 0o700 if os.path.isdir(path) else 0o600)


def _backup_work(root, home, slug, dest):
    def work(op):
        from cousin_lib import backup, jobs
        from cousin_lib.console.longop import OpError
        op.stage("snapshot", "running", dest)
        top = os.path.join(dest, slug)
        with jobs.track_job("backup", "backup %s" % slug, description="into %s" % dest,
                            spawned_by=slug) as job:
            # <dest>/<slug> first, 0700, checked again now it exists: the
            # snapshot's own files are then never readable by others, even
            # before the pass that tightens them.
            try:
                os.makedirs(top, mode=0o700, exist_ok=True)
                os.chmod(top, 0o700)
                _refuse_in_root(root, top)
            except (OSError, ValueError) as err:
                raise OpError("backup of %s refused: %s" % (slug, err))
            try:
                snap = backup.snapshot(home, dest)
            except backup.BackupError as err:
                raise OpError("backup of %s failed: %s" % (slug, err))
            _owner_only(top)
            job.summary = str(snap)
        op.stage("snapshot", "done", str(snap))
        return {"ok": True, "snapshot": str(snap)}
    return work


# ---- install config: value checks ----------------------------------------

def _is_num(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(value)


def _check_value(kind, value, where):
    """The value normalized for `kind`, or ValueError. A key is removed
    only by an explicit `remove`, never by a missing, null or empty value:
    a mistyped number must not delete the key."""
    if value is None:
        raise ValueError("%s needs a value (remove it with remove: true)" % where)
    if kind == "url":
        parsed = urllib.parse.urlsplit(value) if isinstance(value, str) else None
        if (parsed is None or parsed.scheme not in ("http", "https") or not parsed.hostname
                or any(c.isspace() for c in value) or len(value) > 2048):
            raise ValueError("%s must be an http(s) URL" % where)
        return value
    if kind == "str":
        if not isinstance(value, str) or not value or "\n" in value or "\r" in value \
                or len(value) > 256:
            raise ValueError("%s must be one non-empty line of at most 256 characters" % where)
        return value
    if kind == "bool":
        if not isinstance(value, bool):
            raise ValueError("%s must be true or false" % where)
        return value
    if kind == "posnum":
        if not _is_num(value) or value <= 0:
            raise ValueError("%s must be a positive number" % where)
        return value
    if kind in ("posint", "nonnegint", "int"):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("%s must be a whole number" % where)
        if kind == "posint" and value <= 0 or kind == "nonnegint" and value < 0:
            raise ValueError("%s must be %s" % (where, "positive" if kind == "posint"
                                                 else "zero or more"))
        return value
    if kind == "unit":
        if not _is_num(value) or not 0 <= value <= 1:
            raise ValueError("%s must be a number from 0 to 1" % where)
        return float(value)
    if kind == "strlist":
        if not isinstance(value, list) or len(value) > 200 or not all(
                isinstance(v, str) and v and "\n" not in v and len(v) <= 64 for v in value):
            raise ValueError("%s must be a list of short names" % where)
        return list(value)
    raise ValueError("%s: unknown kind %s" % (where, kind))


_CHECK_ROOT = re.compile(r"\S*console-config-check-[^/\s]*/config/")


def _scrub(text):
    """A loader's message with the check's temp root taken out."""
    return _CHECK_ROOT.sub("config/", str(text))


def _with_config(text, filename, fn):
    """fn(temp root) with <temp root>/config/<filename> holding `text`: a
    loader that takes a root reads exactly the edited text."""
    with tempfile.TemporaryDirectory(prefix="console-config-check-") as tmp:
        (Path(tmp) / "config").mkdir()
        (Path(tmp) / "cousins").mkdir()
        (Path(tmp) / "config" / filename).write_text(text)
        return fn(Path(tmp))


def _check_media(text):
    data = tomllib.loads(text)
    for kind in MEDIA_KINDS:
        section = data.get(kind)
        if section is None:
            continue
        if not isinstance(section, dict):
            raise ValueError("[%s] must be a table" % kind)
        if section.get("url") is not None:
            _check_value("url", section["url"], "%s.url" % kind)
        if section.get("model") is not None:
            _check_value("str", section["model"], "%s.model" % kind)
        if section.get("timeout_s") is not None:
            _check_value("posnum", section["timeout_s"], "%s.timeout_s" % kind)
        key_file = section.get("key_file")
        if key_file is not None and (not isinstance(key_file, str) or os.path.isabs(key_file)
                                     or ".." in Path(key_file).parts):
            raise ValueError("%s.key_file must be a path under the root" % kind)


def _check_embedding(text):
    from cousin_lib import memory_search
    config = _with_config(text, "embedding.toml", memory_search._embedding_config)
    if config == "broken":
        raise ValueError("embedding.toml needs a url: without one the semantic leg"
                         " reads the file as broken")
    chars, overlap = config.get("chunk_chars"), config.get("chunk_overlap")
    if _is_num(chars) and _is_num(overlap) and overlap >= chars:
        raise ValueError("chunk_overlap must be smaller than chunk_chars")


def _check_hive(text):
    from cousin_lib import hive
    try:
        _with_config(text, "hive.toml", hive.hive_config)
    except hive.HiveConfigError as err:
        raise ValueError(_scrub(err))


def _check_peers(text):
    from cousin_lib import chat
    from cousin_lib.config import MissingConfigError
    try:
        _with_config(text, "external-peers.toml", chat.load_external_peers)
    except MissingConfigError as err:
        raise ValueError(_scrub(err))


# name -> the file, the keys a route may set (a table pattern, a key, a
# kind), the tables that may be removed, the loader check, what reads it.
TOML_FILES = {
    "media": {
        "file": "media.toml",
        "fields": {(r"^(image|voice|video)$", "url"): "url",
                   (r"^(image|voice|video)$", "model"): "str",
                   (r"^(image|voice|video)$", "timeout_s"): "posnum"},
        "removable": r"^(image|voice|video)$",
        "check": _check_media,
        "applies": "read at every generation: no restart",
    },
    "embedding": {
        "file": "embedding.toml",
        "fields": {(r"^$", "url"): "url", (r"^$", "model"): "str",
                   (r"^$", "timeout_s"): "posnum", (r"^$", "chunk_chars"): "posint",
                   (r"^$", "chunk_overlap"): "nonnegint",
                   (r"^options$", "num_thread"): "posint",
                   (r"^recall$", "min_chars"): "nonnegint",
                   (r"^recall$", "min_score"): "unit", (r"^recall$", "top"): "posint"},
        "removable": None,
        "check": _check_embedding,
        "applies": "read at every search and recall: no restart",
    },
    "hive": {
        "file": "hive.toml",
        "fields": {(r"^$", "enabled"): "bool", (r"^$", "public_url"): "url",
                   (r"^$", "checkin_seconds"): "int", (r"^$", "home_cousin"): "str",
                   (r"^$", "home_chat_url"): "url"},
        "removable": None,
        "check": _check_hive,
        "applies": "read at every hive request: no restart",
    },
    "peers": {
        "file": "external-peers.toml",
        "fields": {(r"^peers\.[A-Za-z0-9][A-Za-z0-9_-]{0,63}$", "url"): "url",
                   (r"^peers\.[A-Za-z0-9][A-Za-z0-9_-]{0,63}$", "send_path"): "str",
                   (r"^peers\.[A-Za-z0-9][A-Za-z0-9_-]{0,63}$", "name"): "str",
                   (r"^peers\.[A-Za-z0-9][A-Za-z0-9_-]{0,63}$", "sender"): "str",
                   (r"^peers\.[A-Za-z0-9][A-Za-z0-9_-]{0,63}$", "reach"): "strlist"},
        "removable": r"^peers\.[A-Za-z0-9][A-Za-z0-9_-]{0,63}$",
        "check": _check_peers,
        "applies": "read at every send and every inbound peer message: no restart",
    },
}


def _config_path(server, filename):
    return Path(server.root) / "config" / filename


def _field_kind(spec, table, key):
    for (pattern, name), kind in spec["fields"].items():
        if name == key and isinstance(table, str) and re.match(pattern, table):
            return kind
    return None


def edit_toml(server, name, changes, remove_tables=(), *, extra_check=None):
    """Apply (table, key, value) changes and table removals to the named
    install TOML file, checked by its loader on the edited text, then
    written through toml_edit.write_file_keys (created when absent).
    ValueError with the reason when refused; nothing written then."""
    from cousin_lib.console import toml_edit
    spec = TOML_FILES[name]
    path = _config_path(server, spec["file"])
    with _lock(server, "config"):
        raw = _read_bytes(path)
        text = raw.decode("utf-8") if raw is not None else ""
        for table in remove_tables:
            text = toml_edit.remove_table(text, table)
        for table, key, value in changes:
            text = toml_edit.set_key(text, table, key, value)
        try:
            expected = tomllib.loads(text)
        except tomllib.TOMLDecodeError as err:
            raise ValueError("the edited file does not parse: %s" % err)
        spec["check"](text)
        if extra_check is not None:
            extra_check(expected)

        def same(parsed):
            if parsed != expected:
                raise ValueError("%s changed while it was being edited" % spec["file"])
        return toml_edit.write_file_keys(path, changes, validate=same, create=True,
                                         remove_tables=list(remove_tables))


def _toml_changes(spec, body):
    """(changes, remove_tables) from a request body, every key checked
    against the file's field table; ValueError otherwise."""
    raw = body.get("changes", [])
    if not isinstance(raw, list) or len(raw) > 50:
        raise ValueError("changes must be a list")
    changes = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("each change is {table, key, value}")
        table, key = item.get("table", ""), item.get("key")
        kind = _field_kind(spec, table, key)
        if kind is None:
            raise ValueError("%s is not a key this editor sets"
                             % (("%s.%s" % (table, key)) if table else key))
        where = ("%s.%s" % (table, key)) if table else key
        remove = item.get("remove", False)
        if not isinstance(remove, bool):
            raise ValueError("%s: remove must be true or false" % where)
        if remove:
            if "value" in item:
                raise ValueError("%s: a value or remove, not both" % where)
            changes.append((table, key, None))
        else:
            changes.append((table, key, _check_value(kind, item.get("value"), where)))
    removes = body.get("remove_tables", [])
    if not isinstance(removes, list) or not all(isinstance(t, str) for t in removes):
        raise ValueError("remove_tables must be a list of table names")
    for table in removes:
        if not spec["removable"] or not re.match(spec["removable"], table):
            raise ValueError("[%s] cannot be removed here" % table)
    return changes, removes


def _parse_toml_file(path):
    raw = _read_bytes(path)
    if raw is None:
        return False, {}, None
    try:
        return True, tomllib.loads(raw.decode("utf-8")), None
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as err:
        return True, {}, "does not parse: %s" % err


def _secret_state_rel(server, rel, reader):
    """What may be shown about a key or token file the config names, read
    the way its own reader reads it (`reader`: "media" is media._read_key,
    a plain read relative to the root; "peer" is chat.read_secret, which
    refuses a file group or others can read and says so). Read only when
    it resolves under the root's config/: a path set by hand to some other
    file is never opened for its last four. Never the value."""
    from cousin_lib import chat
    from cousin_lib.config import MissingConfigError
    from cousin_lib.console.secrets import LAST4_MIN_LEN
    if not isinstance(rel, str) or not rel:
        return None
    root = str(server.root)
    path = os.path.join(root, rel)
    if not _under(os.path.realpath(path), os.path.realpath(os.path.join(root, "config"))):
        return {"set": None, "last4": None,
                "error": "outside the install's config/: not read here"}
    if not os.path.exists(path):
        return {"set": False, "last4": None, "error": "%s does not exist" % rel}
    try:
        if reader == "peer":
            value = chat.read_secret(root, rel, "token file")
        else:
            value = Path(path).read_text().strip()
    except MissingConfigError as err:
        return {"set": False, "last4": None, "error": str(err)}
    except (OSError, UnicodeDecodeError) as err:
        return {"set": False, "last4": None, "error": "%s is unreadable: %s" % (rel, err)}
    if not value:
        return {"set": False, "last4": None, "error": "%s is empty" % rel}
    return {"set": True, "last4": value[-4:] if len(value) >= LAST4_MIN_LEN else None,
            "error": None}


def _file_state(server, name):
    spec = TOML_FILES[name]
    path = _config_path(server, spec["file"])
    exists, data, error = _parse_toml_file(path)
    out = {"path": "config/" + spec["file"], "exists": exists, "error": error,
           "applies": spec["applies"], "restart": None}
    if error is None and exists:
        try:
            spec["check"](path.read_text())
        except (ValueError, tomllib.TOMLDecodeError) as err:
            out["error"] = str(err)
    if name == "media":
        kinds = {}
        for kind in MEDIA_KINDS:
            section = data.get(kind) if isinstance(data.get(kind), dict) else None
            if section is None:
                kinds[kind] = None
                continue
            kinds[kind] = {"url": section.get("url"), "model": section.get("model"),
                           "timeout_s": section.get("timeout_s"),
                           "key_file": section.get("key_file"),
                           "key": _secret_state_rel(server, section.get("key_file"), "media")}
        out["kinds"] = kinds
    elif name in ("embedding", "hive"):
        values = {}
        for (pattern, key) in spec["fields"]:
            table = pattern.strip("^$")
            node = data if not table else data.get(table)
            label = ("%s.%s" % (table, key)) if table else key
            values[label] = node.get(key) if isinstance(node, dict) else None
        out["values"] = values
    elif name == "peers":
        peers = {}
        local = {p.name for p in (Path(server.root) / "cousins").glob("*")
                 if (p / "cousin.toml").is_file()}
        section = data.get("peers") if isinstance(data.get("peers"), dict) else {}
        for slug, entry in sorted(section.items()):
            if not isinstance(entry, dict):
                continue
            peers[slug] = {
                "url": entry.get("url"), "send_path": entry.get("send_path"),
                "name": entry.get("name"), "sender": entry.get("sender"),
                "reach": entry.get("reach") or [],
                "token_file": entry.get("token_file"),
                "inbound_token_file": entry.get("inbound_token_file"),
                "token": _secret_state_rel(server, entry.get("token_file"), "peer"),
                "inbound_token": _secret_state_rel(server, entry.get("inbound_token_file"),
                                                   "peer"),
                "shadowed": slug in local,
                "unknown_reach": sorted(set(entry.get("reach") or []) - local)
                if isinstance(entry.get("reach"), list) else [],
            }
        out["peers"] = peers
    return out


# ---- install config: secret files ----------------------------------------

def _secret_rel(name, target, which=None):
    if name == "media":
        return "config/media-keys/%s.key" % target
    return "config/peer-tokens/%s%s.token" % (target, ".inbound" if which == "inbound" else "")


def _restore_secret(full, previous):
    """Put a secret file back as it was before a failed save: its old
    bytes through the same private writer (0600, atomic), or gone."""
    from cousin_lib import accounts
    try:
        if previous is None:
            os.unlink(full)
        else:
            accounts._write_private_text(Path(full), previous.decode("utf-8"))
    except OSError:
        pass


def set_secret(server, name, target, value, which=None):
    """Write the secret for a media kind's key_file or a peer's token file
    at the path this module picks (secrets.write_secret_file), then point
    the TOML key at it through edit_toml, which checks the change with the
    file's loader. The secret is written first; when the TOML write then
    fails, the file is put back as it was (removed when there was none),
    so a failed save leaves both files unchanged. Returns the key's state
    as its reader sees it."""
    from cousin_lib.console import secrets
    rel = _secret_rel(name, target, which)
    table = target if name == "media" else "peers.%s" % target
    key = "key_file" if name == "media" else (
        "inbound_token_file" if which == "inbound" else "token_file")
    spec = TOML_FILES[name]
    path = _config_path(server, spec["file"])
    exists, data, _error = _parse_toml_file(path)
    if name == "peers" and not isinstance((data.get("peers") or {}).get(target), dict):
        raise LookupError("no peer %s in external-peers.toml: add it first" % target)
    full = os.path.join(str(server.root), rel)
    previous = _read_bytes(full)
    state = secrets.write_secret_file(full, value, within=os.path.join(str(server.root), "config"))
    try:
        from cousin_lib.console import toml_edit
        if toml_edit._lookup(data, table, key) != (True, rel):
            edit_toml(server, name, [(table, key, rel)])
    except BaseException:
        _restore_secret(full, previous)
        raise
    return _secret_state_rel(server, rel, "media" if name == "media" else "peer") or state


def clear_secret(server, name, target, which=None):
    table = target if name == "media" else "peers.%s" % target
    key = "key_file" if name == "media" else (
        "inbound_token_file" if which == "inbound" else "token_file")
    spec = TOML_FILES[name]
    exists, data, _error = _parse_toml_file(_config_path(server, spec["file"]))
    from cousin_lib.console import toml_edit
    present, current = toml_edit._lookup(data, table, key)
    if present:
        edit_toml(server, name, [(table, key, None)])
    rel = _secret_rel(name, target, which)
    if not present or current == rel:
        try:
            os.unlink(os.path.join(str(server.root), rel))
        except OSError:
            pass
    return {"set": False, "last4": None, "error": None}


# ---- install config: JSON and text files ----------------------------------

def _backup_file(server, path):
    raw = _read_bytes(path)
    if raw is None:
        return None
    base = Path(server.root) / "data" / "config-backups"
    base.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S")
    target = base / ("%s-%s-%d%s" % (path.stem, stamp, time.time_ns() % 1000000, path.suffix))
    target.write_bytes(raw)
    os.chmod(target, 0o600)
    old = sorted(base.glob("%s-*%s" % (path.stem, path.suffix)))
    for stale in old[:-BACKUPS_KEPT]:
        try:
            stale.unlink()
        except OSError:
            pass
    return str(target.relative_to(server.root))


def _write_text(server, path, text, base_sha):
    """Refuse (409) when the file changed since it was read (`base_sha`,
    "" for a file that was absent), back it up, then rename the new text
    into place with the old mode (0600 for a new file)."""
    with _lock(server, "config"):
        raw = _read_bytes(path)
        if base_sha is not None and base_sha != _sha(raw):
            raise _err(409, "%s changed since it was loaded: reload and edit again" % path.name,
                       stale=True)
        backup = _backup_file(server, path)
        mode = (path.stat().st_mode & 0o7777) if raw is not None else 0o600
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix="." + path.name + ".",
                                   suffix=".tmp")
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
        return backup, _sha(text.encode("utf-8"))


def check_outbound_filter(data):
    """The keys OutboundPolicy.load reads, in the shape it reads them: it
    swallows an unparsable file into an inert filter and iterates a string
    as its characters, so this refuses what it would misread. Keys it
    ignores (a comment, an extra key in a surface) are accepted as the
    loader accepts them."""
    if not isinstance(data, dict):
        raise ValueError("the filter is a JSON object")
    for key in ("terms", "protected", "trusted_peers"):
        value = data.get(key) or []
        if not isinstance(value, list) or not all(isinstance(v, str) and v.strip()
                                                  for v in value):
            raise ValueError("%s must be a list of non-empty strings" % key)
    surfaces = data.get("surfaces") or {}
    if not isinstance(surfaces, dict):
        raise ValueError("surfaces must be an object of {\"add\": [terms]}")
    for name, entry in surfaces.items():
        if not isinstance(entry, dict):
            raise ValueError("surfaces.%s must be {\"add\": [terms]}" % name)
        add = entry.get("add") or []
        if not isinstance(add, list) or not all(isinstance(v, str) and v.strip() for v in add):
            raise ValueError("surfaces.%s.add must be a list of non-empty strings" % name)


def _networks(values):
    if not isinstance(values, list) or len(values) > 200:
        raise ValueError("allow must be a list of CIDRs")
    out = []
    for value in values:
        if not isinstance(value, str):
            raise ValueError("allow must be a list of CIDRs")
        try:
            net = ipaddress.ip_network(value.strip())
        except ValueError as err:
            raise ValueError("%r is not a network the guard reads (%s)" % (value, err))
        if net.prefixlen == 0:
            raise ValueError("%s admits every address: the guard is the console's only"
                             " boundary when no user is configured" % value)
        out.append(str(net))
    return out


def _harness_values(server):
    from cousin_lib import config as fwconfig
    path = _config_path(server, "harness.toml")
    exists, data, error = _parse_toml_file(path)
    agent = data.get("agent") if isinstance(data.get("agent"), dict) else {}
    src = "config/harness.toml [agent]"
    values = {}
    for key in ("default_model", "default_effort", "commit_attribution"):
        if key in agent:
            values[key] = {"value": agent[key], "source": src}
        elif key == "commit_attribution":
            values[key] = {"value": True, "source": "built-in default (the harness's stock"
                                                    " attribution)"}
        else:
            values[key] = {"value": None, "source": "unset: each cousin's own value, else"
                                                    " a spawn error for a {%s} placeholder"
                                                    % key.split("_", 1)[1]}
    catalogue = list(fwconfig.DEFAULT_MODELS)
    if error is None and exists:
        try:
            catalogue = fwconfig.agent_config(server.root)["models"]
            fwconfig.commit_attribution(server.root)
        except fwconfig.MissingConfigError as err:
            error = str(err)
    return {"ok": True, "path": "config/harness.toml", "exists": exists, "error": error,
            "values": values, "choices": {"effort": list(fwconfig.EFFORT_LEVELS),
                                          "models": catalogue},
            "applies": "read when a cousin starts or is spawned: a running cousin keeps"
                       " what it started with; restart it from its inspector"}


def _check_harness(text):
    from cousin_lib import config as fwconfig

    def run(root):
        fwconfig.agent_config(root)
        fwconfig.commit_attribution(root)
        fwconfig.harness_config(root)
    try:
        _with_config(text, "harness.toml", run)
    except fwconfig.MissingConfigError as err:
        raise ValueError(_scrub(err))


# ---- routes ----------------------------------------------------------------

def register():
    from cousin_lib.console._common import cousin_home

    # -- the supervisor --

    @router.route("GET", "/api/system/supervisor")
    def get_supervisor(req):
        return 200, supervisor_status(req.server)

    @router.route("POST", "/api/system/supervisor/start")
    def start_child(req):
        return supervisor_child(req, "start")

    @router.route("POST", "/api/system/supervisor/stop")
    def stop_child(req):
        return supervisor_child(req, "stop")

    @router.route("POST", "/api/system/supervisor/reload")
    def reload_children(req):
        answer = _supervisor_call(req.server, "reload", 30.0)
        _refresh_fleet(req.server)
        return 200, answer

    # -- schedules --

    @router.route("GET", "/api/system/schedules")
    def all_schedules(req):
        from cousin_lib import schedule
        include = req.query.get("all") in ("1", "true")
        rows = schedule.list_entries(None, include_fired=include, limit=200 if include else None)
        return 200, {"ok": True, "schedules": _schedule_rows(rows)}

    @router.route("GET", "/api/cousins/{slug}/schedules")
    def cousin_schedules(req, slug):
        from cousin_lib import schedule
        cousin_home(req.server, slug)
        include = req.query.get("all") in ("1", "true")
        rows = schedule.list_entries(slug, include_fired=include, limit=100 if include else None)
        return 200, {"ok": True, "slug": slug, "schedules": _schedule_rows(rows)}

    @router.route("POST", "/api/cousins/{slug}/schedules")
    def add_schedule(req, slug):
        from cousin_lib import schedule
        cousin_home(req.server, slug)
        when, prompt = req.body.get("when"), req.body.get("prompt")
        if not isinstance(when, str) or not isinstance(prompt, str):
            raise _err(400, "when and prompt are required")
        if len(prompt) > PROMPT_MAX_CHARS:
            raise _err(400, "the prompt is longer than %d characters" % PROMPT_MAX_CHARS)
        try:
            row = schedule.add(slug, when, prompt)
        except ValueError as err:
            raise _err(400, str(err))
        return 201, {"ok": True, "schedule": _schedule_rows([dict(row, status="pending")])[0]}

    @router.route("POST", "/api/cousins/{slug}/schedules/{job_id}/cancel")
    def cancel_schedule(req, slug, job_id):
        from cousin_lib import schedule
        cousin_home(req.server, slug)
        if not job_id.isdigit():
            raise _err(400, "bad schedule id")
        if not schedule.cancel(int(job_id), slug=slug):
            raise _err(404, "no pending schedule #%s for %s" % (job_id, slug))
        return 200, {"ok": True, "id": int(job_id), "status": "cancelled"}

    # -- console users --

    @router.route("GET", "/api/system/users")
    def list_users(req):
        data, configured = _users_or_error(req.server)
        return 200, {"ok": True, "configured": configured, "users": sorted(data),
                     "me": req.user}

    @router.route("POST", "/api/system/users")
    def add_user(req):
        from cousin_lib.console import auth
        server = req.server
        name = _check_user(req.body.get("name"))
        password = _check_password(req.body.get("password"))
        with _lock(server, "users"):
            data, configured = _users_or_error(server)
            if name in data:
                raise _err(409, "%s is already a console user: reset the password instead"
                           % name)
            server.users.set_password(name, password)
        out = {"ok": True, "user": name, "users": sorted(list(data) + [name])}
        if not configured:
            # The first user closes the console to anyone without a session:
            # the browser that made it is logged in as it, not locked out.
            token = server.sessions.create(name, stamp=auth.user_stamp(server.users, name))
            out["logged_in"] = True
            return 201, out, {"Set-Cookie": auth.cookie_header(
                token, secure=server.secure_cookie)}
        return 201, out

    @router.route("POST", "/api/system/users/{name}/password")
    def reset_password(req, name):
        server = req.server
        name = _check_user(name, from_path=True)
        password = _check_password(req.body.get("password"))
        if req.user is not None and name == req.user:
            raise _err(400, "your own password changes with the current one"
                            " (Settings, account)")
        with _lock(server, "users"):
            data, _configured = _users_or_error(server)
            if name not in data:
                raise _err(404, "no console user %s" % name)
            server.users.set_password(name, password)
        return 200, {"ok": True, "user": name}

    @router.route("POST", "/api/system/users/{name}/remove")
    def remove_user(req, name):
        server = req.server
        name = _check_user(name, from_path=True)
        if req.body.get("confirm") != name:
            raise _err(400, "type the user's name to confirm the removal")
        if req.user is not None and name == req.user:
            raise _err(400, "you cannot remove the user you are logged in as")
        with _lock(server, "users"):
            data, _configured = _users_or_error(server)
            if name not in data:
                raise _err(404, "no console user %s" % name)
            try:
                server.users.remove(name)
            except ValueError as err:
                raise _err(409, "%s: the console would have no user left" % err)
        return 200, {"ok": True, "removed": name, "users": sorted(set(data) - {name})}

    # -- backup --

    @router.route("POST", "/api/system/backup")
    def backup_now(req):
        from cousin_lib.console import longop
        server = req.server
        slugs = req.body.get("slugs")
        registry = sorted(p.name for p in (Path(server.root) / "cousins").glob("*")
                          if (p / "cousin.toml").is_file())
        if slugs in (None, "all"):
            slugs = registry
        if not isinstance(slugs, list) or not slugs or not all(isinstance(s, str) for s in slugs):
            raise _err(400, "slugs is a list of cousins, or \"all\"")
        homes = {slug: cousin_home(server, slug) for slug in slugs}
        try:
            dest = check_backup_dest(server.root, req.body.get("dest"), slugs)
        except ValueError as err:
            raise _err(400, str(err))
        ops, busy = {}, {}
        for slug in slugs:
            try:
                ops[slug] = longop.start(server, slug, "backup",
                                         _backup_work(server.root, homes[slug], slug, dest),
                                         params={"dest": dest})
            except longop.Busy as err:
                busy[slug] = str(err)
        if not ops:
            raise _err(409, "every cousin asked for is busy", busy=busy)
        return 202, {"ok": True, "dest": dest, "ops": ops, "busy": busy}

    # -- install config --

    @router.route("GET", "/api/system/config")
    def get_config(req):
        server = req.server
        files = {name: _file_state(server, name) for name in TOML_FILES}
        for name, filename in (("outbound_filter", "outbound-filter.json"),
                               ("law", "law.md")):
            path = _config_path(server, filename)
            raw = _read_bytes(path)
            files[name] = {"path": "config/" + filename, "exists": raw is not None,
                           "content": raw.decode("utf-8", "replace") if raw is not None else "",
                           "sha": _sha(raw), "restart": None,
                           "applies": ("read at every send: no restart" if name != "law"
                                       else "read into each cousin's boot packet: a cousin"
                                            " sees it at its next start or flip")}
        path = _config_path(server, "net-allowlist.json")
        raw = _read_bytes(path)
        allow, error = [], None
        if raw is not None:
            try:
                data = json.loads(raw)
                allow = data.get("allow", []) if isinstance(data, dict) else []
            except ValueError as err:
                error = "not JSON: %s" % err
        files["allowlist"] = {
            "path": "config/net-allowlist.json", "exists": raw is not None, "error": error,
            "allow": allow, "sha": _sha(raw), "client": req.client,
            "builtin": ["127.0.0.0/8", "::1/128", "10.0.0.0/8", "172.16.0.0/12",
                        "192.168.0.0/16"],
            "applies": "read when the console and each chat server start",
            "restart": {"services": ["console", "chat servers"],
                        "route": CONSOLE_RESTART_ROUTE,
                        "note": "restart the console here; a cousin's chat server"
                                " restarts with the cousin"}}
        commands = {}
        for name in COMMAND_FILES:
            raw = _read_bytes(_config_path(server, name))
            commands[name] = {"path": "config/" + name, "exists": raw is not None,
                              "content": raw.decode("utf-8", "replace") if raw is not None
                              else ""}
        files["commands"] = commands
        return 200, {"ok": True, "files": files}

    @router.route("POST", "/api/system/config/{name}")
    def set_config(req, name):
        if name not in TOML_FILES:
            raise _err(404, "no install config editor named %s" % name)
        spec = TOML_FILES[name]
        try:
            changes, removes = _toml_changes(spec, req.body)
            if not changes and not removes:
                raise ValueError("nothing to change")
            edit_toml(req.server, name, changes, removes)
        except ValueError as err:
            raise _err(400, str(err))
        return 200, {"ok": True, "file": _file_state(req.server, name)}

    def _secret_target(name, target, which):
        if name == "media":
            if target not in MEDIA_KINDS:
                raise _err(404, "no media kind %s" % target)
            return None
        if name != "peers" or not _PEER_RE.match(target or ""):
            raise _err(404, "no secret here")
        if which not in ("outbound", "inbound"):
            raise _err(400, "which is outbound or inbound")
        return which

    @router.route("POST", "/api/system/config/{name}/{target}/secret")
    def put_secret(req, name, target):
        which = _secret_target(name, target, req.body.get("which", "outbound")
                               if name == "peers" else None)
        try:
            state = set_secret(req.server, name, target, req.body.get("value"), which)
        except LookupError as err:
            raise _err(404, str(err))
        except ValueError as err:
            raise _err(400, str(err))
        return 200, {"ok": True, "secret": state}

    @router.route("POST", "/api/system/config/{name}/{target}/secret/clear")
    def drop_secret(req, name, target):
        which = _secret_target(name, target, req.body.get("which", "outbound")
                               if name == "peers" else None)
        try:
            state = clear_secret(req.server, name, target, which)
        except ValueError as err:
            raise _err(400, str(err))
        return 200, {"ok": True, "secret": state}

    @router.route("POST", "/api/system/outbound-filter")
    def set_outbound_filter(req):
        content = req.body.get("content")
        if not isinstance(content, str) or len(content) > TEXT_MAX_CHARS:
            raise _err(400, "content must be the filter's JSON text")
        try:
            check_outbound_filter(json.loads(content))
        except ValueError as err:
            raise _err(400, "the filter was not saved: %s" % err)
        backup, sha = _write_text(req.server, _config_path(req.server, "outbound-filter.json"),
                                  content, req.body.get("base_sha"))
        return 200, {"ok": True, "backup": backup, "sha": sha}

    @router.route("POST", "/api/system/law")
    def set_law(req):
        content = req.body.get("content")
        if not isinstance(content, str) or len(content) > TEXT_MAX_CHARS:
            raise _err(400, "content must be text of at most %d characters" % TEXT_MAX_CHARS)
        backup, sha = _write_text(req.server, _config_path(req.server, "law.md"), content,
                                  req.body.get("base_sha"))
        return 200, {"ok": True, "backup": backup, "sha": sha}

    @router.route("POST", "/api/system/allowlist")
    def set_allowlist(req):
        from cousin_lib.server.netguard import NetGuard
        server = req.server
        try:
            allow = _networks(req.body.get("allow"))
        except ValueError as err:
            raise _err(400, str(err))
        if not NetGuard(allow)(req.client):
            raise _err(400, "your own address %s would no longer be allowed: keep a network"
                            " that holds it" % req.client)
        path = _config_path(server, "net-allowlist.json")
        raw = _read_bytes(path)
        data = {}
        if raw is not None:
            try:
                data = json.loads(raw)
            except ValueError:
                data = {}
            if not isinstance(data, dict):
                data = {}
        data["allow"] = allow
        backup, sha = _write_text(server, path, json.dumps(data, indent=2) + "\n",
                                  req.body.get("base_sha"))
        return 200, {"ok": True, "allow": allow, "backup": backup, "sha": sha,
                     "restart": {"services": ["console", "chat servers"],
                                 "route": CONSOLE_RESTART_ROUTE}}

    # -- harness.toml [agent] defaults --

    @router.route("GET", "/api/system/agent-defaults")
    def get_agent_defaults(req):
        return 200, _harness_values(req.server)

    @router.route("POST", "/api/system/agent-defaults")
    def set_agent_defaults(req):
        from cousin_lib import config as fwconfig
        from cousin_lib import spawn
        from cousin_lib.console import toml_edit
        server = req.server
        path = _config_path(server, "harness.toml")
        if not path.is_file():
            raise _err(409, "config/harness.toml is absent: copy one of the"
                            " config/harness.toml*.example files first")
        keys = ("default_model", "default_effort", "commit_attribution")
        removes = req.body.get("remove", [])
        if not isinstance(removes, list) or not all(k in keys for k in removes):
            raise _err(400, "remove is a list of %s" % ", ".join(keys))
        changes = [("agent", key, None) for key in removes]
        for key in keys:
            if key not in req.body:
                continue
            value = req.body[key]
            if key in removes:
                raise _err(400, "%s: a value or remove, not both" % key)
            if value is None or value == "":
                raise _err(400, "%s needs a value (remove it with remove: [\"%s\"])"
                           % (key, key))
            elif key == "default_model":
                try:
                    changes.append(("agent", key, spawn.check_runtime_value("model", value)))
                except spawn.SpawnError as err:
                    raise _err(400, str(err).replace("runtime.model", "default_model"))
            elif key == "default_effort":
                if value not in fwconfig.EFFORT_LEVELS:
                    raise _err(400, "default_effort must be one of %s"
                               % ", ".join(fwconfig.EFFORT_LEVELS))
                changes.append(("agent", key, value))
            else:
                if not isinstance(value, bool):
                    raise _err(400, "commit_attribution must be true or false")
                changes.append(("agent", key, value))
        if not changes:
            raise _err(400, "nothing to change")
        with _lock(server, "config"):
            text = path.read_text()
            for table, key, value in changes:
                text = toml_edit.set_key(text, table, key, value)
            try:
                _check_harness(text)
                expected = tomllib.loads(text)

                def same(parsed):
                    if parsed != expected:
                        raise ValueError("harness.toml changed while it was being edited")
                toml_edit.write_file_keys(path, changes, validate=same)
            except (ValueError, tomllib.TOMLDecodeError) as err:
                raise _err(400, str(err))
        return 200, _harness_values(server)


register()
