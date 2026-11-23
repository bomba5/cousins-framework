"""Decision register routes over cousin_lib.decisions (docs/decisions.md;
docs/reference/console-api.md for the shapes): the list, the operator's
answer, and a manual retry of what answers still owe. Withdrawing is the
asking cousin's act, so it has no route here. The list only reads: the
console's event poller retries what is owed (decisions.redeliver), one
pass at a time. An answer and a retry emit a decisions-refresh event."""
from __future__ import annotations

from cousin_lib import decisions
from cousin_lib.console import router
from cousin_lib.console.app import HttpError


def _id(raw):
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise HttpError(404, "decision #%s not found" % raw)


def _text(body, key):
    value = body.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise HttpError(400, "%s must be a string" % key)
    return value


def register():
    @router.route("GET", "/api/decisions")
    def list_decisions(req):
        status = req.query.get("status") or None
        if status not in (None,) + decisions.STATUSES:
            raise HttpError(400, "status must be one of %s" % "|".join(decisions.STATUSES))
        root = req.server.root
        if not decisions.db_path(root).is_file():
            return 200, {"decisions": [], "open": 0}     # a read creates no store
        rows = decisions.list_decisions(status=status, root=root)
        return 200, {"decisions": rows,
                     "open": len(decisions.list_decisions(status="open", root=root))}

    @router.route("POST", "/api/decisions/redeliver")
    def redeliver(req):
        root = req.server.root
        # the manual retry: what was marked undeliverable is tried again too
        tried = (decisions.redeliver(retry_gone=True, root=root)
                 if decisions.db_path(root).is_file() else [])
        req.server.emit("decisions-refresh", {"op": "redeliver"})
        return 200, {"ok": True, "tried": tried}

    @router.route("POST", "/api/decisions/{decision_id}/answer")
    def answer(req, decision_id):
        ident = _id(decision_id)
        body = req.body if isinstance(req.body, dict) else {}
        option, text = _text(body, "option"), _text(body, "answer")
        try:
            row = decisions.answer(ident, answer=text, option=option,
                                   user=req.user or "operator", via="the console",
                                   root=req.server.root)
        except decisions.DecisionNotFound as err:
            raise HttpError(404, str(err))
        except decisions.DecisionError as err:
            raise HttpError(400, str(err))
        req.server.emit("decisions-refresh", {"id": ident, "op": "answer"})
        return 200, {"ok": True, "decision": row}


register()
