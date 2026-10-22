"""Skills (docs/reference/skills.md): procedures a cousin loads when it
needs them.

A skill is a folder holding a SKILL.md, the Agent Skills format: `name`
(the folder's name) and `description` (what it does and when to use it)
in the frontmatter, the framework's own keys under `metadata`, then the
steps, plus any files the steps use.

Skills live in git repositories under <install>/skills/<repo>/: `personal`
(shared/ plus a folder per cousin), `work` and `private` (a folder per
cousin). A cousin's cousin.toml `[skills] repos` names the ones it loads
(default `personal`); it loads `shared/` and its own folder in each, never
another cousin's.

A cousin writes a draft as plain files under <home>/skill-drafts/<name>/.
Nothing here lets a cousin approve one: approve_draft, reject_draft and
remove_skill are the console's (routes_skills), and the `skill` tool and
`cousin-skill` only list, show, draft and diff. An approval needs a
trigger test run on the diff shown (pass or fail, the operator's call),
commits on `main` with the repository's git identity and appends a
record (data/skills/approvals.jsonl; `private` keeps its own file), and
a repository loads only while `main` is at the commit its last record
names. A cousin that loads `private` keeps its own skills there, so its
folder, which is its name, never lands in another repository (law rule
11).

At every start the runner exports the approved state read-only to
<home>/run/skills/<plugin>/ and hands each to the agent as a local plugin
(export_plugins); the agent then sees `shared:<name>`, `personal:<name>`,
`work:<name>` or `private:<name>`, the plugin named for the repository and
never for the cousin. guard_tool is the gate's refusal for a tool call
that would write the repositories, the exports or the records."""
from __future__ import annotations

import argparse
import difflib
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path

from cousin_lib import atomic, jsonl

REPOS = ("personal", "work", "private")
SHARED = "shared"
SCOPES = ("own", "shared")
SIDE_EFFECTS = ("read-only", "writes-local", "outward")
TRIGGER_KEYS = ("should-trigger", "must-not-trigger")
# What a skill reaches and where it came from. Absent is not declared;
# "" declares none (`hosts: ""` contacts no host). The list keys are
# comma-separated strings, since the format's metadata values are strings.
DECLARED_KEYS = ("hosts", "reads", "rests-on", "source")
LIST_KEYS = ("hosts", "reads", "rests-on")
SKILL_FILE = "SKILL.md"
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
HOST_RE = re.compile(r"^(?=.{1,253}$)%s(?:\.%s)*$" % (_LABEL, _LABEL))
# a full commit: a prefix can turn ambiguous as a repository grows, and a
# fetch by commit needs it whole
COMMIT = r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})"
READS_RE = re.compile(r"^(?:[^\s@#,]+@%s:\S+|[^\s#,]+#sha256:[0-9a-fA-F]{64})$" % COMMIT)
# a URL with a scheme and a host, or git's user@host:path; then the full
# commit, and the skill's folder in that repository
SOURCE_RE = re.compile(r"^(?P<url>[A-Za-z][A-Za-z0-9+.-]*://[^\s/@]+(?:/\S*)?"
                       r"|[^\s@:/]+@[^\s@:/]+:\S+)@(?P<commit>%s)(?::(?P<folder>[^\s:]\S*))?$"
                       % COMMIT)
LICENSE_RE = re.compile(r"^[A-Za-z0-9.+()-]+(?: (?:AND|OR|WITH) [A-Za-z0-9.+()-]+)*$")
NAME_MAX = 64
DESCRIPTION_MAX = 1024
DRAFTS = "skill-drafts"
EXPORT = ("run", "skills")
BRANCH = "main"
DRAFT_MAX_BYTES = 2 * 1024 * 1024
# git under <install>/skills/ is read-only to a tool call: these pass, every
# other subcommand there is refused (an allowlist, so reset, checkout,
# rebase and anything new are covered).
GIT_READ = ("log", "show", "diff", "status", "cat-file", "ls-files")
# Importing a skill from outside is the operator's act (docs/reference/
# skills.md, "Who does what"): the console's import form, or cousin-skill
# import in the operator's own terminal. A cousin suggests one.
IMPORT_REFUSAL = ("importing a skill from outside is the operator's act: suggest it to them,"
                  " naming its source <https url>@<full commit>:<folder>; they import it in the"
                  " console (the cousin's inspector, skills, import) or with"
                  " `cousin-skill --home <home> import` in their own terminal")
WRITE_PATH_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")


class SkillError(ValueError):
    """A skill, a draft or a repository the caller cannot use as asked."""


# ---------------------------------------------------------------- SKILL.md

def _unquote(value):
    """A YAML scalar's text: a double-quoted one read as the JSON string
    render() writes (its escapes decoded), a single-quoted one with ''
    as a quote, a plain one as it stands. SkillError for a double-quoted
    one JSON does not read (an escape only YAML has, such as \\x41): kept
    as written, it would differ from what the agent's YAML reader loads."""
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        if value[0] == "'":
            return value[1:-1].replace("''", "'")
        try:
            out = json.loads(value)
        except ValueError:
            out = None
        if not isinstance(out, str):
            raise SkillError("frontmatter value %s: a double-quoted value takes JSON's"
                             " escapes only" % value)
        return out
    return value


def parse(text):
    """(frontmatter, body) of a SKILL.md. The frontmatter is the YAML
    subset the format uses: `key: value` lines between the leading `---`
    lines, and `metadata:` followed by indented `key: value` lines, every
    value a string. A key with an empty plain value is absent (YAML's
    null); only `""` is the empty string. SkillError when there is none
    or a line does not parse."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        raise SkillError("SKILL.md must start with a --- frontmatter line")
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        raise SkillError("SKILL.md frontmatter has no closing --- line") from None
    front, metadata, in_meta = {}, None, False
    for raw in lines[1:end]:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indented = raw[:1] in (" ", "\t")
        key, sep, value = raw.strip().partition(":")
        if not sep or not key.strip():
            raise SkillError("frontmatter line does not parse: %r" % raw.strip())
        key = key.strip()
        if indented:
            if not in_meta:
                raise SkillError("indented frontmatter line outside metadata: %r" % raw.strip())
            if value.strip():               # an empty plain value is null: absent
                metadata[key] = _unquote(value)
            continue
        in_meta = key == "metadata" and not value.strip()
        if in_meta:
            metadata = front["metadata"] = {}
        elif value.strip():
            front[key] = _unquote(value)
    return front, "\n".join(lines[end + 1:]).lstrip("\n")


def render(front, body):
    """A SKILL.md from a frontmatter dict (metadata nested) and a body."""
    return frontmatter_text(front) + "\n" + body.strip() + "\n"


def frontmatter_text(front, comment=None):
    """The frontmatter of a SKILL.md, its `---` lines included, with a
    `# comment` line first when given. Every value but the name is
    double-quoted, so a colon, a quote or a leading `[` reads back as the
    same text in parse() and in a YAML reader."""
    out = ["---"] + (["# %s" % comment] if comment else [])
    for key, value in front.items():
        if key == "metadata":
            continue
        out.append("%s: %s" % (key, value if key == "name" else _quoted(value)))
    meta = front.get("metadata") or {}
    if meta:
        out.append("metadata:")
        out.extend("  %s: %s" % (k, _quoted(v)) for k, v in meta.items())
    out.append("---")
    return "\n".join(out) + "\n"


_UNPRINTABLE = re.compile(r"[\x7f-\x84\x86-\x9f\ud800-\udfff\ufffe\uffff]")


def _quoted(value):
    """A YAML double-quoted scalar: a JSON string, whose escapes YAML
    shares, with the controls JSON leaves raw escaped too."""
    return _UNPRINTABLE.sub(lambda m: "\\u%04x" % ord(m.group()),
                            json.dumps(str(value), ensure_ascii=False))


def problems(folder_name, front):
    """What keeps a skill from being loaded or approved, as sentences; []
    when it is sound."""
    out = []
    meta = front.get("metadata") or {}
    name = front.get("name", "")
    if name != folder_name:
        out.append("name %r is not the folder's name %r" % (name, folder_name))
    if not NAME_RE.match(folder_name) or len(folder_name) > NAME_MAX:
        out.append("the name must be 1-%d lowercase letters, digits and single '-',"
                   " not at either end" % NAME_MAX)
    description = front.get("description", "")
    if not description.strip():
        out.append("description is missing")
    elif len(description) > DESCRIPTION_MAX:
        out.append("description is over %d characters" % DESCRIPTION_MAX)
    context = meta.get("context", "")
    if context not in REPOS:
        out.append("metadata.context must be one of %s" % ", ".join(REPOS))
    if meta.get("side-effect", "") not in SIDE_EFFECTS:
        out.append("metadata.side-effect must be one of %s" % ", ".join(SIDE_EFFECTS))
    scope = meta.get("scope", "own")
    if scope not in SCOPES:
        out.append("metadata.scope must be own or shared")
    elif scope == SHARED and context != "personal":
        out.append("a shared skill lives in personal: metadata.context must be personal")
    return out


def _items(value):
    return [item.strip() for item in str(value).split(",") if item.strip()]


def declared_problems(front):
    """What is wrong with the keys that say what a skill reaches and where
    it came from (DECLARED_KEYS, and the licence a skill from outside
    needs), as sentences: the formats docs/reference/skills.md gives them.
    The draft tool and approval check these; loading does not, so a rule
    here never unloads an approved skill."""
    out = []
    meta = front.get("metadata") or {}
    bad = [h for h in _items(meta.get("hosts", "")) if not HOST_RE.match(h)]
    if bad:
        out.append("metadata.hosts takes host names, comma-separated, not %s" % ", ".join(bad))
    bad = [r for r in _items(meta.get("reads", "")) if not READS_RE.match(r)]
    if bad:
        out.append("metadata.reads takes repo@<full commit>:path or path#sha256:<64 hex>,"
                   " comma-separated, not %s" % ", ".join(bad))
    source = str(meta.get("source", "")).strip()
    if source and not SOURCE_RE.match(source):
        out.append("metadata.source takes <url>@<full commit>[:<folder>], not %s" % source)
    licence = str(front.get("license", "")).strip()
    if licence and not LICENSE_RE.match(licence):
        out.append("license takes an SPDX identifier, not %s" % licence)
    elif source and not licence:
        out.append("a skill from outside (metadata.source) needs a license")
    return out


def _normal(key, value):
    """A metadata value as written: a list key's items joined by ", "."""
    value = str(value)
    return ", ".join(_items(value)) if key in LIST_KEYS else value.strip()


