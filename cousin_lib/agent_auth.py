"""The 1.x auth mode (cousin.toml [runtime] auth, config/harness.toml
[auth.api_key]) as far as anything still reads it, and the private-file
reader the accounts and the console's secret writer share.

2.0.0 removed the mode: a runner authenticates through its [agent]
account, and an anthropic-key account in config/accounts.toml replaces
the api_key mode (removed_keys names the leftover keys). What stays:

- read_mode, api_key_config, read_key and key_file, which
  `cousin-migrate` reads to carry a 1.x api_key cousin over to an
  account;
- read_private_file and the key rules (KEY_MAX_CHARS, _KEY_RE), which
  accounts.py and console/secrets.py apply to every secret file.

Two mode names, spelled here and nowhere else: MODE_LOGIN ("claude",
the default, the harness's own login) and MODE_API_KEY ("api_key", a
key from <home>/.secrets/api-key.env, one line `<VAR>=<key>`, file
0600, directory 0700, handed over through the environment only).
"""
import json
import os
import re
import stat
import tempfile
import tomllib
from pathlib import Path

from cousin_lib.config import MissingConfigError, _read_harness_toml

# ---- the mode names: the one place they are spelled -------------------

MODE_LOGIN = "claude"
MODE_API_KEY = "api_key"
AUTH_MODES = (MODE_LOGIN, MODE_API_KEY)
DEFAULT_MODE = MODE_LOGIN

# ---- where things live ------------------------------------------------

SECRETS_DIR = ".secrets"
KEY_FILE_NAME = "api-key.env"
# Relative to the framework root unless harness.toml names another.
DEFAULT_ISOLATED_DIR = "data/harness-api-key-config"
KEY_MAX_CHARS = 512
_KEY_RE = re.compile(r"^[\x21-\x7e]+$")
_VAR_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class AuthError(Exception):
    """The mode cannot be used or switched; the message says why and
    never contains the key."""


# ---- the mode ---------------------------------------------------------

def check_mode(mode):
    if mode not in AUTH_MODES:
        raise AuthError("auth mode must be one of %s, got %r"
                        % (", ".join(AUTH_MODES), mode))
    return mode


def read_mode(home):
    """cousin.toml [runtime] auth, else the default. An unknown value
    is an error: guessing a billing mode is the failure this prevents."""
    path = Path(home) / "cousin.toml"
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as err:
        raise AuthError("cannot read %s: %s" % (path, err))
    value = (data.get("runtime") or {}).get("auth")
    if value is None:
        return DEFAULT_MODE
    try:
        return check_mode(value)
    except AuthError as err:
        raise AuthError("runtime.auth in %s: %s" % (path, err))


def persist_mode(home, mode):
    """Write cousin.toml [runtime] auth. A real change (not a same-mode
    save) is recorded in raw memory as an L1 event, framework:auth."""
    check_mode(mode)
    # Late import: spawn imports this module for the launcher.
    from cousin_lib.spawn import _persist_runtime_line, framework_event
    try:
        previous = read_mode(home)
    except AuthError:
        previous = None
    _persist_runtime_line(home, "auth", mode)
    if previous != mode:
        framework_event(home, "auth", "auth mode %s -> %s"
                        % (previous or "(unreadable)", mode))


# ---- harness configuration --------------------------------------------

def _str_list(table, key, where):
    value = table.get(key, [])
    if not isinstance(value, list) or not all(
            isinstance(v, str) and v for v in value):
        raise AuthError("%s %s must be a list of non-empty strings, got %r"
                        % (where, key, value))
    return list(value)


def api_key_config(root):
    """config/harness.toml [auth.api_key], validated, or None when the
    table is absent (api_key mode is then refused, and claude mode has
    nothing to strip)."""
    where = "config/harness.toml [auth.api_key]"
    try:
        data = _read_harness_toml(root) or {}
    except MissingConfigError as err:
        raise AuthError(str(err))
    table = (data.get("auth") or {}).get("api_key")
    if table is None:
        return None
    if not isinstance(table, dict):
        raise AuthError("%s must be a table" % where)
    out = {}
    for key in ("key_env", "config_dir_env"):
        value = table.get(key)
        if not isinstance(value, str) or not _VAR_RE.match(value):
            raise AuthError("%s %s must be an environment variable name,"
                            " got %r" % (where, key, value))
        out[key] = value
    source = table.get("source_dir")
    if not isinstance(source, str) or not source:
        raise AuthError("%s source_dir must name the harness's normal"
                        " config directory" % where)
    out["source_dir"] = Path(source).expanduser()
    settings = table.get("settings_file")
    out["settings_file"] = (Path(settings).expanduser()
                            if isinstance(settings, str) and settings
                            else None)
    name = table.get("settings_name") or (
        out["settings_file"].name if out["settings_file"] else None)
    if name is not None and (not isinstance(name, str) or "/" in name):
        raise AuthError("%s settings_name must be a file name" % where)
    out["settings_name"] = name
    iso = table.get("isolated_dir") or DEFAULT_ISOLATED_DIR
    if not isinstance(iso, str):
        raise AuthError("%s isolated_dir must be a path" % where)
    iso_path = Path(iso).expanduser()
    out["isolated_dir"] = (iso_path if iso_path.is_absolute()
                           else Path(root) / iso_path)
    for key in ("exclude", "strip_settings_keys", "preserve_settings_keys",
                "login_files", "login_settings_keys", "login_file_keys"):
        out[key] = _str_list(table, key, where)
    return out


