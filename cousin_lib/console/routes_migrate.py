"""Console routes for the kind switch and migration (docs/reference/console-api.md,
"Kind switch and migration"): cousin-migrate's plan, apply, check and
rollback, and the kind switch (`--to sdk|tmux`), through the
migrate library only. migrate.py owns the steps: nothing here changes how
a step runs, it only watches the steps go by.

apply and rollback change a live cousin, so each is a long operation
(console/longop.py) with its steps as stages, and one at a time across
the whole fleet (the runbook's rule: one cousin at a time). A plan or a
check with `validate` spends one model turn, so it is a long operation
too; without it, it answers at once.

The stages come from the library's own records as they are written
(data/migration.json, data/kind-switch.json): each live action the
library is handed is wrapped, so when the next one is called the steps
before it are read back and reported, and the one starting is shown
running. The switch's verify is watched while it runs: when the tmux
pane shows a screen that waits on a person (the trust dialog above all,
data/login-required.json `screen`), the verify stage says so, and the
browser offers the pane to answer it in.

Not built yet: adopt, and `--all
--keep-going` (a fleet action). GET .../migrate says so in `deferred`.

Test seams on req.server.state (never set by a request):
`migrate.live` (overrides migrate._live()'s actions, plus `health` for
check), `migrate.switch_live` (overrides migrate._switch_live()'s), and
`migrate.poll` (seconds between the verify watcher's reads)."""
from __future__ import annotations

import re
import sys
import threading
import traceback

from cousin_lib import accounts, migrate
from cousin_lib.console import longop, router
from cousin_lib.console.app import HttpError
from cousin_lib.console.pane import ANSWERABLE
from cousin_lib.runner.tmux_runner import LOGIN_SCREENS

# the kinds of op the fleet runs one at a time
EXCLUSIVE_KINDS = ("migrate", "kind-switch", "migrate-rollback", "kind-switch-rollback")
OP_KINDS = EXCLUSIVE_KINDS + ("migrate-plan", "migrate-check")
# the runner kinds whose cousin runs in a pane the console can show
# (console/pane.py kind_pane: the tmux kind)
PANE_KINDS = ("tmux",)
POLL_S = 1.0
# what a record may show: never the saved file's bytes or mode
RECORD_KEYS = ("slug", "state", "from", "to", "account", "session_id", "started_at",
               "migrated_at", "switched_at", "rolled_back_at", "failed", "error", "steps",
               "rollback_steps", "rollback_attempts", "rollback_done", "warnings", "validated",
               "waiting_at_rollback", "cli")
DEFERRED = (
    {"id": "adopt", "label": "adopt a live pane",
     "why": "not yet: adopt is not built; a tmux-kind start adopts a pane on its own"},
    {"id": "all", "label": "switch every cousin (--all --keep-going)",
     "why": "not yet: the fleet switch is not built; switch one cousin at a time"},
)
_ISO = re.compile(r"^\d{4}-\d\d-\d\d([T ]\d\d:\d\d(:\d\d(\.\d+)?)?([+-]\d\d:?\d\d|Z)?)?$")


# ---- helpers ----------------------------------------------------------------

def _home(server, slug):
    from cousin_lib.console._common import cousin_home
    return cousin_home(server, slug)


def _live(server):
    live = migrate._live()
    live.update(server.state.get("migrate.live") or {})
    return live


def _switch_live(server):
    live = migrate._switch_live()
    live.update(server.state.get("migrate.switch_live") or {})
    return live


def _poll(server):
    return float(server.state.get("migrate.poll") or POLL_S)


def public_record(rec):
    """A migration or kind-switch record as it may be served: the listed
    keys only (never prior_toml_b64 or prior_mode), or None."""
    if not isinstance(rec, dict):
        return None
    return {k: rec[k] for k in RECORD_KEYS if k in rec}


def login_screen(home):
    """{"screen", "kind", "reason"} from data/login-required.json, or None:
    what the pane waits on (a tmux-kind runner writes the screen it saw).
    Never its detail, which can carry the API's own words."""
    from cousin_lib.runner import auth as runner_auth
    data = runner_auth.read_login_required(home)
    if not isinstance(data, dict):
        return None
    return {k: (str(data[k]) if data.get(k) is not None else None)
            for k in ("screen", "kind", "reason")}


