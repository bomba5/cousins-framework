"""Content blocks as stream events, one mapping for every runner that
reads Claude messages (phase 11 Task 2, R7).

The SDK runner receives message objects and the tmux kind reads the
interactive CLI's transcript as dicts; both record the same event kinds
with the same payloads, so a turn reads the same in the console and in
cousin-watch whichever kind ran it. A parity test runs the SDK runner's
own path on the same content (tests/runner/test_tmux_blocks.py).

One difference is declared, not hidden: the interactive CLI keeps a
thinking block's signature but not its text (phase 11 findings I14b), so
such a block is recorded with length 0 and `"redacted": true`."""

THINKING_CHARS = 8000       # a thinking block's text in the stream, bounded
TEXT_CHARS = 2000           # a user text or a tool result in the stream, bounded


def thinking_payload(text):
    payload = {"length": len(text), "text": text[:THINKING_CHARS]}
    if len(text) > THINKING_CHARS:
        payload["truncated"] = True
    return payload


def tool_result_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(part.get("text", "") for part in content
                         if isinstance(part, dict) and part.get("type") == "text")
    return ""


def assistant_events(content):
    """[(kind, payload)] for an assistant message's content blocks (dicts).
    A message with nothing recordable is one empty `text`, as on the SDK lane."""
    out = []
    for block in content or ():
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            out.append(("text", {"text": block.get("text", "")}))
        elif kind == "tool_use":
            out.append(("tool", {"id": block.get("id"), "name": block.get("name"),
                                 "input": block.get("input")}))
        elif kind == "thinking":
            text = block.get("thinking") or ""
            payload = thinking_payload(text)
            if not text and block.get("signature"):
                payload["redacted"] = True
            out.append(("thinking", payload))
    return out or [("text", {"text": ""})]


def user_events(content, *, echo_of=None):
    """[(kind, payload)] for a user message's content: a string is one
    `user` event; a list gives one `tool_result` per tool result and one
    `user` for its text blocks (or for nothing recorded), as on the SDK lane."""
    if isinstance(content, str):
        return [("user", {"text": content[:TEXT_CHARS], "echo_of": echo_of})]
    out, texts = [], []
    for block in content or ():
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_result":
            out.append(("tool_result", {"tool_use_id": block.get("tool_use_id"),
                                        "is_error": bool(block.get("is_error")),
                                        "text": tool_result_text(block.get("content"))[:TEXT_CHARS]}))
        elif block.get("type") == "text":
            texts.append(block.get("text", ""))
    if texts or not out:
        out.append(("user", {"text": "\n".join(texts)[:TEXT_CHARS], "echo_of": echo_of}))
    return out