# ---------------------------------------------------------------- repositories

def repos_dir(root):
    return Path(root) / "skills"


def repo_dir(root, repo):
    if repo not in REPOS:
        raise SkillError("no repository %r (one of %s)" % (repo, ", ".join(REPOS)))
    return repos_dir(root) / repo


def _git(repo_path, *args, check=True, text=True, env_extra=None):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    env.update(env_extra or {})
    proc = subprocess.run(["git", "-C", str(repo_path), *args], capture_output=True,
                          text=text, env=env, timeout=60)
    if check and proc.returncode != 0:
        err = proc.stderr if text else proc.stderr.decode("utf-8", "replace")
        raise SkillError("git %s: %s" % (args[0], (err or "").strip() or "exit %d" % proc.returncode))
    return proc.stdout


def _records_path(root, repo):
    base = Path(root) / "data" / "skills"
    return base / "private" / "approvals.jsonl" if repo == "private" else base / "approvals.jsonl"


def record(root, repo, *, user, action, commit, skill=None, folder=None, diff_sha=None,
           trigger=None, source=None, dry_run=None):
    """Append one approval record: who did it in the console, to which
    repository, the commit it produced, the sha256 of the diff shown and,
    for an approval, whether its trigger test passed; for a skill from
    outside, also its source and how its dry run ended. `private` keeps
    its own file, since a record names the folder and a private cousin's
    folder is its name."""
    path = _records_path(root, repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    entry = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "repo": repo, "action": action, "user": user, "commit": commit,
             "folder": folder, "skill": skill, "diff_sha256": diff_sha}
    if trigger is not None:
        entry["trigger"] = trigger
    if source is not None:
        entry["source"] = source
    if dry_run is not None:
        entry["dry_run"] = dry_run
    fresh = not path.exists()
    jsonl.append_line(path, json.dumps(entry, sort_keys=True), fsync=True)
    if fresh:
        os.chmod(path, 0o600)
    return entry


def records(root, repo):
    path = _records_path(root, repo)
    out = []
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and entry.get("repo") == repo:
            out.append(entry)
    return out


def ensure_repo(root, repo, *, user):
    """The repository, created when it is missing: `git init` on main, one
    empty commit, and its `init` record. Returns its path."""
    path = repo_dir(root, repo)
    if (path / ".git").is_dir():
        return path
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", BRANCH)
    _git(path, "commit", "-q", "--allow-empty", "-m",
         "chore(skills): start the %s repository" % repo)
    record(root, repo, user=user, action="init", commit=_head(path))
    return path


def _head(path):
    return _git(path, "rev-parse", "--verify", "-q", "refs/heads/%s" % BRANCH).strip()


def state(root, repo):
    """{exists, head, approved, trusted}: trusted only while main is at the
    commit the repository's last record names."""
    path = repo_dir(root, repo)
    if not (path / ".git").is_dir():
        return {"repo": repo, "exists": False, "head": None, "approved": None, "trusted": False}
    try:
        head = _head(path) or None
    except SkillError:
        head = None
    recs = records(root, repo)
    approved = recs[-1]["commit"] if recs else None
    return {"repo": repo, "exists": True, "head": head, "approved": approved,
            "trusted": bool(head) and head == approved}


def _ls_dirs(path, commit, folder):
    out = _git(path, "ls-tree", "-d", "--name-only", commit, "%s/" % folder, check=False)
    return sorted(line.rsplit("/", 1)[-1] for line in out.splitlines() if line.strip())


def _show(path, commit, file_path):
    proc = subprocess.run(["git", "-C", str(path), "show", "%s:%s" % (commit, file_path)],
                          capture_output=True, timeout=60)
    return proc.stdout if proc.returncode == 0 else None


def approved(root, repo, folder):
    """The skills in `folder` at the repository's approved commit:
    [{name, description, metadata, problems}]. [] for a repository that
    is missing or not trusted."""
    st = state(root, repo)
    if not st["trusted"]:
        return []
    path = repo_dir(root, repo)
    out = []
    for name in _ls_dirs(path, st["approved"], folder):
        raw = _show(path, st["approved"], "%s/%s/%s" % (folder, name, SKILL_FILE))
        out.append(_summary(name, raw, repo=repo, folder=folder))
    return out


def _summary(name, raw, **extra):
    try:
        front, _body = parse(raw.decode("utf-8")) if raw is not None else ({}, "")
        found = problems(name, front) if raw is not None else ["no SKILL.md"]
    except (SkillError, UnicodeDecodeError) as err:
        front, found = {}, [str(err)]
    return dict(extra, name=name, description=front.get("description", ""),
                metadata=front.get("metadata") or {}, problems=found)


def show_approved(root, repo, folder, name):
    """{name, text, files} of one approved skill; SkillError when absent."""
    st = state(root, repo)
    if not st["trusted"]:
        raise SkillError("repository %s is not in an approved state" % repo)
    path = repo_dir(root, repo)
    raw = _show(path, st["approved"], "%s/%s/%s" % (folder, name, SKILL_FILE))
    if raw is None:
        raise SkillError("no skill %s:%s/%s" % (repo, folder, name))
    files = _git(path, "ls-tree", "-r", "--name-only", st["approved"], "%s/%s/" % (folder, name))
    return {"repo": repo, "folder": folder, "name": name,
            "text": raw.decode("utf-8", "replace"),
            "files": [f.split("/", 2)[2] for f in files.splitlines() if f.count("/") >= 2]}


# ---------------------------------------------------------------- cousins

