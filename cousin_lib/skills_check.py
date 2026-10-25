"""Skills after approval (docs/reference/skills.md, "Life after approval"):
whether an approved skill has gone stale, when it was last used, and
whether the place it was imported from has moved on.

- A baseline is taken for each approved skill, at approval, or at its
  first check when it was approved before this existed. It holds a
  fingerprint of each `reads` pin and `rests-on` item.
- A check compares them with now. Each item is `same`, `changed`,
  `missing` or `unknown` (not checkable here, or not reachable now). A
  skill is stale when an item changed or went missing; an unknown item
  never makes it stale.
- A rests-on item marked `live:` is one the skill reads afresh each time
  it runs: only whether it is there is checked, so a change to it never
  makes the skill stale, and its going missing does.
- A skill from outside also has its source folder compared with
  upstream's default branch: `update` says upstream changed it. That is
  news for the operator, not staleness.
- The operator can review a stale skill as still fine, in the console
  (rebaseline): what changed becomes its baseline, with a record like an
  approval's.
- Use is recorded by the tool hook (recording.handle) in the using
  cousin's own home, so a private cousin's use never leaves it. A skill
  with no use for UNUSED_DAYS, counted from the later of its last use and
  its approval, is flagged for removal.
- The check is read-only. It reads files and memory, runs
  `<tool> --version` with no shell, and fetches git objects over https
  (depth 1, no credentials, hooks or global settings, a user agent naming
  the framework) into a temporary repository. It never runs a skill, its
  scripts or an outward step. It is rate-limited: the loops daemon runs
  it at most every CHECK_EVERY seconds, an asked-for run at most every
  NOW_GAP seconds, and one run stops starting fetches after RUN_BUDGET
  seconds (what is left is unknown).

Results live beside the approval records: data/skills/baselines/ and
data/skills/checks/, under data/skills/private/ for the private
repository, whose folder names are private cousins' names."""
from __future__ import annotations

import fcntl
import hashlib
from contextlib import contextmanager
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from cousin_lib import atomic, skills
from cousin_lib.skills import SkillError

CHECK_EVERY = 20 * 3600
NOW_GAP = 300
RUN_BUDGET = 300
FETCH_TIMEOUT = 60
TOOL_TIMEOUT = 10
UNUSED_DAYS = 30
USAGE_FILE = ("data", "skills-usage.json")
STATES = ("same", "changed", "missing", "unknown")
LAST_RUN = "last-run.json"

PIN_FILE = re.compile(r"^(?P<path>[^\s#,]+)#sha256:(?P<hex>[0-9a-fA-F]{64})$")
PIN_REPO = re.compile(r"^(?P<repo>[^\s@#,]+)@(?P<commit>[0-9a-fA-F]{40}|[0-9a-fA-F]{64})"
                      r":(?P<path>\S+)$")
TOOL_VERSION = re.compile(r"^(?P<tool>[A-Za-z0-9_.+-]+) --version$")
SHARED_REF = re.compile(r"^(?P<name>reference_[A-Za-z0-9_.-]+?)(?:\.md)?$")
REPO_FILE = re.compile(r"^(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+):(?P<path>[^\s:]+)$")
WORD = re.compile(r"^[A-Za-z0-9_.+-]+$")
LIVE = "live:"
PRESENT = "present"


def _now_iso(now=None):
    return datetime.fromtimestamp(now or time.time(), timezone.utc).isoformat(timespec="seconds")


def _when(text):
    try:
        return datetime.fromisoformat(str(text)).timestamp()
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- where

def _base(root, repo):
    base = Path(root) / "data" / "skills"
    return base / "private" if repo == "private" else base


def _key_path(root, repo, kind, folder, name):
    return _base(root, repo) / kind / repo / folder / ("%s.json" % name)


def baseline_path(root, repo, folder, name):
    return _key_path(root, repo, "baselines", folder, name)


def check_path(root, repo, folder, name):
    return _key_path(root, repo, "checks", folder, name)


def _read(path):
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic.write_text(path, json.dumps(data, indent=1, sort_keys=True) + "\n", mode=0o600)


def owner_home(root, folder):
    """The home of the cousin a folder belongs to, or None (shared/, or a
    cousin that is gone): relative paths and memory topics resolve there."""
    if folder == skills.SHARED:
        return None
    home = Path(root) / "cousins" / folder
    return home if (home / "cousin.toml").is_file() else None


