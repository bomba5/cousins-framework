"""Console routes for WP-B, lifecycle (docs/reference/console-api.md,
"Lifecycle"): reincarnate and transplant through cousin_lib.lifecycle,
each a long operation (console/longop.py).

reincarnate is the cousin's own op: a snapshot, the bequest (the tmux
lane asks the cousin for data/handoff.md and waits for it; the runner
lane carries it on the rollover's handoff request), the role rewritten in
CLAUDE.md and cousin.toml, then a flip. transplant touches two cousins:
it runs as the recipient's op while the donor is held
(longop.exclusive), so nothing else, no flip, no stop and no other op,
starts on either until it ends. Every refusal changes nothing, and the
library writes its own audit (data/lifecycle/audit.jsonl) and snapshots
(data/lifecycle/<slug>/<ts>/).

The library's steps come back at its end; the stages in between come
from the two actions it is handed (the bequest prompt and the flip),
each wrapped so the op shows what runs.

Test seams on req.server.state (never set by a request):
`lifecycle.do_flip` (the flip, lifecycle's do_flip) and `lifecycle.send`
(the bequest prompt, lifecycle's send)."""
from __future__ import annotations

import re
import time

from cousin_lib import lifecycle
from cousin_lib.console import longop, router
from cousin_lib.console.app import HttpError

ROLE_MAX = 200
MIN_TIMEOUT_S, MAX_TIMEOUT_S = 10, 600
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
OP_KINDS = ("reincarnate", "transplant")
# a mode that replaces what a cousin is (its memory, its identity) is
# confirmed by typing its phrase; merge only adds, so a second click does
MODES = (
    {"id": "soul-donation", "confirm": "typed", "phrase": "donate {donor} {recipient}",
     "what": "the recipient carries the donor's memory in its own body; the recipient's own"
             " memory leaves its home (it stays in the snapshot)"},
    {"id": "body-swap", "confirm": "typed", "phrase": "swap {donor} {recipient}",
     "what": "the two trade identity files (CLAUDE.md, self-portrait.md) and their name and"
             " role; slug, port, session and memory stay with each slot"},
    {"id": "merge", "confirm": "second",
     "what": "the donor's memory is braided into the recipient's (MEMORY.md and memory/raw);"
             " the donor keeps its own"},
)


def _home(server, slug):
    from cousin_lib.console._common import cousin_home
    return cousin_home(server, slug)


def _do_flip(server):
    return server.state.get("lifecycle.do_flip") or lifecycle._default_do_flip(server.root)


def _send(server):
    return server.state.get("lifecycle.send") or lifecycle.send_prompt


def _role(body):
    role = body.get("new_role")
    if not isinstance(role, str) or not role.strip():
        raise HttpError(400, "new_role: the new one-line role")
    role = role.strip()
    if _CONTROL.search(role) or len(role) > ROLE_MAX:
        raise HttpError(400, "new_role is one line of at most %d characters" % ROLE_MAX)
    return role


def _timeout(body):
    value = body.get("timeout", lifecycle.BEQUEST_TIMEOUT_SECONDS)
    if value is None:
        return lifecycle.BEQUEST_TIMEOUT_SECONDS
    if isinstance(value, bool) or not isinstance(value, int) \
            or not MIN_TIMEOUT_S <= value <= MAX_TIMEOUT_S:
        raise HttpError(400, "timeout is whole seconds, %d to %d" % (MIN_TIMEOUT_S, MAX_TIMEOUT_S))
    return value


def confirm_phrase(mode, donor, recipient):
    """What the operator types to confirm `mode`, or None when a second
    click (confirm: true) does."""
    entry = next((m for m in MODES if m["id"] == mode), None)
    if entry is None or entry["confirm"] != "typed":
        return None
    return entry["phrase"].format(donor=donor, recipient=recipient)


def _audit(server, record):
    """A console row in the lifecycle audit (data/lifecycle/audit.jsonl,
    beside the library's own rows): who asked, from the console."""
    lifecycle._audit(server.root, dict(record, by="console"))