def cousin_repos(home):
    """The repositories a cousin loads: cousin.toml `[skills] repos`,
    default ["personal"]. SkillError for an unknown name."""
    try:
        data = tomllib.loads((Path(home) / "cousin.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return ["personal"]
    repos = (data.get("skills") or {}).get("repos", ["personal"])
    if not isinstance(repos, list) or not all(isinstance(r, str) for r in repos):
        raise SkillError("cousin.toml [skills] repos must be a list of names")
    bad = [r for r in repos if r not in REPOS]
    if bad:
        raise SkillError("cousin.toml [skills] repos: unknown %s (one of %s)"
                         % (", ".join(bad), ", ".join(REPOS)))
    return list(dict.fromkeys(repos))


def placement_problem(home, repo, scope):
    """Why a draft for `repo` with `scope` cannot be this cousin's, as a
    sentence, or None. It must target a repository the cousin loads, and
    a cousin that loads `private` keeps its own skills there: its folder
    is its name, which then never lands in a repository other cousins
    load (law rule 11)."""
    try:
        repos = cousin_repos(home)
    except SkillError as err:
        return str(err)
    if repo not in repos:
        return ("it targets %s, which this cousin does not load (cousin.toml [skills] repos)"
                % repo)
    if scope != SHARED and repo != "private" and "private" in repos:
        return ("a cousin that loads private keeps its own skills there:"
                " metadata.context must be private")
    return None


def sources(slug, repos):
    """What a cousin loads: [(repo, folder, plugin)], shared/ first."""
    out = []
    for repo in repos:
        if repo == "personal":
            out.append((repo, SHARED, SHARED))
        out.append((repo, slug, repo))
    return out


def loaded(root, home, slug):
    """[{plugin, repo, folder, name, description, metadata, problems}] the
    cousin loads at its next start, with each repository's state."""
    repos = cousin_repos(home)
    out = []
    for repo, folder, plugin in sources(slug, repos):
        for item in approved(root, repo, folder):
            out.append(dict(item, plugin=plugin))
    return out


# ---------------------------------------------------------------- drafts

def drafts_dir(home):
    return Path(home) / DRAFTS


def _draft_path(home, name):
    if not NAME_RE.match(name or "") or len(name) > NAME_MAX:
        raise SkillError("bad skill name %r" % name)
    return drafts_dir(home) / name


def _files(folder):
    out = {}
    for path in sorted(folder.rglob("*")):
        if path.is_symlink():
            raise SkillError("a draft holds no symlinks: %s" % path.relative_to(folder))
        if path.is_file():
            out[str(path.relative_to(folder))] = path.read_bytes()
    total = sum(len(v) for v in out.values())
    if total > DRAFT_MAX_BYTES:
        raise SkillError("draft is %d bytes, over the %d limit" % (total, DRAFT_MAX_BYTES))
    return out


def target(slug, front):
    """(repo, folder) a draft goes to: metadata.context names the
    repository, metadata.scope `shared` puts it in personal's shared/."""
    meta = front.get("metadata") or {}
    repo = meta.get("context", "")
    if repo not in REPOS:
        raise SkillError("metadata.context must be one of %s" % ", ".join(REPOS))
    if meta.get("scope", "own") == SHARED:
        return repo, SHARED
    if slug == SHARED:
        # its own folder would be the one every cousin loads
        raise SkillError("a cousin named %s has no folder of its own" % SHARED)
    return repo, slug


def draft_problems(home, slug, name, front):
    """problems(), plus what a draft also needs before approval: sound
    declared keys (declared_problems), what a skill from outside needs
    (outside_problems), a target this cousin may approve into
    (placement_problem) and both trigger test prompts."""
    found = problems(name, front) + declared_problems(front)
    found += [p for p in outside_problems(home, name, front) if p not in found]
    meta = front.get("metadata") or {}
    try:
        repo, _where = target(slug, front)
    except SkillError as err:
        if str(err) not in found:
            found.append(str(err))
    else:
        why = placement_problem(home, repo, meta.get("scope", "own"))
        if why:
            found.append(why)
    missing = [k for k in TRIGGER_KEYS if not str(meta.get(k, "")).strip()]
    if missing:
        found.append("the trigger test needs %s in metadata" % " and ".join(missing))
    return found


def drafts(home, slug):
    """[{name, description, metadata, problems, repo, folder}] of a
    cousin's drafts, `problems` being everything that keeps one from
    approval (draft_problems)."""
    out = []
    base = drafts_dir(home)
    if not base.is_dir():
        return out
    for folder in sorted(p for p in base.iterdir() if p.is_dir() and not p.name.startswith(".")):
        skill = folder / SKILL_FILE
        raw = skill.read_bytes() if skill.is_file() else None
        item = _summary(folder.name, raw)
        item["repo"], item["folder"] = None, None
        out.append(item)
        try:
            front, _ = parse(raw.decode("utf-8")) if raw is not None else (None, "")
        except (SkillError, UnicodeDecodeError):
            continue  # _summary said why
        if front is None:
            continue
        item["problems"] = draft_problems(home, slug, folder.name, front)
        try:
            item["repo"], item["folder"] = target(slug, front)
        except SkillError:
            pass
    return out


def draft_written(home, slug, name, path):
    """What the tool and the CLI say once a draft is written: that the
    operator approves it, and what still keeps it from approval."""
    item = next((d for d in drafts(home, slug) if d["name"] == name), {})
    text = "draft written: %s (the operator approves it in the console)" % path
    if item.get("problems"):
        text += "; before approval: %s" % "; ".join(item["problems"])
    return text


def show_draft(home, name):
    folder = _draft_path(home, name)
    if not (folder / SKILL_FILE).is_file():
        raise SkillError("no draft %s" % name)
    return {"name": name, "text": (folder / SKILL_FILE).read_text(errors="replace"),
            "files": sorted(_files(folder))}


def write_draft(home, name, *, description, body, context, side_effect,
                scope="own", metadata=None, license=None):
    """Write or replace a draft's SKILL.md; returns its path. Its other
    files are the cousin's own to write in the same folder. A metadata
    value of None is left out, and so is "" but for DECLARED_KEYS, where
    "" declares none. Refused when the format or a declared key is
    broken, or the target is not this cousin's to approve into
    (placement_problem); missing trigger prompts are left to the
    listing, which names them."""
    folder = _draft_path(home, name)
    meta = {"context": context, "side-effect": side_effect, "scope": scope}
    for key, value in (metadata or {}).items():
        if value is None or (str(value).strip() == "" and key not in DECLARED_KEYS):
            continue
        meta[key] = _normal(key, value)
    front = {"name": name, "description": " ".join(str(description).split())}
    if license is not None and str(license).strip():
        front["license"] = str(license).strip()
    front["metadata"] = meta
    found = problems(name, front) + declared_problems(front)
    if not found:
        why = placement_problem(home, context, scope)
        found = [why] if why else []
    if found:
        raise SkillError("; ".join(found))
    folder.mkdir(parents=True, exist_ok=True)
    atomic.write_text(folder / SKILL_FILE, render(front, body or ""))
    return folder / SKILL_FILE


def _approved_files(root, repo, folder, name):
    st = state(root, repo)
    if not st["exists"] or not st["approved"]:
        return {}
    path = repo_dir(root, repo)
    listing = _git(path, "ls-tree", "-r", "--name-only", st["approved"],
                   "%s/%s/" % (folder, name), check=False)
    out = {}
    for f in listing.splitlines():
        rel = f.split("/", 2)[2] if f.count("/") >= 2 else None
        if rel:
            out[rel] = _show(path, st["approved"], f) or b""
    return out


def _unified(old, new, label):
    out = []
    for rel in sorted(set(old) | set(new)):
        a, b = old.get(rel), new.get(rel)
        if a == b:
            continue
        try:
            a_lines = a.decode("utf-8").splitlines(keepends=True) if a is not None else []
            b_lines = b.decode("utf-8").splitlines(keepends=True) if b is not None else []
        except UnicodeDecodeError:
            out.append("binary %s/%s %s\n" % (label, rel, "changed" if a and b
                                                else ("added" if b else "removed")))
            continue
        out.extend(difflib.unified_diff(
            a_lines, b_lines, fromfile="approved/%s/%s" % (label, rel) if a is not None else "/dev/null",
            tofile="draft/%s/%s" % (label, rel) if b is not None else "/dev/null"))
    return "".join(out)


def diff_draft(root, home, slug, name):
    """(diff text, sha256) of a draft against what is approved at its
    target; the console shows the text and approves only that sha."""
    folder = _draft_path(home, name)
    if not (folder / SKILL_FILE).is_file():
        raise SkillError("no draft %s" % name)
    front, _ = parse((folder / SKILL_FILE).read_text())
    repo, where = target(slug, front)
    text = _unified(_approved_files(root, repo, where, name), _files(folder),
                    "%s:%s/%s" % (repo, where, name))
    return text, hashlib.sha256(text.encode()).hexdigest()


# ---------------------------------------------------------------- the console's verbs

def _stamp():
    return time.strftime("%Y%m%d-%H%M%S")


def approve_draft(root, home, slug, name, *, user, diff_sha, fetch=None):
    """Commit a draft on main and record it: only for the diff whose sha
    the console showed, and only once a trigger test has run on that
    very diff. A failed test does not refuse: the operator saw it, and
    the record says so. A skill from outside also needs to be what its
    source published, past the frontmatter (fetched again here), and a
    dry run of this version; a failed dry run, like a failed trigger
    test, is the operator's call. The draft folder goes once it is
    committed."""
    folder = _draft_path(home, name)
    if not (folder / SKILL_FILE).is_file():
        raise SkillError("no draft %s" % name)
    front, _ = parse((folder / SKILL_FILE).read_text())
    found = draft_problems(home, slug, name, front)
    if found:
        raise SkillError("draft %s cannot be approved: %s" % (name, "; ".join(found)))
    repo, where = target(slug, front)
    path = ensure_repo(root, repo, user=user)
    if not state(root, repo)["trusted"]:
        raise SkillError("repository %s has moved past its last approval; nothing is"
                         " approved until that is resolved" % repo)
    text, sha = diff_draft(root, home, slug, name)
    if sha != diff_sha:
        raise SkillError("the draft changed since it was shown; look again")
    if not text:
        raise SkillError("the draft is the same as what is approved")
    tested = last_trigger_test(root, home, slug, name, sha)
    if tested is None:
        raise SkillError("draft %s has no trigger test on this version; run it first" % name)
    source = (front.get("metadata") or {}).get("source") or None
    ran = None
    if source:
        _source, differ, _got = upstream_differences(home, name, fetch=fetch)
        if differ:
            raise SkillError("draft %s is not what %s published: %s differ%s; a skill from"
                             " outside is approved as published" % (
                                 name, source, ", ".join(differ), "" if len(differ) > 1 else "s"))
        ran = last_dry_run(root, home, slug, name, sha)
        if ran is None:
            raise SkillError("draft %s has no dry run on this version: allow one, and the"
                             " cousin runs it and records its job" % name)
    dest = path / where / name
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(folder, dest, symlinks=False)
    _git(path, "add", "-A", "--", "%s/%s" % (where, name))
    _git(path, "commit", "-q", "-m", "feat(skills): approve %s/%s" % (where, name))
    commit = _head(path)
    entry = record(root, repo, user=user, action="approve", commit=commit,
                   skill=name, folder=where, diff_sha=sha,
                   trigger="pass" if tested.get("pass") is True else "fail", source=source,
                   dry_run=None if ran is None else ("pass" if ran.get("pass") else "fail"))
    shutil.rmtree(folder)
    try:
        # the baseline its staleness is judged against (skills_check); a
        # failure here never undoes an approval: the first check takes it
        from cousin_lib import skills_check
        skills_check.capture_baseline(root, repo, where, name)
    except Exception:  # noqa: BLE001
        pass
    return entry


def reject_draft(home, name, *, user, reason=""):
    """Move a draft aside, with a note saying who rejected it and why."""
    folder = _draft_path(home, name)
    if not folder.is_dir():
        raise SkillError("no draft %s" % name)
    dest = drafts_dir(home) / ".rejected" / ("%s-%s" % (name, _stamp()))
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(folder), str(dest))
    atomic.write_text(dest / "REJECTED.txt",
                      "rejected by %s at %s\n%s\n" % (user, datetime.now(timezone.utc)
                                                      .isoformat(timespec="seconds"),
                                                      reason.strip()))
    return dest


def removal_diff(root, repo, folder, name):
    """(diff text, sha256) of deleting an approved skill."""
    old = _approved_files(root, repo, folder, name)
    if not old:
        raise SkillError("no approved skill %s:%s/%s" % (repo, folder, name))
    text = _unified(old, {}, "%s:%s/%s" % (repo, folder, name))
    return text, hashlib.sha256(text.encode()).hexdigest()


def remove_skill(root, repo, folder, name, *, user, diff_sha):
    """Delete an approved skill on main and record it, for the shown diff."""
    if not state(root, repo)["trusted"]:
        raise SkillError("repository %s has moved past its last approval" % repo)
    _text, sha = removal_diff(root, repo, folder, name)
    if sha != diff_sha:
        raise SkillError("the skill changed since it was shown; look again")
    path = repo_dir(root, repo)
    _git(path, "rm", "-r", "-q", "--", "%s/%s" % (folder, name))
    _git(path, "commit", "-q", "-m", "feat(skills): remove %s/%s" % (folder, name))
    return record(root, repo, user=user, action="remove", commit=_head(path),
                  skill=name, folder=folder, diff_sha=sha)


# ---------------------------------------------------------------- loading

def export_dir(home):
    return Path(home).joinpath(*EXPORT)


def _writable_tree(path):
    for dirpath, dirnames, filenames in os.walk(path):
        os.chmod(dirpath, 0o700)
        for f in filenames:
            p = os.path.join(dirpath, f)
            if not os.path.islink(p):
                os.chmod(p, 0o600)


def _readonly_tree(path):
    """Every file read-only, a script keeping its executable bit (0555), so
    an exported skill's scripts run as its steps say."""
    for dirpath, dirnames, filenames in os.walk(path, topdown=False):
        for f in filenames:
            p = os.path.join(dirpath, f)
            if not os.path.islink(p):
                os.chmod(p, 0o555 if os.stat(p).st_mode & 0o111 else 0o444)
        os.chmod(dirpath, 0o555)


def _extract(path, commit, folder, dest):
    """The approved `folder` of a repository into dest/skills/, its prefix
    dropped; regular files and directories only, a file git keeps
    executable (100755) staying executable."""
    data = subprocess.run(["git", "-C", str(path), "archive", "--format=tar", commit,
                           "%s/" % folder], capture_output=True, timeout=120)
    if data.returncode != 0:
        return []
    out = dest / "skills"
    out.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data.stdout)) as tf:
        for member in tf.getmembers():
            rel = Path(member.name)
            if len(rel.parts) < 2 or rel.parts[0] != folder or ".." in rel.parts \
                    or rel.is_absolute():
                continue
            target_path = out.joinpath(*rel.parts[1:])
            if member.isdir():
                target_path.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target_path.parent.mkdir(parents=True, exist_ok=True)
                target_path.write_bytes(tf.extractfile(member).read())
                if member.mode & 0o111:
                    os.chmod(target_path, 0o755)
    return sorted(p.name for p in out.iterdir() if p.is_dir())