# ---- the key file -----------------------------------------------------

def key_file(home):
    return Path(home) / SECRETS_DIR / KEY_FILE_NAME


def _mode_bits(st):
    return stat.S_IMODE(st.st_mode)


class MissingFile(AuthError):
    """A private file, or its directory, does not exist: not written yet."""


def read_private_file(path, *, what="key file", missing_hint=""):
    """The bytes of a private file after every check read_key has always
    made: its directory a directory of ours with no group or other bits,
    the file a regular file of ours with none either, opened with
    O_NOFOLLOW (a symlink is refused). Errors name the file and the
    problem, never the content; an absent file or directory is
    MissingFile, so a caller can tell "not written yet" from "wrong"."""
    path = Path(path)
    secrets = path.parent
    try:
        dst = os.lstat(secrets)
    except FileNotFoundError:
        raise MissingFile("no %s: %s does not exist%s" % (what, path, missing_hint))
    if not stat.S_ISDIR(dst.st_mode):
        raise AuthError("%s is not a directory" % secrets)
    if dst.st_uid != os.getuid():
        raise AuthError("%s is not owned by this user" % secrets)
    if _mode_bits(dst) & 0o077:
        raise AuthError("%s is open to group or others (mode %o); chmod"
                        " 700 it" % (secrets, _mode_bits(dst)))
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        raise MissingFile("no %s: %s does not exist%s" % (what, path, missing_hint))
    except OSError as err:
        raise AuthError("cannot open %s: %s" % (path, err.strerror))
    try:
        fst = os.fstat(fd)
        if not stat.S_ISREG(fst.st_mode):
            raise AuthError("%s is not a regular file" % path)
        if fst.st_uid != os.getuid():
            raise AuthError("%s is not owned by this user" % path)
        if _mode_bits(fst) & 0o077:
            raise AuthError("%s is readable by group or others (mode %o);"
                            " chmod 600 it" % (path, _mode_bits(fst)))
        return os.read(fd, 8192)
    finally:
        os.close(fd)


def read_key(home, key_env):
    """The key from <home>/.secrets/api-key.env, after every check
    (read_private_file), one `<key_env>=<key>` line. Errors name the file
    and the problem, never the content."""
    path = key_file(home)
    raw = read_private_file(path, what="key file",
                            missing_hint="")
    lines = [ln.strip() for ln in raw.decode("utf-8", "replace").splitlines()
             if ln.strip() and not ln.strip().startswith("#")]
    if not lines:
        raise AuthError("key file %s is empty" % path)
    if len(lines) != 1 or "=" not in lines[0]:
        raise AuthError("key file %s must hold one line %s=<key>"
                        % (path, key_env))
    var, _, key = lines[0].partition("=")
    if var.strip() != key_env:
        raise AuthError("key file %s names a variable other than %s"
                        % (path, key_env))
    key = key.strip()
    if not key:
        raise AuthError("key file %s is empty" % path)
    if not _KEY_RE.match(key) or len(key) > KEY_MAX_CHARS:
        raise AuthError("key file %s holds a malformed key" % path)
    return key


def normalize_key(text, key_env):
    """The key from what an operator pasted: a bare key, or a whole
    `<key_env>=<key>` line. Surrounding whitespace is dropped."""
    text = (text or "").strip()
    prefix = key_env + "="
    if text.startswith(prefix):
        text = text[len(prefix):].strip()
    if not text:
        raise AuthError("the key is empty")
    if len(text) > KEY_MAX_CHARS or not _KEY_RE.match(text):
        raise AuthError("the key must be one word of printable characters"
                        " (at most %d)" % KEY_MAX_CHARS)
    return text