# ---------------------------------------------------------------- items

def items_of(metadata):
    """[(of, item)] a skill declares: its reads pins, its rests-on items and
    its source, in that order."""
    metadata = metadata or {}
    out = [("reads", i) for i in skills._items(metadata.get("reads") or "")]
    out += [("rests-on", i) for i in skills._items(metadata.get("rests-on") or "")]
    if metadata.get("source"):
        out.append(("source", str(metadata["source"]).strip()))
    return out


def live(of, item):
    """(read live, the item without its mark): a rests-on item marked
    `live:` is one the skill reads afresh each time it runs, so a change to
    it is expected and only its going missing counts."""
    item = str(item).strip()
    if of == "rests-on" and item.startswith(LIVE):
        return True, item[len(LIVE):].strip()
    return False, item


def classify(of, item):
    """(kind, parts) of one declared item. reads: file-pin, repo-pin.
    rests-on: tool-version, shared-ref, repo-file, file, tool, memory, the
    same for an item marked live (by what follows the mark). source:
    upstream. Anything else: unknown."""
    _read_live, item = live(of, item)
    if of == "source":
        try:
            return "upstream", skills.split_source(item)
        except SkillError:
            return "unknown", ()
    if of == "reads":
        m = PIN_FILE.match(item)
        if m:
            return "file-pin", (m.group("path"), m.group("hex").lower())
        m = PIN_REPO.match(item)
        if m:
            return "repo-pin", (m.group("repo"), m.group("commit").lower(), m.group("path"))
        return "unknown", ()
    m = TOOL_VERSION.match(item)
    if m:
        return "tool-version", (m.group("tool"),)
    m = SHARED_REF.match(item)
    if m:
        return "shared-ref", (m.group("name"),)
    m = REPO_FILE.match(item)
    if m and not item.startswith(("/", ".")):
        return "repo-file", (m.group("repo"), m.group("path"))
    if " " not in item and ("/" in item or re.search(r"\.[A-Za-z0-9]{1,5}$", item)):
        return "file", (item,)
    if WORD.match(item) and shutil.which(item):
        return "tool", (item,)
    return "memory", (item,)


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _resolve(root, home, path):
    """An existing file a declared path names: absolute as it is, else under
    the owner's home, else under the install root. None when there is none."""
    p = Path(path).expanduser()
    if p.is_absolute():
        return p if p.is_file() else None
    for base in ([home] if home else []) + [Path(root)]:
        cand = Path(base) / p
        if cand.is_file():
            return cand
    return None


# ---------------------------------------------------------------- git