def export_plugins(root, home, slug, *, base=None):
    """Export what the cousin loads, afresh, and return (plugin dirs,
    report). Each directory is a local plugin (a manifest plus skills/),
    read-only, named for the repository; a skill that does not parse is
    left out and reported, and so is a repository that is missing or has
    moved past its last approval. Never raises for a skill problem."""
    base = Path(base) if base is not None else export_dir(home)
    if base.exists():
        _writable_tree(base)
        shutil.rmtree(base)
    report = {"loaded": [], "skipped": []}
    try:
        repos = cousin_repos(home)
    except SkillError as err:
        report["skipped"].append({"what": "cousin.toml", "why": str(err)})
        return [], report
    paths, untrusted = [], set()
    for repo, folder, plugin in sources(slug, repos):
        st = state(root, repo)
        if not st["exists"]:
            continue
        if not st["trusted"]:
            if repo not in untrusted:
                untrusted.add(repo)
                report["skipped"].append({"what": repo, "why": "main has moved past its"
                                          " last approval; nothing loads from it"})
            continue
        dest = base / plugin
        names = _extract(repo_dir(root, repo), st["approved"], folder, dest)
        kept = []
        for name in names:
            skill = dest / "skills" / name / SKILL_FILE
            try:
                front, _ = parse(skill.read_text())
                found = problems(name, front)
            except (OSError, SkillError, UnicodeDecodeError) as err:
                found = [str(err)]
            if found:
                shutil.rmtree(dest / "skills" / name)
                report["skipped"].append({"what": "%s:%s" % (plugin, name), "why": "; ".join(found)})
            else:
                kept.append(name)
                report["loaded"].append("%s:%s" % (plugin, name))
        if not kept:
            if dest.exists():
                shutil.rmtree(dest)
            continue
        manifest = dest / ".claude-plugin" / "plugin.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps({"name": plugin, "version": "1.0.0",
                                        "description": "approved %s skills" % plugin}) + "\n")
        _readonly_tree(dest)
        paths.append(dest)
    if base.exists():
        os.chmod(base, 0o755)
    return paths, report


def _loads_private(home):
    """Whether the cousin loads `private`; True when its list does not
    parse, so a doubt keeps its traces with private's."""
    try:
        return "private" in cousin_repos(home)
    except SkillError:
        return True


def trigger_path(root, home, slug, name):
    """Where a draft's last trigger test result is kept: under
    <install>/data/skills/, which guard_tool refuses to tools (best-effort
    for Bash), so a session does not write the pass the approve view shows
    by accident; never inside the draft,
    where it would change the draft's own diff. A cousin that loads
    `private` keeps its results beside private's records, since the path
    names it."""
    base = Path(root) / "data" / "skills"
    if _loads_private(home):
        base = base / "private"
    return base / "trigger-tests" / slug / ("%s.json" % name)


