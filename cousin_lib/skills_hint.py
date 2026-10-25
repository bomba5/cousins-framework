"""The per-turn skills hint (docs/reference/skills.md, "The per-turn
hint"): which of the skills a cousin loads match the message in front of
it, named in the turn's context, so the cousin uses the skill instead of
working from memory.

A skill matches when the message's embedding is near the embedding of the
skill's name and description, which says when to use it. Only the
embedding seam (config/embedding.toml) is asked: no model call, no keyword
fallback, and without the seam there is no hint. The skills are the ones
exported for this connect (<home>/run/skills/<plugin>/skills/<name>/),
read again at every message since a rollover or a reconnect exports
afresh, so a hint never names a skill the agent cannot load, and a
cousin's skills never leave its own runner. A skill's vector is taken the
first time its name and description are seen, and kept.

MIN_SCORE and TOP were set on the approved skills' trigger prompts and on
400 real operator messages (2026-10-25): at 0.62, 8% of the messages got a
hint, most of them the skill the message was about."""
from __future__ import annotations

import threading
from pathlib import Path

from cousin_lib import skills

HINT_PREFIX = "[fw-skills] skills you load that match this message: "
HINT_SUFFIX = (". Use the one that fits instead of working from memory;"
               " ignore them if none does.")
MIN_SCORE = 0.62
TOP = 3
MIN_CHARS = 24


def exported(home):
    """[(plugin:name, text)] of the skills exported for this start, `text`
    being the skill's name and description as its SKILL.md says them."""
    out = []
    for path in sorted(skills.export_dir(home).glob("*/skills/*/%s" % skills.SKILL_FILE)):
        try:
            front, _body = skills.parse(path.read_text())
        except (OSError, UnicodeDecodeError, skills.SkillError):
            continue
        description = " ".join(str(front.get("description") or "").split())
        if description:
            name = path.parent.name
            out.append(("%s:%s" % (path.parents[2].name, name), "%s: %s" % (name, description)))
    return out


class Hinter:
    """One runner's hint. `hint(body)` is blocking (it asks the embedding
    service), so the hook runs it on a worker thread."""

    def __init__(self, home, root=None):
        self.home, self.root = Path(home), root
        self._vectors = {}
        self._lock = threading.Lock()

    def _skill_vectors(self, skills_now, config):
        """[(plugin:name, vector)] for the skills exported now. A vector is
        kept per model and text, so a skill exported again unchanged is not
        asked for again, and one that failed is asked at the next message."""
        from cousin_lib import memory_search
        with self._lock:
            out = []
            for name, text in skills_now:
                key = (config.get("model"), text)
                if key not in self._vectors:
                    self._vectors[key] = memory_search._embed(text, config)
                out.append((name, self._vectors[key]))
            return out

    def hint(self, body):
        """(line, [(plugin:name, score)]) for a message's body: the line
        names up to TOP skills at MIN_SCORE or nearer, best first, of the
        skills exported now; ("", []) when none does, the body is short,
        the cousin loads no skills or there is no embedding seam."""
        from cousin_lib import memory_search
        body = str(body or "").strip()
        if len(body) < MIN_CHARS:
            return "", []
        skills_now = exported(self.home)
        if not skills_now:
            return "", []
        config = memory_search._embedding_config(self.root)
        if not isinstance(config, dict):
            return "", []
        vectors = self._skill_vectors(skills_now, config)
        query = memory_search._embed(body, config)
        scored = sorted(((memory_search._cosine(query, v), name) for name, v in vectors),
                        reverse=True)
        hits = [(name, round(score, 3)) for score, name in scored if score >= MIN_SCORE][:TOP]
        if not hits:
            return "", []
        return HINT_PREFIX + ", ".join(name for name, _ in hits) + HINT_SUFFIX, hits
