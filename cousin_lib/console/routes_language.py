"""Console routes for the plain-language check (docs/plain-language.md;
the routes in docs/reference/console-api.md):

  POST /api/language/check    {text, glossary?} -> {findings, fixed}
  GET  /api/language/config   config/plain-language.toml, checked, and each
                              cousin's [plain_language] reply mode
  PUT  /api/language/config   {config} checked, then written whole
  GET  /api/language/terms    ?path=&top=&glossary= the most used terms
  POST /api/language/groups   {path, glossary?, groups?} synonym groups' uses
  POST /api/language/derive   {path, glossary?, groups?} -> {text}: the
                              derived glossary to copy, never written here

A cousin's reply mode is written through POST /api/cousins/<slug>/settings
(routes_agent.py, the field plain_language.reply), the way every other
per-cousin setting is. A path the operator names (a glossary) must lie
under the install root, a cousin's home or the framework checkout.
The glossary scans (cousin_lib/glossary.py) only read."""
from __future__ import annotations

import os
from pathlib import Path

from cousin_lib import glossary as code_glossary
from cousin_lib import plain_language
from cousin_lib.console import router
from cousin_lib.console.app import HttpError

TEXT_MAX_CHARS = 200_000


def _checkout():
    """The framework checkout this console runs from, or None (an installed
    package: site-packages is no place to scan)."""
    from cousin_lib import version
    from cousin_lib.config import FrameworkConfig
    return version.CHECKOUT if FrameworkConfig.looks_like_checkout(version.CHECKOUT) else None


def allowed_bases(root):
    """The directories a path from the console may name: the install root,
    each cousin's home (where it really is) and the framework checkout."""
    root = Path(root)
    bases = [root] + [b for b in (_checkout(),) if b is not None]
    cousins = root / "cousins"
    if cousins.is_dir():
        bases += [entry for entry in sorted(cousins.iterdir())
                  if (entry / "cousin.toml").is_file()]
    return [Path(os.path.realpath(b)) for b in bases]


def allowed_path(root, raw):
    """The real path `raw` names (relative to the root), or 400 when it is
    empty or lies outside allowed_bases."""
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
        raise HttpError(400, "path required")
    path = Path(raw.strip()).expanduser()
    if not path.is_absolute():
        path = Path(root) / path
    real = Path(os.path.realpath(path))
    from cousin_lib.home_files import SECRET_NAMES
    if SECRET_NAMES & (set(path.parts) | set(real.parts)):
        raise HttpError(400, "%s: a secrets directory is never read here" % raw)
    if not any(real == b or real.is_relative_to(b) for b in allowed_bases(root)):
        raise HttpError(400, "%s is outside the install, the cousins' homes and the"
                             " framework checkout" % raw)
    return real


def _config(root):
    try:
        return plain_language.load_config(root), None
    except plain_language.ConfigError as err:
        return plain_language.default_config(), str(err)


def _glossary(root, cfg, raw):
    try:
        if raw:
            return plain_language.load_glossary(allowed_path(root, raw))
        return plain_language.configured_glossary(root, cfg)
    except (OSError, UnicodeDecodeError, plain_language.ConfigError) as err:
        raise HttpError(400, "glossary: %s" % (getattr(err, "strerror", None) or err))


def reply_modes(root):
    """[{slug, name, reply, set}] for each local cousin: its own reply
    mode, or the install default when it sets none."""
    from cousin_lib.console._common import read_toml
    rows = []
    base = Path(root) / "cousins"
    if not base.is_dir():
        return rows
    for home in sorted(base.iterdir()):
        if not (home / "cousin.toml").is_file():
            continue
        data = read_toml(home)
        cousin = data.get("cousin") or {}
        table = data.get("plain_language")
        own = table.get("reply") if isinstance(table, dict) else None
        rows.append({"slug": cousin.get("slug") or home.name,
                     "name": cousin.get("name") or home.name.capitalize(),
                     "reply": plain_language.reply_mode(home, root),
                     "set": own in plain_language.REPLY_MODES})
    return rows