def trigger_test(root, home, slug, name, *, checker=None):
    """Run a draft's trigger test and keep its result (trigger_path): its
    should-trigger prompt and its must-not-trigger prompt, each one turn on
    the cousin's own account and model, with what the cousin loads plus
    the draft as `draft:<name>`. Runs in a child process of the console
    (`cousin-skill --home H trigger-test NAME`), since the check scrubs
    the process's auth variables while it runs."""
    folder = _draft_path(home, name)
    if not (folder / SKILL_FILE).is_file():
        raise SkillError("no draft %s" % name)
    front, _ = parse((folder / SKILL_FILE).read_text())
    meta = front.get("metadata") or {}
    missing = [k for k in TRIGGER_KEYS if not str(meta.get(k, "")).strip()]
    if missing:
        raise SkillError("draft %s has no %s prompt in its metadata" % (name, " or ".join(missing)))
    _text, sha = diff_draft(root, home, slug, name)
    if checker is None:
        from cousin_lib import accounts
        from cousin_lib.runner.main import _agent_table, effort_of
        from cousin_lib.runner.sdk import skill_trigger_check
        account = accounts.for_cousin(home, root)
        agent = _agent_table(home)
        model, effort = agent.get("model"), effort_of(agent)

        def checker(prompt, plugin_dirs):
            return skill_trigger_check(account, root, prompt=prompt, model=model,
                                       effort=effort, plugin_dirs=plugin_dirs)
    else:
        model = "test"
    expect = "draft:%s" % name
    with tempfile.TemporaryDirectory(prefix="cousin-trigger-plugins-") as tmp:
        plugins, _report = export_plugins(root, home, slug, base=Path(tmp) / "loaded")
        draft = Path(tmp) / "draft"
        (draft / ".claude-plugin").mkdir(parents=True)
        (draft / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": "draft", "version": "1.0.0", "description": "the draft"}) + "\n")
        shutil.copytree(folder, draft / "skills" / name, symlinks=False)
        result = {"skill": expect, "diff_sha256": sha, "model": model,
                  "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        for key, wanted in (("should-trigger", True), ("must-not-trigger", False)):
            prompt = str(meta[key])
            fired, detail = checker(prompt, list(plugins) + [draft])
            result[key] = {"prompt": prompt, "fired": fired, "detail": detail,
                           "pass": (fired == expect) == wanted}
        result["pass"] = all(result[k]["pass"] for k in TRIGGER_KEYS)
    path = trigger_path(root, home, slug, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent.parent, 0o700)
    except OSError:
        pass
    atomic.write_text(path, json.dumps(result, indent=1, sort_keys=True) + "\n")
    return result


def last_trigger_test(root, home, slug, name, diff_sha):
    """The draft's last trigger test result, when it was run on this very
    diff; None otherwise (never run, or the draft changed since)."""
    try:
        result = json.loads(trigger_path(root, home, slug, name).read_text())
    except (OSError, ValueError):
        return None
    return result if isinstance(result, dict) and result.get("diff_sha256") == diff_sha else None


def project_skills_dir(home):
    """Where the tmux lane's project settings would find skills that never
    passed approval."""
    return Path(home) / ".claude" / "skills"


# ---------------------------------------------------------------- from outside

FETCH_SCHEMES = ("https",)
FETCH_TIMEOUT = 120
# git -c settings of every fetch: no protocol but the ones asked for, no
# credential helper, no hooks, and every object received checked, so a
# tree with a `..` or `.git` entry fails here, before _path_ok sees it.
FETCH_CONFIG = ("protocol.allow=never", "credential.helper=", "core.hooksPath=%s" % os.devnull,
                "fetch.fsckObjects=true")
DRY_RUN_KEY = "dry-run-task"
IMPORT_NOTE = ("written by the cousins framework at import; the rest of the folder is as"
               " published at metadata.source")
LICENSE_NAME = re.compile(r"^(?:LICEN[CS]E|COPYING|UNLICENSE)(?:[.-][A-Za-z0-9.-]+)?$", re.I)
# The open licences a skill from outside may carry, each as its SPDX id, the
# phrases its text holds and the ones it does not; the more specific first.
OPEN_LICENSES = (
    ("Apache-2.0", ("apache license", "version 2.0"), ()),
    ("BSD-3-Clause", ("redistribution and use in source and binary forms", "neither the name"), ()),
    ("BSD-2-Clause", ("redistribution and use in source and binary forms",), ("neither the name",)),
    ("MIT", ("permission is hereby granted, free of charge",), ()),
    ("ISC", ("permission to use, copy, modify, and", "distribute this software for any purpose"),
     ()),
    ("Unlicense", ("this is free and unencumbered software released into the public domain",), ()),
    ("CC0-1.0", ("cc0 1.0 universal",), ()),
    ("CC-BY-4.0", ("creative commons attribution 4.0 international",), ()),
)
OPEN_IDS = tuple(spdx for spdx, _has, _not in OPEN_LICENSES)
# The licences a skill from outside may carry, per repository it may go
# to; `work` takes none until the employer's policy is known.
OUTSIDE_LICENSES = {"personal": OPEN_IDS, "private": OPEN_IDS}
OUTSIDE_REPOS = tuple(OUTSIDE_LICENSES)
SCRIPT_SUFFIXES = (".py", ".sh", ".bash", ".zsh", ".js", ".mjs", ".cjs", ".ts", ".rb", ".pl",
                   ".ps1", ".php", ".lua")
UPSTREAM_KEPT = ("name", "description", "compatibility")


def _reads_as(text, spdx):
    norm = " ".join(str(text).lower().split())
    for name, has, has_not in OPEN_LICENSES:
        if name == spdx:
            return all(p in norm for p in has) and not any(p in norm for p in has_not)
    return False


def license_of(text):
    """The SPDX id of the open licence a licence file's text is, or None."""
    return next((spdx for spdx in OPEN_IDS if _reads_as(text, spdx)), None)


def _license_file(names):
    found = sorted(n for n in names if "/" not in n and LICENSE_NAME.match(n))
    return found[0] if found else None


def _path_ok(path):
    """Whether `path` may name a skill's folder or a file in it: relative,
    no part empty, `.`, `..` or `.git` (in any case), no backslash and no
    control character. A fetched tree is checked against it before a byte
    is written, whatever git let through."""
    text = str(path or "")
    return (bool(text) and "\\" not in text and not any(ord(c) < 32 or ord(c) == 127 for c in text)
            and all(part not in ("", ".", "..") and part.lower() != ".git"
                    for part in text.split("/")))


def split_source(source):
    """(url, commit, folder) of a metadata.source, the folder None when it
    names none. SkillError when it does not parse."""
    m = SOURCE_RE.match(str(source or "").strip())
    if not m:
        raise SkillError("metadata.source takes <url>@<full commit>[:<folder>], not %s" % source)
    folder = m.group("folder")
    if folder is not None and not _path_ok(folder):
        raise SkillError("metadata.source names a bad folder: %s" % folder)
    return m.group("url"), m.group("commit").lower(), folder


def fetch_folder(url, commit, folder, *, schemes=FETCH_SCHEMES, timeout=FETCH_TIMEOUT):
    """{"files": {path: bytes}, "executable": [path], "license_file": path,
    "license_from_root": bool} of `folder` at `commit` in the git
    repository at `url`, paths relative to the folder. Exactly that commit
    is fetched, depth 1, into a temporary repository: no branch or tag is
    followed, no credential, hook, submodule or global setting is used,
    and only `schemes` may be spoken. git checks every object it receives
    (FETCH_CONFIG), and runs with no GIT_* or *ASKPASS variable of ours
    and its HOME in the temporary directory, so no askpass, injected
    setting or ~/.netrc reaches the fetch. Refused: a path _path_ok
    refuses, a symlink or a submodule in the folder, no SKILL.md, over
    DRAFT_MAX_BYTES, and no licence file in the folder or at the
    repository's root (that one is added to the folder's files under its
    own name)."""
    scheme = url.split("://", 1)[0].lower() if "://" in url else "ssh"
    if scheme not in schemes:
        raise SkillError("a skill from outside is fetched over %s only, not %s"
                         % (" or ".join(schemes), url))
    if not _path_ok(folder):
        raise SkillError("bad folder %r" % folder)
    config = [arg for setting in FETCH_CONFIG for arg in ("-c", setting)]
    for allowed in schemes:
        config += ["-c", "protocol.%s.allow=always" % allowed]

    def git(*args, text=True):
        proc = subprocess.run(["git", *config, *args], capture_output=True, text=text,
                              env=env, timeout=timeout)
        if proc.returncode != 0:
            err = proc.stderr if text else proc.stderr.decode("utf-8", "replace")
            raise SkillError("fetching %s at %s: %s" % (url, commit, (err or "").strip()[-300:]
                                                        or "git exit %d" % proc.returncode))
        return proc.stdout

    with tempfile.TemporaryDirectory(prefix="cousin-skill-fetch-") as tmp:
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("GIT_") and not k.endswith("ASKPASS")}
        env.update(GIT_TERMINAL_PROMPT="0", LC_ALL="C", GIT_CONFIG_NOSYSTEM="1",
                   GIT_CONFIG_GLOBAL=os.devnull, HOME=tmp, XDG_CONFIG_HOME=tmp)
        repo = str(Path(tmp) / "repo.git")
        try:
            git("init", "-q", "--bare", repo)
            git("-C", repo, "fetch", "-q", "--depth", "1", "--no-tags", url, commit)
            got = git("-C", repo, "rev-parse", "--verify", "-q", "%s^{commit}" % commit).strip()
            if got.lower() != commit.lower():
                raise SkillError("%s has no commit %s" % (url, commit))
            listing = git("-C", repo, "ls-tree", "-r", "-l", "-z", commit, "--", folder + "/")
            files, executable, total = {}, [], 0
            for rec in listing.split("\0"):
                if not rec:
                    continue
                meta, _, path = rec.partition("\t")
                mode, kind, oid, size = meta.split()
                rel = path[len(folder) + 1:]
                if not _path_ok(rel):
                    raise SkillError("%s holds a bad path: %r" % (folder, rel))
                if mode == "120000":
                    raise SkillError("%s holds a symlink (%s); a skill from outside holds regular"
                                     " files only" % (folder, rel))
                if kind != "blob":
                    raise SkillError("%s holds a submodule (%s)" % (folder, rel))
                total += int(size)
                if total > DRAFT_MAX_BYTES:
                    raise SkillError("%s is over %d bytes" % (folder, DRAFT_MAX_BYTES))
                files[rel] = git("-C", repo, "cat-file", "blob", oid, text=False)
                if mode == "100755":
                    executable.append(rel)
            if not files:
                raise SkillError("%s has no folder %s at %s" % (url, folder, commit))
            if SKILL_FILE not in files:
                raise SkillError("%s has no %s at %s" % (folder, SKILL_FILE, commit))
            licence, from_root = _license_file(files), False
            if licence is None:
                top = {}
                for rec in git("-C", repo, "ls-tree", "-z", commit).split("\0"):
                    meta, _, path = rec.partition("\t")
                    if rec and meta.split()[1] == "blob":
                        top[path] = meta.split()[2]
                licence = _license_file(top)
                if licence is not None:
                    files[licence] = git("-C", repo, "cat-file", "blob", top[licence], text=False)
                    from_root = True
            if licence is None:
                raise SkillError("%s has no licence file in %s or at its root" % (url, folder))
        except subprocess.TimeoutExpired:
            raise SkillError("fetching %s took over %ds" % (url, timeout)) from None
    return {"files": files, "executable": sorted(executable), "license_file": licence,
            "license_from_root": from_root}


def _after_frontmatter(text):
    """A SKILL.md's text after its closing `---` line, exactly."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        raise SkillError("SKILL.md must start with a --- frontmatter line")
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        raise SkillError("SKILL.md frontmatter has no closing --- line") from None
    return "\n".join(lines[end + 1:])


def _split_upstream(text):
    """({key: text}, [other keys]) of the frontmatter of a SKILL.md written
    outside: plain, quoted and block (| or >) values are read; a key
    holding a list or a map is named in the others."""
    lines = text.split("\n")
    _after_frontmatter(text)
    end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    values, others, i = {}, [], 1
    while i < end:
        raw = lines[i]
        i += 1
        if not raw.strip() or raw.lstrip().startswith("#") or raw[:1] in (" ", "\t"):
            continue
        key, sep, value = raw.partition(":")
        key, value = key.strip(), value.strip()
        if not sep or not key:
            raise SkillError("frontmatter line does not parse: %r" % raw.strip())
        more = []
        while i < end and (not lines[i].strip() or lines[i][:1] in (" ", "\t")):
            more.append(lines[i].strip())
            i += 1
        if value[:1] in ("|", ">"):
            words = [m for m in more if m]
            values[key] = (" ".join(words) if value[:1] == ">" else "\n".join(words))
        elif not value or value[:1] in ("[", "{") or (more and more[0].startswith("- ")):
            others.append(key)
        else:
            try:
                values[key] = " ".join([_unquote(value)] + [m for m in more if m])
            except SkillError:
                others.append(key)
    return values, others


def import_targets(home):
    """Where a skill from outside can land for this cousin, per scope:
    {"own": [repo], "shared": [repo]}, the repositories import_draft takes
    (OUTSIDE_REPOS the cousin loads, shared only in personal, and a cousin
    that loads private keeps its own there). The console's import form
    offers only these."""
    return {scope: [repo for repo in OUTSIDE_REPOS
                    if (scope != SHARED or repo == "personal")
                    and placement_problem(home, repo, scope) is None]
            for scope in SCOPES}


def import_draft(home, slug, source, *, context, side_effect, name=None, scope="own",
                 metadata=None, license=None, fetch=None):
    """Write a draft from a skill published outside (docs/reference/skills.md,
    "Installing a skill from outside"): the folder `source` names, fetched
    at its commit (fetch_folder), byte for byte, but for its SKILL.md
    frontmatter, which is written here: its name and description, its
    licence as an SPDX id the licence file must read as, our keys and the
    source. Returns (path, report): what upstream's frontmatter held that
    was not kept, and the licence. An import replaces the whole draft."""
    url, commit, folder = split_source(source)
    if not folder:
        raise SkillError("an import names the skill's folder: <url>@<full commit>:<folder>")
    if context not in OUTSIDE_REPOS:
        raise SkillError("a skill from outside goes to %s, not %s"
                         % (" or ".join(OUTSIDE_REPOS), context))
    if side_effect == "outward":
        raise SkillError("a skill from outside that acts outward is not imported")
    got = (fetch or fetch_folder)(url, commit, folder)
    files = got["files"]
    bad = sorted(rel for rel in files if not _path_ok(rel))
    if bad:
        raise SkillError("%s holds a bad path: %r" % (folder, bad[0]))
    try:
        text = files[SKILL_FILE].decode("utf-8")
    except UnicodeDecodeError:
        raise SkillError("its %s is not UTF-8" % SKILL_FILE) from None
    values, others = _split_upstream(text)
    rest = _after_frontmatter(text)
    name = name or values.get("name", "")
    licence_text = files[got["license_file"]].decode("utf-8", "replace")
    stated = license or license_of(licence_text)
    if stated not in OUTSIDE_LICENSES[context]:
        raise SkillError("its licence (%s) is not one %s takes from outside: %s"
                         % (stated or got["license_file"], context,
                            ", ".join(OUTSIDE_LICENSES[context])))
    if not _reads_as(licence_text, stated):
        raise SkillError("its licence file %s does not read as %s" % (got["license_file"], stated))
    front = {"name": name, "description": " ".join(values.get("description", "").split()),
             "license": stated}
    if values.get("compatibility"):
        front["compatibility"] = " ".join(values["compatibility"].split())
    meta = {"context": context, "side-effect": side_effect, "scope": scope}
    for key, value in (metadata or {}).items():
        if key == "source" or value is None or (str(value).strip() == ""
                                                and key not in DECLARED_KEYS):
            continue
        meta[key] = _normal(key, value)
    meta["source"] = "%s@%s:%s" % (url, commit, folder)
    front["metadata"] = meta
    found = problems(name, front) + declared_problems(front)
    if not found:
        why = placement_problem(home, context, scope)
        found = [why] if why else []
    if found:
        raise SkillError("; ".join(found))
    dest = _draft_path(home, name)
    if dest.exists():
        shutil.rmtree(dest)
    for rel, data in files.items():
        path = dest / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if rel == SKILL_FILE:
            data = (frontmatter_text(front, IMPORT_NOTE) + rest).encode("utf-8")
        path.write_bytes(data)
        if rel in got["executable"]:
            os.chmod(path, 0o755)
    report = {"license": stated, "license_file": got["license_file"],
              "license_from_root": got["license_from_root"],
              "not_kept": sorted(set(others) | (set(values) - set(UPSTREAM_KEPT)))}
    return dest / SKILL_FILE, report


def outside_problems(home, name, front):
    """What a draft from outside (one with metadata.source) needs before
    approval, offline: its folder named, a repository and side effect
    phase 2 takes, an open licence its licence file reads as, and the
    task its dry run does. [] for a draft written here."""
    meta = front.get("metadata") or {}
    if not meta.get("source"):
        return []
    try:
        _url, _commit, folder = split_source(meta["source"])
    except SkillError as err:
        return [str(err)]
    out = []
    if not folder:
        out.append("a skill from outside names its folder: metadata.source"
                   " <url>@<full commit>:<folder>")
    if meta.get("context") not in OUTSIDE_REPOS:
        out.append("a skill from outside goes to %s only" % " or ".join(OUTSIDE_REPOS))
    if meta.get("side-effect") == "outward":
        out.append("a skill from outside may not act outward")
    stated = str(front.get("license", "")).strip()
    allowed = OUTSIDE_LICENSES.get(meta.get("context"), ())
    if allowed and stated not in allowed:
        out.append("a skill from outside in %s carries one of %s, not %s"
                   % (meta.get("context"), ", ".join(allowed), stated or "no licence"))
    try:
        files = _files(_draft_path(home, name))
    except SkillError as err:
        files = {}
        out.append(str(err))
    licence = _license_file(files)
    if licence is None:
        out.append("a skill from outside keeps its licence file")
    elif stated in allowed and not _reads_as(files[licence].decode("utf-8", "replace"), stated):
        out.append("its licence file %s does not read as %s" % (licence, stated))
    if not str(meta.get(DRY_RUN_KEY, "")).strip():
        out.append("a skill from outside needs metadata.%s: the sample task its dry run does"
                   % DRY_RUN_KEY)
    return out


def upstream_differences(home, name, *, fetch=None):
    """(source, [paths], fetched) of a draft from outside: the paths where
    it is not what its source published, its SKILL.md compared past the
    frontmatter the import writes. SkillError when it names no source or
    the source cannot be fetched."""
    folder_path = _draft_path(home, name)
    if not (folder_path / SKILL_FILE).is_file():
        raise SkillError("no draft %s" % name)
    front, _ = parse((folder_path / SKILL_FILE).read_text())
    source = (front.get("metadata") or {}).get("source")
    if not source:
        raise SkillError("draft %s names no source" % name)
    url, commit, folder = split_source(source)
    if not folder:
        raise SkillError("metadata.source names no folder")
    got = (fetch or fetch_folder)(url, commit, folder)
    mine, theirs = _files(folder_path), got["files"]
    differ = []
    for rel in sorted(set(mine) | set(theirs)):
        a, b = mine.get(rel), theirs.get(rel)
        if rel == SKILL_FILE and a is not None and b is not None:
            try:
                same = _after_frontmatter(a.decode("utf-8")) == _after_frontmatter(b.decode("utf-8"))
            except (UnicodeDecodeError, SkillError):
                same = False
            if not same:
                differ.append("%s past its frontmatter" % rel)
        elif a != b:
            differ.append(rel)
    return source, differ, got


def scripts_in(home, name):
    """[{path, why}] of the files in a draft an agent could run: under
    scripts/, executable, starting with #!, or with a script's suffix."""
    folder = _draft_path(home, name)
    out = []
    for rel, data in sorted(_files(folder).items()):
        why = []
        if rel.startswith("scripts/"):
            why.append("under scripts/")
        if os.access(folder / rel, os.X_OK):
            why.append("executable")
        if data[:2] == b"#!":
            why.append("starts with #!")
        if rel.lower().endswith(SCRIPT_SUFFIXES):
            why.append("a script's suffix")
        if why:
            out.append({"path": rel, "why": ", ".join(why), "bytes": len(data)})
    return out


def dry_run_path(root, home, slug, name):
    """Where a draft's dry run is kept, beside its trigger test result
    (trigger_path): the operator's allow, then the job that ran it."""
    base = Path(root) / "data" / "skills"
    if _loads_private(home):
        base = base / "private"
    return base / "dry-runs" / slug / ("%s.json" % name)


def _read_json(path):
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_private_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent.parent, 0o700)
    except OSError:
        pass
    atomic.write_text(path, json.dumps(data, indent=1, sort_keys=True) + "\n")


