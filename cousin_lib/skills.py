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
`cousin-skill` only list, show, draft and diff. An approval commits on
`main` with the repository's git identity and appends a record
(data/skills/approvals.jsonl; `private` keeps its own file), and a
repository loads only while `main` is at the commit its last record
names.

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
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path

from cousin_lib import atomic, jsonl

REPOS = ("personal", "work", "private")
SHARED = "shared"
SCOPES = ("own", "shared")
SIDE_EFFECTS = ("read-only", "writes-local", "outward")
SKILL_FILE = "SKILL.md"
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
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
WRITE_PATH_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")


class SkillError(ValueError):
    """A skill, a draft or a repository the caller cannot use as asked."""


# ---------------------------------------------------------------- SKILL.md

def _unquote(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def parse(text):
    """(frontmatter, body) of a SKILL.md. The frontmatter is the YAML
    subset the format uses: `key: value` lines between the leading `---`
    lines, and `metadata:` followed by indented `key: value` lines, every
    value a string. SkillError when there is none or a line does not
    parse."""
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
            metadata[key] = _unquote(value)
            continue
        in_meta = key == "metadata" and not value.strip()
        if in_meta:
            metadata = front["metadata"] = {}
        else:
            front[key] = _unquote(value)
    return front, "\n".join(lines[end + 1:]).lstrip("\n")


def render(front, body):
    """A SKILL.md from a frontmatter dict (metadata nested) and a body."""
    out = ["---"]
    for key, value in front.items():
        if key == "metadata":
            continue
        out.append("%s: %s" % (key, value))
    meta = front.get("metadata") or {}
    if meta:
        out.append("metadata:")
        out.extend("  %s: %s" % (k, json.dumps(str(v)) if _needs_quotes(str(v)) else v)
                   for k, v in meta.items())
    out.append("---")
    return "\n".join(out) + "\n\n" + body.strip() + "\n"


def _needs_quotes(value):
    return value == "" or value != value.strip() or ":" in value or value[:1] in "\"'#"


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


def record(root, repo, *, user, action, commit, skill=None, folder=None, diff_sha=None):
    """Append one approval record: who did it in the console, to which
    repository, the commit it produced and the sha256 of the diff shown.
    `private` keeps its own file, since a record names the folder and a
    private cousin's folder is its name."""
    path = _records_path(root, repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    entry = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "repo": repo, "action": action, "user": user, "commit": commit,
             "folder": folder, "skill": skill, "diff_sha256": diff_sha}
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
    return repo, (SHARED if meta.get("scope", "own") == SHARED else slug)


def drafts(home, slug):
    """[{name, description, metadata, problems, repo, folder}] of a
    cousin's drafts."""
    out = []
    base = drafts_dir(home)
    if not base.is_dir():
        return out
    for folder in sorted(p for p in base.iterdir() if p.is_dir() and not p.name.startswith(".")):
        skill = folder / SKILL_FILE
        raw = skill.read_bytes() if skill.is_file() else None
        item = _summary(folder.name, raw)
        try:
            front, _ = parse(raw.decode("utf-8")) if raw is not None else ({}, "")
            item["repo"], item["folder"] = target(slug, front)
        except (SkillError, UnicodeDecodeError):
            item["repo"], item["folder"] = None, None
        out.append(item)
    return out


def show_draft(home, name):
    folder = _draft_path(home, name)
    if not (folder / SKILL_FILE).is_file():
        raise SkillError("no draft %s" % name)
    return {"name": name, "text": (folder / SKILL_FILE).read_text(errors="replace"),
            "files": sorted(_files(folder))}


def write_draft(home, name, *, description, body, context, side_effect,
                scope="own", metadata=None):
    """Write or replace a draft's SKILL.md; returns its path. Its other
    files are the cousin's own to write in the same folder."""
    folder = _draft_path(home, name)
    meta = {"context": context, "side-effect": side_effect, "scope": scope}
    meta.update({k: v for k, v in (metadata or {}).items() if v not in (None, "")})
    front = {"name": name, "description": " ".join(str(description).split()),
             "metadata": meta}
    found = problems(name, front)
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


def approve_draft(root, home, slug, name, *, user, diff_sha):
    """Commit a draft on main and record it: only for the diff whose sha
    the console showed. The draft folder goes once it is committed."""
    folder = _draft_path(home, name)
    if not (folder / SKILL_FILE).is_file():
        raise SkillError("no draft %s" % name)
    front, _ = parse((folder / SKILL_FILE).read_text())
    found = problems(name, front)
    if found:
        raise SkillError("draft %s cannot be approved: %s" % (name, "; ".join(found)))
    repo, where = target(slug, front)
    if repo not in cousin_repos(home):
        # a private cousin's folder is its name: it never lands in a
        # repository the cousin does not load (law rule 11)
        raise SkillError("draft %s targets %s, which this cousin does not load"
                         " (cousin.toml [skills] repos)" % (name, repo))
    path = ensure_repo(root, repo, user=user)
    if not state(root, repo)["trusted"]:
        raise SkillError("repository %s has moved past its last approval; nothing is"
                         " approved until that is resolved" % repo)
    text, sha = diff_draft(root, home, slug, name)
    if sha != diff_sha:
        raise SkillError("the draft changed since it was shown; look again")
    if not text:
        raise SkillError("the draft is the same as what is approved")
    dest = path / where / name
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(folder, dest, symlinks=False)
    _git(path, "add", "-A", "--", "%s/%s" % (where, name))
    _git(path, "commit", "-q", "-m", "feat(skills): approve %s/%s" % (where, name))
    commit = _head(path)
    entry = record(root, repo, user=user, action="approve", commit=commit,
                   skill=name, folder=where, diff_sha=sha)
    shutil.rmtree(folder)
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
    for dirpath, dirnames, filenames in os.walk(path, topdown=False):
        for f in filenames:
            p = os.path.join(dirpath, f)
            if not os.path.islink(p):
                os.chmod(p, 0o444)
        os.chmod(dirpath, 0o555)


def _extract(path, commit, folder, dest):
    """The approved `folder` of a repository into dest/skills/, its prefix
    dropped; regular files and directories only."""
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


TRIGGER_KEYS = ("should-trigger", "must-not-trigger")


def trigger_path(home, name):
    """Where a draft's last trigger test result is kept: beside the drafts,
    never inside one, where it would change the draft's own diff."""
    return drafts_dir(home) / ".trigger-tests" / ("%s.json" % name)


def trigger_test(root, home, slug, name, *, checker=None):
    """Run a draft's trigger test and keep its result (trigger_path): its
    should-trigger prompt and its must-not-trigger prompt, each one turn on
    the cousin's own account and model, with what the cousin loads plus
    the draft as `draft:<name>`. Runs in a child process of the console
    (`cousin-skill --home H trigger-test NAME`), since the check scrubs
    the process's auth variables while it runs."""
    import tempfile
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
    path = trigger_path(home, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic.write_text(path, json.dumps(result, indent=1, sort_keys=True) + "\n")
    return result


def last_trigger_test(home, name, diff_sha):
    """The draft's last trigger test result, when it was run on this very
    diff; None otherwise (never run, or the draft changed since)."""
    try:
        result = json.loads(trigger_path(home, name).read_text())
    except (OSError, ValueError):
        return None
    return result if isinstance(result, dict) and result.get("diff_sha256") == diff_sha else None


def project_skills_dir(home):
    """Where the tmux lane's project settings would find skills that never
    passed approval."""
    return Path(home) / ".claude" / "skills"


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


def guard_tool(tool_name, tool_input, *, root=None, home=None, cwd=None):
    """The refusal for a call that would write the skills repositories,
    the exports or the approval records, or None. Path tools by their
    path; Bash by the targets it writes (perimeter's parse) and by its git
    commands: under <install>/skills/ only GIT_READ subcommands pass.
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
    if root is None:
        return None
    here = cwd
    for segment in perimeter._segments(perimeter._shell_tokens(command)):
        if not segment:
            continue
        head = os.path.basename(segment[0])
        if head == "cd" and len(segment) > 1:
            here = _absolute(segment[1], here)
            continue
        if head != "git":
            continue
        args, where = segment[1:], here
        while args and args[0].startswith("-"):
            if args[0] == "-C" and len(args) > 1:
                where = _absolute(args[1], where)
                args = args[2:]
            elif args[0] in ("-c", "--git-dir", "--work-tree") and len(args) > 1:
                if args[0] in ("--git-dir", "--work-tree"):
                    where = _absolute(args[1], where)
                args = args[2:]
            else:
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
    lines = []
    for item in loaded(root, home, slug):
        flag = " (problem: %s)" % "; ".join(item["problems"]) if item["problems"] else ""
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
        description="a cousin's skills: list, show, draft, diff (approval is the console's)")
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
    p.add_argument("--body-file", help="the steps (default: stdin)")
    p = sub.add_parser("diff", help="a draft against what is approved")
    p.add_argument("name")
    p = sub.add_parser("trigger-test", help="run a draft's trigger test (the console runs it)")
    p.add_argument("name")
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
                               scope=args.scope,
                               metadata={"should-trigger": args.should_trigger,
                                         "must-not-trigger": args.must_not_trigger})
            print("draft written: %s (the operator approves it in the console)" % path)
        elif args.command == "diff":
            text, _sha = diff_draft(root, home, slug, args.name)
            print(text or "no difference from what is approved")
        elif args.command == "trigger-test":
            print(json.dumps(trigger_test(root, home, slug, args.name)))
    except SkillError as err:
        print("cousin-skill: %s" % err, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
