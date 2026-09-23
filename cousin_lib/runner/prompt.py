"""The prompt is composed, not copied (spec, same heading).

    system prompt = the claude_code preset
                  + the law                 immutable, never truncated
                  + the framework contract  generated (contract.py)
                  + the authored identity   the operator's words
                  + operator rules          the shared tier's kind: rule entries
    first message = the state digest (state_digest, below)

The system prompt MUST be byte-stable across generations (phase 0
finding 3): nothing here reads a clock, a counter, the generation, a
hash or any state file. The digest carries all of that, under the boot
packet's budget rules (boot.fit). Every read is under the `root` the
caller passes; nothing here discovers a root from the environment."""
import re
from pathlib import Path

from cousin_lib import boot, self_portrait, template_sync
from cousin_lib.runner import contract

IDENTITY_ABSENT = (
    "## Identity (degraded)\n\n"
    "No authored identity is on disk for this cousin: no committed self-portrait"
    " and no authored sections in CLAUDE.md. This is a degraded state, not a"
    " licence to invent one. Use the plain professional register of your role:"
    " no persona, no pet names, no invented history. Tell the operator the"
    " identity layer is missing when it matters.")

_KEEP_HEADINGS = ("## Identity", "## Voice")
_COMMENT = re.compile(r"<!--.*?-->", re.S)
_TITLE = re.compile(r"^# .+ - .+$")
_PARA = re.compile(r"\n\s*\n")


def _read(path):
    try:
        return Path(path).read_text()
    except OSError:
        return ""


def _norm(text):
    return " ".join(text.split())


def _paragraphs(text):
    return [p.strip() for p in _PARA.split(_COMMENT.sub("", text)) if p.strip()]


def _template_paragraphs(root, home):
    """The rendered CLAUDE.md template's paragraphs, normalised: framework
    text, never authored. A paragraph still holding a {{placeholder}} (the
    role paragraph, the voice guide) is the authored slot, not template
    text, and is left out. No template or no cousin.toml: an empty set,
    so nothing is filtered (and the doctrine may then reach the prompt;
    the contract's precedence sentence covers that case)."""
    try:
        text = template_sync._render(template_sync._template_text(root),
                                     template_sync._values(home))
    except Exception:  # noqa: BLE001 - a missing template filters nothing
        return set()
    return {_norm(p) for p in _paragraphs(text) if "{{" not in p}


def _claude_identity(text, template):
    """The operator-authored parts of a CLAUDE.md (R3)."""
    if not text.strip():
        return ""
    kept = []
    first = text.splitlines()[0].strip()
    if _TITLE.match(first):
        kept.append(first)
    head, marker, tail = text.partition(template_sync.MARKER)
    _pre, sections = template_sync._sections(head)
    for title, body in sections:
        if title not in _KEEP_HEADINGS:
            continue
        paras = [p for p in _paragraphs(body) if _norm(p) not in template]
        if paras:
            kept.append("%s\n\n%s" % (title, "\n\n".join(paras)))
    if marker:
        paras = [p for p in _paragraphs(tail) if _norm(p) not in template]
        if paras:
            kept.append("\n\n".join(paras))
    return "\n\n".join(kept)


def authored_identity(home, *, root):
    """(text, degraded). The CLAUDE.md authored parts, then the committed
    self-portrait; the fixed IDENTITY_ABSENT when neither has any."""
    home = Path(home)
    portrait = _read(self_portrait.committed_path(home)).strip()
    claude = _claude_identity(_read(home / "CLAUDE.md"), _template_paragraphs(root, home))
    title_only = bool(claude) and _TITLE.fullmatch(claude) is not None
    if not portrait and (not claude or title_only):
        return IDENTITY_ABSENT, True
    return "\n\n".join(p for p in (claude, portrait) if p), False


def compose_system_prompt(home, *, root, registry, version=None):
    """law + contract + identity + operator rules. Never truncated."""
    if version is None:
        from cousin_lib.version import version as _v
        version = _v()
    law = boot.law_text(root).strip()
    identity, _degraded = authored_identity(home, root=root)
    rules, _index = boot.shared_parts(root)
    sections = []
    if law:
        sections.append("# Framework law\n\n" + law)
    sections.append(contract.render(registry, version).strip())
    sections.append(identity.strip())
    if rules:
        sections.append("# Operator rules every cousin follows\n\n" + "\n\n".join(rules))
    return "\n\n".join(sections) + "\n"


def system_prompt_option(home, *, root, registry, version=None):
    """The SDK's SystemPromptPreset for this cousin."""
    return {"type": "preset", "preset": "claude_code",
            "append": compose_system_prompt(home, root=root, registry=registry, version=version),
            "exclude_dynamic_sections": True, "snapshot": True}