def allow_dry_run(root, home, slug, name, *, user, diff_sha):
    """The operator's go for a dry run of this very version of a draft from
    outside (the console's verb): after reading its scripts, the cousin
    may run its task once. Returns the record."""
    _text, sha = diff_draft(root, home, slug, name)
    if sha != diff_sha:
        raise SkillError("the draft changed since it was shown; look again")
    front, _ = parse((_draft_path(home, name) / SKILL_FILE).read_text())
    meta = front.get("metadata") or {}
    if not meta.get("source"):
        raise SkillError("only a skill from outside has a dry run")
    entry = {"allowed_by": user, "allowed_at": datetime.now(timezone.utc).isoformat(
        timespec="seconds"), "diff_sha256": sha, "task": str(meta.get(DRY_RUN_KEY, ""))}
    _write_private_json(dry_run_path(root, home, slug, name), entry)
    return entry


def record_dry_run(root, home, slug, name, job_id, *, job=None):
    """Record the job that ran an allowed dry run (the cousin's verb): the
    framework reads the job row, its owner, how it ended, its exit code
    and its log, so the result is the job's, never the cousin's word.
    Refused: no allow for this version, a run recorded on this allow
    already (another run needs another allow, so the operator sees a
    failed one before there is a second), a job of another cousin's, one
    still running, one started before the allow, one with no exit code
    (the job tool's `run` records one)."""
    _text, sha = diff_draft(root, home, slug, name)
    path = dry_run_path(root, home, slug, name)
    allowed = _read_json(path)
    if not allowed or allowed.get("diff_sha256") != sha:
        raise SkillError("no dry run of this version of %s is allowed: the operator allows one"
                         " in the console" % name)
    if allowed.get("job") is not None:
        raise SkillError("the dry run this allow was for is recorded already (job #%s): another"
                         " run needs another allow from the operator" % allowed["job"])
    if job is None:
        from cousin_lib import jobs
        job = jobs.get_job(int(job_id))
    if not job:
        raise SkillError("no job #%s" % job_id)
    if job.get("spawned_by") != slug:
        raise SkillError("job #%s is %s's, not %s's" % (job_id, job.get("spawned_by"), slug))
    if job.get("status") == "running":
        raise SkillError("job #%s is still running" % job_id)
    try:
        started = datetime.fromisoformat(str(job.get("started_at")))
        late = started < datetime.fromisoformat(allowed["allowed_at"])
    except (TypeError, ValueError):
        late = True
    if late:
        raise SkillError("job #%s started before the dry run was allowed" % job_id)
    if job.get("exit_code") is None:
        raise SkillError("job #%s has no exit code: run the dry run with the job tool's run"
                         % job_id)
    entry = dict(allowed, job=int(job_id), status=job.get("status"), exit_code=job["exit_code"],
                 command=job.get("command"), log=job.get("log_path"),
                 finished_at=job.get("finished_at"), recorded_at=datetime.now(
                     timezone.utc).isoformat(timespec="seconds"))
    entry["pass"] = job.get("status") == "done" and job["exit_code"] == 0
    _write_private_json(path, entry)
    return entry


