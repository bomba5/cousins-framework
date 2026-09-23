"""The in-process tool transport (spec, "Features as in-process tools").

Library functions are the single implementation; this module is the
third transport over them, beside the CLI and the stdio MCP server.
Schemas come from the one registry through mcp_server.build_schema, so a
tool cannot exist on one transport and not the other; HANDLERS maps each
registry command to the library function that does the work, and a
registry command with no handler stops the runner at start.

Every handler is a plain synchronous function `(ctx, args) -> str` that
returns the text the CLI would have printed, and never starts a
subprocess: a peer send is an HTTP POST, everything else is a library
call in this process.

`reply` is the only writer of chat.db on this lane. It routes by the
live Turn: one thread, implicit; two, the destination must be named.
"""
import asyncio
import json
import pathlib
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from cousin_lib import mcp_server
from cousin_lib.delivery import parse_thread, thread_for_chat
from cousin_lib.runner.base import RunnerError


@dataclass
class ToolContext:
    home: Path
    slug: str
    name: str
    root: Path
    turn: object
    policy: object            # has .outbound_filter (Task 7's Policy)
    stream: object = None     # EventStream or None
    registry: object = None   # set by build_tool_server


def _str(a, key, default=""):
    value = a.get(key)
    return default if value is None else str(value)


def _required(a, key, command):
    value = a.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValueError("%s: missing required property %r" % (command, key))
    return value


def _int(a, key, command):
    value = _required(a, key, command)
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError("%s: %s must be an integer, not %r" % (command, key, value))


# ------------------------------------------------------------- memory

def _m_search(ctx, a):
    from cousin_lib import memory, memory_search
    query = str(_required(a, "query", "search"))
    top = int(a.get("top") or 5)
    if a.get("json"):
        hits, _notice = memory_search.search(query, top=top, home=Path(ctx.home),
                                             collection=a.get("collection"))
        return json.dumps(hits)
    return memory.search_text(ctx.home, query, top=top, collection=a.get("collection"))


def _m_decide(ctx, a):
    # The registry sends decide through --stdin, whose parser strips each
    # chunk; the in-process path strips the same way (ruling P7).
    from cousin_lib import memory
    return memory.decide(ctx.home, _str(a, "topic").strip(), _str(a, "decision").strip(),
                         _str(a, "reasoning").strip(), level=a.get("level"),
                         cite=a.get("cite"))


def _m_remember(ctx, a):
    from cousin_lib import memory
    return memory.remember(ctx.home, a.get("topic"), a.get("fact"),
                           level=a.get("level"), cite=a.get("cite"))


def _m_obsolete(ctx, a):
    # The CLI marks, prints one line, then rebuilds the distilled views
    # and prints the distill report; the report is formatted here instead
    # of printed so nothing reaches this process's stdout.
    from cousin_lib import distill, memory
    topic, why = _str(a, "topic"), _str(a, "why")
    memory.mark_obsolete(ctx.home, topic, why, force=bool(a.get("force")), by=ctx.slug)
    lines = ["Marked obsolete [%s]: %s" % (topic.strip(), " ".join(why.split()))]
    report = distill.distill(Path(ctx.home))
    lines.append("distilled %d topics from %d raw entries:"
                 % (report["topics"], report["entries"]))
    if report.get("obsolete"):
        lines.append("  (%d obsolete topic(s) left out; history kept in raw)"
                     % report["obsolete"])
    for fname, count in report["files"].items():
        lines.append("  %s: %d" % (fname, count))
    return "\n".join(lines)


def _m_recall(ctx, a):
    from cousin_lib import memory
    keyword = _str(a, "keyword")
    last = a.get("last")
    last = 20 if last is None else int(last)
    return memory.format_recall(memory.recall_entries(ctx.home, keyword, last),
                                keyword.lower())


def _m_activity(ctx, a):
    from cousin_lib import memory
    return memory.note_activity(ctx.home, _str(a, "text") or "Idle")