class Remote:
    """Read-only access to the git repositories a run asks about. Remote ones
    are fetched over https only, depth 1, into one temporary bare
    repository with the import's hardening (skills.FETCH_CONFIG, no GIT_*
    or askpass variables, its own HOME) and a user agent naming the
    framework; each url's HEAD at most once a run. A local repository (an
    absolute path) is read in place at its HEAD. Errors are SkillError."""

    def __init__(self, *, timeout=FETCH_TIMEOUT, budget=RUN_BUDGET, fetch=None):
        self.timeout = timeout
        self.deadline = time.monotonic() + budget
        self.fetch_override = fetch
        self._tmp = None
        self._heads = {}
        self._have = set()

    def close(self):
        if self._tmp is not None:
            self._tmp.cleanup()
            self._tmp = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    @staticmethod
    def url_of(repo):
        """https URL of a declared repository: a URL as it is, `host/owner/name`
        (a dot in the first part), else `owner/name` on github.com. A local
        absolute path stays a path."""
        repo = str(repo).strip()
        if repo.startswith("/"):
            return repo
        if "://" in repo:
            return repo
        first = repo.split("/", 1)[0]
        return "https://%s" % repo if "." in first else "https://github.com/%s" % repo

    def _repo(self):
        if self._tmp is None:
            self._tmp = tempfile.TemporaryDirectory(prefix="cousin-skill-check-")
            self._git(None, "init", "-q", "--bare", str(Path(self._tmp.name) / "r.git"))
        return str(Path(self._tmp.name) / "r.git")

    def _env(self):
        home = self._tmp.name if self._tmp is not None else tempfile.gettempdir()
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("GIT_") and not k.endswith("ASKPASS")}
        env.update(GIT_TERMINAL_PROMPT="0", LC_ALL="C", GIT_CONFIG_NOSYSTEM="1",
                   GIT_CONFIG_GLOBAL=os.devnull, HOME=home, XDG_CONFIG_HOME=home)
        return env

    def _git(self, repo, *args):
        config = [a for s in skills.FETCH_CONFIG for a in ("-c", s)]
        config += ["-c", "protocol.https.allow=always", "-c", "http.userAgent=%s" % user_agent()]
        cmd = ["git", *config] + (["-C", repo] if repo else []) + list(args)
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, env=self._env(),
                                  timeout=self.timeout)
        except subprocess.TimeoutExpired:
            raise SkillError("git %s took over %ds" % (args[0], self.timeout)) from None
        if proc.returncode != 0:
            raise SkillError("git %s: %s" % (args[0], (proc.stderr or "").strip()[-200:]
                                             or "exit %d" % proc.returncode))
        return proc.stdout

    def _fetch(self, url, what):
        if self.fetch_override is not None:
            return self.fetch_override(self, url, what)
        if not url.lower().startswith("https://"):
            raise SkillError("only https is fetched, not %s" % url)
        if time.monotonic() > self.deadline:
            raise SkillError("the run's time budget is spent")
        repo = self._repo()
        self._git(repo, "fetch", "-q", "--depth", "1", "--no-tags", url, what)
        return self._git(repo, "rev-parse", "--verify", "-q", "FETCH_HEAD^{commit}").strip()

    def head(self, repo):
        url = self.url_of(repo)
        if url.startswith("/"):
            return self._local(url, "rev-parse", "--verify", "-q", "HEAD^{commit}").strip()
        if url not in self._heads:
            self._heads[url] = self._fetch(url, "HEAD")
            self._have.add(self._heads[url])
        return self._heads[url]

    def ensure(self, repo, commit):
        url = self.url_of(repo)
        if url.startswith("/") or commit in self._have:
            return
        got = self._fetch(url, commit)
        if got.lower() != commit.lower():
            raise SkillError("%s has no commit %s" % (url, commit))
        self._have.add(got)

    def oid(self, repo, rev, path):
        """The object id of `path` at `rev`, or None when it is not there."""
        url = self.url_of(repo)
        spec = "%s:%s" % (rev, path.strip("/"))
        try:
            if url.startswith("/"):
                return self._local(url, "rev-parse", "--verify", "-q", spec).strip() or None
            return self._git(self._repo(), "rev-parse", "--verify", "-q", spec).strip() or None
        except SkillError:
            return None

    def _local(self, path, *args):
        proc = subprocess.run(["git", "-C", path, *args], capture_output=True, text=True,
                              env=dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C"),
                              timeout=self.timeout)
        if proc.returncode != 0:
            raise SkillError("git %s in %s: %s" % (args[0], path, (proc.stderr or "").strip()[-200:]
                                                     or "exit %d" % proc.returncode))
        return proc.stdout


def user_agent():
    try:
        from cousin_lib import version
        v = version.version()
    except Exception:  # noqa: BLE001 - a user agent never fails a check
        v = "unknown"
    return "cousins-framework/%s (skills check; read-only)" % v


# ---------------------------------------------------------------- fingerprints

class _Memory:
    """Each home's raw memory read once a run, by topic."""

    def __init__(self):
        self._by_home = {}

    def newest(self, home, topic):
        from cousin_lib import memory
        key = str(home)
        if key not in self._by_home:
            index = {}
            for e in memory.list_raw(home, since_days=36500):
                index.setdefault(str(e.get("topic") or "").strip(), []).append(e)
            for rows in index.values():
                rows.sort(key=lambda e: memory.entry_timestamp(e) or 0.0)
            self._by_home[key] = index
        rows = self._by_home[key].get(str(topic).strip())
        return memory.entry_id(rows[-1]) if rows else None


def _exe(tool, hint=None):
    """The executable for a tool: the path its baseline resolved when that
    still exists (so every process checks the same binary, whatever its
    PATH), else the first on this process's PATH."""
    if hint and os.path.isfile(hint) and os.access(hint, os.X_OK):
        return hint
    return shutil.which(tool)


