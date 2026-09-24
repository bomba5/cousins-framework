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

from cousin_lib import lifecycle
from cousin_lib.console import longop, router
from cousin_lib.console.app import HttpError

ROLE_MAX = 200
MIN_TIMEOUT_S, MAX_TIMEOUT_S = 10, 600
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
MODES = (
    {"id": "soul-donation", "confirm": "second",
     "what": "the recipient carries the donor's memory in its own body; the recipient's own"
             " memory leaves its home (it stays in the snapshot)"},
    {"id": "body-swap", "confirm": "typed",
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


def swap_phrase(donor, recipient):
    """What the operator types to confirm a body swap."""
    return "swap %s %s" % (donor, recipient)


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
            hold.release()
    return work


def register():
    @router.route("GET", "/api/lifecycle/modes")
    def modes(req):
        return 200, {"ok": True, "modes": [dict(m) for m in MODES],
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
        return longop.start_response(server, slug, "reincarnate",
                                     _reincarnate_work(server, slug, role, timeout),
                                     params={"new_role": role, "timeout": timeout,
                                             "by": req.user})

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
        if mode == "body-swap":
            if body.get("confirm") != swap_phrase(donor, recipient):
                raise HttpError(400, "a body swap is confirmed by typing %r"
                                % swap_phrase(donor, recipient))
        elif body.get("confirm") is not True:
            raise HttpError(400, "the transplant changes %s: confirm it" % recipient)
        try:
            hold = longop.exclusive(server, donor, "transplant (donor)")
        except longop.Busy as err:
            raise HttpError(409, str(err), busy=True)
        try:
            return longop.start_response(
                server, recipient, "transplant",
                _transplant_work(server, donor, recipient, mode, hold),
                params={"donor": donor, "recipient": recipient, "mode": mode, "by": req.user})
        except BaseException:
            hold.release()
            raise


register()