# ---------------------------------------------------------------- job

def _j_start(ctx, a):
    # Register-only mode of `cousin-job start`: the tool takes no command
    # line, so there is no process to track, only a row and its log.
    from cousin_lib import jobs
    kind = _str(a, "kind") or "other"
    if kind == "shell":
        raise ValueError("start takes no command, so a shell row would never close;"
                         " use a Bash call with run_in_background")
    title = str(_required(a, "title", "start"))
    desc = _str(a, "desc")
    job_id = jobs.register_job(kind=kind, title=title, description=desc, spawned_by=ctx.slug)
    log_path = None
    try:
        log_path = str(jobs._default_log_path(job_id))
        jobs._write_log_header(log_path, kind, title, desc)
        jobs.set_log_path(job_id, log_path)
    except OSError:
        log_path = None
    if a.get("json"):
        return json.dumps({"job_id": job_id, "log_path": log_path})
    return str(job_id)


def _j_close(ctx, a, status):
    from cousin_lib import jobs
    job_id = _int(a, "id", status)
    job = jobs.get_job(job_id)
    if not job:
        raise ValueError("job #%d not found" % job_id)
    summary = _str(a, "summary")
    jobs.finish_job(job_id, status=status, summary=summary,
                    exit_code=a.get("exit") if status == "failed" else None)
    lines = []
    reaped = jobs.reap_group(job)
    if reaped:
        lines.append("job #%d: stopped %d process(es) still running in its group: %s"
                     % (job_id, len(reaped), " ".join(map(str, reaped))))
    if job.get("log_path") and jobs.is_minted_log(job["log_path"]):
        try:
            with open(job["log_path"], "a", encoding="utf-8") as fh:
                fh.write("\n## %s %s\n%s\n" % (
                    status, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), summary))
        except OSError:
            pass
    lines.append("job #%d %s" % (job_id, status))
    return "\n".join(lines)


def _j_done(ctx, a):
    return _j_close(ctx, a, "done")


def _j_fail(ctx, a):
    return _j_close(ctx, a, "failed")


def _j_list(ctx, a):
    from cousin_lib import jobs
    rows = jobs.list_jobs(status=a.get("status") or None,
                          spawned_by=ctx.slug if a.get("mine") else None,
                          active_only=bool(a.get("active")))
    if a.get("json"):
        return json.dumps(rows, indent=2, sort_keys=True, default=str)
    if not rows:
        return "(no jobs)"
    lines = ["%4s  %-10s %-9s %-10s %s" % ("ID", "STATUS", "KIND", "OWNER", "TITLE")]
    for j in rows:
        leak = ""
        if j["status"] != "running" and j.get("pgid") and jobs.live_members(j):
            leak = "  [LEAK: processes still running; cousin-job cancel %d]" % j["id"]
        lines.append("%4d  %-10s %-9s %-10s %s%s"
                     % (j["id"], j["status"], j["kind"], j["spawned_by"] or "-",
                        (j["title"] or "")[:60], leak))
    return "\n".join(lines)


def _j_show(ctx, a):
    from cousin_lib import jobs
    job_id = _int(a, "id", _str(a, "command") or "show")
    job = jobs.get_job(job_id)
    if not job:
        raise ValueError("job #%d not found" % job_id)
    job["live_processes"] = jobs.live_members(job)
    if a.get("json") or a.get("command") in ("status", "result"):
        return json.dumps(job, indent=2, sort_keys=True, default=str)
    lines = ["  %-14s: %s" % (k, v) for k, v in job.items()]
    if job["status"] != "running" and job["live_processes"]:
        lines.append("  LEAK: the job is %s but its processes still run;"
                     " cousin-job cancel %d stops them" % (job["status"], job["id"]))
    return "\n".join(lines)


# ----------------------------------------------------------- schedule