def fingerprint(kind, parts, *, root, home, remote, mem, hint=None):
    """(state, value, detail) of an item as it is now. state is None when the
    value was read (the caller compares), else missing or unknown. For a
    tool, detail is the executable's path, which the baseline keeps as the
    next check's `hint`."""
    try:
        if kind in ("file-pin", "file"):
            path = _resolve(root, home, parts[0])
            if path is None:
                return "missing", None, "no file %s" % parts[0]
            return None, _sha256_file(path), str(path)
        if kind == "shared-ref":
            path = Path(root) / "shared" / ("%s.md" % parts[0])
            if not path.is_file():
                return "missing", None, "no shared reference %s" % parts[0]
            return None, _sha256_file(path), str(path)
        if kind == "tool-version":
            exe = _exe(parts[0], hint)
            if exe is None:
                return "missing", None, "%s is not on PATH" % parts[0]
            proc = subprocess.run([exe, "--version"], capture_output=True, text=True,
                                  timeout=TOOL_TIMEOUT, stdin=subprocess.DEVNULL)
            out = (proc.stdout or proc.stderr or "").strip().splitlines()
            line = " ".join(out[0].split()) if out else ""
            if not line:
                return "unknown", None, "%s --version said nothing" % parts[0]
            return None, line, exe
        if kind == "tool":
            exe = _exe(parts[0], hint)
            if exe is None:
                return "missing", None, "%s is not on PATH" % parts[0]
            return None, _sha256_file(os.path.realpath(exe)), exe
        if kind == "memory":
            if home is None:
                return "unknown", None, "no owner memory to read"
            newest = mem.newest(home, parts[0])
            if newest is None:
                return "missing", None, "no memory topic %r" % parts[0]
            return None, newest, "newest entry"
        if kind == "repo-pin":
            repo, commit, path = parts
            remote.ensure(repo, commit)
            head = remote.head(repo)
            now = remote.oid(repo, head, path)
            if now is None:
                return "missing", None, "%s is gone at %s %s" % (path, repo, head[:12])
            return None, now, "%s at %s" % (repo, head[:12])
        if kind == "repo-file":
            repo, path = parts
            head = remote.head(repo)
            now = remote.oid(repo, head, path)
            if now is None:
                return "missing", None, "%s is gone at %s %s" % (path, repo, head[:12])
            return None, now, "%s at %s" % (repo, head[:12])
        if kind == "upstream":
            url, commit, folder = parts
            head = remote.head(url)
            now = remote.oid(url, head, folder or "")
            if now is None:
                return "missing", None, "%s is gone upstream at %s" % (folder or "/", head[:12])
            return None, now, "upstream at %s" % head[:12]
    except (OSError, SkillError, subprocess.SubprocessError) as err:
        return "unknown", None, str(err)[:200]
    return "unknown", None, "not checkable here"


def _pinned(kind, parts, *, remote):
    """The value a pin itself fixes, or None: a file pin's hash, a repo pin's
    blob at the pinned commit, a source's folder tree at its commit."""
    if kind == "file-pin":
        return parts[1]
    if kind == "repo-pin":
        repo, commit, path = parts
        remote.ensure(repo, commit)
        return remote.oid(repo, commit, path)
    if kind == "upstream":
        url, commit, folder = parts
        remote.ensure(url, commit)
        return remote.oid(url, commit, folder or "")
    return None


# ---------------------------------------------------------------- one skill

def _metadata(root, repo, folder, name):
    """(metadata, approved commit, the skill's own tree id at it). The tree
    is what a baseline is kept for: an approval of another skill moves the
    commit, never this tree."""
    st = skills.state(root, repo)
    if not st["trusted"]:
        raise SkillError("repository %s is not in an approved state" % repo)
    path = skills.repo_dir(root, repo)
    raw = skills._show(path, st["approved"], "%s/%s/%s" % (folder, name, skills.SKILL_FILE))
    if raw is None:
        raise SkillError("no approved skill %s:%s/%s" % (repo, folder, name))
    tree = skills._git(path, "rev-parse", "--verify", "-q",
                       "%s:%s/%s" % (st["approved"], folder, name), check=False).strip()
    front, _body = skills.parse(raw.decode("utf-8"))
    return front.get("metadata") or {}, st["approved"], tree or None