def _audited(server, work, record):
    """`work`, with a console-request row when its op starts and a
    console-result row (ok, error) when it ends, both with the op id."""
    def run(op):
        out = None
        _audit(server, dict(record, step="console-request", op_id=op.id))
        try:
            out = work(op)
            return out
        finally:
            ok = isinstance(out, dict) and bool(out.get("ok"))
            _audit(server, dict(record, step="console-result", op_id=op.id, ok=ok,
                                error=None if ok else (out or {}).get("error") if
                                isinstance(out, dict) else "the op raised"))
    return run


def held_by(server, slug):
    """The transplant holding `slug` as its donor: {"op_id", "recipient",
    "mode", "since"}, or None."""
    return server.state.get("lifecycle.held", {}).get(slug)


def _flip_summary(result):
    return {k: result[k] for k in ("ok", "error", "new_generation", "queued")
            if isinstance(result, dict) and k in result}


def _wrap_flip(op, real, name_of, before=None):
    """The library's do_flip, showing each flip as a stage named
    `name_of(slug)`: `flip` for one cousin, `flip <slug>` for two."""
    def do_flip(slug, reason=None):
        if before:
            before(slug)
        name = name_of(slug)
        op.stage(name, "running", "flipping %s" % slug)
        result = (real(slug) if reason is None else real(slug, reason=reason)) or {}
        op.stage(name, "done" if result.get("ok") else "failed",
                 "generation %s" % result["new_generation"] if result.get("ok")
                 and result.get("new_generation") is not None
                 else result.get("error") if not result.get("ok") else None)
        return result
    return do_flip


def _bequest_stage(step):
    if step.get("carried"):
        return "done", step.get("skipped") or "carried on the rollover's handoff request"
    if step.get("wrote"):
        return "done", "the cousin wrote data/handoff.md"
    return "skipped", step.get("reason") or "no bequest"


def _reincarnate_work(server, slug, role, timeout):
    real_flip, real_send = _do_flip(server), _send(server)

    def work(op):
        op.stage("snapshot", "running")
        asked = []

        def send(config, text):
            asked.append(True)
            op.stage("snapshot", "done")
            op.stage("bequest", "running", "asked for data/handoff.md; waiting up to %ds" % timeout)
            return real_send(config, text)

        def before_flip(_slug):
            op.stage("snapshot", "done")
            # the tmux lane asked and waited already; the runner lane's
            # bequest rides the flip's own handoff request
            op.stage("bequest", "done", None if asked else
                     "carried on the rollover's handoff request")
            op.stage("rewrite", "done", "the role in CLAUDE.md and cousin.toml")

        result = lifecycle.reincarnate(
            slug, new_role=role, root=server.root, timeout=timeout, send=send,
            do_flip=_wrap_flip(op, real_flip, lambda s: "flip", before_flip))
        for step in result.get("steps") or ():
            if step.get("step") == "snapshot":
                op.stage("snapshot", "done", step.get("path"))
            elif step.get("step") == "bequest":
                op.stage("bequest", *_bequest_stage(step))
            elif step.get("step") == "rewrite":
                op.stage("rewrite", "done", "role %r%s" % (
                    step.get("new_role"), "" if step.get("claude_md") else "; no CLAUDE.md"))
        out = {"ok": bool(result.get("ok")), "snapshot": result.get("snapshot"),
               "flip": _flip_summary(result.get("flip"))}
        if not out["ok"]:
            out["error"] = result.get("error") or "the reincarnation failed"
        return out
    return work


