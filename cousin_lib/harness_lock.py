"""The harness lock: the agent harness the framework is tested with, and
what this host runs, compared once when a runner starts.

`config/harness.lock.toml` (beside the package, in the source tree: the
checkout on a bare host, /opt/framework in the image; never the
install's own config/) names the Agent SDK, the Claude Code CLI its wheel
bundles, the opencode release and the model ids. The unit suite keeps the
image's pins, the sdk extra, DEFAULT_MODELS and the README equal to it
(tests/test_harness_lock.py); this module reads what is installed:

- the SDK by its distribution's metadata (importlib.metadata), the CLI
  it bundles by the version file the wheel records (nothing runs);
- `claude` on PATH (the tmux kind, or an SDK that bundles no CLI) and
  the opencode binary by `<binary> --version`, once, with a timeout: a
  hung binary is an unreadable version, never a hung start.

`check(kind, agent)` is the comparison for one runner kind: None for a
kind that runs no harness (fake), else {"kind", "ok", "locked",
"installed", "problems", "message"}. A version that differs, a package
that is missing and a version that cannot be read are each a problem;
`ok` is no problem. An unreadable version is "unpinned CLI": it is never
silently a match."""
import importlib.metadata
import importlib.util
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

LOCK_PATH = Path(__file__).resolve().parents[1] / "config" / "harness.lock.toml"
SDK_DIST = "claude-agent-sdk"
# `<binary> --version` gets this long; a binary that has not answered by
# then is unreadable (subprocess.run kills it)
VERSION_TIMEOUT_S = 5.0
_VERSION = re.compile(r"\d+(?:\.\d+)+(?:[-+][0-9A-Za-z.-]+)?")
UNPINNED = "unpinned CLI"


class LockError(Exception):
    """The lock is missing, does not parse or lacks a key."""


def load(path=None):
    """The lock as a dict, its shape checked; LockError otherwise."""
    path = Path(path or LOCK_PATH)
    try:
        data = tomllib.loads(path.read_text())
    except FileNotFoundError:
        raise LockError("no harness lock at %s" % path)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as err:
        raise LockError("cannot read %s: %s" % (path, err))
    want = (("sdk", SDK_DIST, str), ("sdk", "bundled_cli", str), ("opencode", "version", str),
            ("models", "claude", list), ("models", "opencode_default", str))
    for table, key, kind in want:
        value = (data.get(table) or {}).get(key)
        if not isinstance(value, kind) or not value:
            raise LockError("%s: [%s] %s must be a non-empty %s"
                            % (path, table, key, kind.__name__))
    if not all(isinstance(m, str) and m for m in data["models"]["claude"]):
        raise LockError("%s: [models] claude must list model ids" % path)
    return data


def sdk_version():
    """The installed claude-agent-sdk's version, None when it is not installed."""
    try:
        return importlib.metadata.version(SDK_DIST)
    except importlib.metadata.PackageNotFoundError:
        return None


def sdk_cli():
    """{"installed", "cli", "bundled"} of the SDK's own CLI, read without
    running anything: the SDK prefers its bundled binary, whose version
    the wheel records in claude_agent_sdk._cli_version."""
    spec = importlib.util.find_spec("claude_agent_sdk")
    if spec is None or not spec.origin:
        return {"installed": False, "cli": None, "bundled": False}
    try:
        from claude_agent_sdk._cli_version import __cli_version__ as cli
    except ImportError:
        cli = None
    bundled = (Path(spec.origin).parent / "_bundled" / "claude").is_file()
    return {"installed": True, "cli": cli, "bundled": bundled}


def binary_version(binary, *, timeout=None, run=subprocess.run):
    """(version, None) from `<binary> --version`, else (None, why). Never
    raises and never waits past `timeout` (VERSION_TIMEOUT_S)."""
    timeout = VERSION_TIMEOUT_S if timeout is None else timeout
    shown = "`%s --version`" % Path(binary).name
    try:
        out = run([binary, "--version"], stdin=subprocess.DEVNULL, capture_output=True,
                  text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, "%s gave no answer within %gs" % (shown, timeout)
    except (OSError, ValueError) as err:
        return None, "%s: %s" % (shown, getattr(err, "strerror", None) or err)
    if out.returncode != 0:
        return None, "%s exited %d" % (shown, out.returncode)
    found = _VERSION.search(out.stdout or "")
    if not found:
        return None, "%s printed no version" % shown
    return found.group(0), None


def _compare(problems, what, installed, locked):
    if installed is None:
        problems.append("%s: not installed, locked %s" % (what, locked))
    elif installed != locked:
        problems.append("%s: installed %s, locked %s" % (what, installed, locked))


def _path_cli(lock, installed, problems, which, run):
    """`claude` on PATH, read once, against the lock's CLI."""
    found = which("claude")
    if not found:
        problems.append("%s: no `claude` on PATH" % UNPINNED)
        return
    version, why = binary_version(found, run=run)
    installed.update(cli=version, cli_source="path")
    if version is None:
        problems.append("%s: %s" % (UNPINNED, why))
    else:
        _compare(problems, "claude on PATH", version, lock["sdk"]["bundled_cli"])


def check(kind, agent=None, *, lock=None, environ=None, which=None, run=None):
    """The harness a runner of `kind` runs, against the lock (see the
    module docstring). `agent` is cousin.toml [agent] (the opencode
    binary's [agent] opencode_bin)."""
    if kind not in ("sdk", "tmux", "opencode"):
        return None
    which = which or shutil.which
    run = run or subprocess.run
    installed, locked, problems = {}, {}, []
    try:
        lock = lock if lock is not None else load()
    except LockError as err:
        problems.append(str(err))
        return _result(kind, locked, installed, problems)
    if kind == "sdk":
        locked.update({SDK_DIST: lock["sdk"][SDK_DIST], "cli": lock["sdk"]["bundled_cli"]})
        installed[SDK_DIST] = sdk_version()
        _compare(problems, SDK_DIST, installed[SDK_DIST], lock["sdk"][SDK_DIST])
        own = sdk_cli()
        if own["bundled"]:
            installed.update(cli=own["cli"], cli_source="bundled")
            if own["cli"] is None:
                problems.append("%s: the bundled CLI's version is not recorded" % UNPINNED)
            else:
                _compare(problems, "bundled Claude Code CLI", own["cli"],
                         lock["sdk"]["bundled_cli"])
        elif own["installed"]:
            _path_cli(lock, installed, problems, which, run)
    elif kind == "tmux":
        locked["cli"] = lock["sdk"]["bundled_cli"]
        _path_cli(lock, installed, problems, which, run)
    else:
        from cousin_lib.runner.opencode import opencode_bin
        locked["opencode"] = lock["opencode"]["version"]
        binary = opencode_bin(agent or {}, environ)
        resolved = binary if Path(binary).is_absolute() else which(binary)
        if not resolved:
            installed["opencode"] = None
            problems.append("%s: no opencode binary (%s)" % (UNPINNED, binary))
        else:
            version, why = binary_version(resolved, run=run)
            installed["opencode"] = version
            if version is None:
                problems.append("%s: %s" % (UNPINNED, why))
            else:
                _compare(problems, "opencode", version, lock["opencode"]["version"])
    return _result(kind, locked, installed, problems)


def _result(kind, locked, installed, problems):
    if problems:
        message = "; ".join(problems)
    else:
        message = "as locked: " + ", ".join("%s %s" % kv for kv in sorted(locked.items()))
    return {"kind": kind, "ok": not problems, "locked": locked, "installed": installed,
            "problems": problems, "message": message}
