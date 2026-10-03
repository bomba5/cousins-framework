"""cousin-upgrade --switch and --restart: the code moved to the target
release, and every process restarted on it in order, each checked by the
version it comes back on (docs/commands.md, "cousin-upgrade").

The switch, once the plan is computed (upgrade.build_plan, after a
`git fetch --tags` unless --no-fetch) and confirmed:

1. Refused before anything moves (exit 2): tracked changes in the
   checkout; a dependency change without --deps; a venv whose python
   does not import cousin_lib from the checkout being upgraded.
2. data/upgrade.json records FROM (version, commit, branch) and TO.
3. `git checkout --detach <TO commit>` in that checkout. A branch HEAD
   was on is left where it was, so FROM stays one checkout away.
4. `<python> -m pip install --no-deps -e <checkout>` (with --deps, pip
   also installs the dependencies) into the venv this command runs in.
5. A fresh interpreter of that venv must import cousin_lib at TO's
   version from the checkout.
6. The restarts, unless --no-restart (restart_all).

The restarts, in the plan's order (loops, console, each runner, the
caller's own runner last), each through the supervisor (`stop` then
`start`; a runner's Telegram bridge stops and starts with it):

- A process that already says it runs TO (version.announce: its pid,
  version and commit, under run/versions/) is left alone, so a second
  run does only what is left.
- A runner mid-turn (its stream's last `state` is running,
  waiting_permission, rate_limited or rolling_over) is waited for, up
  to --idle-timeout (600 s); still busy, it is `pending`: recorded in
  data/upgrade.json, and `cousin-upgrade --restart` does it later.
- A child held down (`stopped`) or `failing` is left as it is: it runs
  the new code when it is started. One this command stopped itself (a
  run cut short between stop and start) is started.
- After `start`, the child must be `running` on a new pid that says it
  runs TO (version, commit, checkout) within --verify-timeout (90 s).
  A target that does not announce (older than this release) is checked
  by the new pid only.
- The first restart that fails stops the run: the rest are `not
  reached`, and the report says how to roll back.
- No supervisor, or a process that is not its child (its own unit), is
  `by hand`.
- The caller's own runner (COUSIN_HOME names a cousin of this root) is
  restarted detached, after this call returns: `systemd-run --user`
  when there is a user manager, else a process in its own session,
  running `<python> -m cousin_lib.upgrade --restart --only runner:<slug>
  --yes` with absolute paths, FRAMEWORK_ROOT and the venv's bin on PATH
  set explicitly (a transient unit inherits neither) and COUSIN_HOME
  empty (it is no cousin's call). It waits for the caller's turn to end
  like any busy runner, then records its result in data/upgrade.json.
"""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from cousin_lib import upgrade, version

STATE = "data/upgrade.json"
LOG = "data/upgrade-restart.log"
BUSY = ("running", "waiting_permission", "rate_limited", "rolling_over")
IDLE_TIMEOUT_S = 600.0
VERIFY_TIMEOUT_S = 90.0
STOP_WAIT_S = 65.0       # a runner's stop: its 35 s budget, the supervisor's 60 s answer wait
POLL_S = 1.0
UNIT_PREFIX = "cousin-upgrade-restart-"
# The --restart this module's CLI takes. A target whose copy of this file
# names it can restart its caller detached; an older one cannot.
RESTART_PROTOCOL = 1
# What a fresh interpreter imports: run from / so the working directory
# (a checkout, perhaps) is not what `import cousin_lib` finds.
PROBE = ("import json; from cousin_lib import version as v; print(json.dumps("
         "{'version': v.read_version(), 'checkout': str(v.CHECKOUT),"
         " 'commit': v.git_commit()}))")
# The outcomes a row can have; the last three leave something for a person.
DONE = ("done", "already", "skipped", "detached")
LEFT = ("pending", "by hand", "not reached")


class SwitchError(Exception):
    """A step could not be done; the message says why."""


# ------------------------------------------------------------- the world