def _s_add(ctx, a):
    from cousin_lib import schedule
    row = schedule.add(ctx.slug, str(_required(a, "when", "add")),
                       _str(a, "prompt"))
    return "scheduled #%d for %s at %s" % (
        row["id"], row["cousin"],
        datetime.fromtimestamp(row["target_ts"]).isoformat(timespec="seconds"))


def _s_list(ctx, a):
    from cousin_lib import schedule
    everything = bool(a.get("all"))
    entries = schedule.list_entries(ctx.slug, include_fired=everything,
                                    limit=50 if everything else None)
    if not entries:
        return "no jobs for %s%s" % (ctx.slug, " (incl history)" if everything else " pending")
    return schedule.format_entries(entries)


def _s_cancel(ctx, a):
    from cousin_lib import schedule
    job_id = _int(a, "id", "cancel")
    if schedule.cancel(job_id, slug=ctx.slug):
        return "cancelled #%d" % job_id
    raise ValueError("no pending job #%d for %s" % (job_id, ctx.slug))


# ------------------------------------------------------------ meeting

def _meeting_text(m):
    """cousin-meeting's _print_meeting, as text."""
    turn = m["turn_slug"] or "the user's floor"
    lines = ["#%d [%s] %s - round %d, %s; participants %s"
             % (m["id"], m["state"], m["topic"], m["round"], turn,
                ", ".join(m["participants"]))]
    for e in m.get("transcript", []):
        lines.append("  %s (r%d) %s: %s" % (e["created_at"][11:16], e["round"],
                                            e["speaker"], e["text"]))
    return "\n".join(lines)


def _mt_say(ctx, a):
    from cousin_lib import meetings
    return _meeting_text(meetings.say(_int(a, "id", "say"), ctx.slug,
                                      _str(a, "text"), root=ctx.root))


def _mt_pass(ctx, a):
    from cousin_lib import meetings
    return _meeting_text(meetings.pass_turn(_int(a, "id", "pass"), ctx.slug, root=ctx.root))


def _mt_minutes(ctx, a):
    from cousin_lib import meetings
    return _meeting_text(meetings.minutes(_int(a, "id", "minutes"), ctx.slug,
                                          _str(a, "text"), root=ctx.root))


def _mt_show(ctx, a):
    from cousin_lib import meetings
    return _meeting_text(meetings.show(_int(a, "id", "show"), root=ctx.root))


# ------------------------------------------------------------ tracker

def _item_text(item, as_json):
    """cousin-tracker's _print_item, as text."""
    if as_json:
        return json.dumps({"item": item}, indent=2, sort_keys=True)
    return "#%d %s [%s]%s%s" % (
        item["id"], item["title"], item["state"],
        " domain=%s" % item["domain"] if item["domain"] else "",
        " owner=%s" % item["owner"] if item["owner"] else "")


def _tags(value):
    if value is None:
        return None
    return [value] if isinstance(value, str) else list(value)


def _t_add(ctx, a):
    from cousin_lib import tracker
    item = tracker.add(_str(a, "title"), domain=_str(a, "domain"),
                       state=_str(a, "state") or "open", tags=_tags(a.get("tag")) or (),
                       owner=ctx.slug, notes=_str(a, "notes"), root=ctx.root)
    return _item_text(item, bool(a.get("json")))


def _t_update(ctx, a):
    from cousin_lib import tracker
    item = tracker.update(_int(a, "id", "update"), title=a.get("title"),
                          domain=a.get("domain"), state=a.get("state"),
                          tags=_tags(a.get("tag")), notes=a.get("notes"), root=ctx.root)
    return _item_text(item, bool(a.get("json")))


def _t_state(ctx, a):
    from cousin_lib import tracker
    item = tracker.set_state(_int(a, "id", "state"), _str(a, "state"), root=ctx.root)
    if a.get("json"):
        return _item_text(item, True)
    return "#%d -> %s" % (item["id"], item["state"])