def import_written(home, slug, path, report):
    """What the tool and the CLI say once an import wrote its draft."""
    text = draft_written(home, slug, path.parent.name, path)
    text += "; licence %s (%s%s)" % (report["license"], report["license_file"],
                                     ", from the repository's root"
                                     if report["license_from_root"] else "")
    if report["not_kept"]:
        text += "; upstream's %s not kept" % ", ".join(report["not_kept"])
    return text + ("; next: the operator reads its scripts and allows a dry run in the"
                   " console")


def dry_run_written(entry):
    return ("dry run recorded: job #%d %s, exit %s, so %s; the operator approves in the"
            " console" % (entry["job"], entry["status"], entry["exit_code"],
                          "a pass" if entry["pass"] else "a fail"))


LICENSE_SHOWN = 64 * 1024


def outside_view(root, home, slug, name, diff_sha):
    """What the approve view shows for a draft from outside, offline: where
    it came from, its licence and the file's text, the files an agent
    could run, its dry-run task and the dry run on this diff. None for a
    draft written here."""
    folder_path = _draft_path(home, name)
    front, _ = parse((folder_path / SKILL_FILE).read_text())
    meta = front.get("metadata") or {}
    source = meta.get("source")
    if not source:
        return None
    try:
        url, commit, folder = split_source(source)
    except SkillError:
        url = commit = folder = None
    files = _files(folder_path)
    licence = _license_file(files)
    return {"source": source, "url": url, "commit": commit, "folder": folder,
            "license": front.get("license"), "license_file": licence,
            "license_text": files[licence].decode("utf-8", "replace")[:LICENSE_SHOWN]
            if licence else "",
            "scripts": scripts_in(home, name), "task": str(meta.get(DRY_RUN_KEY, "")),
            "dry_run": dry_run_state(root, home, slug, name, diff_sha)}


def upstream_view(home, name, *, fetch=None):
    """The source fetched again: {source, differ, not_kept, upstream}, the
    paths where the draft is not as published and what upstream's
    frontmatter held that the import did not keep (allowed-tools, say),
    with the values it could read."""
    source, differ, got = upstream_differences(home, name, fetch=fetch)
    values, others = _split_upstream(got["files"][SKILL_FILE].decode("utf-8", "replace"))
    not_kept = sorted(set(others) | (set(values) - set(UPSTREAM_KEPT)))
    return {"source": source, "differ": differ, "not_kept": not_kept,
            "upstream": {k: values[k] for k in not_kept if k in values}}


def dry_run_state(root, home, slug, name, diff_sha):
    """The draft's dry run on this very diff: the allow, then its job, or
    None (never allowed, or the draft changed since)."""
    entry = _read_json(dry_run_path(root, home, slug, name))
    return entry if entry and entry.get("diff_sha256") == diff_sha else None


def last_dry_run(root, home, slug, name, diff_sha):
    """The draft's recorded dry run on this very diff, or None."""
    entry = dry_run_state(root, home, slug, name, diff_sha)
    return entry if entry and entry.get("job") is not None else None


# ---------------------------------------------------------------- the gate

def _inside(path, base):
    try:
        Path(os.path.normpath(path)).relative_to(os.path.normpath(base))
        return True
    except ValueError:
        return False


def _absolute(path, cwd):
    p = os.path.expanduser(str(path))
    return p if os.path.isabs(p) else os.path.join(str(cwd), p)


def _protected(path, root, home, cwd):
    p = _absolute(path, cwd)
    if root is not None and (_inside(p, repos_dir(root))
                             or _inside(p, Path(root) / "data" / "skills")):
        return True
    return home is not None and _inside(p, export_dir(home))


GIT_ENV = ("GIT_DIR", "GIT_WORK_TREE")
WRAPPERS = ("env", "command", "exec", "nohup", "time", "nice")
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _command_of(segment, here):
    """(argv, where) of one shell command with its prefix peeled off:
    leading VAR=value words (GIT_DIR and GIT_WORK_TREE move `where`) and
    the wrappers in WRAPPERS with their options (env's -C/--chdir moves
    `where`; env -u and nice -n take a value)."""
    args, where = list(segment), here
    while args:
        word = args[0]
        if _ASSIGNMENT.match(word):
            key, _, value = word.partition("=")
            if key in GIT_ENV and value:
                where = _absolute(value, where)
            args = args[1:]
            continue
        wrapper = os.path.basename(word)
        if wrapper not in WRAPPERS:
            break
        args = args[1:]
        while args and args[0].startswith("-") and args[0] != "--":
            opt, _, value = args[0].partition("=")
            args = args[1:]
            if wrapper == "env" and opt in ("-C", "--chdir", "-u", "--unset") and not value \
                    and args:
                value, args = args[0], args[1:]
            elif wrapper == "nice" and opt in ("-n", "--adjustment") and not value and args:
                args = args[1:]
            if wrapper == "env" and opt in ("-C", "--chdir") and value:
                where = _absolute(value, where)
        if args and args[0] == "--":
            args = args[1:]
    return args, where


def _cli_subcommand(args):
    """The cousin-skill subcommand of one command's words (`cousin-skill
    [--home H] <sub>` or `python [-I] -m cousin_lib.skills [--home H]
    <sub>`), or None for any other command."""
    words = list(args)
    head = os.path.basename(words[0]) if words else ""
    if head == "cousin-skill":
        rest = words[1:]
    elif head.startswith("python") and "-m" in words:
        at = words.index("-m")
        if at + 1 >= len(words) or words[at + 1] != "cousin_lib.skills":
            return None
        rest = words[at + 2:]
    else:
        return None
    while rest and rest[0].startswith("-"):
        opt = rest[0]
        rest = rest[2:] if opt == "--home" else rest[1:]
    return rest[0] if rest else None


def guard_tool(tool_name, tool_input, *, root=None, home=None, cwd=None):
    """The refusal for a call that would write the skills repositories,
    the exports or the approval records, or None. Path tools by their
    path; Bash by the targets it writes (perimeter's parse) and by its git
    commands: under <install>/skills/ only GIT_READ subcommands pass. A
    `cousin-skill import` from a cousin's shell is refused: importing is
    the operator's act (IMPORT_REFUSAL).
    Best-effort for Bash, like the perimeter: a write hidden behind a
    variable is not seen."""
    if root is None and home is None:
        return None
    cwd = cwd or home or os.getcwd()
    name = str(tool_name or "")
    if not isinstance(tool_input, dict):
        return None
    why = ("skills: %s may not write %s; skills change only through the console's"
           " approval (docs/reference/skills.md)")
    if name in WRITE_PATH_TOOLS:
        for field in ("file_path", "notebook_path"):
            value = tool_input.get(field)
            if isinstance(value, str) and value.strip() and _protected(value, root, home, cwd):
                return why % (name, value)
        return None
    if name != "Bash":
        return None
    from cousin_lib import perimeter
    command = tool_input.get("command")
    if not isinstance(command, str):
        return None
    for path in perimeter._bash_write_targets(command):
        if _protected(path, root, home, cwd):
            return why % ("Bash", path)
    here, exported = cwd, None
    if root is None:
        for segment in perimeter._segments(perimeter._shell_tokens(command)):
            args, _where = _command_of(segment, here)
            if args and _cli_subcommand(args) == "import":
                return "skills: %s" % IMPORT_REFUSAL
        return None
    for segment in perimeter._segments(perimeter._shell_tokens(command)):
        args, where = _command_of(segment, exported or here)
        if not args:
            continue
        head = os.path.basename(args[0])
        if _cli_subcommand(args) == "import":
            return "skills: %s" % IMPORT_REFUSAL
        if head in ("cd", "pushd") and len(args) > 1:
            here = _absolute(args[1], here)
            continue
        if head == "export":
            for word in args[1:]:
                key, _, value = word.partition("=")
                if key in GIT_ENV and value:
                    exported = _absolute(value, here)
            continue
        if head != "git":
            continue
        args = args[1:]
        while args and args[0].startswith("-"):
            opt, _, value = args[0].partition("=")
            if opt in ("-C", "--git-dir", "--work-tree"):
                if not value and len(args) > 1:
                    value, args = args[1], args[1:]
                if value:
                    where = _absolute(value, where)
            elif opt in ("-c", "--namespace", "--config-env") and not value and len(args) > 1:
                args = args[1:]
            args = args[1:]
        if args and _inside(where, repos_dir(root)) and args[0] not in GIT_READ:
            return ("skills: git %s under %s is refused; only %s pass there, and a"
                    " skill changes only through the console's approval"
                    % (args[0], repos_dir(root), ", ".join(GIT_READ)))
    return None


