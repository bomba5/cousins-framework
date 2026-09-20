"""What the harness recorded about a cousin's own MCP server.

The harness keeps one log per session per MCP server, JSON lines
carrying a timestamp, the session id and either a debug line or an
error, including the failing server's stderr verbatim. Nothing
surfaced it to the cousin: on 2026-09-20 four cousins booted with no
MCP tools at all, saw only `CONNECTION_CLOSED` in the boot packet, and
fell back to the CLIs. The reason (`registry: meeting: no commands`,
377 ms in) sat in a cache directory for sixteen hours.

A degraded surface that still works is the easiest failure to ignore,
so the boot packet now says when the last recorded connection failed
and why, and `cousin-mcp --last-connection` answers the same question
at any time.
"""
import json

from cousin_lib.config import (FrameworkConfig, expand_harness_path,
                               _read_harness_toml)

SERVER_NAME = "cousin"
DEFAULT_LOGS_DIR = "~/.cache/claude-cli-nodejs/{home_encoded}/mcp-logs-{server}"


def logs_dir(home, root=None, server=SERVER_NAME):
    """Where the harness keeps this cousin's MCP logs for `server`:
    config/harness.toml `mcp_logs_dir`, else the Claude Code default.
    A template like transcripts_dir, plus {server}."""
    if root is None:
        root = FrameworkConfig.root_from_home(home)
    data = _read_harness_toml(root) or {}
    template = data.get("mcp_logs_dir") or DEFAULT_LOGS_DIR
    return expand_harness_path(template.replace("{server}", server), home)


def _lines(path):
    for raw in path.read_text(errors="replace").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            row = json.loads(raw)
        except ValueError:
            continue        # a truncated or interleaved write, not a finding
        if isinstance(row, dict):
            yield row


def _outcome(rows):
    """The last connection attempt in one file, as a dict, or None."""
    start = None
    for i, row in enumerate(rows):
        if "Starting connection" in str(row.get("debug", "")):
            start = i
    if start is None:
        return None
    ok, detail, stderr = None, "", []
    for row in rows[start:]:
        text = str(row.get("debug") or row.get("error") or "")
        if text.startswith("Server stderr:"):
            stderr.append(text[len("Server stderr:"):].strip())
        elif "Successfully connected" in text:
            ok, detail = True, text
        elif "Connection failed" in text and ok is None:
            ok, detail = False, text
    return {"ok": bool(ok), "detail": detail,
            "stderr": "\n".join(s for s in stderr if s),
            "when": rows[start].get("timestamp", ""),
            "session_id": rows[start].get("sessionId", "")}


def last_connection(home, root=None, *, session_id=None, server=SERVER_NAME):
    """The most recent recorded connection of this cousin's MCP server,
    or None when nothing was recorded (no log directory, no file, or no
    file for `session_id`). With session_id, only that session counts,
    which is what the boot packet wants: another session's failure is
    not this one's. Never raises on a missing or damaged log; a
    diagnostic that fails loudly on its own absence is worse than none.
    """
    try:
        folder = logs_dir(home, root, server)
    except Exception:
        return None
    if not folder.is_dir():
        return None
    files = sorted((p for p in folder.glob("*.jsonl") if p.is_file()),
                   key=lambda p: p.name, reverse=True)
    for path in files:
        try:
            rows = list(_lines(path))
        except OSError:
            continue
        if session_id is not None and not any(
                r.get("sessionId") == session_id for r in rows):
            continue
        out = _outcome(rows)
        if out is None:
            continue
        out["path"] = str(path)
        return out
    return None
