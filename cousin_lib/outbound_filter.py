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
