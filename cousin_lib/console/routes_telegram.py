"""Per-cousin Telegram provisioning (docs/reference/console-api.md,
"Telegram"): status, the write-only token, the operators, the refused
senders waiting to be added, the enable switch and a token check. The
token never leaves the server: every answer says only whether one is
set. A bridge is the supervisor's child (R10, #101): a change is written
to cousin.toml and the supervisor is asked to rescan (`reload`), which
adds, removes or restarts `telegram:<slug>`; the console never starts a
bridge itself. A cousin with no runner kind has no bridge in 2.0.0: the
config is written and `bridge` says why (delivery.lane_refusal)."""
from __future__ import annotations

from cousin_lib import delivery, supervisor, telegram_admin
from cousin_lib.console import router
from cousin_lib.console.app import HttpError
from cousin_lib.console._common import cousin_home


def _status(req, slug, home):
    out = telegram_admin.status(home, req.server.root)
    out["slug"] = slug
    return out


def _guard(fn, *args, **kw):
    try:
        return fn(*args, **kw)
    except telegram_admin.TelegramAdminError as err:
        raise HttpError(400, str(err))


def _runner_lane(home):
    from cousin_lib import spawn
    return spawn.runner_lane(home)


def _supervisor_rescan(req, home):
    """A runner cousin's config change, handed to the supervisor: its
    rescan starts, stops or restarts the bridge, and stops one running
    outside it when the config no longer runs. Only with no supervisor
    to ask does the console stop a bridge itself (one started outside
    any supervisor), when the config no longer runs; it never starts
    one. A supervisor that is alive but slow is not a missing one (#113):
    its rescan follows when it answers, so the console stops nothing
    (a second stop would race it). What to report."""
    try:
        answer = supervisor.request(req.server.root, "reload")
    except supervisor.SupervisorAbsent:
        if telegram_admin.ready(home, req.server.root) is not None:
            telegram_admin.stop_bridge(home)
        return "no cousin-supervisor running; the bridge starts with it"
    except supervisor.SupervisorUnavailable as err:
        return "the supervisor did not answer the rescan (%s); the bridge follows its rescan" % err
    if not answer.get("ok"):
        return "the supervisor refused the rescan: %s" % (answer.get("error") or "no reason given")
    return "supervised"


def _restart_bridge(req, home):
    """Apply a config change: the bridge reads its config once, at
    start, so the supervisor restarts it. What to report as `bridge`."""
    if _runner_lane(home):
        return _supervisor_rescan(req, home)
    return delivery.lane_refusal(home)


def register():
    @router.route("GET", "/api/cousins/{slug}/telegram")
    def show(req, slug):
        home = cousin_home(req.server, slug)
        # First-time setup: a token but no operator yet, so no bridge
        # runs to notice the Start press. Look for it here.
        if not telegram_admin.operators(home):
            telegram_admin.discover(home, req.server.root)
        return 200, _status(req, slug, home)

    @router.route("POST", "/api/cousins/{slug}/telegram/token")
    def token(req, slug):
        home = cousin_home(req.server, slug)
        value = req.body.get("token")
        if not isinstance(value, str):
            raise HttpError(400, "token must be a string")
        _guard(telegram_admin.set_token, home, req.server.root, slug, value)
        check = telegram_admin.check_token(home, req.server.root)
        telegram_admin.discover(home, req.server.root)
        state = _restart_bridge(req, home)
        out = _status(req, slug, home)
        out["check"] = check
        out["bridge"] = state
        return 200, out

    @router.route("POST", "/api/cousins/{slug}/telegram/operators")
    def set_operators(req, slug):
        home = cousin_home(req.server, slug)
        ops = req.body.get("operators")
        if not isinstance(ops, list):
            raise HttpError(400, "operators must be a list")
        _guard(telegram_admin.set_operators, home, ops)
        state = _restart_bridge(req, home)
        out = _status(req, slug, home)
        out["bridge"] = state
        return 200, out

    @router.route("POST", "/api/cousins/{slug}/telegram/enabled")
    def set_enabled(req, slug):
        home = cousin_home(req.server, slug)
        enabled = req.body.get("enabled")
        if not isinstance(enabled, bool):
            raise HttpError(400, "enabled must be true or false")
        telegram_admin.set_enabled(home, enabled)
        state = _restart_bridge(req, home)
        out = _status(req, slug, home)
        out["bridge"] = state
        return 200, out

    @router.route("POST", "/api/cousins/{slug}/telegram/check")
    def check(req, slug):
        home = cousin_home(req.server, slug)
        telegram_admin.discover(home, req.server.root)
        return 200, telegram_admin.check_token(home, req.server.root)


register()
