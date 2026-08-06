"""Cousin template rendering.

The template is the single source of a new cousin's CLAUDE.md. The
renderer's one hard rule: rendered output contains no unsubstituted
placeholder. A cousin whose voice guide never got filled in has no
authored register on bedrock and will improvise one - that is a spawn
failure, not a TODO.
"""
import re

_PLACEHOLDER_RE = re.compile(r"\{\{([A-Z_]+)\}\}")
_COMMENT_RE = re.compile(r"<!--.*?-->\n?", re.S)


class TemplateError(Exception):
    """Rendering could not complete; the message names what is missing."""


def render_template(text, values):
    """Substitute {{KEY}} placeholders literally and fail on leftovers.

    HTML comments are stripped first: template comments are guidance for
    template editors, and persisting "replace this before saving" into a
    cousin's bedrock is drift-bait. Stripping first also means a
    placeholder mentioned inside a comment is documentation, never a
    render failure."""
    text = _COMMENT_RE.sub("", text)
    for key, value in values.items():
        text = text.replace("{{%s}}" % key, str(value))
    leftover = sorted(set(_PLACEHOLDER_RE.findall(text)))
    if leftover:
        raise TemplateError(
            "unsubstituted placeholder(s): %s" % ", ".join(leftover)
        )
    return text