PINNED = ("file-pin", "repo-pin", "upstream")
NETWORK = ("repo-pin", "repo-file", "upstream")


def capture_baseline(root, repo, folder, name, *, origin="approval", now=None):
    """Take and keep the baseline of an approved skill: each local item's
    value now. Offline: an item that needs the network (repo-file) gets
    its value at its first check, and a pin needs none (the pin is its own
    baseline). Never raises for an item it cannot read (kept as None)."""
    metadata, commit, tree = _metadata(root, repo, folder, name)
    home = owner_home(root, folder)
    mem = _Memory()
    values = {}
    for of, item in items_of(metadata):
        kind, parts = classify(of, item)
        read_live = live(of, item)[0]
        value = None
        if kind not in NETWORK:
            if kind == "file-pin":
                value = parts[1]
            else:
                state, value, detail = fingerprint(kind, parts, root=root, home=home,
                                                   remote=None, mem=mem)
                if state is not None:
                    value = None
                elif read_live:
                    value = PRESENT
        values[item] = {"of": of, "kind": kind, "value": value}
        if read_live:
            values[item]["live"] = True
        if kind in ("tool", "tool-version") and value is not None:
            values[item]["path"] = detail
    data = {"skill": "%s:%s/%s" % (repo, folder, name), "commit": commit, "tree": tree,
            "at": _now_iso(now), "from": origin, "items": values}
    _write(baseline_path(root, repo, folder, name), data)
    return data


def _compare(state, value, detail, then):
    """The item's state from what it is now and its baseline value."""
    if state is None:
        if then is None:
            return "unknown", "no baseline value to compare: %s" % detail
        return ("same" if value == then else "changed"), detail
    if state == "missing" and then is None:
        # never found, so it cannot have gone missing: an item this check
        # does not understand is unknown, never stale
        return "unknown", "never found: %s" % detail
    return state, detail


def check_skill(root, repo, folder, name, *, remote=None, now=None):
    """Check one approved skill against its baseline (taken when it has none,
    or when the skill changed since it was taken) and keep the result. A
    pin is compared with what it pins; any other item with no baseline value
    yet takes the one it has now."""
    metadata, commit, tree = _metadata(root, repo, folder, name)
    base = _read(baseline_path(root, repo, folder, name))
    if base is None or base.get("tree") != tree:
        # only a change to this skill's own folder takes a new baseline: an
        # approval of another skill moves the commit, not this tree
        base = capture_baseline(root, repo, folder, name, now=now,
                                origin="first check" if base is None else "skill changed")
    own = remote is None
    remote = remote or Remote()
    adopted = False
    try:
        home = owner_home(root, folder)
        mem = _Memory()
        rows = []
        for of, item in items_of(metadata):
            kind, parts = classify(of, item)
            read_live, bare = live(of, item)
            kept = (base.get("items") or {}).setdefault(item, {"of": of, "kind": kind,
                                                               "value": None})
            if kind in ("tool", "memory") and kept.get("kind") in ("tool", "memory"):
                # a bare word is a tool when PATH finds it; this process's
                # PATH may not, so the baseline's reading stands
                kind, parts = kept["kind"], (bare,)
            then = kept.get("value")
            if kind in PINNED and kind != "file-pin" and not kept.get("reviewed"):
                # a pin is its own baseline, until the operator reviews a
                # change to it as still fine (rebaseline)
                try:
                    then = _pinned(kind, parts, remote=remote)
                except (OSError, SkillError, subprocess.SubprocessError) as err:
                    rows.append({"of": of, "item": item, "kind": kind, "state": "unknown",
                                 "then": None, "now": None, "detail": str(err)[:200]})
                    continue
            state, value, detail = fingerprint(kind, parts, root=root, home=home,
                                               remote=remote, mem=mem, hint=kept.get("path"))
            if read_live and state is None:
                # read afresh each run: that it is there is all that counts
                value = PRESENT
            if (kind not in PINNED or kept.get("reviewed")) and then is None and state is None:
                # no baseline value yet (a network item, one unreadable at
                # approval, or one reviewed while it was gone): this check's
                # value becomes it
                kept["value"], then, adopted = value, value, True
                if kind in ("tool", "tool-version"):
                    kept["path"] = detail
                detail = "baseline taken at this check: %s" % detail
            state, detail = _compare(state, value, detail, then)
            row = {"of": of, "item": item, "kind": kind, "state": state,
                   "then": then, "now": value, "detail": detail}
            if read_live:
                row["live"] = True
                row["detail"] = "read live, only its presence is checked: %s" % detail
            rows.append(row)
    finally:
        if own:
            remote.close()
    if adopted:
        _write(baseline_path(root, repo, folder, name), base)
    stale = any(r["state"] in ("changed", "missing") for r in rows if r["of"] != "source")
    update = any(r["state"] in ("changed", "missing") for r in rows if r["of"] == "source")
    result = {"skill": "%s:%s/%s" % (repo, folder, name), "commit": commit, "tree": tree,
              "at": _now_iso(now),
              "stale": stale, "update": update, "items": rows,
              "baseline": {"at": base.get("at"), "from": base.get("from")}}
    _write(check_path(root, repo, folder, name), result)
    return result


