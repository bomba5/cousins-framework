"""Per-surface outbound content filter.

Some cousins are protected: their existence is confidential to the
operator, so their slugs must never appear in text leaving the protected
zone, and their own outbound traffic is scanned unless it goes to the
operator surface or an explicitly trusted peer. Vocabulary and peers are
configuration - the framework ships no banned words and trusts nobody by
default. An install without a policy file has an inert filter.

Matching is word-boundary and case-insensitive: a term must never match
inside a longer word, both because false blocks erode trust in the filter
and because noise buries real hits.
"""
import json
import os
import re
import tomllib
from pathlib import Path


class FilterBlocked(Exception):
    def __init__(self, terms, context=""):
        self.terms = terms
        self.context = context
        super().__init__(
            "outbound text blocked%s: contains %s"
            % (" (%s)" % context if context else "", ", ".join(terms))
        )


class OutboundPolicy:
    def __init__(self, terms=(), protected=(), trusted_peers=(), surfaces=None):
        self.terms = list(terms)
        self.protected = set(protected)
        self.trusted_peers = set(trusted_peers)
        self.surfaces = dict(surfaces or {})

    @classmethod
    def load(cls, framework_root):
        path = Path(framework_root) / "config" / "outbound-filter.json"
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return cls()
        return cls(
            terms=data.get("terms") or [],
            protected=data.get("protected") or [],
            trusted_peers=data.get("trusted_peers") or [],
            surfaces=data.get("surfaces") or {},
        )

    def _active_terms(self, surface):
        terms = set(self.terms) | self.protected
        additions = self.surfaces.get(surface, {}).get("add") or []
        return terms | set(additions)

    def scan(self, text, surface="chat"):
        if not text:
            return []
        found = []
        for term in sorted(self._active_terms(surface)):
            if re.search(r"\b" + re.escape(term) + r"\b", text, re.IGNORECASE):
                found.append(term)
        return found

    def is_internal(self, from_slug, dest_slug):
        if dest_slug in self.protected:
            return True
        if from_slug in self.protected and (
            not dest_slug or dest_slug in self.trusted_peers
        ):
            return True
        return False

    def check(self, text, *, from_slug, dest_slug, surface="chat", context=""):
        if os.environ.get("COUSIN_FILTER_OVERRIDE") == "1":
            return
        if self.is_internal(from_slug, dest_slug):
            return
        hits = self.scan(text, surface=surface)
        if hits:
            raise FilterBlocked(hits, context=context)


def private_cousins(framework_root):
    """{slug: display name} for each cousin law 11 protects: its
    cousin.toml says `[memory] scope = "private"` in so many words. An
    unset scope is private for nomination (plan_bulk_propose excludes
    it) but does not make a private cousin: which cousins the operator
    keeps out of other cousins' reach is a choice the operator writes
    down. A cousin.toml that does not parse is skipped."""
    base = Path(framework_root) / "cousins"
    found = {}
    if not base.is_dir():
        return found
    for entry in sorted(base.iterdir()):
        try:
            data = tomllib.loads((entry / "cousin.toml").read_text())
        except (OSError, ValueError):
            continue
        cousin = data.get("cousin") or {}
        slug = cousin.get("slug")
        if not isinstance(slug, str) or not slug:
            continue
        if (data.get("memory") or {}).get("scope") == "private":
            found[slug] = str(cousin.get("name") or slug.capitalize())
    return found


def law11_names(framework_root):
    """Every name law 11 keeps out of text other cousins read: each
    private cousin's slug and display name (private_cousins), and the
    slugs config/outbound-filter.json lists as protected. Works with no
    policy file: the protected set comes from the cousin configs."""
    names = set(OutboundPolicy.load(framework_root).protected)
    for slug, name in private_cousins(framework_root).items():
        names.update((slug, name))
    return {n for n in names if isinstance(n, str) and n.strip()}