# ---------------------------------------------------------------- text the tool and CLI print

def lane_of(home):
    """The cousin's runner kind from cousin.toml [agent] runner, or None."""
    try:
        data = tomllib.loads((Path(home) / "cousin.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return None
    runner = (data.get("agent") or {}).get("runner")
    return runner if isinstance(runner, str) else None


def text_list(root, home, slug, *, lane=None):
    if (lane or lane_of(home)) == "opencode":
        return "none: this lane loads no skills"
    from cousin_lib import skills_check
    lines = []
    for item in loaded(root, home, slug):
        flag = " (problem: %s)" % "; ".join(item["problems"]) if item["problems"] else ""
        try:
            homes = skills_check.usage_homes(root, item["folder"])
            if Path(home) not in homes:
                homes.append(Path(home))
            seen = skills_check.annotate(root, item, homes=homes)
        except Exception:  # noqa: BLE001 - a listing never fails on a check file
            seen = {}
        check = seen.get("check") or {}
        if check.get("stale"):
            flag += " [stale: %s]" % ", ".join(check.get("changed") or [])
        if check.get("update"):
            flag += " [upstream changed]"
        if seen.get("unused"):
            flag += " [unused %d days]" % skills_check.UNUSED_DAYS
        lines.append("%s:%s - %s%s" % (item["plugin"], item["name"], item["description"], flag))
    out = ["loaded at the next start:"] + (["  " + l for l in lines] or ["  none"])
    found = drafts(home, slug)
    out.append("drafts (not loaded until approved in the console):")
    out += ["  %s -> %s:%s - %s%s" % (d["name"], d["repo"] or "?", d["folder"] or "?",
                                      d["description"],
                                      " (problem: %s)" % "; ".join(d["problems"])
                                      if d["problems"] else "")
            for d in found] or ["  none"]
    return "\n".join(out)


def text_show(root, home, slug, name):
    """A draft of that name first, else the loaded skill (`plugin:name` or
    a bare name)."""
    plugin, _, bare = name.rpartition(":")
    if not plugin:
        try:
            d = show_draft(home, bare)
            return "draft %s (files: %s)\n\n%s" % (bare, ", ".join(d["files"]), d["text"])
        except SkillError:
            pass
    for repo, folder, plug in sources(slug, cousin_repos(home)):
        if plugin and plug != plugin:
            continue
        try:
            s = show_approved(root, repo, folder, bare)
        except SkillError:
            continue
        return "%s:%s (files: %s)\n\n%s" % (plug, bare, ", ".join(s["files"]), s["text"])
    raise SkillError("no skill or draft %r for this cousin" % name)


# ---------------------------------------------------------------- the CLI

def _cli_home(args):
    home = args.home or os.environ.get("COUSIN_HOME")
    if not home:
        raise SkillError("no cousin: pass --home or set COUSIN_HOME")
    home = Path(home).resolve()
    root = Path(os.environ.get("FRAMEWORK_ROOT") or home.parent.parent).resolve()
    return root, home, home.name


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="cousin-skill",
        description="a cousin's skills: list, show, draft, diff, check (approval is the console's)")
    parser.add_argument("--home", help="the cousin's home (default: COUSIN_HOME)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="what loads at the next start, and the drafts")
    p = sub.add_parser("show", help="a draft, or a loaded skill (plugin:name)")
    p.add_argument("name")
    p = sub.add_parser("draft", help="write or replace a draft's SKILL.md")
    p.add_argument("name")
    p.add_argument("--description", required=True)
    p.add_argument("--context", required=True, choices=REPOS)
    p.add_argument("--side-effect", required=True, choices=SIDE_EFFECTS)
    p.add_argument("--scope", default="own", choices=SCOPES)
    p.add_argument("--should-trigger", default="")
    p.add_argument("--must-not-trigger", default="")
    p.add_argument("--hosts", help="every host it may contact, comma-separated; \"\" for none")
    p.add_argument("--reads", help="what it reads, pinned: repo@<full commit>:path or"
                                   " path#sha256:<hex>, comma-separated")
    p.add_argument("--rests-on", help="paths, tool versions, memory topics it depends on,"
                                      " comma-separated")
    p.add_argument("--source", help="a skill from outside: <url>@<full commit>[:<folder>]")
    p.add_argument("--license", help="an SPDX identifier; required with --source")
    p.add_argument("--body-file", help="the steps (default: stdin)")
    p.add_argument("--dry-run-task", default="",
                   help="a skill from outside: the sample task its dry run does")
    p = sub.add_parser("import", help="a draft from a skill published outside: the operator's"
                                      " act, refused inside a cousin's environment (COUSIN_HOME)")
    p.add_argument("source", help="<https url>@<full commit>:<folder>")
    p.add_argument("--context", required=True, choices=REPOS)
    p.add_argument("--side-effect", required=True, choices=SIDE_EFFECTS)
    p.add_argument("--name", help="the draft's name (default: the skill's own)")
    p.add_argument("--scope", default="own", choices=SCOPES)
    p.add_argument("--should-trigger", default="")
    p.add_argument("--must-not-trigger", default="")
    p.add_argument("--dry-run-task", default="", help="the sample task its dry run does")
    p.add_argument("--hosts", help="every host it may contact, comma-separated; \"\" for none")
    p.add_argument("--reads", help="what it reads, pinned, comma-separated")
    p.add_argument("--rests-on", help="what it depends on, comma-separated")
    p.add_argument("--license", help="the SPDX id its licence file reads as (default: found)")
    p = sub.add_parser("dry-run", help="record the job that ran a draft's allowed dry run")
    p.add_argument("name")
    p.add_argument("--job", required=True, type=int)
    p = sub.add_parser("diff", help="a draft against what is approved")
    p.add_argument("name")
    p = sub.add_parser("trigger-test", help="run a draft's trigger test (the console runs it)")
    p.add_argument("name")
    p = sub.add_parser("check", help="whether the cousin's loaded skills (or one) went stale;"
                                     " read-only")
    p.add_argument("name", nargs="?", help="plugin:name or a bare name (default: all)")
    p.add_argument("--name", dest="name_flag", help="the same, as a flag (the skill tool's form)")
    args = parser.parse_args(argv)
    try:
        root, home, slug = _cli_home(args)
        if args.command == "list":
            print(text_list(root, home, slug))
        elif args.command == "show":
            print(text_show(root, home, slug, args.name))
        elif args.command == "draft":
            body = Path(args.body_file).read_text() if args.body_file else sys.stdin.read()
            path = write_draft(home, args.name, description=args.description, body=body,
                               context=args.context, side_effect=args.side_effect,
                               scope=args.scope, license=args.license,
                               metadata={"should-trigger": args.should_trigger,
                                         "must-not-trigger": args.must_not_trigger,
                                         "hosts": args.hosts, "reads": args.reads,
                                         "rests-on": args.rests_on, "source": args.source,
                                         DRY_RUN_KEY: args.dry_run_task})
            print(draft_written(home, slug, args.name, path))
        elif args.command == "import":
            if os.environ.get("COUSIN_HOME"):
                raise SkillError(IMPORT_REFUSAL)
            path, report = import_draft(
                home, slug, args.source, context=args.context, side_effect=args.side_effect,
                name=args.name, scope=args.scope, license=args.license,
                metadata={"should-trigger": args.should_trigger,
                          "must-not-trigger": args.must_not_trigger,
                          DRY_RUN_KEY: args.dry_run_task, "hosts": args.hosts,
                          "reads": args.reads, "rests-on": args.rests_on})
            print(import_written(home, slug, path, report))
        elif args.command == "dry-run":
            print(dry_run_written(record_dry_run(root, home, slug, args.name, args.job)))
        elif args.command == "diff":
            text, _sha = diff_draft(root, home, slug, args.name)
            print(text or "no difference from what is approved")
        elif args.command == "trigger-test":
            print(json.dumps(trigger_test(root, home, slug, args.name)))
        elif args.command == "check":
            from cousin_lib import skills_check
            print(skills_check.text_check(root, home, slug, args.name_flag or args.name))
    except SkillError as err:
        print("cousin-skill: %s" % err, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
