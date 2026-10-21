"""Console routes for skills (docs/reference/skills.md;
docs/reference/console-api.md, "Skills").

A package seam (app.py PACKAGE_ROUTE_MODULES): this module is the
package's own, and skills.jsx is its browser side (System > skills, and a
panel in each cousin's inspector).

Everything goes through cousin_lib/skills: listing, showing, the diff a
draft or a removal makes, and the verbs only the console has: approve,
reject, remove, and allowing a dry run of a skill from outside. Approve,
remove and the allow take the sha256 of the diff the page showed and
refuse any other. A record names the console user who acted.

Privacy: the System routes (GET /api/skills and /api/skills/shared/...)
never show a cousin that loads the `private` repository, nor its drafts:
its folder is its name. Its skills show only on its own routes, which the
inspector uses."""
from __future__ import annotations

import json
import subprocess
import sys

from cousin_lib import skills
from cousin_lib.config import FrameworkConfig
from cousin_lib.console import router
from cousin_lib.console._common import check_slug, cousin_home
from cousin_lib.console.app import HttpError
from cousin_lib.skills import SkillError

TRIGGER_TIMEOUT = 300
FETCH_TIMEOUT = 60


def _who(req):
    return req.user or "_open"


def _name(name):
    if not skills.NAME_RE.match(name or "") or len(name) > skills.NAME_MAX:
        raise HttpError(400, "bad skill name")
    return name


def _cousins(server):
    """[(slug, home, repos)] for every registered cousin, repos [] when its
    cousin.toml's list does not parse."""
    out = []
    for config in FrameworkConfig(server.root).list_cousins():
        try:
            repos = skills.cousin_repos(config.home)
        except SkillError:
            repos = []
        out.append((config.slug, config.home, repos))
    return out


def _private_slugs(server):
    return {slug for slug, _home, repos in _cousins(server) if "private" in repos}


def _cousin(req, slug):
    check_slug(slug)
    home = cousin_home(req.server, slug)
    return home


def _source(home, slug, plugin):
    """(repo, folder) of a plugin name the cousin loads, else 404."""
    try:
        sources = skills.sources(slug, skills.cousin_repos(home))
    except SkillError as err:
        raise HttpError(400, str(err))
    for repo, folder, name in sources:
        if name == plugin:
            return repo, folder
    raise HttpError(404, "this cousin loads no %s skills" % plugin)


def _fetch(url, commit, folder):
    """The console's fetch of a source: skills.fetch_folder with a shorter
    time limit, since a page waits on it."""
    return skills.fetch_folder(url, commit, folder, timeout=FETCH_TIMEOUT)


def _tell(home, text):
    """One row on the cousin's system thread, from the framework (the way
    jobs.notify_owner tells a job's owner). Whether it was taken; never
    raises: the allow is recorded either way."""
    try:
        from cousin_lib import delivery
        item = delivery.Item(thread_id=delivery.thread_id("system"), source="skill",
                             body=text, sender="framework")
        wait = not isinstance(delivery.backend_for(home), delivery.InboxBackend)
        return bool(delivery.accepted(delivery.deliver(home, item, wait=wait), home))
    except Exception:  # noqa: BLE001 - the operator sees told: false
        return False


def _restart_note(scope_text):
    return "loads at the next start of %s; restart it from the inspector to load it now" % scope_text


def _call(fn, *args, **kw):
    try:
        return fn(*args, **kw)
    except SkillError as err:
        message = str(err)
        status = 404 if message.startswith(("no draft", "no approved", "no skill")) else 409
        raise HttpError(status, message)


