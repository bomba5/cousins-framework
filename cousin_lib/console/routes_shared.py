"""The shared-tier review (docs/reference/console-api.md, "Memory and the
shared-tier review"): listings with hashes, content strictly inside
each scope, diffs, the audit tail, and promote/reject through
cousin_lib.shared_tier with the reviewer taken from the session (or,
unconfigured, from the body), and the reviewer list itself
(config/shared-reviewers.json): readable, and writable by a logged-in
user only, each change a row in the tier's audit."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from cousin_lib import shared_tier
from cousin_lib.console import router
from cousin_lib.console._common import check_slug
from cousin_lib.console.app import HttpError


def _stat_row(path):
    try:
        st = path.stat()
        sha = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    except OSError:
        return {"size": 0, "mtime": 0, "sha": ""}
    return {"size": st.st_size, "mtime": int(st.st_mtime), "sha": sha}


def _scope_dir(root, scope):
    if scope == "canonical":
        return Path(root) / "shared"
    if scope == "pending":
        return Path(root) / "shared" / "proposed"
    raise HttpError(400, "scope must be canonical or pending")


def _reviewer(req):
    if req.server.users.configured():
        return req.user
    by = req.body.get("by")
    if not isinstance(by, str) or not by.strip():
        raise HttpError(400, "by is required when auth is not configured")
    return by.strip()


def _review(req, action):
    slug = req.body.get("slug")
    file = req.body.get("file")
    check_slug(slug)
    if not isinstance(file, str) or not file or "/" in file \
            or file.startswith("."):
        raise HttpError(400, "file must be a canonical file name")
    by = _reviewer(req)
    try:
        if action == "approve":
            shared_tier.promote(file, proposer=slug, by=by)
        else:
            reason = req.body.get("reason") or ""
            shared_tier.reject(file, proposer=slug, by=by,
                               reason=str(reason))
    except shared_tier.PromoteRefused as err:
        raise HttpError(403, str(err))
    except FileNotFoundError as err:
        raise HttpError(404, str(err))
    return 200, {"ok": True, "file": file}


REVIEWERS_MAX = 50
REVIEWER_CHARS = 64


def _reviewers_path(root):
    return Path(root) / "config" / "shared-reviewers.json"


def _read_reviewers(root):
    """(data, error): the file's object ({} when absent), or None and why
    it cannot be read. Unreadable is never read as empty: the promote
    path treats it as "no reviewers configured", and a write over it
    would lose what the operator is fixing by hand."""
    path = _reviewers_path(root)
    try:
        text = path.read_text()
    except FileNotFoundError:
        return {}, None
    except OSError as err:
        return None, "unreadable: %s" % err
    try:
        data = json.loads(text)
    except ValueError as err:
        return None, "not valid JSON: %s" % err
    if not isinstance(data, dict) or not isinstance(
            data.get("reviewers", []), list):
        return None, "not an object with a reviewers list"
    return data, None


def _clean_reviewers(value):
    if not isinstance(value, list):
        raise HttpError(400, "reviewers must be a list of names")
    if len(value) > REVIEWERS_MAX:
        raise HttpError(400, "at most %d reviewers" % REVIEWERS_MAX)
    out, seen = [], set()
    for name in value:
        if not isinstance(name, str) or not name.strip():
            raise HttpError(400, "each reviewer is a non-empty name")
        name = name.strip()
        if len(name) > REVIEWER_CHARS or any(ord(c) < 32 or c == "\x7f"
                                              for c in name):
            raise HttpError(400, "a reviewer name is at most %d printable"
                                 " characters" % REVIEWER_CHARS)
        if name.casefold() not in seen:
            seen.add(name.casefold())
            out.append(name)
    return out


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _reviewers_state(req):
    data, error = _read_reviewers(req.server.root)
    names = [r for r in (data or {}).get("reviewers", [])
             if isinstance(r, str)]
    you = None
    if req.user and data is not None:
        try:
            principals = {shared_tier._principal(r) for r in names}
            you = shared_tier._principal(req.user) in principals
        except Exception:  # noqa: BLE001 - a hint, never a failure
            you = None
    return {"configured": bool(data) and "reviewers" in data,
            "reviewers": names, "error": error, "user": req.user,
            "you_review": you}


def register():
    @router.route("GET", "/api/shared/reviewers")
    def reviewers(req):
        return 200, _reviewers_state(req)

    @router.route("POST", "/api/shared/reviewers")
    def set_reviewers(req):
        """{"reviewers": [name, ...]}: replace the list (other keys in the
        file stay). Who may promote into the shared tier is a perimeter,
        so a logged-in user only; the change is audited."""
        if not req.server.users.configured() or not req.user:
            raise HttpError(403, "changing the reviewers needs a logged-in"
                                 " console user")
        names = _clean_reviewers(req.body.get("reviewers"))
        data, error = _read_reviewers(req.server.root)
        if data is None:
            raise HttpError(409, "config/shared-reviewers.json is %s; fix"
                                 " or remove it by hand first" % error)
        data["reviewers"] = names
        _write_json(_reviewers_path(req.server.root), data)
        shared_tier._audit("reviewers", req.user,
                           "config/shared-reviewers.json",
                           {"reviewers": names})
        return 200, {"ok": True, **_reviewers_state(req)}

    @router.route("GET", "/api/shared/list")
    def list_shared(req):
        root = req.server.root
        state = shared_tier.list_shared()
        canonical = []
        for name in state["canonical"]:
            canonical.append({"name": name,
                              **_stat_row(root / "shared" / name)})
        pending = []
        for name in state["pending"]:
            slug, sep, rest = name.partition("__")
            pending.append({"name": name,
                            "slug": slug if sep else "",
                            "origin": rest if sep else name,
                            **_stat_row(root / "shared" / "proposed" / name)})
        return 200, {"canonical": canonical, "pending": pending}

    @router.route("GET", "/api/shared/content")
    def content(req):
        name = req.query.get("name")
        if not name:
            raise HttpError(400, "name is required")
        base = _scope_dir(req.server.root, req.query.get("scope", "canonical"))
        try:
            candidate = (base / name).resolve()
            inside = candidate.parent == base.resolve()
        except OSError:
            inside = False
        if not inside or not candidate.is_file():
            raise HttpError(404, "no such file in that scope")
        return 200, {"content": candidate.read_text(errors="replace")}

    @router.route("GET", "/api/shared/diff")
    def diff(req):
        file = req.query.get("file")
        slug = req.query.get("slug")
        if not file or not slug:
            raise HttpError(400, "file and slug are required")
        check_slug(slug)
        proposed = (req.server.root / "shared" / "proposed"
                    / shared_tier._proposed_name(slug, file))
        if not proposed.is_file():
            raise HttpError(404, "no proposal from %s for %s" % (slug, file))
        return 200, {"diff": shared_tier.diff_proposal(file, slug)}

    @router.route("GET", "/api/shared/audit")
    def audit(req):
        n = max(1, req.int_query("n", 100))
        path = req.server.root / "shared" / "audit.jsonl"
        try:
            lines = path.read_text().splitlines()
        except OSError:
            lines = []
        entries = []
        for line in reversed(lines[-n:]):
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict):
                entries.append(entry)
        return 200, {"entries": entries}

    @router.route("POST", "/api/shared/approve")
    def approve(req):
        return _review(req, "approve")

    @router.route("POST", "/api/shared/reject")
    def reject(req):
        return _review(req, "reject")


register()
