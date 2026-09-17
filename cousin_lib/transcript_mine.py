"""Flip-time transcript mining.

Only what a cousin deliberately wrote used to survive a flip; the lived
session - its conclusions, its dead ends - evaporated with the harness
transcript. At flip, this module reads the dying session's transcript
(one JSON object per line; assistant turns carry a list of content
blocks, the text ones being the cousin's own words) and writes the
sentences that read like conclusions or dead ends as raw candidates,
where the existing raw -> distill pipeline turns them into durable
memory or lets them fold away.

Deliberately mechanical: no model call, a keyword test per sentence.
Operator lines already persist in the chat store; tool activity in the
trace ledger; the cousin's own conclusions were the gap. Sidechain
turns (sub-agents) are excluded: their results surface in the main
thread anyway.

Where the transcripts live is configuration (config/harness.toml,
Task 1's seam); absent means the capability is off. Best-effort
throughout: mine() returns 0 for an absent config, an absent file, or
a session with nothing worth keeping, and never raises for malformed
lines. The flip records it as a stage but cannot be failed by it.
"""
import json
import re
from pathlib import Path

from cousin_lib import memory
from cousin_lib.config import expand_harness_path, harness_config

MAX_ENTRIES = 24
MIN_SENTENCE_CHARS = 24
MAX_SENTENCE_CHARS = 400
SOURCE = "flip-transcript"
TRUTH_LEVEL = "L3_COUSIN_CONCLUSION"

# A sentence is a conclusion when it carries a reason or a resolution,
# a dead end when it names a failure. Word-bounded so "also" is not
# "so" and "unfixed" is not "fixed".
_CONCLUSION = re.compile(
    r"\b(because|so|therefore|the cause|fixed|decided)\b", re.IGNORECASE)
_DEAD_END = re.compile(r"\b(does not|failed|wrong)\b", re.IGNORECASE)
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+|\n+")


def _assistant_texts(path):
    """Yield the main-thread assistant text turns of one transcript,
    tolerating any line that is not what the harness writes."""
    with open(path) as fh:
        for line in fh:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict) or record.get("isSidechain"):
                continue
            if record.get("type") != "assistant":
                continue
            content = (record.get("message") or {}).get("content")
            if not isinstance(content, list):
                continue
            texts = [block.get("text") for block in content
                     if isinstance(block, dict)
                     and block.get("type") == "text" and block.get("text")]
            if texts:
                yield "\n".join(texts)


def _keep(sentence):
    return bool(_CONCLUSION.search(sentence) or _DEAD_END.search(sentence))


def candidates(texts, *, max_entries=MAX_ENTRIES):
    """The kept sentences of an iterable of assistant texts, in order,
    deduplicated, capped."""
    kept = []
    seen = set()
    for text in texts:
        for raw in _SENTENCE_BREAK.split(text):
            sentence = " ".join(raw.split())
            if len(sentence) < MIN_SENTENCE_CHARS or not _keep(sentence):
                continue
            sentence = sentence[:MAX_SENTENCE_CHARS]
            if sentence in seen:
                continue
            seen.add(sentence)
            kept.append(sentence)
            if len(kept) >= max_entries:
                return kept
    return kept


def transcript_path(home, root, session_id):
    """Where the harness keeps this session's transcript, or None when
    the seam is unset."""
    cfg = harness_config(root)
    if not cfg or not cfg.get("transcripts_dir") or not session_id:
        return None
    base = expand_harness_path(cfg["transcripts_dir"], home)
    return base / ("%s.jsonl" % session_id)


def mine(home, root, session_id, *, max_entries=MAX_ENTRIES):
    """Mine one session's transcript into raw candidates. Returns the
    number written; 0 when the seam is unset, the file is absent, or
    nothing qualified."""
    home = Path(home)
    path = transcript_path(home, root, session_id)
    if path is None or not path.is_file():
        return 0
    try:
        kept = candidates(_assistant_texts(path), max_entries=max_entries)
    except OSError:
        return 0
    topic = "episode:%s" % session_id[:8]
    for sentence in kept:
        memory._append_raw(home, {
            "topic": topic,
            "content": sentence,
            "truth_level": TRUTH_LEVEL,
            "source": SOURCE,
        })
    return len(kept)