def last_check(root, repo, folder, name):
    return _read(check_path(root, repo, folder, name))


# ---------------------------------------------------------------- review

def review_rows(result):
    """The rows of a check that make its skill stale: what changed or went
    missing in reads and rests-on (an upstream change is news, not
    staleness)."""
    return [r for r in (result or {}).get("items") or []
            if r.get("of") != "source" and r.get("state") in ("changed", "missing")]


def staleness(root, repo, folder, name):
    """(view, sha256) of what a review of a stale skill covers: its last
    check's time and tree and the rows that make it stale. The console
    shows the view and sends the sha256 back (rebaseline)."""
    res = last_check(root, repo, folder, name)
    if res is None:
        raise SkillError("%s:%s/%s has not been checked yet" % (repo, folder, name))
    view = {"skill": res.get("skill"), "at": res.get("at"), "tree": res.get("tree"),
            "stale": bool(res.get("stale")), "items": review_rows(res)}
    return view, hashlib.sha256(json.dumps(view, sort_keys=True).encode()).hexdigest()


def rebaseline(root, repo, folder, name, *, user, sha, now=None):
    """The operator's "reviewed, still fine" for a stale skill, for the view
    whose sha256 the console showed: each item that changed takes the value
    that check read as its baseline, and one that went missing has none
    (unknown, never stale, until it is found again). A reviewed pin keeps
    its text and is compared with the reviewed value from then on. A
    record like an approval's says who did it and which items; the check
    is rewritten to match, so the skill reads ok at once. An edit of the
    skill takes a new baseline, as always, and the review goes with it."""
    view, shown = staleness(root, repo, folder, name)
    if shown != sha:
        raise SkillError("the check changed since it was shown; look again")
    if not view["items"]:
        raise SkillError("%s:%s/%s is not stale" % (repo, folder, name))
    _meta, commit, tree = _metadata(root, repo, folder, name)
    base = _read(baseline_path(root, repo, folder, name))
    if view["tree"] != tree or base is None or base.get("tree") != tree:
        raise SkillError("the skill changed since it was checked; check it again first")
    at = _now_iso(now)
    reviewed = {}
    for row in view["items"]:
        kept = base.setdefault("items", {}).setdefault(row["item"], {"of": row["of"],
                                                                      "kind": row["kind"]})
        kept["value"] = row.get("now") if row["state"] == "changed" else None
        kept["reviewed"] = {"at": at, "by": user}
        reviewed[row["item"]] = row["state"]
    _write(baseline_path(root, repo, folder, name), base)
    entry = skills.record(root, repo, user=user, action="rebaseline", commit=commit,
                          skill=name, folder=folder, diff_sha=sha, items=list(reviewed))
    res = last_check(root, repo, folder, name)
    for r in res.get("items") or []:
        if r.get("item") in reviewed and r.get("of") != "source":
            gone = reviewed[r["item"]] == "missing"
            r["state"] = "unknown" if gone else "same"
            r["then"] = None if gone else r.get("now")
            r["detail"] = "reviewed by %s at %s, still fine: %s" % (user, at, r.get("detail"))
    res["stale"] = bool(review_rows(res))
    _write(check_path(root, repo, folder, name), res)
    return entry


# ---------------------------------------------------------------- all of them