def _t_list(ctx, a):
    from cousin_lib import tracker
    tag = a.get("tag")
    if isinstance(tag, list):
        tag = tag[0] if tag else None
    items = tracker.list_items(domain=a.get("domain") or None, state=a.get("state") or None,
                               tag=tag or None, owner=a.get("owner") or None, root=ctx.root)
    if a.get("json"):
        return json.dumps({"items": items}, indent=2, sort_keys=True)
    if not items:
        return "(no tracker items)"
    lines = ["%4s  %-8s %-10s %-10s %s" % ("ID", "STATE", "OWNER", "DOMAIN", "TITLE")]
    for it in items:
        lines.append("%4d  %-8s %-10s %-10s %s"
                     % (it["id"], it["state"], (it["owner"] or "-")[:10],
                        (it["domain"] or "-")[:10], it["title"][:60]))
    return "\n".join(lines)


def _t_show(ctx, a):
    from cousin_lib import tracker
    item_id = _int(a, "id", "show")
    item = tracker.show(item_id, root=ctx.root)
    if item is None:
        raise tracker.ItemNotFound(item_id)
    if a.get("json"):
        return _item_text(item, True)
    lines = []
    for key in tracker.FIELDS:
        value = item[key]
        if key == "tags":
            value = ", ".join(value) if value else "-"
        lines.append("  %-10s: %s" % (key, value))
    return "\n".join(lines)


def _t_delete(ctx, a):
    from cousin_lib import tracker
    item_id = _int(a, "id", "delete")
    if not tracker.delete(item_id, root=ctx.root):
        raise tracker.ItemNotFound(item_id)
    if a.get("json"):
        return json.dumps({"ok": True, "deleted": item_id})
    return "deleted #%d" % item_id


HANDLERS = {
    "memory": {"search": _m_search, "decide": _m_decide, "remember": _m_remember,
               "obsolete": _m_obsolete, "recall": _m_recall, "activity": _m_activity},
    "job": {"start": _j_start, "done": _j_done, "fail": _j_fail, "list": _j_list,
            "show": _j_show},
    "schedule": {"add": _s_add, "list": _s_list, "cancel": _s_cancel},
    "meeting": {"say": _mt_say, "pass": _mt_pass, "minutes": _mt_minutes, "show": _mt_show},
    "tracker": {"add": _t_add, "update": _t_update, "state": _t_state, "list": _t_list,
                "show": _t_show, "delete": _t_delete},
}


# -------------------------------------------------------------- reply

def _config(ctx):
    from cousin_lib.config import CousinConfig
    return CousinConfig.load(ctx.home)


def _same_thread(a, b):
    """Thread ids name one thread when their kinds match and their keys
    normalise to the same chat user ('operator:Priya' is 'operator:priya')."""
    from cousin_lib.server.storage import normalize_chat_user
    ka, _, va = str(a).partition(":")
    kb, _, vb = str(b).partition(":")
    return ka == kb and normalize_chat_user(va) == normalize_chat_user(vb)


def _pick_thread(ctx, thread):
    """The thread a reply goes to. A named operator: or person: thread is
    always accepted, live or not: a schedule, loop or peer turn must be
    able to reach its operator, as `cousin-reply --user` can (ruling
    P12); the live thread's spelling is used when one matches. The live
    turn decides only the implicit default: one live thread, that one;
    two, refused with the list; none, refused."""
    turn = ctx.turn
    if turn is None:
        active, live = False, ()
    elif hasattr(turn, "snapshot"):
        active, live = turn.snapshot()
    else:
        active, live = bool(turn.active), tuple(turn.threads)
    if not active:
        live = ()
    if thread:
        kind, key = parse_thread(thread)      # a malformed id is refused here
        if kind == "peer":
            raise ValueError("%s is a peer thread, not the chat surface; answer a"
                             " peer with send (to=%s)" % (thread, key))
        if kind not in ("operator", "person"):
            raise ValueError("%s is not a chat-surface thread; name operator:<name>"
                             " or person:<name>" % thread)
        return next((t for t in live if _same_thread(t, thread)), thread)
    if not live:
        raise ValueError("no turn is live; name the thread (thread=operator:<name>"
                         " or person:<name>)")
    if len(live) > 1:
        raise ValueError("%d threads are live in this turn; name one with thread: %s"
                         % (len(live), ", ".join(live)))
    return live[0]


