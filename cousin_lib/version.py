"""The framework's version: one number, in pyproject.toml.

`cousin_lib.__version__` and the console's `GET /api/version` read it
from the checkout's pyproject.toml when the package runs from a
checkout (an editable install's recorded metadata keeps the version it
was installed with, so after a bump it would lie), else from the
installed distribution's metadata. `cousin-version bump` edits the
pyproject in place, touching nothing but the version string.

A process reads the version and the git commit once, at first use:
the console shows what it is RUNNING, so a checkout that was bumped or
pulled but not restarted shows the old values until the restart.
"""
import argparse
import functools
import re
import subprocess
import sys
import tomllib
from pathlib import Path

DISTRIBUTION = "cousins-framework"
CHECKOUT = Path(__file__).resolve().parents[1]
PARTS = ("major", "minor", "patch")
UNKNOWN = "0+unknown"

_VERSION_LINE = re.compile(
    r'(?m)^(?P<lead>version\s*=\s*")(?P<v>\d+\.\d+\.\d+)(?P<tail>")')


class VersionError(Exception):
    pass


def pyproject_version(path):
    """project.version from a pyproject naming this distribution, else
    None."""
    try:
        data = tomllib.loads(Path(path).read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return None
    project = data.get("project") or {}
    if project.get("name") != DISTRIBUTION:
        return None
    value = project.get("version")
    return str(value) if value else None


def read_version():
    found = pyproject_version(CHECKOUT / "pyproject.toml")
    if found:
        return found
    import importlib.metadata as md
    try:
        return md.version(DISTRIBUTION)
    except md.PackageNotFoundError:
        return UNKNOWN


@functools.lru_cache(maxsize=None)
def version():
    """The version this process runs, read once."""
    return read_version()


@functools.lru_cache(maxsize=None)
def git_commit():
    """The checkout's short commit when the package runs from a git
    checkout, read once; None otherwise or when git is unavailable."""
    if not (CHECKOUT / ".git").exists():
        return None
    try:
        r = subprocess.run(["git", "-C", str(CHECKOUT), "rev-parse",
                            "--short", "HEAD"], capture_output=True,
                           text=True, timeout=3, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    commit = (r.stdout or "").strip()
    return commit if r.returncode == 0 and commit else None


def browse_url(remote):
    """An https URL a browser can open for a git remote, or None.

    Accepts scp-style (git@host:owner/repo.git), ssh:// and http(s)
    remotes; drops a trailing .git, any port, and any user or token
    embedded in the remote (the route that shows this is public). Local
    paths and file:// remotes have nothing to browse: None."""
    if not remote or not isinstance(remote, str):
        return None
    remote = remote.strip()
    m = re.match(r"^[\w.-]+@([\w.-]+):(?!//)(.+)$", remote)
    if m:
        host, path = m.group(1), m.group(2)
    else:
        m = re.match(r"^(?:https?|ssh|git)://(?:[^@/]*@)?([\w.-]+)(?::\d+)?/(.+)$",
                     remote)
        if not m:
            return None
        host, path = m.group(1), m.group(2)
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if not path or "/" not in path:
        return None
    return "https://%s/%s" % (host, path)


def commit_url(repo_url, commit):
    if not repo_url or not commit:
        return None
    return "%s/commit/%s" % (repo_url, commit)


@functools.lru_cache(maxsize=None)
def repo_url():
    """The browsable URL of the checkout's origin remote, read once;
    None outside a git checkout or without a usable remote."""
    if not (CHECKOUT / ".git").exists():
        return None
    try:
        r = subprocess.run(["git", "-C", str(CHECKOUT), "remote", "get-url",
                            "origin"], capture_output=True, text=True,
                           timeout=3, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return browse_url((r.stdout or "").strip()) if r.returncode == 0 else None


def bumped(current, part):
    if part not in PARTS:
        raise VersionError("part must be one of %s" % ", ".join(PARTS))
    try:
        major, minor, patch = (int(x) for x in current.split("."))
    except ValueError:
        raise VersionError("version %r is not MAJOR.MINOR.PATCH" % current)
    if part == "major":
        return "%d.0.0" % (major + 1)
    if part == "minor":
        return "%d.%d.0" % (major, minor + 1)
    return "%d.%d.%d" % (major, minor, patch + 1)


def bump_text(text, part):
    """(old, new, text) with the [project] version bumped and every
    other byte of the file kept."""
    header = re.search(r"(?m)^\[project\]\s*$", text)
    if header is None:
        raise VersionError("no [project] table")
    start = header.end()
    nxt = re.search(r"(?m)^\s*\[", text[start:])
    end = start + nxt.start() if nxt else len(text)
    m = _VERSION_LINE.search(text, start, end)
    if m is None:
        raise VersionError('no version = "MAJOR.MINOR.PATCH" line in'
                           " [project]")
    old = m.group("v")
    new = bumped(old, part)
    out = text[:m.start("v")] + new + text[m.end("v"):]
    if tomllib.loads(out)["project"]["version"] != new:
        raise VersionError("the bumped file does not read back as %s" % new)
    return old, new, out


def bump_file(path, part):
    path = Path(path)
    try:
        text = path.read_text()
    except OSError as err:
        raise VersionError("cannot read %s: %s" % (path, err))
    old, new, out = bump_text(text, part)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(out)
    tmp.replace(path)
    return old, new


def _default_pyproject():
    for candidate in (CHECKOUT / "pyproject.toml",
                      Path.cwd() / "pyproject.toml"):
        if pyproject_version(candidate):
            return candidate
    return None


def version_main(argv=None):
    """cousin-version [--pyproject P] [bump [major|minor|patch]].
    Exit 0 done, 2 no pyproject to bump or a malformed one."""
    parser = argparse.ArgumentParser(
        prog="cousin-version",
        description="print the framework version, or bump it in"
                    " pyproject.toml")
    parser.add_argument("action", nargs="?", choices=("bump",))
    parser.add_argument("part", nargs="?", choices=PARTS, default="patch")
    parser.add_argument("--pyproject",
                        help="the pyproject.toml to read or bump (default:"
                             " the checkout's)")
    args = parser.parse_args(argv)
    if args.action is None:
        if args.pyproject:
            found = pyproject_version(args.pyproject)
            if not found:
                print("cousin-version: %s names no %s version"
                      % (args.pyproject, DISTRIBUTION), file=sys.stderr)
                return 2
            print(found)
            return 0
        commit = git_commit()
        print(version() + (" (%s)" % commit if commit else ""))
        return 0
    path = Path(args.pyproject) if args.pyproject else _default_pyproject()
    if path is None:
        print("cousin-version: no %s pyproject.toml here; run it in the"
              " checkout or pass --pyproject" % DISTRIBUTION,
              file=sys.stderr)
        return 2
    try:
        old, new = bump_file(path, args.part)
    except VersionError as err:
        print("cousin-version: %s: %s" % (path, err), file=sys.stderr)
        return 2
    print("%s -> %s (%s)" % (old, new, path))
    return 0


if __name__ == "__main__":
    sys.exit(version_main())