def _groups(req):
    """The synonym groups of a groups or derive body: the glossary's
    entries (the named file, else the configured one) and `groups`, a
    list of "a,b,c" strings; 400 when there are none."""
    body = req.body
    root = req.server.root
    extra = body.get("groups") or []
    if not isinstance(extra, list) or not all(isinstance(g, str) for g in extra):
        raise HttpError(400, "groups must be a list of \"a,b,c\" strings")
    cfg, _problem = _config(root)
    glossary = _glossary(root, cfg, body.get("glossary"))
    groups = code_glossary.synonym_groups(glossary, extra)
    if not groups:
        raise HttpError(400, "no synonym groups: name a glossary or a group")
    return groups


def register():
    @router.route("POST", "/api/language/check")
    def check(req):
        text = req.body.get("text")
        if not isinstance(text, str) or len(text) > TEXT_MAX_CHARS:
            raise HttpError(400, "text must be a string of at most %d characters"
                            % TEXT_MAX_CHARS)
        root = req.server.root
        cfg, problem = _config(root)
        if problem:
            raise HttpError(400, "config/plain-language.toml: %s" % problem)
        glossary = _glossary(root, cfg, req.body.get("glossary"))
        found = plain_language.check(text, config=cfg, glossary=glossary)
        return 200, {"findings": [f.as_dict() for f in found],
                     "fixed": plain_language.apply_fixes(text, found),
                     "glossary_terms": len(glossary or {})}

    @router.route("GET", "/api/language/config")
    def get_config(req):
        root = req.server.root
        cfg, problem = _config(root)
        out = {"config": cfg, "exists": plain_language.config_path(root).is_file(),
               "path": "config/%s" % plain_language.CONFIG_NAME,
               "rules": list(plain_language.RULES),
               "modes": list(plain_language.REPLY_MODES),
               "cousins": reply_modes(root)}
        if problem:
            out["error"] = problem
        return 200, out

    @router.route("PUT", "/api/language/config")
    def put_config(req):
        cfg = req.body.get("config")
        if isinstance(cfg, dict) and isinstance(cfg.get("glossary"), str) \
                and cfg["glossary"].strip():
            allowed_path(req.server.root, cfg["glossary"])
        try:
            cfg = plain_language.write_config(req.server.root, cfg)
        except plain_language.ConfigError as err:
            raise HttpError(400, "not saved: %s" % err)
        return 200, {"ok": True, "config": cfg}

    @router.route("GET", "/api/language/terms")
    def terms(req):
        root = req.server.root
        path = allowed_path(root, req.query.get("path"))
        try:
            top = int(req.query.get("top") or 50)
        except ValueError:
            raise HttpError(400, "top must be a whole number")
        if not 1 <= top <= 1000:
            raise HttpError(400, "top must be from 1 to 1000")
        if not path.exists():
            raise HttpError(404, "%s: no such file or directory" % req.query.get("path"))
        cfg, _problem = _config(root)
        glossary = _glossary(root, cfg, req.query.get("glossary"))
        corpus = code_glossary.scan(path)
        return 200, {"terms": code_glossary.terms(path, top=top, glossary=glossary,
                                                  corpus=corpus),
                     "files": corpus.files, "skipped": corpus.skipped,
                     "truncated": corpus.truncated}

    def _scan_groups(req):
        path = allowed_path(req.server.root, req.body.get("path"))
        if not path.exists():
            raise HttpError(404, "%s: no such file or directory" % req.body.get("path"))
        return code_glossary.check_groups(path, _groups(req))

    @router.route("POST", "/api/language/groups")
    def groups(req):
        rows = _scan_groups(req)
        return 200, {"groups": rows, "flagged": sum(1 for r in rows if r["flagged"])}

    @router.route("POST", "/api/language/derive")
    def derive(req):
        rows = _scan_groups(req)
        return 200, {"text": code_glossary.derive_text(rows), "entries": len(rows)}


register()