def waiting_line(screen):
    if screen == "trust":
        return "waiting for the operator to accept the trust dialog in the pane (screen: trust)"
    return "waiting on a person in the pane (screen: %s)" % screen


def _lane(home):
    from cousin_lib.delivery import _runner_kind
    return _runner_kind(home) or "tmux-legacy"


def _no_kind(home):
    """A plan, apply or rollback of the migration (no `to`) is
    refused before anything runs: 2.0.0 keeps no conversion from the legacy
    lane. 409 with delivery.lane_refusal for a cousin with no runner;
    400 telling a runner cousin to name a kind."""
    from cousin_lib.delivery import RUNNER_KINDS, _runner_kind
    raise HttpError(400 if _runner_kind(home) in RUNNER_KINDS else 409,
                    migrate.no_kind_line(home))


def _bool(body, key):
    value = body.get(key, False)
    if value is None:
        return False
    if not isinstance(value, bool):
        raise HttpError(400, "%s must be true or false" % key)
    return value


def _to(body):
    to = body.get("to")
    if to is None or to == "":
        return None
    if to not in migrate.SWITCH_KINDS:
        raise HttpError(400, "to must be one of %s" % ", ".join(migrate.SWITCH_KINDS))
    return to


def _account(body):
    name = body.get("account")
    if name is None or name == "":
        return None
    if not isinstance(name, str) or not accounts._NAME.match(name):
        raise HttpError(400, "bad account name")
    return name


def _since(body):
    since = body.get("since")
    if since is None or since == "":
        return None
    stamp = migrate._stamp(since) if isinstance(since, str) and _ISO.match(since) else None
    if stamp is None:
        raise HttpError(400, "since must be an ISO time")
    return stamp


def _options(body):
    """(to, account, validate) of a plan or an apply. The kind switch keeps
    the cousin's own account and has no model turn to validate."""
    to, account, validate = _to(body), _account(body), _bool(body, "validate")
    if to and account:
        raise HttpError(400, "the kind switch keeps the cousin's account: no account with to")
    if to and validate:
        raise HttpError(400, "validate is for the tmux-lane migration, not the kind switch")
    return to, account, validate


def _confirmed(body, what):
    if body.get("confirm") is not True:
        raise HttpError(400, "%s changes a live cousin: confirm it" % what)


def running_migration(server):
    """{"slug", "kind"} of the migration, switch or rollback running on
    the fleet, or None."""
    return server.state.get("migrate.running")


def _start_exclusive(server, slug, kind, work, params):
    """longop.start_response, one at a time across the fleet for the kinds
    in EXCLUSIVE_KINDS: 409 while another cousin's runs."""
    lock = server.state.setdefault("migrate.lock", threading.Lock())
    with lock:
        busy = running_migration(server)
        if busy:
            raise HttpError(409, "a %s runs on %s: one migration or kind switch at a time"
                            % (busy["kind"], busy["slug"]), busy=True)

        def run(op):
            try:
                return work(op)
            finally:
                with lock:
                    server.state["migrate.running"] = None

        answer = longop.start_response(server, slug, kind, run, params=params)
        server.state["migrate.running"] = {"slug": slug, "kind": kind}
        return answer


def _need_supervisor(up, root):
    if not up(root):
        raise HttpError(409, "no cousin-supervisor runs for %s: the migration, the kind"
                             " switch and their rollbacks stop and start the cousin through it"
                             % root)


class _Stages:
    """The library's record read back into op stages: each step once, as
    it is written; `running(name)` first reports what the record holds by
    then, then shows `name` running."""

    def __init__(self, op, read, failed_step=None):
        self.op = op
        self.read = read
        self.failed_step = failed_step
        self.seen = {}
        self.current = None

    def sync(self, key="steps"):
        rec = self.read() or {}
        for s in rec.get(key) or ():
            name = s.get("step")
            if not name:
                continue
            if "ok" in s:
                status = "done" if s["ok"] else "failed"
            else:
                status = ("failed" if self.failed_step and self.failed_step(rec) == name
                          else "done")
            row = (status, s.get("detail"))
            if self.seen.get(name) != row:
                self.seen[name] = row
                self.op.stage(name, status, s.get("detail"))
        return rec

    def running(self, name, detail=None, key="steps"):
        # the actions are called one after another: the one before this
        # returned, so it is done even when its record row comes later
        if self.current and self.current != name and self.current not in self.seen:
            self.seen[self.current] = ("done", None)
            self.op.stage(self.current, "done")
        self.sync(key)
        self.seen.pop(name, None)
        self.current = name
        self.op.stage(name, "running", detail)


