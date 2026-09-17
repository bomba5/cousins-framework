"""A tiny method+path router shared by every console route module.

`route(method, pattern)` registers a handler; `{name}` segments become
keyword arguments. Literal segments win over parameters. Registering the
same method+pattern twice replaces the handler (idempotent imports).
`dispatch` returns (status, body) where body is a JSON-able object, or
whatever the handler returns; 404 for no path, 405 for a known path with
another method.
"""
from __future__ import annotations

import re

_ROUTES: list[tuple[str, str, re.Pattern, object]] = []


def clear() -> None:
    del _ROUTES[:]


def routes():
    return [(m, p) for m, p, _rx, _h in _ROUTES]


def _compile(pattern: str) -> re.Pattern:
    parts = []
    for seg in pattern.strip("/").split("/"):
        if seg.startswith("{") and seg.endswith("}"):
            parts.append("(?P<%s>[^/]+)" % seg[1:-1])
        else:
            parts.append(re.escape(seg))
    return re.compile("^/" + "/".join(parts) + "/?$")


def route(method: str, pattern: str):
    def deco(handler):
        method_u = method.upper()
        for i, (m, p, _rx, _h) in enumerate(_ROUTES):
            if m == method_u and p == pattern:
                _ROUTES[i] = (m, p, _compile(pattern), handler)
                break
        else:
            _ROUTES.append((method_u, pattern, _compile(pattern), handler))
        # literal-first ordering: fewer parameters sort earlier
        _ROUTES.sort(key=lambda r: (r[1].count("{"), -len(r[1])))
        return handler
    return deco


def dispatch(method: str, path: str, *, req):
    method_u = method.upper()
    path_matched = False
    for m, _p, rx, handler in _ROUTES:
        mo = rx.match(path)
        if not mo:
            continue
        path_matched = True
        if m != method_u:
            continue
        return handler(req, **mo.groupdict())
    if path_matched:
        return 405, {"error": "method not allowed"}
    return 404, {"error": "not found"}