def register():
    @router.route("GET", "/api/skills")
    def overview(req):
        """The shared set and every non-private cousin's folders in
        personal and work, with each repository's state, and the drafts
        waiting from those cousins."""
        private = _private_slugs(req.server)
        repos = []
        for repo in ("personal", "work"):
            st = skills.state(req.server.root, repo)
            folders = []
            if st["trusted"]:
                path = skills.repo_dir(req.server.root, repo)
                listed = skills._ls_dirs(path, st["approved"], ".") if st["approved"] else []
                for folder in sorted(set(listed) - private):
                    found = skills.approved(req.server.root, repo, folder)
                    if found:
                        folders.append({"folder": folder, "skills": found})
            repos.append(dict(st, folders=folders))
        drafts = []
        for slug, home, cousin_repos in _cousins(req.server):
            if slug in private or "private" in cousin_repos:
                continue
            for d in skills.drafts(home, slug):
                if d.get("repo") == "private":
                    continue
                drafts.append(dict(d, cousin=slug))
        return 200, {"repos": repos, "drafts": drafts}

    @router.route("GET", "/api/skills/shared/{name}")
    def show_shared(req, name):
        return 200, _call(skills.show_approved, req.server.root, "personal", skills.SHARED,
                          _name(name))

    @router.route("GET", "/api/skills/shared/{name}/removal")
    def shared_removal(req, name):
        text, sha = _call(skills.removal_diff, req.server.root, "personal", skills.SHARED,
                          _name(name))
        return 200, {"diff": text, "diff_sha256": sha}

    @router.route("POST", "/api/skills/shared/{name}/remove")
    def remove_shared(req, name):
        entry = _call(skills.remove_skill, req.server.root, "personal", skills.SHARED,
                      _name(name), user=_who(req), diff_sha=str(req.body.get("diff_sha256") or ""))
        return 200, {"ok": True, "record": entry, "note": _restart_note("every cousin")}

    @router.route("GET", "/api/cousins/{slug}/skills")
    def cousin_skills(req, slug):
        home = _cousin(req, slug)
        try:
            repos = skills.cousin_repos(home)
        except SkillError as err:
            return 200, {"repos": [], "error": str(err), "loaded": [], "drafts": []}
        return 200, {"repos": repos,
                     "states": [skills.state(req.server.root, r) for r in repos],
                     "lane": skills.lane_of(home),
                     "loaded": skills.loaded(req.server.root, home, slug),
                     "drafts": skills.drafts(home, slug)}

    @router.route("GET", "/api/cousins/{slug}/skills/approved/{plugin}/{name}")
    def show_loaded(req, slug, plugin, name):
        home = _cousin(req, slug)
        repo, folder = _source(home, slug, plugin)
        return 200, dict(_call(skills.show_approved, req.server.root, repo, folder, _name(name)),
                         plugin=plugin)

    @router.route("GET", "/api/cousins/{slug}/skills/approved/{plugin}/{name}/removal")
    def loaded_removal(req, slug, plugin, name):
        home = _cousin(req, slug)
        repo, folder = _source(home, slug, plugin)
        text, sha = _call(skills.removal_diff, req.server.root, repo, folder, _name(name))
        return 200, {"diff": text, "diff_sha256": sha}

    @router.route("POST", "/api/cousins/{slug}/skills/approved/{plugin}/{name}/remove")
    def remove_loaded(req, slug, plugin, name):
        home = _cousin(req, slug)
        repo, folder = _source(home, slug, plugin)
        entry = _call(skills.remove_skill, req.server.root, repo, folder, _name(name),
                      user=_who(req), diff_sha=str(req.body.get("diff_sha256") or ""))
        who = "every cousin" if folder == skills.SHARED else slug
        return 200, {"ok": True, "record": entry, "note": _restart_note(who)}

    @router.route("GET", "/api/cousins/{slug}/skills/drafts/{name}")
    def show_draft(req, slug, name):
        home = _cousin(req, slug)
        draft = _call(skills.show_draft, home, _name(name))
        text, sha = _call(skills.diff_draft, req.server.root, home, slug, name)
        summary = next((d for d in skills.drafts(home, slug) if d["name"] == name), {})
        return 200, dict(draft, diff=text, diff_sha256=sha, repo=summary.get("repo"),
                         folder=summary.get("folder"), problems=summary.get("problems", []),
                         metadata=summary.get("metadata", {}),
                         trigger=skills.last_trigger_test(req.server.root, home, slug, name, sha),
                         outside=_call(skills.outside_view, req.server.root, home, slug, name,
                                       sha))

    @router.route("GET", "/api/cousins/{slug}/skills/drafts/{name}/upstream")
    def draft_upstream(req, slug, name):
        """A draft from outside against its source, fetched again now (the
        approval fetches it once more): where it is not as published, and
        what upstream's frontmatter held that the import did not keep."""
        home = _cousin(req, slug)
        return 200, dict(_call(skills.upstream_view, home, _name(name), fetch=_fetch), ok=True)

    @router.route("POST", "/api/cousins/{slug}/skills/drafts/{name}/dry-run/allow")
    def allow_dry_run(req, slug, name):
        """The operator's go for one dry run of the version shown; the
        cousin is told on its system thread, runs the task as a job and
        records it (`skill dry-run`)."""
        home = _cousin(req, slug)
        entry = _call(skills.allow_dry_run, req.server.root, home, slug, _name(name),
                      user=_who(req), diff_sha=str(req.body.get("diff_sha256") or ""))
        told = _tell(home, "[cousin-skill] %s allowed a dry run of your draft %s. Do its task"
                           " (%s) as one job with the job tool's run, then record it with the"
                           " skill tool: dry-run, name %s, job <its id>. The operator approves"
                           " in the console after that."
                     % (_who(req), name, entry.get("task") or "metadata.dry-run-task", name))
        return 200, {"ok": True, "dry_run": entry, "told": told}

    @router.route("POST", "/api/cousins/{slug}/skills/drafts/{name}/approve")
    def approve(req, slug, name):
        home = _cousin(req, slug)
        entry = _call(skills.approve_draft, req.server.root, home, slug, _name(name),
                      user=_who(req), diff_sha=str(req.body.get("diff_sha256") or ""))
        who = "every cousin" if entry.get("folder") == skills.SHARED else slug
        return 200, {"ok": True, "record": entry, "note": _restart_note(who)}

    @router.route("POST", "/api/cousins/{slug}/skills/drafts/{name}/reject")
    def reject(req, slug, name):
        home = _cousin(req, slug)
        reason = req.body.get("reason") or ""
        if not isinstance(reason, str):
            raise HttpError(400, "reason must be text")
        _call(skills.reject_draft, home, _name(name), user=_who(req), reason=reason)
        return 200, {"ok": True}

    @router.route("POST", "/api/cousins/{slug}/skills/drafts/{name}/trigger-test")
    def trigger_test(req, slug, name):
        """The draft's trigger test, in a child process (the check scrubs
        the auth variables of the process it runs in); its result is kept
        under <install>/data/skills/ (skills.trigger_path) and comes back
        here."""
        home = _cousin(req, slug)
        _call(skills.show_draft, home, _name(name))
        try:
            proc = subprocess.run([sys.executable, "-m", "cousin_lib.skills", "--home", str(home),
                                   "trigger-test", name], capture_output=True, text=True,
                                  timeout=TRIGGER_TIMEOUT,
                                  env=dict(__import__("os").environ,
                                           FRAMEWORK_ROOT=str(req.server.root)))
        except subprocess.TimeoutExpired:
            raise HttpError(504, "the trigger test took over %ds" % TRIGGER_TIMEOUT)
        if proc.returncode != 0:
            raise HttpError(409, (proc.stderr or proc.stdout).strip().replace(
                "cousin-skill: ", "") or "the trigger test failed")
        try:
            result = json.loads(proc.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            raise HttpError(500, "the trigger test printed no result")
        return 200, {"ok": True, "trigger": result}


register()