def _watch_pane(op, home, poll, stage="verify"):
    """While the verify runs: say when the pane waits on a person (a
    tmux-kind runner records the screen in data/login-required.json).
    Returns end(), which stops it and waits for it, so no stage is
    reported after the verify returned."""
    stop = threading.Event()

    def watch():
        said = None
        while not stop.wait(poll):
            seen = login_screen(home)
            screen = seen and seen.get("screen")
            if stop.is_set():                    # the verify returned meanwhile
                return
            if screen and screen != said:
                said = screen
                op.stage(stage, "running", waiting_line(screen))
            elif not screen and said:
                said = None
                op.stage(stage, "running", "the pane's screen is clear again")

    thread = threading.Thread(target=watch, daemon=True, name="console-migrate-watch")
    thread.start()

    def end():
        stop.set()
        thread.join(poll + 5.0)
    return end


# ---- the work ---------------------------------------------------------------

def _apply_work(server, home, account, validate):
    root = server.root
    live = _live(server)

    def work(op):
        st = _Stages(op, lambda: migrate.read_record(home))
        op.stage("plan", "running", "the checks%s" % (" and one model turn" if validate else ""))
        wrapped = dict(live)

        def close(slug, root_):
            op.stage("plan", "done", "ready")
            st.running("close", "the clean stop: the handoff, then the session ends")
            return live["close"](slug, root_)

        def import_auto(home_, root_):
            st.running("import")
            return live["import_auto"](home_, root_)

        def start(home_, root_):
            st.running("start")
            return live["start"](home_, root_)

        def verify(*a, **kw):
            st.running("verify")
            return live["verify"](*a, **kw)

        wrapped.update(close=close, import_auto=import_auto, start=start, verify=verify)
        try:
            rec = migrate.apply(home, root=root, account=account, validate=validate, **wrapped)
        except migrate.MigrateError as err:
            raise longop.OpError(str(err))
        st.sync()
        out = {"ok": rec.get("state") == "migrated", "state": rec.get("state"),
               "warnings": rec.get("warnings") or []}
        if not out["ok"]:
            last = (rec.get("steps") or [{}])[-1]
            out["error"] = "step %s failed: %s; roll back from the migration panel" % (
                last.get("step"), last.get("detail"))
        return out
    return work


def _switch_work(server, home, to):
    root = server.root
    live = _switch_live(server)
    poll = _poll(server)

    def work(op):
        st = _Stages(op, lambda: migrate.read_switch_record(home),
                     failed_step=lambda rec: rec.get("failed") if rec.get("state") == "failed"
                     else None)
        wrapped = dict(live)

        def close(home_, root_):
            st.running("close", "the runner stops at idle; its session is kept")
            return live["close"](home_, root_)

        def cursor_end(*a, **kw):
            st.running("cursor")
            return live["cursor_end"](*a, **kw)

        def start(home_, root_):
            st.running("start")
            return live["start"](home_, root_)

        def verify(*a, **kw):
            st.running("verify")
            end = _watch_pane(op, home, poll)
            try:
                return live["verify"](*a, **kw)
            finally:
                end()

        wrapped.update(close=close, cursor_end=cursor_end, start=start, verify=verify)
        try:
            rec = migrate.switch_apply(home, root=root, to=to, **wrapped)
        except migrate.MigrateError as err:
            st.sync()
            raise longop.OpError(str(err))
        except Exception as err:  # noqa: BLE001 - the library leaves the record "switching"
            raise longop.OpError(_switch_crashed(home, st, err))
        st.sync()
        return {"ok": True, "state": rec.get("state"), "warnings": rec.get("warnings") or []}
    return work


