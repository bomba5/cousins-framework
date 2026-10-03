"""cousin-upgrade: what moving an install to another release would do
(docs/operations.md, "Upgrades").

This release builds the plan only: `cousin-upgrade --dry-run` computes
it and prints it, and nothing is written, no code is switched and
nothing restarts. Without --dry-run the command refuses (exit 2): the
apply path comes in a later release.

The plan is computed before the checkout moves, so every shipped text
comes from git (`git show <ref>:<path>`), never from the working tree:

- TO is `--to` (a tag or any ref), else the newest release tag by
  `git tag --sort=-v:refname` (not the last line of a plain sort, where
  v3.9.x follows v3.10.x, and not `git describe`, which is the nearest
  tag, not the newest). A TO older than the running version is refused
  unless --to names it.
- FROM, for the code, is the running version (cousin_lib.version) and
  its tag, else the checkout's HEAD. Per home it is what
  data/template-sync.json records as that home's last registry sync,
  when it records one: after the code moves the running version reads
  as TO, so it cannot say what a home last got.
- Each home's registry is planned by template_sync's structural sync
  fed TO's shipped registry (tables and keys it would add, framework
  values it would migrate) and FROM's (entries a release retired:
  reported, never removed). A home with no registry is listed as such.
  `.mcp.json` is compared with its re-render, `policy.toml` is named
  and never touched, CLAUDE.md gets template_sync's per-home diff
  against TO's template.
- The seeded files (config/law.md, shared/) are compared with TO's
  templates (shared_tier.compare_templates).
- The restarts are listed in order: loops, console, each runner, the
  caller's own runner last and detached.
- The CHANGELOG sections between FROM and TO (headings and bold leads)
  and whether pyproject's dependencies changed are part of the report.

Exit 0 the plan was computed, 1 it could not be, 2 refused.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

from cousin_lib import mcp_server, shared_tier, template_sync, version

REGISTRY_EXAMPLE = "config/mcp-registry.toml.example"
TEMPLATE = "templates/cousin-CLAUDE.template.md"
LAW = "templates/law.md"
RULES = "templates/shared"
POLICY_EXAMPLE = "templates/policy.toml.example"
STATE = "data/template-sync.json"
_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
_SECTION = re.compile(r"^## (\d+\.\d+\.\d+)(?: - (\S+))?\s*$", re.M)
_GROUP = re.compile(r"^### (.+?)\s*$", re.M)
_LEAD = re.compile(r"^- \*\*(.+?)\*\*", re.M | re.S)


class UpgradeError(Exception):
    """The plan cannot be computed; the message says why (exit 1)."""


class Refused(Exception):
    """The plan is refused as asked; the message says why (exit 2)."""


# ----------------------------------------------------------------- git

def _git(checkout, *args, check=True):
    """One git command in the checkout. --no-optional-locks: `status`
    must not refresh the index, so the plan writes nothing there
    either."""
    try:
        r = subprocess.run(["git", "--no-optional-locks", "-C",
                            str(checkout), *args],
                           capture_output=True, text=True, timeout=30,
                           check=False)
    except (OSError, subprocess.SubprocessError) as err:
        raise UpgradeError("git %s: %s" % (" ".join(args), err))
    if check and r.returncode != 0:
        raise UpgradeError("git %s: %s" % (" ".join(args),
                                           r.stderr.strip() or "failed"))
    return r


def resolve(checkout, ref):
    """The full commit `ref` names, or None."""
    r = _git(checkout, "rev-parse", "--verify", "--quiet",
             "%s^{commit}" % ref, check=False)
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def show(checkout, ref, path):
    """The text of `path` at `ref`, or None when the ref has no such
    file."""
    r = _git(checkout, "show", "%s:%s" % (ref, path), check=False)
    return r.stdout if r.returncode == 0 else None


def newest_tag(checkout):
    """The newest release tag (v<major>.<minor>.<patch>) by version
    order, or None."""
    out = _git(checkout, "tag", "--list", "v*", "--sort=-v:refname").stdout
    for line in out.splitlines():
        if _TAG.match(line.strip()):
            return line.strip()
    return None


def _semver(text):
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)$", str(text or ""))
    return tuple(int(x) for x in m.groups()) if m else None


def version_at(checkout, ref):
    """project.version in pyproject.toml at `ref`, or None."""
    text = show(checkout, ref, "pyproject.toml")
    if text is None:
        return None
    try:
        project = tomllib.loads(text).get("project") or {}
    except tomllib.TOMLDecodeError:
        return None
    return str(project.get("version")) if project.get("version") else None


def _dirty(checkout):
    """Tracked files with uncommitted changes in the checkout."""
    out = _git(checkout, "status", "--porcelain", "--untracked-files=no",
               check=False).stdout
    return [line[3:] for line in out.splitlines() if line.strip()]


def _rules_at(checkout, ref):
    """{name: text} of the house rules (templates/shared/*.md, not the
    examples below it) at `ref`."""
    out = _git(checkout, "ls-tree", ref, RULES + "/", check=False).stdout
    rules = {}
    for line in out.splitlines():
        meta, _, path = line.partition("\t")
        if meta.split()[1:2] == ["blob"] and path.endswith(".md"):
            rules[Path(path).name] = show(checkout, ref, path)
    return rules


# ------------------------------------------------------- release notes

def changelog(text, low, high):
    """The CHANGELOG sections with low < version <= high, newest first:
    [{"version", "date", "groups": [{"title", "leads"}]}], each lead the
    bold opening of one bullet."""
    if not text:
        return []
    low, high = _semver(low) or (0, 0, 0), _semver(high)
    heads = list(_SECTION.finditer(text))
    out = []
    for i, m in enumerate(heads):
        v = _semver(m.group(1))
        if high is None or not (low < v <= high):
            continue
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        body = text[m.end():end]
        groups = []
        subs = list(_GROUP.finditer(body))
        for j, g in enumerate(subs):
            gend = subs[j + 1].start() if j + 1 < len(subs) else len(body)
            leads = [" ".join(x.split()) for x in
                     _LEAD.findall(body[g.end():gend])]
            groups.append({"title": g.group(1), "leads": leads})
        out.append({"version": m.group(1), "date": m.group(2),
                    "groups": groups})
    return out


def _deps(text):
    try:
        data = tomllib.loads(text or "")
    except tomllib.TOMLDecodeError:
        return {}
    project = data.get("project") or {}
    return {"requires-python": project.get("requires-python"),
            "dependencies": project.get("dependencies") or [],
            "optional-dependencies":
                project.get("optional-dependencies") or {},
            "build-system": (data.get("build-system") or {}).get("requires")
            or []}


def dependency_changes(old_text, new_text):
    """One line per pyproject dependency field that differs."""
    old, new = _deps(old_text), _deps(new_text)
    out = []
    for key in ("requires-python", "dependencies", "build-system"):
        if old.get(key) != new.get(key):
            out.append("%s: %s -> %s" % (key, old.get(key), new.get(key)))
    olds = old.get("optional-dependencies") or {}
    news = new.get("optional-dependencies") or {}
    for extra in sorted(set(olds) | set(news)):
        if olds.get(extra) != news.get(extra):
            out.append("optional-dependencies.%s: %s -> %s"
                       % (extra, olds.get(extra), news.get(extra)))
    return out


# --------------------------------------------------------------- homes

def _homes(root):
    base = Path(root) / "cousins"
    if not base.is_dir():
        return []
    return [p for p in sorted(base.iterdir()) if (p / "cousin.toml").is_file()]


def _slug(home):
    try:
        data = tomllib.loads((home / "cousin.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return home.name
    return (data.get("cousin") or {}).get("slug") or home.name


def _home_from(home, checkout, code_from):
    """(ref, source, note) the home's registry was last synced from:
    data/template-sync.json's registry_to when it records one that
    resolves, else the code's FROM."""
    path = home / STATE
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return code_from, "running", None
    except (OSError, ValueError) as err:
        return code_from, "running", "%s unreadable (%s)" % (STATE, err)
    ref = data.get("registry_to") if isinstance(data, dict) else None
    if not ref:
        return code_from, "running", "%s names no registry_to" % STATE
    if resolve(checkout, ref) is None:
        return code_from, "running", ("%s names %s, which this checkout"
                                      " does not have" % (STATE, ref))
    return ref, STATE, None


def _counts(diff):
    lines = diff.splitlines()
    plus = sum(1 for x in lines
               if x.startswith("+") and not x.startswith("+++"))
    minus = sum(1 for x in lines
                if x.startswith("-") and not x.startswith("---"))
    return plus, minus


def _registry_plan(home, root, theirs, base):
    if not (home / mcp_server.REGISTRY_NAME).is_file():
        return {"status": "none"}
    try:
        out = template_sync._registry_sync(home, root, apply=False,
                                           theirs=theirs, base=base)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as err:
        return {"status": "error", "error": "%s" % err}
    changed = out["added"] or out["corrected"]
    return {"status": "changes" if changed else "in step",
            "added": out["added"], "migrated": out["corrected"],
            "retired": out["retired"]}


def _mcp_plan(home, root, slug):
    if not (home / ".mcp.json").exists():
        return {"status": "absent"}
    try:
        _path, _text, changed = mcp_server.refreshed_mcp_json(
            home, root=root, slug=slug)
    except mcp_server.RegistrationError as err:
        return {"status": "error", "error": str(err)}
    return {"status": "would change" if changed else "in step"}


def _claude_plan(home, root, template):
    if not (home / "CLAUDE.md").is_file():
        return {"status": "none"}
    try:
        diff, notes = template_sync.diff(home, root, template=template)
    except (template_sync.SyncError, OSError, UnicodeDecodeError,
            tomllib.TOMLDecodeError) as err:
        return {"status": "error", "error": str(err)}
    plus, minus = _counts(diff)
    return {"status": "differs" if diff else "in step", "added_lines": plus,
            "removed_lines": minus, "notes": notes, "diff": diff}


def home_plan(home, root, checkout, *, theirs, template, code_from,
              base_text):
    """One home's part of the plan; reads only."""
    home = Path(home)
    ref, source, note = _home_from(home, checkout, code_from)
    base = base_text(ref)
    out = {"slug": _slug(home), "home": str(home),
           "from": {"ref": ref, "source": source},
           "notes": [note] if note else []}
    if base is None:
        out["notes"].append("no shipped registry at %s: retired entries"
                            " cannot be told" % ref)
    out["registry"] = _registry_plan(home, root, theirs, base)
    out["mcp_json"] = _mcp_plan(home, root, out["slug"])
    out["policy"] = {"present": (home / "policy.toml").is_file()}
    out["claude_md"] = _claude_plan(home, root, template)
    return out


# ------------------------------------------------------------ restarts

def _caller(root):
    """The slug of the cousin running this command, when it is one of
    this root's."""
    home = os.environ.get("COUSIN_HOME")
    if not home:
        return None
    home = Path(os.path.abspath(home))
    if home.parent != Path(os.path.abspath(root)) / "cousins":
        return None
    return _slug(home)


def restart_plan(root, caller=None):
    """The restarts an upgrade would do, in order, from the running
    supervisor's status when it answers (else from the configuration):
    {"supervisor": None or why it is not reachable, "order": [rows]}."""
    from cousin_lib import supervisor
    try:
        body = supervisor.request(root, "status", timeout=3.0)
        children = body.get("children") or {}
        unreachable = None
    except supervisor.SupervisorUnavailable as err:
        children, unreachable = None, str(err)
    rows = []
    for name in ("loops", "console"):
        if children is None:
            how = "unknown (no supervisor status)"
        elif name in children:
            how = "supervisor"
        else:
            how = "not a supervisor child: its own unit or process"
        row = (children or {}).get(name) or {}
        rows.append({"name": name, "how": how, "state": row.get("state"),
                     "detached": False})
    if children is None:
        runners = ["runner:%s" % c.slug
                   for c in supervisor.runner_cousins(root)]
    else:
        runners = sorted(n for n in children if n.startswith("runner:"))
    own = None
    for name in runners:
        slug = name.split(":", 1)[1]
        row = (children or {}).get(name) or {}
        entry = {"name": name, "how": "supervisor" if children is not None
                 else "unknown (no supervisor status)",
                 "state": row.get("state"), "detached": False,
                 "bridge": children is not None
                 and ("telegram:%s" % slug) in children}
        if slug == caller:
            entry["detached"] = True
            own = entry
            continue
        rows.append(entry)
    if own is not None:
        rows.append(own)
    return {"supervisor": unreachable, "order": rows}


# ---------------------------------------------------------------- plan

def build_plan(root, checkout, *, to=None, running=None):
    """The whole plan, computed from git and the homes' current bytes;
    nothing is written. Raises Refused for a downgrade --to does not
    name or a --to that names nothing, UpgradeError when the plan
    cannot be computed."""
    root, checkout = Path(root), Path(checkout)
    if not (checkout / ".git").exists():
        raise UpgradeError("%s is not a git checkout: only a checkout can be"
                           " planned in this release" % checkout)
    explicit = to is not None
    if to is None:
        to = newest_tag(checkout)
        if to is None:
            raise UpgradeError("%s has no release tag (v<major>.<minor>."
                               "<patch>); name a ref with --to" % checkout)
    to_commit = resolve(checkout, to)
    if to_commit is None:
        raise Refused("--to %s names nothing in %s" % (to, checkout))
    to_version = version_at(checkout, to)
    if _semver(to_version) is None:
        raise UpgradeError("%s has no readable version in pyproject.toml"
                           % to)
    if running is None:
        running = (version.version() if checkout == version.CHECKOUT
                   else version.pyproject_version(checkout / "pyproject.toml")
                   or version.UNKNOWN)
    notes = []
    code_from = "v%s" % running
    if resolve(checkout, code_from) is None:
        notes.append("no tag %s: FROM is the checkout's HEAD" % code_from)
        code_from = "HEAD"
    head = resolve(checkout, "HEAD") or ""
    old, new = _semver(running), _semver(to_version)
    if old is None:
        direction = "unknown"
        notes.append("the running version %s is not MAJOR.MINOR.PATCH"
                      % running)
    else:
        direction = ("downgrade" if new < old else
                     "same" if new == old else "upgrade")
    if direction == "downgrade" and not explicit:
        raise Refused("the newest release tag %s (%s) is older than the"
                      " running %s: nothing to upgrade to; --to %s plans"
                      " the downgrade" % (to, to_version, running, to))
    theirs = show(checkout, to, REGISTRY_EXAMPLE)
    template = show(checkout, to, TEMPLATE)
    if theirs is None or template is None:
        raise UpgradeError("%s ships no %s" % (
            to, REGISTRY_EXAMPLE if theirs is None else TEMPLATE))
    if direction == "downgrade":
        log = changelog(show(checkout, code_from, "CHANGELOG.md"),
                        to_version, running)
    else:
        log = changelog(show(checkout, to, "CHANGELOG.md"), running,
                        to_version)
    deps = dependency_changes(show(checkout, code_from, "pyproject.toml"),
                              show(checkout, to, "pyproject.toml"))
    if (root / "config" / mcp_server.REGISTRY_NAME).is_file():
        notes.append("config/%s is this install's own default registry (spawn"
                     " renders from it); the homes are planned against %s"
                     " at %s, and that file is not touched"
                     % (mcp_server.REGISTRY_NAME, REGISTRY_EXAMPLE, to))
    if show(checkout, code_from, POLICY_EXAMPLE) != show(checkout, to,
                                                          POLICY_EXAMPLE):
        notes.append("%s changed between %s and %s: each policy.toml is"
                     " operator policy, compare by hand"
                     % (POLICY_EXAMPLE, code_from, to))
    seeded = shared_tier.compare_templates(root, shipped={
        "law": show(checkout, to, LAW), "rules": _rules_at(checkout, to)})
    bases = {}

    def base_text(ref):
        if ref not in bases:
            bases[ref] = show(checkout, ref, REGISTRY_EXAMPLE)
        return bases[ref]

    homes = [home_plan(h, root, checkout, theirs=theirs, template=template,
                       code_from=code_from, base_text=base_text)
             for h in _homes(root)]
    return {"root": str(root), "checkout": str(checkout),
            "from": {"version": running, "ref": code_from,
                     "commit": head[:12]},
            "to": {"ref": to, "version": to_version,
                   "commit": to_commit[:12], "explicit": explicit},
            "direction": direction, "dirty": _dirty(checkout),
            "dependencies": deps, "changelog": log, "notes": notes,
            "seeded": seeded, "homes": homes,
            "restarts": restart_plan(root, _caller(root))}


# -------------------------------------------------------------- report

def _paths(label, items):
    return "%s %d (%s)" % (label, len(items), ", ".join(items))


def render(plan, *, full=False):
    """The plan as the report a person reads."""
    out = []
    say = out.append
    f, t = plan["from"], plan["to"]
    say("cousin-upgrade --dry-run: a plan; nothing written, nothing"
        " restarted")
    say("install   %s" % plan["root"])
    say("checkout  %s%s" % (plan["checkout"], " (tracked changes: %s)"
                            % ", ".join(plan["dirty"]) if plan["dirty"]
                            else ""))
    say("from      %s (%s, %s)" % (f["version"], f["ref"], f["commit"]))
    say("to        %s (%s, %s)%s" % (t["version"], t["ref"], t["commit"],
                                     "" if plan["direction"] == "upgrade"
                                     else "  [%s]" % plan["direction"]))
    say("dependencies %s" % ("changed: pip install -e . again"
                             if plan["dependencies"] else "unchanged"))
    for line in plan["dependencies"]:
        say("  %s" % line)
    for note in plan["notes"]:
        say("note: %s" % note)
    if plan["changelog"]:
        say("")
        say("changelog (%s):" % ("rolled back" if plan["direction"]
                                 == "downgrade" else "new"))
        for sec in plan["changelog"]:
            say("  %s%s" % (sec["version"], " - %s" % sec["date"]
                            if sec["date"] else ""))
            for group in sec["groups"]:
                for lead in group["leads"] or [""]:
                    say("    %-8s %s" % (group["title"], lead))
    say("")
    say("seeded files (against %s; never overwritten):" % t["ref"])
    for row in plan["seeded"]:
        extra = ""
        if row["status"] == "differs":
            extra = " (+%d -%d)" % _counts(row["diff"])
        say("  %-22s %s%s" % (row["status"], row["path"], extra))
        if full and row["diff"]:
            out.extend(row["diff"].rstrip("\n").splitlines())
    say("")
    say("homes:")
    for h in plan["homes"]:
        reg = h["registry"]
        if reg["status"] == "none":
            line = "no mcp-registry.toml"
        elif reg["status"] == "error":
            line = "cannot plan: %s" % reg["error"]
        else:
            parts = [_paths(label, reg[key]) for label, key in
                     (("add", "added"), ("migrate", "migrated"),
                      ("retired, kept", "retired")) if reg[key]]
            line = "; ".join(parts) or "in step"
        say("  %s (from %s, %s)" % (h["slug"], h["from"]["ref"],
                                    h["from"]["source"]))
        say("    registry     %s" % line)
        m = h["mcp_json"]
        say("    .mcp.json    %s" % {"absent": "absent (a start writes it)",
                                     "error": "cannot plan: %s"
                                     % m.get("error")}.get(m["status"],
                                                           m["status"]))
        say("    policy.toml  %s" % ("present, operator policy, not touched"
                                     if h["policy"]["present"] else "none"))
        c = h["claude_md"]
        if c["status"] == "differs":
            cl = "differs (+%d -%d), reported only" % (c["added_lines"],
                                                      c["removed_lines"])
        elif c["status"] == "error":
            cl = "cannot plan: %s" % c["error"]
        else:
            cl = {"none": "no CLAUDE.md"}.get(c["status"], c["status"])
        say("    CLAUDE.md    %s" % cl)
        for note in c.get("notes") or []:
            say("                 %s" % note)
        if full and c.get("diff"):
            out.extend(c["diff"].rstrip("\n").splitlines())
        for note in h["notes"]:
            say("    note: %s" % note)
    say("")
    r = plan["restarts"]
    say("restarts it would do, in order (none done now):")
    if r["supervisor"]:
        say("  supervisor not reachable: %s" % r["supervisor"])
    for i, row in enumerate(r["order"], 1):
        bits = [row["how"]]
        if row.get("state"):
            bits.append(row["state"])
        if row.get("bridge"):
            bits.append("with its telegram bridge")
        if row["detached"]:
            bits.append("the caller's own: last, detached")
        say("  %d. %-20s %s" % (i, row["name"], ", ".join(bits)))
    return "\n".join(out) + "\n"


# ----------------------------------------------------------------- CLI

def upgrade_main(argv=None):
    """cousin-upgrade [--to REF] --dry-run [--json] [--full]. Exit 0 the
    plan was computed, 1 it could not be, 2 refused."""
    from cousin_lib.config import FrameworkConfig, MissingConfigError
    parser = argparse.ArgumentParser(
        prog="cousin-upgrade",
        description="plan moving this install to another release: code,"
                    " seeded files, each home's registry, .mcp.json and"
                    " CLAUDE.md, restarts. This release plans only"
                    " (--dry-run); nothing is written.")
    parser.add_argument("--to", metavar="REF",
                        help="the release tag or ref to plan for (default:"
                             " the newest v<major>.<minor>.<patch> tag); an"
                             " older one than the running version is"
                             " planned only when named here")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the plan and change nothing (the only"
                             " mode in this release)")
    parser.add_argument("--json", action="store_true",
                        help="the plan as JSON")
    parser.add_argument("-v", "--full", action="store_true",
                        help="print each diff under its line")
    parser.add_argument("--root", help="the framework root (else"
                                       " FRAMEWORK_ROOT, else the checkout"
                                       " you are in)")
    parser.add_argument("--checkout", metavar="PATH",
                        help="the git checkout to read releases from"
                             " (default: the one this command runs from)")
    args = parser.parse_args(argv)
    if not args.dry_run:
        print("cousin-upgrade: only --dry-run is built in this release;"
              " the apply path comes in a later one", file=sys.stderr)
        return 2
    try:
        root = FrameworkConfig.resolve(args.root, cwd_fallback=True).root
    except MissingConfigError as err:
        print("cousin-upgrade: %s" % err, file=sys.stderr)
        return 2
    checkout = Path(args.checkout).resolve() if args.checkout \
        else version.CHECKOUT
    try:
        plan = build_plan(root, checkout, to=args.to)
    except Refused as err:
        print("cousin-upgrade: %s" % err, file=sys.stderr)
        return 2
    except UpgradeError as err:
        print("cousin-upgrade: %s" % err, file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(plan, indent=2))
    else:
        sys.stdout.write(render(plan, full=args.full))
    return 0


if __name__ == "__main__":
    sys.exit(upgrade_main())