def _check_attachment(kind, value):
    from cousin_lib.reply import ATTACHMENT_KINDS
    path = pathlib.Path(value)
    exts = ATTACHMENT_KINDS[kind][0]
    if path.suffix.lower() not in exts:
        raise ValueError("%s must be one of %s, got %s" % (kind, ", ".join(exts), path.name))
    if not path.is_file():
        raise ValueError("%s %s: no such file" % (kind, path))
    return path


def reply(ctx, text, *, thread=None, reply_to=None, image=None, video=None):
    """Write one reply row into this cousin's chat.db, on the thread the
    live turn names. The only chat.db writer on the SDK lane."""
    if image and video:
        raise ValueError("one attachment per reply: image or video")
    attachment = None
    if image or video:
        kind = "image" if image else "video"
        attachment = (kind, _check_attachment(kind, image or video))
    text = ("" if text is None else str(text)).rstrip("\n")
    if attachment is not None and not text.strip():
        text = "(%s: %s)" % (attachment[0], attachment[1].name)
    if not text.strip():
        raise ValueError("a reply needs text or an attachment")
    target = _pick_thread(ctx, thread)
    kind, key = parse_thread(target)
    if kind == "peer":
        raise ValueError("%s is a peer thread, not the chat surface; answer a peer"
                         " with send (to=%s)" % (target, key))
    if kind not in ("operator", "person"):
        raise ValueError("%s is not a chat-surface thread; name the destination with"
                         " thread=operator:<name> or person:<name>" % target)
    if getattr(ctx.policy, "outbound_filter", True):
        from cousin_lib.outbound_filter import OutboundPolicy
        OutboundPolicy.load(ctx.root).check(text, from_slug=ctx.slug, dest_slug="",
                                            surface="chat", context="reply")
    attachment_kind = attachment_path = None
    if attachment is not None:
        from cousin_lib.reply import stage_attachment
        attachment_kind = attachment[0]
        attachment_path = str(stage_attachment(ctx.home, attachment[0], attachment[1]))
    from cousin_lib.server.storage import ChatStore, normalize_chat_user
    store = None
    try:
        store = ChatStore(Path(ctx.home) / "data" / "chat.db")
        row = store.add_message(
            chat_user=normalize_chat_user(key), user=ctx.name, message=text,
            msg_type=ctx.slug,
            reply_to=json.dumps({"id": reply_to}) if reply_to is not None else None,
            reply_to_user=key, attachment_kind=attachment_kind,
            attachment_path=attachment_path)
    except BaseException:
        if attachment_path:
            Path(attachment_path).unlink(missing_ok=True)
        raise
    finally:
        if store is not None:
            store.close()
    return "replied to %s (#%d)" % (key, row["id"])


# --------------------------------------------------------------- send

def _operators(ctx, cfg):
    """The registry's configured operators plus the cousin's own
    [operator] name, in that order, without repeats."""
    names = []
    tool = _registry_tool(ctx, "send")
    for n in list((tool or {}).get("operators") or []) + [cfg.operator_name]:
        if n and n not in names:
            names.append(n)
    return names


def _peers(fw, slug):
    """What `cousin-chat list` prints, minus the (self) line: the
    peer-visible registry and the external peers."""
    from cousin_lib import chat
    out = [c.slug for c in chat.list_peers(fw, slug) if c.slug != slug]
    try:
        out += [p.slug for p in chat.list_external_peers(fw, slug) if p.slug not in out]
    except Exception:  # noqa: BLE001 - a bad external-peers file costs only those peers
        pass
    return out