def _switch_crashed(home, st, err):
    """A switch step raised something the library does not word (it only
    catches its own MigrateError): the record, still `switching`, is
    marked failed at the step that ran, and the reason names the step and
    the exception's type. Its text goes to the console's stderr only (an
    exception's text can carry a secret)."""
    step = st.current or "the start"
    reason = "failed at %s: %s (see the console log); roll back to the kind it came from" % (
        step, type(err).__name__)
    print("cousin-console: kind switch on %s failed at %s:\n%s" % (
        home.name, step, traceback.format_exc()), file=sys.stderr, flush=True)
    rec = migrate.read_switch_record(home)
    if isinstance(rec, dict) and rec.get("state") == "switching":
        rec.update(state="failed", failed=step, error=reason)
        try:
            migrate._write_switch_record(home, rec)
        except OSError:
            pass
    st.sync()
    if st.current:
        st.op.stage(st.current, "failed", reason)
    return "the kind switch " + reason


def _rollback_work(server, home, force):
    root = server.root
    live = _live(server)

    def work(op):
        st = _Stages(op, lambda: migrate.read_record(home))
        wrapped = dict(live)
        for name, stage in (("stop", "stop"), ("reload", "reload"),
                            ("start_tmux", "start_tmux"), ("release", "release")):
            def wrap(*a, _fn=live[name], _stage=stage, **kw):
                st.running(_stage, key="rollback_steps")
                return _fn(*a, **kw)
            wrapped[name] = wrap
        try:
            rec = migrate.rollback(home, root=root, force=force, **wrapped)
        except migrate.MigrateError as err:
            raise longop.OpError(str(err))
        st.sync("rollback_steps")
        return {"ok": True, "state": rec.get("state"),
                "waiting_at_rollback": rec.get("waiting_at_rollback")}
    return work


def _switch_rollback_work(server, home, to):
    root = server.root
    live = _switch_live(server)

    def work(op):
        st = _Stages(op, lambda: migrate.read_switch_record(home))
        wrapped = dict(live)

        def close(home_, root_):
            st.running("close", key="rollback_steps")
            return live["close"](home_, root_)

        def cursor_end(*a, **kw):
            st.running("cursor", key="rollback_steps")
            return live["cursor_end"](*a, **kw)

        def start(home_, root_):
            st.running("start", key="rollback_steps")
            return live["start"](home_, root_)

        wrapped.update(close=close, cursor_end=cursor_end, start=start)
        try:
            rec = migrate.switch_rollback(home, root=root, to=to, **wrapped)
        except migrate.MigrateError as err:
            raise longop.OpError(str(err))
        st.sync("rollback_steps")
        return {"ok": True, "state": rec.get("state")}
    return work


def _check(server, home, since, validate):
    live = _live(server)
    return migrate.check(home, since=since, health=live.get("health") or migrate.chat_health,
                         root=server.root, validate=validate, validator=live.get("validator"),
                         cli_version=live.get("cli_version"))


# ---- routes -----------------------------------------------------------------