def write_key(home, text, key_env):
    """Write <home>/.secrets/api-key.env from pasted text: the directory
    0700 with a `*` .gitignore of its own (a home may be a git
    repository), the file created 0600 beside it and renamed into
    place, so no reader ever sees a half-written or open file."""
    key = normalize_key(text, key_env)
    secrets = Path(home) / SECRETS_DIR
    secrets.mkdir(mode=0o700, exist_ok=True)
    os.chmod(secrets, 0o700)
    ignore = secrets / ".gitignore"
    if not ignore.exists():
        ignore.write_text("*\n")
    fd, tmp = tempfile.mkstemp(dir=secrets, prefix=".key-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write("%s=%s\n" % (key_env, key))
        os.replace(tmp, key_file(home))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return key_state(home, key_env)


def key_state(home, key_env):
    """What may be shown about the key: set or not, its last four
    characters when it is long enough for that to reveal nothing
    useful, and why it is unusable. Never the key."""
    if key_env is None:
        return {"set": key_file(home).exists(), "last4": None,
                "error": "config/harness.toml has no [auth.api_key]"}
    try:
        key = read_key(home, key_env)
    except AuthError as err:
        exists = os.path.lexists(key_file(home))
        return {"set": False, "last4": None,
                "error": str(err) if exists else None}
    return {"set": True, "last4": key[-4:] if len(key) >= 16 else None,
            "error": None}


# ---- the isolated harness config directory ----------------------------

def _load_json(path):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as err:
        raise AuthError("cannot read %s: %s" % (path, err))


def build_isolated_dir(cfg):
    """Build or refresh the config directory the harness uses in
    api_key mode: a symlink to every entry of its normal config
    directory except the excluded (login and account-bound) ones, and
    a copy of its settings JSON without the account keys. Keys named
    in preserve_settings_keys survive from the previous copy (what the
    harness recorded while running on the key). A login file found in
    the directory is not removed: the launch refuses and says which."""
    iso = cfg["isolated_dir"]
    iso.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(iso, 0o700)
    source = cfg["source_dir"]
    exclude = set(cfg["exclude"])
    if cfg["settings_name"]:
        exclude.add(cfg["settings_name"])
    linked = []
    wanted = {}
    if source.is_dir():
        for entry in sorted(source.iterdir()):
            if entry.name not in exclude:
                wanted[entry.name] = entry
    for existing in sorted(iso.iterdir()):
        if existing.is_symlink() and existing.name not in wanted:
            existing.unlink()
    for name, target in wanted.items():
        link = iso / name
        if link.is_symlink():
            if os.readlink(link) == str(target):
                linked.append(name)
                continue
            link.unlink()
        elif link.exists():
            # The harness made its own entry here; it is the api_key
            # side's, and stays.
            continue
        link.symlink_to(target)
        linked.append(name)
    wrote = None
    if cfg["settings_name"]:
        dest = iso / cfg["settings_name"]
        base = (_load_json(cfg["settings_file"])
                if cfg["settings_file"] else None) or {}
        if not isinstance(base, dict):
            raise AuthError("%s is not a JSON object" % cfg["settings_file"])
        previous = _load_json(dest) if dest.exists() else None
        out = {k: v for k, v in base.items()
               if k not in set(cfg["strip_settings_keys"])}
        if isinstance(previous, dict):
            for key in cfg["preserve_settings_keys"]:
                if key in previous:
                    out[key] = previous[key]
        fd, tmp = tempfile.mkstemp(dir=iso, prefix=".settings-")
        with os.fdopen(fd, "w") as fh:
            os.fchmod(fh.fileno(), 0o600)
            json.dump(out, fh, indent=2)
        os.replace(tmp, dest)
        wrote = str(dest)
    return {"dir": str(iso), "linked": linked, "settings": wrote}


def _holds_login(file_path, login_keys):
    """Whether a login file really carries a login. Without login_keys
    its existence is the login. With them (the harness also writes
    unrelated state into the same file, e.g. plugin sign-in data), only a
    JSON object carrying one of those keys counts, and a file that is not
    a readable JSON object counts too: fail closed."""
    if not login_keys:
        return True
    try:
        data = _load_json(file_path)
    except AuthError:
        return True
    if not isinstance(data, dict):
        return True
    return any(k in data for k in login_keys)


def check_isolated_dir(cfg):
    """Refuse an isolated directory that is missing or holds a login:
    one of login_files, or a settings copy carrying one of
    login_settings_keys. With a login there, the harness would bill it
    and ignore the key."""
    iso = cfg["isolated_dir"]
    if not iso.is_dir():
        raise AuthError("the api_key config directory %s does not exist"
                        % iso)
    for name in cfg["login_files"]:
        if os.path.lexists(iso / name) and _holds_login(
                iso / name, cfg.get("login_file_keys") or []):
            raise AuthError(
                "the api_key config directory holds a login (%s); remove"
                " it, then switch again: with a login present the harness"
                " bills the login, not the key" % (iso / name))
    if cfg["settings_name"]:
        data = _load_json(iso / cfg["settings_name"])
        if isinstance(data, dict):
            found = [k for k in cfg["login_settings_keys"] if k in data]
            if found:
                raise AuthError(
                    "the api_key settings copy %s carries account keys"
                    " (%s); switch to api_key again to rebuild it"
                    % (iso / cfg["settings_name"], ", ".join(found)))