def approved_skills(root):
    """[(repo, folder, name)] of every skill approved in a trusted repository."""
    out = []
    for repo in skills.REPOS:
        st = skills.state(root, repo)
        if not st["trusted"] or not st["approved"]:
            continue
        path = skills.repo_dir(root, repo)
        for folder in skills._ls_dirs(path, st["approved"], "."):
            for name in skills._ls_dirs(path, st["approved"], folder):
                out.append((repo, folder, name))
    return out


def last_run(root):
    return _read(_base(root, "personal") / "checks" / LAST_RUN)


def due(root, *, now=None, every=CHECK_EVERY):
    """Whether a scheduled run is due: none yet, or the last began `every` ago."""
    last = _when((last_run(root) or {}).get("at"))
    return last is None or (now or time.time()) - last >= every


RUN_LOCK = "run.lock"


def _lock_path(root):
    return _base(root, "personal") / "checks" / RUN_LOCK


@contextmanager
def _run_lock(root):
    """One staleness run at a time: the loops daemon's and the console's
    never overlap. SkillError when another holds it."""
    path = _lock_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SkillError("a staleness run is already in progress") from None
        yield
    finally:
        os.close(fd)


def running(root):
    """Whether a staleness run holds the lock now."""
    path = _lock_path(root)
    if not path.exists():
        return False
    fd = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def check_all(root, *, now=None, budget=RUN_BUDGET, fetch=None, gap=None):
    """Check every approved skill, the private repository's too (its results
    stay under data/skills/private/), and keep a run summary of counts
    only. With `gap`, refuses (SkillError) when the last run began less
    than `gap` seconds ago."""
    now = now or time.time()
    last = _when((last_run(root) or {}).get("at"))
    if gap is not None and last is not None and now - last < gap:
        raise SkillError("the last check ran %ds ago; the next may run in %ds"
                         % (now - last, gap - (now - last)))
    summary = {"at": _now_iso(now), "checked": 0, "stale": 0, "updates": 0, "unknown": 0,
               "errors": 0, "by": user_agent()}
    with _run_lock(root), Remote(budget=budget, fetch=fetch) as remote:
        _write(_base(root, "personal") / "checks" / LAST_RUN, dict(summary, running=True))
        for repo, folder, name in approved_skills(root):
            try:
                result = check_skill(root, repo, folder, name, remote=remote, now=now)
            except (OSError, SkillError, ValueError):
                summary["errors"] += 1
                continue
            summary["checked"] += 1
            summary["stale"] += int(result["stale"])
            summary["updates"] += int(result["update"])
            summary["unknown"] += sum(1 for r in result["items"] if r["state"] == "unknown")
    summary["finished"] = _now_iso()
    _write(_base(root, "personal") / "checks" / LAST_RUN, summary)
    return summary


# ---------------------------------------------------------------- use

def usage_path(home):
    return Path(home).joinpath(*USAGE_FILE)


