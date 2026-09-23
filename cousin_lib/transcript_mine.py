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
A kept sentence that hedges ("probably", "might", "I suspect", "I
think", "likely", "maybe", "not sure", "seems") is a hypothesis: it is
written at L4_COUSIN_HYPOTHESIS under episode:<id8>:hypothesis, the
rest at L3_COUSIN_CONCLUSION under episode:<id8>. Hedging only
reclassifies; it never makes a sentence worth keeping by itself.
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
HYPOTHESIS_LEVEL = "L4_COUSIN_HYPOTHESIS"

# A sentence is a conclusion when it carries a reason or a resolution,
# a dead end when it names a failure. Word-bounded so "also" is not
# "so" and "unfixed" is not "fixed".
_CONCLUSION = re.compile(
    r"\b(because|so|therefore|the cause|fixed|decided)\b", re.IGNORECASE)
_DEAD_END = re.compile(r"\b(does not|failed|wrong)\b", re.IGNORECASE)
# A kept sentence that hedges is a hypothesis, not a conclusion: it is
# written at L4 under its own topic, so the distilled view shows the
# session's newest conclusion and its newest open guess side by side.
# Word-bounded: "likelihood" is not "likely", "mighty" is not "might".
_HEDGE = re.compile(
    r"\b(probably|might|i suspect|i think|likely|maybe|not sure|seems)\b",
    re.IGNORECASE)
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+|\n+")


def texts_from_entries(entries):
    """The main-thread assistant text turns of parsed transcript entries
    (the harness's file format, and our session store's entries: the
    same JSON), tolerating anything that is not an assistant turn."""
    for record in entries:
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


def _assistant_texts(path):
    """texts_from_entries over one transcript file, skipping bad lines."""
    def lines():
        with open(path) as fh:
            for line in fh:
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
    yield from texts_from_entries(lines())


def normalize(sentence):
    """The dedupe key: case and whitespace do not make a sentence new."""
    return " ".join(str(sentence).lower().split())


def _keep(sentence):
    return bool(_CONCLUSION.search(sentence) or _DEAD_END.search(sentence))


def is_hedged(sentence):
    return bool(_HEDGE.search(sentence))


def level_for(sentence):
    """L4 for a hedged sentence, L3 for everything else kept."""
    return HYPOTHESIS_LEVEL if is_hedged(sentence) else TRUTH_LEVEL


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
        level = level_for(sentence)
        memory._append_raw(home, {
            "topic": (topic if level == TRUTH_LEVEL
                      else topic + ":hypothesis"),
            "content": sentence,
            "truth_level": level,
            "source": SOURCE,
        })
    return len(kept)