def _send(ctx, a):
    from cousin_lib import chat
    from cousin_lib.config import FrameworkConfig
    from cousin_lib.server.storage import normalize_chat_user
    to, text = _str(a, "to").strip(), _str(a, "text")
    if not to:
        raise ValueError("send: 'to' is required")
    if not text.strip():
        raise ValueError("send: 'text' is required")
    cfg = _config(ctx)
    operators = _operators(ctx, cfg)
    operator = next((o for o in operators
                     if normalize_chat_user(o) == normalize_chat_user(to)), None)
    if operator is not None:
        return reply(ctx, text, thread=thread_for_chat(cfg, operator),
                     image=a.get("image"), video=a.get("video"))
    fw = FrameworkConfig.from_env()
    peers = _peers(fw, ctx.slug)
    if to not in peers:
        tool = _registry_tool(ctx, "send") or {}
        if tool.get("errors_name_peers", True):
            known = ", ".join(sorted(peers)) or "(none)"
        else:
            known = "%d known (see the peer list command)" % len(peers)
        raise ValueError("unknown destination %r; known peers: %s; known operators: %s"
                         % (to, known, ", ".join(operators) or "(none)"))
    if a.get("image") or a.get("video"):
        raise ValueError("send: image and video go to an operator reply only, not to a peer")
    from cousin_lib.outbound_filter import OutboundPolicy
    # One root for the filter on both routes: reply loads it from ctx.root too.
    policy = OutboundPolicy.load(ctx.root) if getattr(ctx.policy, "outbound_filter", True) else None
    result = chat.send_message(fw, cfg, to, text, policy=policy, display_name=ctx.name)
    return json.dumps({"ok": True, "to": to, "id": (result or {}).get("id")}, sort_keys=True)


# ------------------------------------------------------------ handoff

def handoff(ctx, text):
    """The handoff for the next generation, at <home>/data/handoff-manual.md."""
    from cousin_lib import memory
    text = ("" if text is None else str(text)).strip()
    if not text:
        raise ValueError("handoff needs text")
    path = Path(ctx.home) / "data" / "handoff-manual.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Handoff - %s, written %s\n\n%s\n"
                    % (ctx.name, datetime.now().astimezone().isoformat(timespec="minutes"), text))
    memory.record_event(ctx.home, "framework", "framework:handoff",
                        "handoff written (%d chars)" % len(text), "runner")
    return "handoff written"


RUNNER_TOOLS = [
    {"name": "reply",
     "description": "Answer on the chat surface. Routes by the live thread of this turn; with two"
                    " live threads, name one. Operator and person threads only; a peer is answered"
                    " with send. The only tool that writes the chat surface.",
     "inputSchema": {"type": "object", "properties": {
         "text": {"type": "string"},
         "thread": {"type": "string",
                    "description": "operator:<name> or person:<name>; required when two threads are live"},
         "reply_to": {"type": "integer", "description": "the chat message id you answer"},
         "image": {"type": "string", "description": "path to a PNG, JPEG, GIF or WebP"},
         "video": {"type": "string", "description": "path to an MP4, WebM, MOV or M4V"}},
         "required": ["text"], "additionalProperties": False}},
    {"name": "handoff",
     "description": "Write the handoff for the next generation (data/handoff-manual.md).",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}},
                     "required": ["text"], "additionalProperties": False}},
]


# ---------------------------------------------------------- transport

def _registry_tool(ctx, name):
    registry = getattr(ctx, "registry", None)
    if not registry:
        return None
    tool = registry["tools"].get(name)
    return tool if tool is not None and tool.get("enabled", True) else None


def _commands(tool):
    commands = set(tool["commands"])
    if tool["kind"] == "job":
        commands |= {"status", "result"}
    return commands


def tool_definitions(registry):
    """The registry's enabled tools with their schemas, then reply and handoff."""
    return list(mcp_server.list_tools(registry)) + [dict(t) for t in RUNNER_TOOLS]


def missing_handlers(registry):
    """Every "<tool>.<command>" the registry enables that HANDLERS lacks."""
    out = []
    for name, tool in registry["tools"].items():
        if not tool["enabled"] or tool["kind"] == "send":
            continue                  # the send kind is one handler, _send
        for cmd in sorted(_commands(tool)):
            if cmd not in HANDLERS.get(name, {}):
                out.append("%s.%s" % (name, cmd))
    return out


