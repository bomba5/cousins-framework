"""The daily cost cap: [agent] daily_cost_cap_usd, per cousin, in USD.

The measure is usage.db's cost_usd summed over the current UTC day
(usage.spent_today): on the login lane the SDK's API-equivalent price, not
a bill; on the key lane the API's figure; on opencode opencode's own. Off
at 0, the default. The key is read from cousin.toml at every turn start,
never only at the runner's start, so a change applies to the next turn.

At a turn boundary, with the claimed row in hand (`admit`):

- under the cap the turn runs, and `cap:<slug>` is ok in the health record;
- at or over it, a row on an operator or person thread runs all the same:
  a `cap` stream event (`allowed: "person"`) and one runner note at the
  head of the row's context block (the side digest's way into a turn);
- any other row (loop, peer, schedule, meeting, system) is refused: closed
  `failed` with the reason, a `cap` event (`refused`), `cap:<slug>`
  failing, and once per UTC day data/cost-cap.json, which the Telegram
  bridge relays to the operators once (telegram.relay_cap_notice).

The decision is the turn's FIRST row's: a row folded into a running turn
is never decided again. Never refused: a flip (the rollover, no turn of its
own), the boot digest (a new session's first message; refused, the session
would run without its state) and an interrupt row (never a turn).

Not counted: a dreaming pass, and any other side session that writes no
usage.db row. The tmux kind writes no usage.db, so it has no measure and
does not read the key (agent_settings)."""
import json
import math
import os
import time
import tomllib
from pathlib import Path

from cousin_lib import health, usage
from cousin_lib.delivery import DeliveryError, FAILED, parse_thread
from cousin_lib.runner.base import INTERRUPT, SURFACE_KINDS

KEY = "daily_cost_cap_usd"
NOTICE_FILE = "data/cost-cap.json"
# the rows a cap never refuses (module docstring)
NEVER_REFUSED = ("flip", "boot", INTERRUPT)


def _cousin_toml(home):
    try:
        return tomllib.loads((Path(home) / "cousin.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _slug(data, home):
    cousin = data.get("cousin")
    slug = cousin.get("slug") if isinstance(cousin, dict) else None
    return slug if isinstance(slug, str) and slug else Path(home).name


def check_value(value):
    """The cap as a float, or ValueError: a number, 0 or more (0 is off)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or math.isnan(value) or math.isinf(value):
        raise ValueError("must be a number of US dollars (0 is off)")
    if value < 0:
        raise ValueError("must be 0 (off) or more")
    return float(value)


def _read(home):
    """(limit, slug, problem): the cap as cousin.toml has it now, 0.0 when
    unset; a value that is not a number >= 0 reads as 0.0 (off), with the
    reason in `problem`."""
    data = _cousin_toml(home)
    agent = data.get("agent")
    value = agent.get(KEY, 0) if isinstance(agent, dict) else 0
    try:
        return check_value(value), _slug(data, home), None
    except ValueError as err:
        return 0.0, _slug(data, home), "[agent] %s %s; read as off" % (KEY, err)


def limit_of(home):
    """The cap in USD now, 0.0 when off (the console's tokens view)."""
    return _read(home)[0]


def reached(spent, limit):
    return "daily cost cap reached: spent $%.2f of $%.2f today (UTC)" % (spent, limit)


def note(spent, limit):
    return ("[runner] daily cost cap reached: $%.2f of $%.2f today (UTC); this turn"
            " runs because a person sent it." % (spent, limit))


def _health(root, slug, ok, error=None):
    try:
        health.record(root, [("cap:%s" % slug, ok, error)])
    except Exception:  # noqa: BLE001 - the record is a view, never a reason to stop
        pass


def _clear_failing(root, slug):
    """A cap turned off leaves no failing `cap:<slug>` row behind."""
    entry = health.read(root).get("cap:%s" % slug)
    if entry and entry.get("state") == "failing":
        _health(root, slug, True)


def _notice_once(home, *, slug, limit, spent, now):
    """data/cost-cap.json for today, written the first time a turn is
    refused on that UTC day; True when this call wrote it."""
    day = usage.utc_day(now)
    path = Path(home) / NOTICE_FILE
    try:
        if json.loads(path.read_text()).get("day") == day:
            return False
    except (OSError, ValueError, AttributeError):
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("%s.%d.tmp" % (path.name, os.getpid()))
    tmp.write_text(json.dumps({"day": day, "slug": slug, "limit": limit, "spent": spent,
                               "since": now}))
    os.replace(tmp, path)
    return True


def read_notice(home):
    """data/cost-cap.json as a dict, None when absent or unreadable."""
    try:
        data = json.loads((Path(home) / NOTICE_FILE).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("day") else None


def _person(row):
    try:
        kind, _ = parse_thread(row["thread_id"])
    except DeliveryError:
        return False
    return kind in SURFACE_KINDS


def admit(home, row, *, inbox, stream, root, now=None):
    """The claimed `row` as its turn should run it (a person's over the cap
    carries the note), or None when the cap refused it: the row is closed
    then, and no turn runs. Called once per turn, on its first row only.
    A failure here is the cap's, never the turn's: the row runs."""
    if row.get("source") in NEVER_REFUSED:
        return row
    try:
        limit, slug, problem = _read(home)
        if problem:
            _health(root, slug, False, problem)
            return row
        if limit <= 0:
            _clear_failing(root, slug)
            return row
        now = time.time() if now is None else now
        spent = usage.spent_today(home, now)
        if spent < limit:
            _health(root, slug, True)
            return row
        reason = reached(spent, limit)
        event = {"limit": limit, "spent": round(spent, 6), "over": True,
                 "inbox_id": row["id"], "thread_id": row["thread_id"]}
        if _person(row):
            _health(root, slug, False, reason + "; only chat from a person runs")
            stream.append("cap", dict(event, allowed="person"))
            context = row.get("context") or ""
            return dict(row, context=note(spent, limit) + ("\n\n" + context if context else ""))
    except Exception as exc:  # noqa: BLE001 - the cap never fails a turn by itself
        try:
            stream.append("error", {"error": "daily cost cap: %s: %s"
                                    % (type(exc).__name__, exc)})
        except Exception:  # noqa: BLE001
            pass
        return row
    inbox.done(row["id"], FAILED, reason)
    _health(root, slug, False, reason + "; only chat from a person runs")
    try:
        told = _notice_once(home, slug=slug, limit=limit, spent=spent, now=now)
    except OSError:
        told = False
    stream.append("cap", dict(event, refused=True, detail=reason, notice=told))
    return None
