"""The nightly crash run (#294): one scenario that touches every producer a
cousin's home has (a chat message, a peer's message, a due one-shot, a
job, the runner answering them), run in a real process that dies at a
crash point the seed picks, at the hit the seed picks. Then everything a
restart and a sender's retry do runs without the crash, and the stores
must hold: each message in the inbox once and answered, the one-shot
fired once, no job left running, no chat row left pending.

    python tests/crash_nightly.py --seed 1234 --rounds 40
    python tests/crash_nightly.py --seed 1234 --round 17      # replay one

A round prints one JSON line (seed, round, point, what it found); the
exit code is 1 when any round broke an invariant. tests/test_crash_nightly.py
runs a few fixed rounds in the suite; .github/workflows/nightly.yml runs
many on tmpfs with a new seed every night."""
import argparse
import json
import os
import pathlib
import random
import sqlite3
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]

# The points this scenario reaches, and the most hits each can have in it
# (chat.stored and chat.put fire for the chat message and the peer's).
POINTS = {"chat.stored": 2, "chat.put": 2, "peer.seen": 1, "schedule.delivered": 1,
          "job.registered": 1, "job.exited": 1, "runner.result_recorded": 2}

SCENARIO = r"""
import os, pathlib, sys, time
from cousin_lib import chat, jobs, peer_inbound, schedule
from cousin_lib.config import CousinConfig
from cousin_lib.runner import main as runner_main
root = pathlib.Path(os.environ["FRAMEWORK_ROOT"])
home = root / "cousins" / "wren"
wren = CousinConfig.load(home)
step = sys.argv[1]
if step in ("all", "chat"):
    chat.deliver_local(wren, {"user": "Priya", "message": "chat one"})
if step in ("all", "peer"):
    try:
        peer_inbound.accept(root, identity="kestrel", display="Kestrel", to="wren",
                            message="peer one", msg_id="m-nightly-0001",
                            sent_at=time.time(), allowed=lambda slug: True)
    except peer_inbound.Refused as err:
        if err.status not in (409,):
            raise
if step in ("all", "tick"):
    schedule.tick(deliver=schedule._default_deliver)
if step == "all":
    jobs.jobs_main(["start", "shell", "--json", "--", "nightly", "true"])
if step in ("all", "runner"):
    sys.exit(runner_main.runner_main(["--home", str(home), "--once"]))
"""


def setup(root):
    (root / "config").mkdir(parents=True)
    home = root / "cousins" / "wren"
    for sub in ("data", "run", "memory"):
        (home / sub).mkdir(parents=True)
    (home / "cousin.toml").write_text(
        '[cousin]\nslug = "wren"\nname = "Wren"\n\n[agent]\nrunner = "fake"\n')
    (root / "data").mkdir(exist_ok=True)
    env = environ(root)
    code = ("from cousin_lib import schedule; c = schedule._db();"
            " c.execute(\"INSERT INTO scheduled_jobs (cousin, target_ts, prompt, created_at)"
            " VALUES ('wren', 1000000000, 'water the tins', 999990000)\"); c.commit()")
    subprocess.run([sys.executable, "-c", code], env=env, check=True, timeout=60)
    return home


def environ(root, crash=None):
    env = dict(os.environ, FRAMEWORK_ROOT=str(root), COUSIN_HOME=str(root / "cousins" / "wren"),
               PYTHONPATH=str(ROOT))
    env.pop("COUSIN_CRASH_AT", None)
    env.pop("COUSIN_CRASH_MARK", None)
    if crash:
        # every process that inherits it (a job's own runner too) names the
        # point in the mark before it dies: a round can tell it fired
        env["COUSIN_CRASH_AT"] = crash
        env["COUSIN_CRASH_MARK"] = str(root / "crash-mark")
    return env


def run(root, step, crash=None):
    return subprocess.run([sys.executable, "-c", SCENARIO, step], env=environ(root, crash),
                          capture_output=True, text=True, timeout=180)