def _transplant_work(server, donor, recipient, mode, hold):
    real_flip = _do_flip(server)

    def work(op):
        try:
            op.stage("snapshot", "running", "%s and %s" % (donor, recipient))
            applied = []

            def before_flip(_slug):
                if not applied:
                    applied.append(True)
                    op.stage("snapshot", "done")
                    op.stage("apply", "done", mode)

            result = lifecycle.transplant(donor=donor, recipient=recipient, mode=mode,
                                          root=server.root,
                                          do_flip=_wrap_flip(op, real_flip,
                                                             lambda s: "flip %s" % s,
                                                             before_flip))
            snaps = result.get("snapshots") or {}
            if snaps:
                op.stage("snapshot", "done", "; ".join("%s: %s" % kv for kv in snaps.items()))
            for step in result.get("steps") or ():
                if step.get("step") == "apply":
                    extra = ("; %d raw lines merged in" % step["raw_lines_added"]
                             if "raw_lines_added" in step else "")
                    op.stage("apply", "done", mode + extra)
            out = {"ok": bool(result.get("ok")), "snapshots": snaps,
                   "flips": {s: _flip_summary(r) for s, r in (result.get("flips") or {}).items()}}
            if not out["ok"]:
                out["error"] = result.get("error") or "the transplant failed"
            return out
        finally:
            held = server.state.get("lifecycle.held", {})
            if held.get(donor, {}).get("recipient") == recipient:
                del held[donor]
            hold.release()
    return work


def register():
    @router.route("GET", "/api/lifecycle/modes")
    def modes(req):
        return 200, {"ok": True, "modes": [dict(m) for m in MODES], "op_kinds": list(OP_KINDS),
                     "timeout": lifecycle.BEQUEST_TIMEOUT_SECONDS,
                     "timeout_range": [MIN_TIMEOUT_S, MAX_TIMEOUT_S], "role_max": ROLE_MAX}

    @router.route("POST", "/api/cousins/{slug}/reincarnate")
    def reincarnate(req, slug):
        server = req.server
        _home(server, slug)
        body = req.body
        role, timeout = _role(body), _timeout(body)
        if body.get("confirm") is not True:
            raise HttpError(400, "reincarnate rewrites the role and flips %s: confirm it" % slug)
        record = {"op": "reincarnate", "slug": slug, "actor": req.user, "new_role": role}
        answer = longop.start_response(
            server, slug, "reincarnate",
            _audited(server, _reincarnate_work(server, slug, role, timeout), record),
            params={"new_role": role, "timeout": timeout, "by": req.user})
        return answer

    @router.route("GET", "/api/cousins/{slug}/lifecycle")
    def lifecycle_state(req, slug):
        _home(req.server, slug)
        return 200, {"ok": True, "slug": slug, "held": held_by(req.server, slug)}

    @router.route("POST", "/api/lifecycle/transplant")
    def transplant(req):
        server = req.server
        body = req.body
        donor, recipient, mode = body.get("donor"), body.get("recipient"), body.get("mode")
        _home(server, donor)
        _home(server, recipient)
        if mode not in lifecycle.MODES:
            raise HttpError(400, "mode must be one of %s" % ", ".join(lifecycle.MODES))
        if donor == recipient:
            raise HttpError(400, "donor and recipient must differ")
        phrase = confirm_phrase(mode, donor, recipient)
        if phrase is not None:
            if body.get("confirm") != phrase:
                raise HttpError(400, "a %s is confirmed by typing %r" % (mode, phrase))
        elif body.get("confirm") is not True:
            raise HttpError(400, "the transplant changes %s: confirm it" % recipient)
        try:
            hold = longop.exclusive(server, donor, "transplant (donor)")
        except longop.Busy as err:
            raise HttpError(409, str(err), busy=True)
        record = {"op": "transplant", "donor": donor, "recipient": recipient, "mode": mode,
                  "actor": req.user}
        # the donor's own inspector says what holds it (a Hold is never a
        # reported op); set before the op can end, which removes it
        held = server.state.setdefault("lifecycle.held", {})
        mark = {"op_id": None, "recipient": recipient, "mode": mode, "since": time.time()}
        held[donor] = mark
        try:
            answer = longop.start_response(
                server, recipient, "transplant",
                _audited(server, _transplant_work(server, donor, recipient, mode, hold), record),
                params={"donor": donor, "recipient": recipient, "mode": mode, "by": req.user})
        except BaseException:
            if held.get(donor) is mark:
                del held[donor]
            hold.release()
            raise
        mark["op_id"] = answer[1]["op"]["id"]
        return answer


register()