def record_use(home, skill, *, ok=True, now=None):
    """One use of `skill` ("plugin:name", the Skill tool's input) by the
    cousin at `home`: last time, count, whether that call went through."""
    skill = str(skill or "").strip()
    if not skill or len(skill) > 200:
        return None
    path = usage_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_name(path.name + ".lock")
    with open(lock, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        data = _read(path) or {}
        row = data.get(skill) if isinstance(data.get(skill), dict) else {}
        row = {"last": _now_iso(now), "count": int(row.get("count") or 0) + 1, "ok": bool(ok)}
        data[skill] = row
        _write(path, data)
    return row


def usage(home):
    return _read(usage_path(home)) or {}


def plugin_of(repo, folder):
    return skills.SHARED if folder == skills.SHARED else repo


def approved_at(root, repo, folder, name):
    """When the skill's current version was approved (the newest approve
    record for it), or None."""
    for rec in reversed(skills.records(root, repo)):
        if rec.get("action") == "approve" and rec.get("folder") == folder \
                and rec.get("skill") == name:
            return rec.get("at")
    return None


def usage_homes(root, folder, *, include_private=False):
    """The homes whose use of a skill in `folder` counts: the folder's own
    cousin for its own skill, every cousin loading personal for shared/. A
    cousin that loads private only when `include_private` (its use stays out
    of what other cousins and System show)."""
    out = []
    base = Path(root) / "cousins"
    for home in sorted(base.iterdir()) if base.is_dir() else []:
        if not (home / "cousin.toml").is_file():
            continue
        try:
            repos = skills.cousin_repos(home)
        except SkillError:
            continue
        if "private" in repos and not include_private:
            continue
        if folder == skills.SHARED:
            if "personal" in repos:
                out.append(home)
        elif home.name == folder:
            out.append(home)
    return out


def last_use(repo, folder, name, homes):
    """(iso, count) of the latest use across `homes`, or (None, 0)."""
    key = "%s:%s" % (plugin_of(repo, folder), name)
    best, count = None, 0
    for home in homes:
        row = usage(home).get(key)
        if not isinstance(row, dict):
            continue
        count += int(row.get("count") or 0)
        if best is None or (_when(row.get("last")) or 0) > (_when(best) or 0):
            best = row.get("last")
    return best, count


def unused(approved, last, *, now=None, days=UNUSED_DAYS):
    """Whether a skill has gone `days` without use, counted from the later
    of its approval and its last use."""
    since = max(t for t in (_when(approved), _when(last), 0) if t is not None)
    return since > 0 and (now or time.time()) - since >= days * 86400


def annotate(root, item, *, homes, now=None):
    """An approved-skill summary (skills.approved / skills.loaded) with
    `check` (its last result, items left out), `usage` (last, count) and
    `unused`. `homes` are the cousins whose use counts."""
    repo, folder, name = item["repo"], item["folder"], item["name"]
    res = last_check(root, repo, folder, name)
    check = None
    if res is not None:
        check = {"at": res.get("at"), "stale": res.get("stale"), "update": res.get("update"),
                 "changed": [r["item"] for r in res.get("items") or []
                             if r.get("state") in ("changed", "missing")],
                 "unknown": sum(1 for r in res.get("items") or [] if r.get("state") == "unknown")}
    last, count = last_use(repo, folder, name, homes)
    approved = approved_at(root, repo, folder, name)
    return dict(item, check=check, usage={"last": last, "count": count},
                approved_at=approved, unused=unused(approved, last, now=now))


# ---------------------------------------------------------------- text

def text_check(root, home, slug, name=None, *, now=None, gap=NOW_GAP):
    """Check the cousin's own loaded skills (or one of them) and say what
    came out. A skill checked less than `gap` seconds ago is not fetched
    again: its last result is shown."""
    lines = []
    found = False
    for repo, folder, plugin in skills.sources(slug, skills.cousin_repos(home)):
        st = skills.state(root, repo)
        if not st["trusted"]:
            continue
        for skill_name in skills._ls_dirs(skills.repo_dir(root, repo), st["approved"], folder):
            if name and name not in (skill_name, "%s:%s" % (plugin, skill_name)):
                continue
            found = True
            res = last_check(root, repo, folder, skill_name)
            fresh = res is not None and (now or time.time()) - (_when(res.get("at")) or 0) < gap
            if not fresh:
                try:
                    res = check_skill(root, repo, folder, skill_name, now=now)
                except SkillError as err:
                    lines.append("%s:%s - not checked: %s" % (plugin, skill_name, err))
                    continue
            verdict = "stale" if res["stale"] else "ok"
            if res["update"]:
                verdict += ", upstream changed"
            lines.append("%s:%s - %s (checked %s)" % (plugin, skill_name, verdict, res["at"]))
            for r in res["items"]:
                if r["state"] != "same":
                    lines.append("    %s %s: %s (%s)" % (r["of"], r["item"], r["state"],
                                                        r["detail"]))
    if name and not found:
        raise SkillError("no loaded skill %r for this cousin" % name)
    return "\n".join(lines) if lines else "no approved skills to check"


# ---------------------------------------------------------------- the module run

def main(argv=None):
    """python -m cousin_lib.skills_check --root R run [--gap SECONDS]: one
    staleness run of every approved skill (the console's "check now" starts
    it in a child process; the loops daemon calls check_all itself)."""
    import argparse
    import sys
    parser = argparse.ArgumentParser(prog="python -m cousin_lib.skills_check",
                                     description="check every approved skill for staleness")
    parser.add_argument("--root", required=True, help="the framework root")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("run", help="one run, read-only")
    p.add_argument("--gap", type=int, default=None,
                   help="refuse when the last run began less than this many seconds ago")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(check_all(args.root, gap=args.gap), sort_keys=True))
    except SkillError as err:
        print("skills check: %s" % err, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