def recover(root):
    """What a restart and a sender's retry do, none of it killed: the
    loops daemon's chat sweep and one-shot tick, the peer's retry of an
    unconfirmed send, the job reaper, then the runner draining the inbox."""
    from_env = environ(root)
    sweep = ("import os, pathlib; from cousin_lib.config import CousinConfig;"
             " from cousin_lib.server import chat_api;"
             " chat_api.redeliver_pending(CousinConfig.load(pathlib.Path(os.environ['COUSIN_HOME'])),"
             " older_than_s=0)")
    out = []
    out.append(subprocess.run([sys.executable, "-c", sweep], env=from_env,
                              capture_output=True, text=True, timeout=60))
    out.append(run(root, "peer"))
    out.append(run(root, "tick"))
    # a minute on: a row registered and never forked is past its spawn
    # grace (jobs.SPAWN_GRACE_S), which the reaper waits out
    jobs_db = root / "data" / "jobs.db"
    if jobs_db.exists():
        conn = sqlite3.connect(jobs_db)
        conn.execute("UPDATE jobs SET started_at='2026-01-01T00:00:00+00:00'"
                     " WHERE status='running' AND pid IS NULL")
        conn.commit()
        conn.close()
    reap = "from cousin_lib import jobs; jobs.reap_lost()"
    end = time.monotonic() + 30
    while time.monotonic() < end:
        subprocess.run([sys.executable, "-c", reap], env=from_env, capture_output=True,
                       timeout=60)
        if not [r for r in _rows(root / "data" / "jobs.db", "SELECT status FROM jobs")
                if r[0] == "running"]:
            break
        time.sleep(0.2)
    out.append(run(root, "runner"))
    return [p for p in out if p.returncode != 0]


def _rows(path, sql):
    if not path.exists():
        return []
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def check(root):
    """The invariants; a list of what broke them (empty: held)."""
    home = root / "cousins" / "wren"
    broke = []
    chat = _rows(home / "data" / "chat.db",
                 "SELECT message, delivery FROM messages WHERE type='user'")
    for text in ("chat one", "peer one"):
        n = sum(1 for m, _ in chat if m == text)
        if n != 1:
            broke.append("chat.db holds %r %d times" % (text, n))
    if any(d is not None for _, d in chat):
        broke.append("a chat row is still pending: %r" % chat)
    inbox = _rows(home / "data" / "inbox.db", "SELECT source, body, state FROM inbox")
    for text in ("chat one", "peer one"):
        n = sum(1 for s, b, _ in inbox if s == "chat" and b == text)
        if n != 1:
            broke.append("the inbox holds %r %d times" % (text, n))
    if sum(1 for s, _b, _ in inbox if s == "schedule") != 1:
        broke.append("the one-shot is in the inbox %d times"
                     % sum(1 for s, _b, _ in inbox if s == "schedule"))
    open_rows = [(s, b[:30], st) for s, b, st in inbox if st != "done"]
    if open_rows:
        broke.append("rows not answered: %r" % open_rows)
    # answered once: every inbox row is named by exactly one turn's result
    answered = {}
    for path in (home / "data" / "stream").glob("*.jsonl"):
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("kind") == "result":
                for i in (event.get("payload") or {}).get("inbox_ids") or ():
                    answered[i] = answered.get(i, 0) + 1
    ids = [r[0] for r in _rows(home / "data" / "inbox.db",
                               "SELECT id FROM inbox WHERE outcome='delivered'"
                               " AND detail NOT LIKE 'closed at restart%'")]
    twice = {i: n for i, n in answered.items() if n > 1}
    if twice:
        broke.append("rows answered more than once: %r" % twice)
    unanswered = [i for i in ids if i not in answered]
    if unanswered:
        broke.append("rows closed with no result naming them: %r" % unanswered)
    fired = _rows(root / "data" / "scheduled.db", "SELECT status FROM scheduled_jobs")
    if fired != [("fired",)]:
        broke.append("the one-shot reads %r" % fired)
    running = _rows(root / "data" / "jobs.db", "SELECT id FROM jobs WHERE status='running'")
    if running:
        broke.append("jobs still running: %r" % running)
    return broke


def pick(seed, n):
    rnd = random.Random("%s:%s" % (seed, n))
    point = rnd.choice(sorted(POINTS))
    return point, rnd.randint(1, POINTS[point])


def one_round(seed, n):
    point, hit = pick(seed, n)
    return dict(one_round_at("%s:%d" % (point, hit)), seed=seed, round=n)


def one_round_at(crash):
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        setup(root)
        first = run(root, "all", crash=crash)
        mark = root / "crash-mark"
        fired = mark.read_text().split()[0] if mark.exists() else None
        failed = recover(root)
        broke = check(root)
        if fired is None:
            broke.append("the point never fired: the round proved nothing")
        for p in failed:
            broke.append("a recovery step failed (%d): %s" % (p.returncode, p.stderr[-400:]))
        return {"crash_at": crash, "killed": first.returncode == -9, "fired": fired,
                "broke": broke}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="crash_nightly")
    parser.add_argument("--seed", required=True)
    parser.add_argument("--rounds", type=int, default=20)
    parser.add_argument("--round", type=int, help="replay this one round only")
    args = parser.parse_args(argv)
    rounds = [args.round] if args.round is not None else range(args.rounds)
    bad = 0
    for n in rounds:
        out = one_round(args.seed, n)
        bad += bool(out["broke"])
        print(json.dumps(out), flush=True)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