class Ops:
    """Everything the switch and the restarts touch outside this
    process, one method each, so a test hands in a fake."""

    def __init__(self, root, checkout, python=None):
        self.root = Path(root)
        self.checkout = Path(checkout)
        self.python = python or sys.executable

    def git(self, *args, timeout=120):
        """(returncode, stdout, stderr) of one git command in the
        checkout."""
        try:
            r = subprocess.run(["git", "-C", str(self.checkout), *args],
                               capture_output=True, text=True,
                               timeout=timeout, check=False)
        except (OSError, subprocess.SubprocessError) as err:
            return 1, "", str(err)
        return r.returncode, r.stdout, r.stderr

    def pip_install(self, deps):
        """(returncode, the output's last lines) of pip_argv."""
        try:
            r = subprocess.run(pip_argv(self.python, self.checkout, deps),
                               capture_output=True, text=True, timeout=1800,
                               check=False, cwd="/")
        except (OSError, subprocess.SubprocessError) as err:
            return 1, str(err)
        out = (r.stdout + r.stderr).strip().splitlines()
        return r.returncode, "\n".join(out[-5:])

    def probe(self):
        """{"version", "checkout", "commit"} as a fresh interpreter of
        this venv imports cousin_lib; SwitchError when it cannot."""
        try:
            r = subprocess.run([self.python, "-c", PROBE], capture_output=True,
                               text=True, timeout=60, check=False, cwd="/")
        except (OSError, subprocess.SubprocessError) as err:
            raise SwitchError("%s cannot run: %s" % (self.python, err))
        if r.returncode != 0:
            tail = (r.stderr.strip().splitlines() or ["exit %d" % r.returncode])
            raise SwitchError("%s cannot import cousin_lib: %s"
                              % (self.python, tail[-1]))
        try:
            return json.loads(r.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            raise SwitchError("%s said %r, not what it imports"
                              % (self.python, r.stdout.strip()))

    def supervisor(self, op, timeout=10.0, **args):
        from cousin_lib import supervisor
        return supervisor.request(self.root, op, timeout=timeout, **args)

    def runner_state(self, home):
        """The runner's turn state (its stream's last `state`), None when
        no runner is alive on the home."""
        from cousin_lib.runner import status
        found = status.status(home)
        return found["state"] if found["alive"] else None

    def head(self):
        """(commit, version) the checkout holds now: HEAD and its
        working tree's pyproject version, which a process started now
        runs."""
        rc, out, _err = self.git("rev-parse", "HEAD")
        found = version.pyproject_version(self.checkout / "pyproject.toml")
        return (out.strip() if rc == 0 else None), found

    def announced(self, name):
        return version.announced(self.root, name)

    def alive(self, pid):
        try:
            os.kill(int(pid), 0)
        except PermissionError:
            return True
        except (OSError, TypeError, ValueError):
            return False
        return True

    def spawn_detached(self, argv, env, unit):
        """Start `argv` outside this process tree with `env` on top of a
        clean environment; a line saying how. A transient user unit when
        a user manager answers, else a process in its own session with
        its output in data/upgrade-restart.log."""
        if shutil.which("systemd-run") and os.environ.get("XDG_RUNTIME_DIR"):
            cmd = (["systemd-run", "--user", "--unit", unit, "--collect",
                    "--quiet", "--working-directory", str(self.root)]
                   + ["--setenv=%s=%s" % kv for kv in sorted(env.items())]
                   + list(argv))
            try:
                r = subprocess.run(cmd, capture_output=True, text=True,
                                   timeout=30, check=False)
            except (OSError, subprocess.SubprocessError):
                r = None
            if r is not None and r.returncode == 0:
                return "the user unit %s (journalctl --user -u %s)" % (unit,
                                                                      unit)
            if r is not None and "already" in (r.stderr or ""):
                return "the user unit %s, already pending" % unit
        log = self.root / LOG
        log.parent.mkdir(parents=True, exist_ok=True)
        full = dict(os.environ)
        full.update(env)
        with open(log, "ab") as out:
            proc = subprocess.Popen(list(argv), cwd=str(self.root), env=full,
                                    stdin=subprocess.DEVNULL, stdout=out,
                                    stderr=subprocess.STDOUT,
                                    start_new_session=True, close_fds=True)
        return "process %d in its own session (output in %s)" % (proc.pid, LOG)

    def now(self):
        return time.monotonic()

    def sleep(self, seconds):
        time.sleep(seconds)

    def wall(self):
        return time.time()


def pip_argv(python, checkout, deps):
    return [str(python), "-m", "pip", "install", "--quiet"] \
        + ([] if deps else ["--no-deps"]) + ["-e", str(checkout)]


# ------------------------------------------------------------ the record

def read_state(root):
    try:
        data = json.loads((Path(root) / STATE).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_state(root, state):
    upgrade._write(Path(root) / STATE, json.dumps(state, indent=2) + "\n")


def rollback(plan, python, deps=False):
    """How to put FROM back: {"command", "by_hand": [lines]}."""
    f, checkout = plan["from"], plan["checkout"]
    sha = f.get("sha") or f["commit"]
    to_from = ("checkout %s" % f["branch"]) if f.get("branch") \
        else "checkout --detach %s" % sha
    return {"command": "cousin-upgrade --switch --to %s%s --yes"
                       % (sha[:12], " --deps" if plan["dependencies"] else ""),
            "by_hand": ["git -C %s %s" % (checkout, to_from),
                        " ".join(pip_argv(python, checkout, deps)),
                        "cousin-upgrade --restart --yes (or: systemctl"
                        " --user restart cousin-supervisor.service)"]}


# ------------------------------------------------------------ restarting

def _row(name, status, detail, **extra):
    return dict(extra, name=name, status=status, detail=detail)


def _target(name):
    """The supervisor's address for a child: a runner by slug, the
    console and the loops daemon by name."""
    if name.startswith("runner:"):
        return {"slug": name.split(":", 1)[1]}
    return {"name": name}


def mismatch(said, expect):
    """Why what a process said it runs is not `expect` ({"version",
    "sha", "checkout"}), or None."""
    if said.get("version") != expect["version"]:
        return "version %s, not %s" % (said.get("version"), expect["version"])
    commit = said.get("commit")
    if commit and expect.get("sha") and not expect["sha"].startswith(commit):
        return "commit %s, not %s" % (commit, expect["sha"][:12])
    where = said.get("checkout")
    if where and expect.get("checkout") and \
            os.path.realpath(where) != os.path.realpath(expect["checkout"]):
        return "code from %s, not %s" % (where, expect["checkout"])
    return None


def _on_target(ops, name, pid, expect):
    """The process `name` says it runs `expect`, on `pid` (the
    supervisor's) or, with no pid known, on a live one."""
    said = ops.announced(name)
    if not said or not said.get("pid"):
        return None
    if pid is not None and said["pid"] != pid:
        return None
    if pid is None and not ops.alive(said["pid"]):
        return None
    return None if mismatch(said, expect) else said


def wait_idle(ops, home, timeout):
    """None once the runner on `home` is not mid-turn, else the state it
    is still in when `timeout` runs out."""
    deadline = ops.now() + timeout
    while True:
        state = ops.runner_state(home)
        if state not in BUSY:
            return None
        if ops.now() >= deadline:
            return state
        ops.sleep(POLL_S)


def verify_restart(ops, name, old_pid, expect, *, verify, timeout):
    """The row for a child just started: `done` once it runs on a new pid
    that says it runs `expect`, `failed` when it says otherwise or is
    not there within `timeout`."""
    from cousin_lib.supervisor import SupervisorUnavailable
    deadline = ops.now() + timeout
    last = "no status"
    while True:
        try:
            child = (ops.supervisor("status", timeout=5.0).get("children")
                     or {}).get(name)
        except SupervisorUnavailable as err:
            child, last = None, str(err)
        pid = (child or {}).get("pid")
        if child and child.get("state") == "running" and pid \
                and pid != old_pid:
            if verify != "version":
                return _row(name, "done", "pid %d (the target does not say"
                            " its version: checked by its new pid only)"
                            % pid, pid=pid)
            said = ops.announced(name)
            if said and said.get("pid") == pid:
                bad = mismatch(said, expect)
                if bad:
                    return _row(name, "failed", "came back on %s" % bad,
                                pid=pid)
                return _row(name, "done", "pid %d runs %s (%s)" % (
                    pid, said["version"], said.get("commit") or "no commit"),
                    pid=pid)
            last = "pid %d has not said which release it runs" % pid
        elif child:
            last = "%s%s" % (child.get("state"), ": %s" % child["reason"]
                             if child.get("reason") else "")
        if ops.now() >= deadline:
            return _row(name, "failed", "not back on %s within %gs (%s)"
                        % (expect["version"], timeout, last))
        ops.sleep(POLL_S)


def restart_one(ops, row, expect, *, verify, idle_timeout, verify_timeout,
                recorded=None, mark=None):
    """Restart one child of the plan's order through the supervisor and
    check it; the row of what happened."""
    from cousin_lib.supervisor import SupervisorUnavailable
    name = row["name"]
    runner = name.startswith("runner:")
    try:
        children = ops.supervisor("status", timeout=5.0).get("children") or {}
    except SupervisorUnavailable as err:
        said = _on_target(ops, name, None, expect)
        if said:
            return _row(name, "already", "runs %s (pid %d)"
                        % (said["version"], said["pid"]))
        return _row(name, "by hand", "no supervisor answers (%s): restart"
                    " it by hand; `cousin-upgrade --restart` then checks it"
                    % err)
    child = children.get(name)
    if child is None:
        said = _on_target(ops, name, None, expect)
        if said:
            return _row(name, "already", "runs %s (pid %d), not a"
                        " supervisor child" % (said["version"], said["pid"]))
        if runner:
            return _row(name, "skipped", "not a supervisor child"
                        " (auto_start = false, held, another lane): it runs"
                        " the new code at its next start")
        return _row(name, "by hand", "not a supervisor child (its own"
                    " unit?): restart it by hand; `cousin-upgrade --restart`"
                    " then checks it")
    said = _on_target(ops, name, child.get("pid"), expect)
    if said:
        return _row(name, "already", "runs %s (pid %d)"
                    % (said["version"], said["pid"]))
    ours = (recorded or {}).get("status") == "stopping"
    if child.get("state") in ("stopped", "failing") and not ours:
        return _row(name, "skipped", "%s%s: left as it is; it runs the new"
                    " code when started" % (child["state"], " (%s)"
                                            % child["reason"]
                                            if child.get("reason") else ""))
    if runner and child.get("pid"):
        home = ops.root / "cousins" / name.split(":", 1)[1]
        busy = wait_idle(ops, home, idle_timeout)
        if busy:
            return _row(name, "pending", "mid-turn (%s) after %gs: recorded;"
                        " `cousin-upgrade --restart` restarts it once idle"
                        % (busy, idle_timeout))
    old = child.get("pid")
    if mark is not None:
        mark(name, _row(name, "stopping", "stopped by cousin-upgrade to"
                        " restart it", pid=old))
    try:
        if old:
            answer = ops.supervisor("stop", timeout=STOP_WAIT_S,
                                    wait=True, by="cousin-upgrade",
                                    **_target(name))
            if not answer.get("ok"):
                return _row(name, "failed", "stop refused: %s"
                            % answer.get("error"))
        answer = ops.supervisor("start", timeout=30.0, **_target(name))
    except SupervisorUnavailable as err:
        return _row(name, "failed", "%s; it may be down: cousin-supervisor"
                    " start %s" % (err, _start_args(name)))
    if not answer.get("ok"):
        return _row(name, "failed", "start: %s; it is down: cousin-supervisor"
                    " start %s" % (answer.get("error") or answer.get("state"),
                                   _start_args(name)))
    return verify_restart(ops, name, old, expect, verify=verify,
                          timeout=verify_timeout)


def _start_args(name):
    target = _target(name)
    return target["slug"] if "slug" in target else "--name %s" % name


def detached_argv(ops, name, idle_timeout):
    return [str(ops.python), "-m", "cousin_lib.upgrade", "--restart",
            "--only", name, "--yes", "--root", str(ops.root),
            "--checkout", str(ops.checkout),
            "--idle-timeout", "%g" % idle_timeout]


def detached_env(ops):
    """What a detached restart needs and a transient unit would not
    have: FRAMEWORK_ROOT, the venv's bin first on PATH, and COUSIN_HOME
    empty (it is no cousin's call, so it never detaches again)."""
    bin_dir = str(Path(ops.python).parent)
    path = os.environ.get("PATH") or os.defpath
    return {"FRAMEWORK_ROOT": str(ops.root),
            "PATH": bin_dir + os.pathsep + path,
            "COUSIN_HOME": "", "PYTHONUNBUFFERED": "1"}


def restart_detached(ops, row, *, can, idle_timeout):
    """The caller's own runner: restarted after this call returns."""
    name = row["name"]
    slug = name.split(":", 1)[1]
    by_hand = ("once this turn is over: cousin-supervisor stop %s, then"
               " cousin-supervisor start %s" % (slug, slug))
    if not can:
        return _row(name, "by hand", "the target cannot restart its caller"
                    " detached; " + by_hand)
    try:
        how = ops.spawn_detached(detached_argv(ops, name, idle_timeout),
                                 detached_env(ops), UNIT_PREFIX + slug)
    except (OSError, subprocess.SubprocessError) as err:
        return _row(name, "by hand", "a detached restart did not start"
                    " (%s); %s" % (err, by_hand))
    return _row(name, "detached", "after this turn, by %s; %s records the"
                " result" % (how, STATE))


def restart_all(ops, order, expect, *, verify, can_detach, idle_timeout,
                verify_timeout, state, save):
    """Every row of `order`, in order; the first failure stops the run.
    Each result is recorded in state["restarts"] and saved at once."""
    rows, failed = [], False
    recorded = state.setdefault("restarts", {})

    def mark(name, row):
        recorded[name] = dict(row, at=int(ops.wall()))
        save()

    for row in order:
        name = row["name"]
        if failed:
            out = _row(name, "not reached", "an earlier restart failed")
        elif row.get("detached"):
            out = restart_detached(ops, row, can=can_detach,
                                   idle_timeout=idle_timeout)
        else:
            out = restart_one(ops, row, expect, verify=verify,
                              idle_timeout=idle_timeout,
                              verify_timeout=verify_timeout,
                              recorded=recorded.get(name), mark=mark)
        mark(name, out)
        rows.append(out)
        failed = failed or out["status"] == "failed"
    return rows


# ------------------------------------------------------------ switching

def expect_of(plan):
    t = plan["to"]
    return {"version": t["version"], "sha": t["sha"],
            "checkout": plan["checkout"]}


def preflight(plan, ops, *, deps=False):
    """Why the switch is refused before anything moves, or None: tracked
    changes, a dependency change without --deps, a venv that does not
    import cousin_lib from this checkout."""
    if plan["dirty"] and not at_target(plan):
        return ("the checkout has tracked changes (%s): commit or drop them"
                " first; nothing switched" % ", ".join(plan["dirty"]))
    if plan["dependencies"] and not deps and not at_target(plan):
        return ("the dependencies changed (%s): install them first, or pass"
                " --deps to let pip fetch them; nothing switched"
                % "; ".join(plan["dependencies"]))
    try:
        found = ops.probe()
    except SwitchError as err:
        return "%s; nothing switched" % err
    if os.path.realpath(found.get("checkout") or "") != \
            os.path.realpath(plan["checkout"]):
        return ("%s imports cousin_lib from %s, not from the checkout being"
                " upgraded (%s); nothing switched"
                % (ops.python, found.get("checkout"), plan["checkout"]))
    return None


def at_target(plan):
    return (plan["from"].get("sha") or "") == plan["to"]["sha"]


def run_switch(plan, ops, *, deps=False, restart=True,
               idle_timeout=IDLE_TIMEOUT_S, verify_timeout=VERIFY_TIMEOUT_S):
    """Move the checkout to TO, reinstall, check, restart; the result:
    {"steps": [rows], "restarts": [rows], "rollback", "outcome":
    "done" | "left" | "failed"}. Every step is recorded in
    data/upgrade.json as it goes."""
    t, f = plan["to"], plan["from"]
    state = {"from": {"version": f["version"], "ref": f["ref"],
                      "sha": f.get("sha"), "branch": f.get("branch")},
             "to": {"version": t["version"], "ref": t["ref"], "sha": t["sha"]},
             "checkout": plan["checkout"], "at": int(ops.wall()),
             "stage": "switching", "restarts": {},
             "rollback": rollback(plan, ops.python, deps)}
    previous = read_state(ops.root)
    if previous.get("to", {}).get("sha") == t["sha"]:
        # the same switch again: what it did stands (a runner it stopped
        # is started), and once at TO, its FROM is still the one to go
        # back to (the plan's would be TO itself)
        state["restarts"] = previous.get("restarts") or {}
        if at_target(plan):
            state["from"] = previous.get("from") or state["from"]
            state["rollback"] = previous.get("rollback") or state["rollback"]
    result = {"steps": [], "restarts": [], "rollback": state["rollback"],
              "outcome": "failed"}

    def save():
        try:
            write_state(ops.root, state)
        except OSError as err:
            result.setdefault("warnings", []).append(
                "%s not written: %s" % (STATE, err))

    def step(name, status, detail):
        result["steps"].append({"step": name, "status": status,
                                "detail": detail})
        if status == "failed":
            state["stage"] = "failed: %s: %s" % (name, detail)
            save()
        return status != "failed"

    save()
    if at_target(plan):
        step("checkout", "already", "HEAD is %s" % t["commit"])
    else:
        rc, _out, err = ops.git("checkout", "--quiet", "--detach", t["sha"])
        if rc != 0:
            step("checkout", "failed", "git checkout: %s; nothing moved"
                 % (err.strip() or "exit %d" % rc))
            return result
        rc, out, err = ops.git("rev-parse", "HEAD")
        if rc != 0 or out.strip() != t["sha"]:
            step("checkout", "failed", "HEAD is %s, not %s"
                 % (out.strip()[:12] or err.strip(), t["commit"]))
            return result
        step("checkout", "done", "HEAD detached at %s (%s)%s" % (
            t["ref"], t["commit"], "; the branch %s stays at %s"
            % (f["branch"], (f.get("sha") or "")[:12]) if f.get("branch")
            else ""))
    rc, out = ops.pip_install(deps)
    if rc != 0:
        step("install", "failed", "pip: %s" % (out or "exit %d" % rc))
        return result
    step("install", "done", " ".join(pip_argv(ops.python, ops.checkout, deps)))
    try:
        found = ops.probe()
    except SwitchError as err:
        step("check", "failed", str(err))
        return result
    expect = expect_of(plan)
    bad = mismatch(found, expect)
    if bad:
        step("check", "failed", "a fresh %s imports %s" % (ops.python, bad))
        return result
    step("check", "done", "a fresh interpreter imports %s from %s"
         % (found["version"], found["checkout"]))
    state["stage"] = "switched"
    save()
    if not restart:
        result["outcome"] = "left"
        state["stage"] = "switched; restarts not done (--no-restart)"
        save()
        return result
    state["stage"] = "restarting"
    result["restarts"] = restart_all(
        ops, plan["restarts"]["order"], expect,
        verify=plan["switch"]["verify"],
        can_detach=plan["switch"]["self_restart"],
        idle_timeout=idle_timeout, verify_timeout=verify_timeout,
        state=state, save=save)
    result["outcome"] = outcome(result["restarts"])
    state["stage"] = result["outcome"]
    save()
    return result


def outcome(rows):
    if any(r["status"] == "failed" for r in rows):
        return "failed"
    if any(r["status"] in LEFT for r in rows):
        return "left"
    return "done"


def run_restarts(root, checkout, order, ops, *, idle_timeout=IDLE_TIMEOUT_S,
                 verify_timeout=VERIFY_TIMEOUT_S):
    """`cousin-upgrade --restart`: the restarts alone, against what the
    checkout holds now (its pyproject version, its HEAD), resuming
    data/upgrade.json's record when it is for the same commit."""
    sha, found = ops.head()
    if not sha or not found:
        raise SwitchError("%s has no readable HEAD or version" % checkout)
    expect = {"version": found, "sha": sha, "checkout": str(checkout)}
    state = read_state(root)
    if state.get("to", {}).get("sha") != sha:
        state = {"to": {"version": found, "ref": "HEAD", "sha": sha},
                 "checkout": str(checkout), "restarts": {}}
    state["at"] = int(ops.wall())
    state["stage"] = "restarting"

    def save():
        write_state(root, state)

    rows = restart_all(ops, order, expect, verify="version", can_detach=True,
                       idle_timeout=idle_timeout,
                       verify_timeout=verify_timeout, state=state, save=save)
    state["stage"] = outcome(rows)
    save()
    return {"expect": expect, "restarts": rows, "outcome": state["stage"],
            "rollback": state.get("rollback")}


# -------------------------------------------------------------- report

def render_switch_plan(plan, *, deps=False, fetched=None, restart=True):
    """The switch's steps as the plan shows them (nothing done yet);
    `fetched` None for a dry-run, else whether --switch fetched."""
    t, f = plan["to"], plan["from"]
    sw = plan["switch"]
    out = ["code switch (%s):" % ("--switch; none done now" if fetched is None
                                  else "after the question")]
    say = out.append
    say("  0. git fetch --tags: %s" % {
        None: "not for a plan (--switch fetches first, --no-fetch skips it)",
        True: "done", False: "skipped (--no-fetch)"}[fetched])
    if at_target(plan):
        say("  1. checkout: HEAD is already %s" % t["commit"])
    else:
        refused = ""
        if plan["dirty"]:
            refused = "  [refused now: tracked changes]"
        say("  1. git checkout --detach %s (%s)%s" % (t["ref"], t["commit"],
                                                      refused))
    pip = " ".join(pip_argv(sw["python"], plan["checkout"], deps))
    refused = "  [refused now: dependencies changed, --deps installs them]" \
        if plan["dependencies"] and not deps and not at_target(plan) else ""
    say("  2. %s%s" % (pip, refused))
    say("  3. check: a fresh %s imports cousin_lib %s from %s"
        % (sw["python"], t["version"], plan["checkout"]))
    say("  4. %s" % ("the restarts below, in order" if restart
                     else "no restarts (--no-restart)"))
    rb = rollback(plan, sw["python"], deps)
    say("rollback: %s" % rb["command"])
    say("  by hand: %s" % "; ".join(rb["by_hand"][:2]))
    if f.get("branch"):
        say("  (HEAD is on the branch %s now; the switch detaches it and"
            " leaves the branch where it is)" % f["branch"])
    return out


def render_rows(rows, title):
    out = [title]
    for i, row in enumerate(rows, 1):
        out.append("  %d. %-20s %-12s %s" % (i, row["name"], row["status"],
                                             row["detail"]))
    return out


def render_result(plan, result):
    out = ["", "switch to %s (%s, %s):" % (plan["to"]["version"],
                                           plan["to"]["ref"],
                                           plan["to"]["commit"])]
    for s in result["steps"]:
        out.append("  %-9s %-8s %s" % (s["step"], s["status"], s["detail"]))
    if result["restarts"]:
        out.extend(render_rows(result["restarts"], "restarts:"))
    out.extend(result.get("warnings") or [])
    out.extend(_closing(result))
    return "\n".join(out) + "\n"


def _closing(result):
    rb = result.get("rollback") or {}
    if result["outcome"] == "failed":
        out = ["", "FAILED. The rest was not done. To roll back:",
               "  %s" % rb.get("command")]
        out += ["  or by hand:"] + ["    %s" % x for x in rb.get("by_hand") or []]
        return out
    if result["outcome"] == "left":
        return ["", "left for a person: the rows above marked pending, by"
                " hand or not reached; `cousin-upgrade --restart` does what"
                " is left (%s records it)" % STATE]
    if rb.get("command"):
        return ["", "done; to roll back: %s" % rb["command"]]
    return ["", "done"]


def render_restarted(result):
    e = result["expect"]
    out = ["", "restarts on %s (%s):" % (e["version"], e["sha"][:12])]
    out.extend(render_rows(result["restarts"], "")[1:])
    out.extend(_closing(result))
    return "\n".join(out) + "\n"