def _dispatch(ctx, name, args):
    if name == "reply":
        return reply(ctx, args.get("text"), thread=args.get("thread"),
                     reply_to=args.get("reply_to"), image=args.get("image"),
                     video=args.get("video"))
    if name == "handoff":
        return handoff(ctx, args.get("text"))
    reg_tool = _registry_tool(ctx, name)
    if name == "send" or (reg_tool is not None and reg_tool["kind"] == "send"):
        if reg_tool is not None:
            mcp_server.check_enums(reg_tool["properties"], args, name)
        return _send(ctx, args)
    table = HANDLERS.get(name)
    if table is None or (getattr(ctx, "registry", None) and reg_tool is None):
        raise ValueError("unknown tool %r" % name)
    command = args.get("command") or ""
    known = sorted(_commands(reg_tool)) if reg_tool is not None else sorted(table)
    if not command or command not in known:
        raise ValueError("%s: unknown command %r; known: %s" % (name, command, ", ".join(known)))
    if command not in table:
        raise ValueError("%s: command %r has no in-process handler" % (name, command))
    if reg_tool is not None:
        mcp_server.check_enums(reg_tool["properties"], args, command)
    return table[command](ctx, args)


def call(ctx, name, args):
    """One tool call. (text, is_error); never raises."""
    t0 = time.monotonic()
    args = dict(args or {})
    command = args.get("command") or ""
    try:
        text = _dispatch(ctx, name, args)
        text = "" if text is None else str(text)
        is_error = False
    except Exception as err:  # noqa: BLE001 - a bad call is a tool error, never a crash
        text, is_error = "%s: %s" % (type(err).__name__, err), True
    if ctx.stream is not None:
        try:
            ctx.stream.append("tool_call", {"tool": name, "command": command,
                                            "is_error": is_error,
                                            "ms": int((time.monotonic() - t0) * 1000)})
        except Exception:  # noqa: BLE001 - the stream never fails a call
            pass
    return text, is_error


def build_tool_server(ctx, registry=None):
    """The SDK's in-process MCP server config: one tool per definition.
    RunnerError when a registry command has no handler, before the SDK
    is touched. With no registry given: the cousin's own, else the
    install's (default_registry_path); a root that has neither falls
    back to the shipped default (shipped_default_registry), the one
    every cousin is given, and says so with a `policy` event
    (`registry`, `fallback: true`: which tools the model gets is tool
    policy) rather than refuse to start."""
    if registry is None:
        path = mcp_server.default_registry_path(
            {"FRAMEWORK_ROOT": str(ctx.root), "COUSIN_HOME": str(ctx.home)})
        if path is not None:
            registry = mcp_server.load_registry(path)
        else:
            registry = mcp_server.parse_registry(
                mcp_server.shipped_default_registry(ctx.root), "shipped default")
            if ctx.stream is not None:
                ctx.stream.append("policy", {
                    "registry": "shipped default", "fallback": True,
                    "why": "no MCP registry under %s or %s/config" % (ctx.home, ctx.root)})
    missing = missing_handlers(registry)
    if missing:
        raise RunnerError("registry commands with no in-process handler: %s"
                          % ", ".join(missing))
    ctx.registry = registry
    from claude_agent_sdk import create_sdk_mcp_server, tool

    def make(tool_name):
        async def handler(args):
            text, is_error = await asyncio.to_thread(call, ctx, tool_name, args)
            return {"content": [{"type": "text", "text": text}], "is_error": is_error}
        return handler

    sdk_tools = [tool(d["name"], d["description"], d["inputSchema"])(make(d["name"]))
                 for d in tool_definitions(registry)]
    return create_sdk_mcp_server(mcp_server.SERVER_NAME, version="1.0.0", tools=sdk_tools)
