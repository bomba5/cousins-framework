"""Per-cousin Telegram provisioning (docs/reference/console-api.md,
"Telegram"): status, the write-only token, the operators, the refused
senders waiting to be added, the enable switch and a token check. The
token never leaves the server: every answer says only whether one is
set. Enabling starts the bridge when the cousin runs; disabling stops
it. A runner cousin's bridge is the supervisor's child (R10, #101): a
change there is written to cousin.toml and the supervisor is asked to
rescan (`reload`), which adds, removes or restarts `telegram:<slug>`;
the console never starts a runner cousin's bridge itself."""
from __future__ import annotations

from cousin_lib import supervisor, telegram_admin
from cousin_lib.console import router
from cousin_lib.console.app import HttpError
from cousin_lib.config import CousinConfig
from cousin_lib.console._common import cousin_home, session_alive


def _status(req, slug, home):
    out = telegram_admin.status(home, req.server.root)
    out["slug"] = slug
    return out


def _guard(fn, *args, **kw):
    try:
        return fn(*args, **kw)
    except telegram_admin.TelegramAdminError as err:
        raise HttpError(400, str(err))


def _cousin_running(req, home):
    return session_alive(req.server, CousinConfig.load(home))


def _runner_lane(home):
    from cousin_lib import spawn
    return spawn.runner_lane(home)


def _supervisor_rescan(req):
    """A runner cousin's config change, handed to the supervisor: its
    rescan starts, stops or restarts the bridge. What to report."""
    try:
        answer = supervisor.request(req.server.root, "reload")
    except supervisor.SupervisorUnavailable:
        return "no cousin-supervisor running; the bridge starts with it"
    if not answer.get("ok"):
        return "the supervisor refused the rescan: %s" % (answer.get("error") or "no reason given")
    return "supervised"


def _restart_bridge(req, home):
    """Apply a config change: the bridge reads its config once, at
    start. It runs only while its cousin runs, like the chat server."""
    if _runner_lane(home):
        return _supervisor_rescan(req)
    telegram_admin.stop_bridge(home)
    if _cousin_running(req, home):
        telegram_admin.start_bridge(home, req.server.root)


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
        _restart_bridge(req, home)
        out = _status(req, slug, home)
        out["check"] = check
        return 200, out

    @router.route("POST", "/api/cousins/{slug}/telegram/operators")
    def set_operators(req, slug):
        home = cousin_home(req.server, slug)
        ops = req.body.get("operators")
        if not isinstance(ops, list):
            raise HttpError(400, "operators must be a list")
        _guard(telegram_admin.set_operators, home, ops)
        _restart_bridge(req, home)
        return 200, _status(req, slug, home)

    @router.route("POST", "/api/cousins/{slug}/telegram/enabled")
    def set_enabled(req, slug):
        home = cousin_home(req.server, slug)
        enabled = req.body.get("enabled")
        if not isinstance(enabled, bool):
            raise HttpError(400, "enabled must be true or false")
        telegram_admin.set_enabled(home, enabled)
        if _runner_lane(home):
            state = _supervisor_rescan(req)
            if not enabled:
                # a bridge started outside the supervisor (before #101)
                # is not the rescan's to stop; the switch still means off
                telegram_admin.stop_bridge(home)
        elif enabled and _cousin_running(req, home):
            state, _pid = telegram_admin.start_bridge(home, req.server.root)
        elif enabled:
            state = "starts with the cousin"
        else:
            state = telegram_admin.stop_bridge(home)
        out = _status(req, slug, home)
        out["bridge"] = state
        return 200, out

    @router.route("POST", "/api/cousins/{slug}/telegram/check")
    def check(req, slug):
        home = cousin_home(req.server, slug)
        telegram_admin.discover(home, req.server.root)
        return 200, telegram_admin.check_token(home, req.server.root)


register()
