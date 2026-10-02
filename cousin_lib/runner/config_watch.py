"""Config that changes under a live session: say so, and tighten now.

A runner reads its law, policy.toml, cousin.toml and .mcp.json when it
starts, and the system prompt is frozen for the generation (the cache).
An edit made while the session runs used to be invisible to it until a
restart, and a cousin went on quoting rules that were no longer true.
So the runner keeps the text of each file as the session started, and at
each submitted prompt (hooks.on_prompt) compares: anything that changed
reaches the model as one runner note in that prompt's context.

What applies when:
- config/law.md: the system prompt keeps the old text until the next
  generation; the note carries the diff, and the new text wins.
- policy.toml: what the edit ADDS applies at once (Policy.tightened_by);
  what it removes waits for the next start, so a model cannot loosen its
  own rules by editing the file. A file that no longer parses keeps the
  session on the policy it has, and the note says the next start will
  refuse it.
- cousin.toml, .mcp.json: read at start only; the note says a restart
  applies them."""
import difflib
from pathlib import Path

DIFF_LINES = 60
DIFF_CHARS = 3000
NOTE_HEAD = "[runner] configuration changed since this session started:"


def files(home, root):
    """name -> path of what is watched."""
    home, root = Path(home), Path(root)
    return {"config/law.md": root / "config" / "law.md",
            "policy.toml": home / "policy.toml",
            "cousin.toml": home / "cousin.toml",
            ".mcp.json": home / ".mcp.json"}


def _read(path):
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError):
        return ""


class ConfigWatch:
    """The watched files' text as the session started, and what changed
    since the last `check`."""

    def __init__(self, home, root):
        self.paths = files(home, root)
        self.seen = {name: _read(path) for name, path in self.paths.items()}

    def check(self):
        """[(name, old, new)] for each file whose text changed since the
        last check (None: absent); the new text becomes the baseline."""
        out = []
        for name, path in self.paths.items():
            now = _read(path)
            if now != self.seen[name]:
                out.append((name, self.seen[name], now))
                self.seen[name] = now
        return out


def _diff(old, new, name):
    lines = list(difflib.unified_diff((old or "").splitlines(), (new or "").splitlines(),
                                      "before", "now", lineterm="", n=1))
    text = "\n".join(lines[:DIFF_LINES])
    if len(lines) > DIFF_LINES or len(text) > DIFF_CHARS:
        text = text[:DIFF_CHARS] + "\n... (diff cut; read %s for the rest)" % name
    return text


def note(changes, live_policy, parse_policy):
    """(note text or "", new live policy). `live_policy` is the Policy the
    session enforces now; `parse_policy(text)` builds one (raising on a
    bad file)."""
    parts, policy = [], live_policy
    for name, old, new in changes:
        if name == "config/law.md":
            if new is None:
                parts.append("- config/law.md was removed. Your system prompt keeps the law"
                             " it started with; the next generation boots without one.")
            else:
                parts.append("- config/law.md changed. Your system prompt still holds the"
                             " old text until your next generation: where they differ, the"
                             " new text below wins.\n```diff\n%s\n```"
                             % _diff(old, new, name))
        elif name == "policy.toml":
            try:
                incoming = parse_policy(new or "")
            except Exception as err:  # noqa: BLE001 - any refusal is the same answer
                parts.append("- policy.toml no longer loads (%s). This session keeps enforcing"
                             " the policy it has; the next runner start will refuse the file"
                             " until it is fixed." % err)
                continue
            policy, added, removed, skipped = policy.tightened_by(incoming)
            line = "- policy.toml changed."
            if added:
                line += " In force now: %s." % "; ".join(added)
            if removed:
                line += (" Removed, but still enforced until the next runner start"
                         " (a session never loosens its own policy): %s." % "; ".join(removed))
            if skipped:
                line += (" Not applied, because it would deny the handoff every generation"
                         " ends through: %s." % "; ".join(skipped))
            if not (added or removed or skipped):
                line += " Nothing it enforces changed."
            parts.append(line)
        else:
            parts.append("- %s changed. It is read when the runner starts: a restart applies"
                         " it, and until then this session runs on the old file." % name)
    if not parts:
        return "", policy
    return NOTE_HEAD + "\n" + "\n".join(parts), policy