def register():
    @router.route("GET", "/api/cousins/{slug}/migrate")
    def state(req, slug):
        server = req.server
        home = _home(server, slug)
        try:
            up = bool(_live(server)["supervisor_up"](server.root))
        except Exception:  # noqa: BLE001 - unknown reads as not up
            up = False
        from cousin_lib.delivery import RUNNER_KINDS, _runner_kind, lane_refusal
        refusal = None if _runner_kind(home) in RUNNER_KINDS else lane_refusal(home)
        return 200, {"ok": True, "slug": slug, "lane": _lane(home), "refusal": refusal,
                     "kinds": list(migrate.SWITCH_KINDS), "steps": list(migrate.STEPS),
                     "switch_steps": {k: list(v) for k, v in migrate.SWITCH_STEPS.items()},
                     "migration": public_record(migrate.read_record(home)),
                     "switch": public_record(migrate.read_switch_record(home)),
                     "loginScreen": login_screen(home), "supervisor": up,
                     "running": running_migration(server), "deferred": list(DEFERRED),
                     "person_screens": list(LOGIN_SCREENS), "pane_answers": list(ANSWERABLE),
                     "pane_kinds": list(PANE_KINDS), "op_kinds": list(OP_KINDS)}

    @router.route("POST", "/api/cousins/{slug}/migrate/plan")
    def plan(req, slug):
        server = req.server
        home = _home(server, slug)
        to, account, validate = _options(req.body)
        if to:
            p = migrate.switch_plan(home, root=server.root, to=to, **_switch_live(server))
            return 200, {"ok": True, "plan": p}
        _no_kind(home)
        live = _live(server)
        if not validate:
            try:
                p = migrate.plan(home, root=server.root, account=account, **live)
            except migrate.MigrateError as err:
                raise HttpError(400, str(err))
            return 200, {"ok": True, "plan": p}

        def work(op):
            op.stage("validate", "running", "the checks and one smallest model turn")
            try:
                p = migrate.plan(home, root=server.root, account=account, validate=True, **live)
            except migrate.MigrateError as err:
                raise longop.OpError(str(err))
            check = next((c for c in p["checks"] if c["check"] == "validate"), None)
            op.stage("validate", "done" if check and check["ok"] else "failed",
                     check and check["detail"])
            return {"ok": True, "plan": p}
        return longop.start_response(server, slug, "migrate-plan", work,
                                     params={"account": account, "validate": True})

    @router.route("POST", "/api/cousins/{slug}/migrate/apply")
    def apply(req, slug):
        server = req.server
        home = _home(server, slug)
        to, account, validate = _options(req.body)
        if not to:
            _no_kind(home)
        _confirmed(req.body, "the kind switch" if to else "the migration")
        busy = running_migration(server)
        if busy:
            raise HttpError(409, "a %s runs on %s: one migration or kind switch at a time"
                            % (busy["kind"], busy["slug"]), busy=True)
        if to:
            live = _switch_live(server)
            _need_supervisor(live["supervisor_up"], server.root)
            steps = list(migrate.SWITCH_STEPS[to])
            steps[steps.index("toml") + 1:steps.index("toml") + 1] = ["cursor"]
            steps[steps.index("start") + 1:steps.index("start") + 1] = ["notice"]
            return _start_exclusive(server, slug, "kind-switch", _switch_work(server, home, to),
                                    {"to": to, "steps": steps, "by": req.user})
        _need_supervisor(_live(server)["supervisor_up"], server.root)
        return _start_exclusive(server, slug, "migrate",
                                _apply_work(server, home, account, validate),
                                {"account": account, "validate": validate, "by": req.user,
                                 "steps": ["plan"] + list(migrate.STEPS)})

    @router.route("POST", "/api/cousins/{slug}/migrate/check")
    def check(req, slug):
        server = req.server
        home = _home(server, slug)
        since, validate = _since(req.body), _bool(req.body, "validate")
        if not validate:
            return 200, {"ok": True, "check": _check(server, home, since, False)}

        def work(op):
            op.stage("check", "running", "the records and one smallest model turn")
            c = _check(server, home, since, True)
            op.stage("check", "done" if c.get("ok") else "failed",
                     c.get("validate") or ("ok" if c.get("ok") else "not ok"))
            return {"ok": True, "check": c}
        return longop.start_response(server, slug, "migrate-check", work,
                                     params={"validate": True})

    @router.route("POST", "/api/cousins/{slug}/migrate/rollback")
    def rollback(req, slug):
        server = req.server
        home = _home(server, slug)
        body = req.body
        which = body.get("which")
        if which not in ("migration", "switch"):
            raise HttpError(400, "which must be migration or switch")
        if which == "migration":
            _no_kind(home)
        force = _bool(body, "force")
        _confirmed(body, "the rollback")
        if which == "switch":
            to = _to(body)
            if not to:
                raise HttpError(400, "to must name the kind the switch came from")
            if force:
                raise HttpError(400, "the kind switch's rollback takes no force")
            _need_supervisor(_switch_live(server)["supervisor_up"], server.root)
            return _start_exclusive(server, slug, "kind-switch-rollback",
                                    _switch_rollback_work(server, home, to),
                                    {"to": to, "by": req.user})
        if force and body.get("force_confirm") is not True:
            raise HttpError(400, "force rolls back with inbox rows nobody will read: it asks a"
                                 " second time (force_confirm)")
        _need_supervisor(_live(server)["supervisor_up"], server.root)
        return _start_exclusive(server, slug, "migrate-rollback",
                                _rollback_work(server, home, force),
                                {"force": force, "by": req.user})


register()
