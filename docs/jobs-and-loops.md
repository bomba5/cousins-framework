# Jobs, loops and schedules

Four ways to keep track of work and to have it happen on time: jobs for
what's running right now, loops for recurring prompts (the heartbeat is
one), one-shot schedules for "remind me in 30 minutes", and the tracker
for longer threads of work. They all keep their state in files under
the framework root, so nothing is lost if the console or the loops
daemon restarts.

## Jobs

A job is a row saying "this is running, this is who started it, here's
the log". The store is `data/jobs.db` under the framework root, shared
by every [cousin](glossary.md#cousin), and the console's Jobs view reads it.

Register work you're doing yourself, then close it:

```
cousin-job start subagent "map the auth module" --desc "for the refactor"
#   -> 14
cousin-job done 14 "mapped; notes in notes/auth.md"
cousin-job fail 14 "gave up, module is generated" --exit 1
cousin-job cancel 14 "not needed any more"
```

Or hand it a command and let it run in the background:

```
cousin-job start shell "rebuild the index" -- cousin-memory reindex
#   -> 15
cousin-job tail 15 -f
```

With a command after `--`, the job runs detached, writes its output to
`data/job-logs/job-<id>.log` under the framework root (or wherever
`--log` says), and closes itself as `done` or `failed` with the
command's exit code. You don't call `done` for those. A job started
without a command still gets a log in the same place: its title and
`--desc` at start, its outcome and summary at `done`, `fail` or
`cancel`. Don't point
`--log` at a file the command itself reads, or it will read its own
output forever. `--home-log REL` is `--log` confined to the cousin's
home: it's refused, with no row made, when it's absolute, starts with
`~`, climbs out with `..` or lands in `.secrets`. The options can also go before
`--`, with the title after it: `cousin-job start shell --json --
"--weird title" CMD...`. In that shape nothing after `--` is ever read
as an option, so any title stays a title and the command can't set
`--log`; it's the shape the `job` tool uses. The title-first shape
(`start shell TITLE --log L -- CMD`) still takes its options after the
title, as before. Either way, a command whose program starts with `-`
is refused.

A cousin does the same through its `job` tool without a shell: `run`
with `title` and `argv` (the command as an array, never a shell string),
and optionally `desc` and `log` (relative to its home, passed as
`--home-log`). It's this same launcher, run from the cousin's home, and it answers at once with the
job id and the log path ([MCP tools](mcp.md#what-a-cousin-gets)). The
tool's `start` refuses `shell`, since it takes no command and the row
would never close. The row keeps the command line as given, secrets
included, so don't put one in the command.

Looking at jobs:

```
cousin-job list                  # the newest 100, newest first
cousin-job list --active         # still running
cousin-job list --mine           # started by this cousin
cousin-job list --status failed
cousin-job show 15 --json
cousin-job tail 15 --lines 100
```

Kinds are `subagent`, `shell`, `build` and `other`; media generation
records its own `media` rows ([media](media.md)). Statuses are
`running`, `done`, `failed` and `cancelled`. `start --json` prints
`{"job_id": ..., "log_path": ...}` for scripts.

A shell job's command runs in its own process group, and so does
everything it starts, including a child that outlives the wrapper (an
`ssh host tail -F`, a server started with `&`). Closing the job, with
`cancel`, `done` or `fail` or from the console, ends that group: SIGTERM,
then SIGKILL after 3 seconds for anything left. A finished job whose group
still has processes is a leak: `cousin-job list` marks it `LEAK` and
`show` lists the pids, and `cousin-job cancel <id>` ends them. Only
processes in the job's own group that started after the job are ever
signalled; nothing is killed by name.

`cousin-job` needs `COUSIN_HOME` (to know who's asking) and a framework
root (`FRAMEWORK_ROOT`, or a home under `<root>/cousins/`).

Housekeeping happens when the console lists jobs: anything still
`running` after 24 hours is marked failed (its process most likely died
without closing the row), and only the newest 1000 finished rows are
kept, together with their logs in `data/job-logs/`.

### Automatic tracking from Claude Code

Every cousin spawned by the framework has hooks in its
`.claude/settings.json` that record jobs on their own, so a cousin
doesn't have to remember:

- **Subagents.** A call to the `Agent` tool (`Task` in older Claude Code)
  becomes a `subagent` job titled by its description. It closes `done`
  with the start of the answer, or `failed` (`cancelled` if you
  interrupted it). A subagent launched in the background stays running
  until it actually finishes. Its log starts with the prompt; when it
  ends, the hook finds the subagent's own transcript (the harness keeps
  it beside a `.meta.json` naming the launching call) and appends it as
  text: what the agent said, each tool call on one line (`-> Grep: def
  main in src`), the first lines of each result, then the outcome.
- **Background shells.** A `Bash` call with `run_in_background` becomes
  a `shell` job with the command. The hook wraps the command in an exit
  trap, so the row closes with the command's real exit code when it
  ends, and copies its output (stdout and stderr) into the job's log as
  it runs, so the console shows it live; the harness still gets every
  line. If the home or root path has characters the trap can't quote
  safely, the command runs unwrapped and the row is left to the 24-hour
  reap.
- **Media.** An image, voice or video request is a `media` job whose log
  has the provider, the request, and the saved file or the error.

Foreground calls get no job row, but nothing goes unrecorded: every tool
call, by the cousin or one of its subagents, success or failure, is one
line in the cousin's **activity log**, `data/activity/<YYYY-MM-DD>.log`
in its home (local date):

```
21:17:03  Bash         ok    git status --short  # Show working tree status
21:17:05  Edit         ok    <root>/cousins/wren/STATUS.md
21:17:09  Bash         FAIL  make test  -> Exit code 2
21:17:12  Grep         ok    [Explore] def main in src
```

A subagent's lines carry its type in brackets. The hook never blocks or
fails a tool call; its errors go to `data/job-hooks.log` in the cousin
home. Commands are logged as written, one more reason never to put a
secret on a command line.

A cousin spawned before these hooks existed gets them with:

```
cousin-spawn wren --repair-settings
```

## Loops

A loop is a prompt that gets delivered to a cousin's session on a
schedule. Loops live in the cousin's own `cousin.toml`:

```toml
[[loops]]
name = "morning-standup"
daily_at = "09:00"
days = ["mon", "tue", "wed", "thu", "fri"]
prompt = "Post your plan for the day."

[[loops]]
name = "watch-ci"
interval_seconds = 600
prompt = "Check the CI dashboard and flag red builds."

[[loops]]
name = "month-end"
cron = "0 18 28-31 * *"
prompt = "If today is the last day of the month, write the summary."
enabled = false
```

Each loop needs a `name` (lowercase letters, digits, `-` and `_`,
starting with a letter, up to 32 characters, unique in the file), a
`prompt`, and exactly one schedule:

- `interval_seconds`: every N seconds since the last fire.
- `daily_at = "HH:MM"`, optionally with `days` (three-letter weekdays):
  once a day at or after that time.
- `cron`: standard five fields. When both day-of-month and day-of-week
  are set, either one matching is enough, as in real cron.

`enabled = false` turns a loop off without deleting it. A loop with a
problem (two schedules, no prompt, a bad name) is skipped and the
daemon logs why, naming the cousin. You can also edit loops in the
console's Loops view, which writes the same `[[loops]]` tables back.

### The loops daemon

One process fires everything: loops, heartbeats, one-shot schedules,
timed [flips](glossary.md#flip), meeting [turns](glossary.md#turn), and the memory index refresh that keeps every
cousin's search index level with its files (checked every 5 minutes per
cousin, one home at a time, only changed files embedded; each pass that
did work is logged as `cousin-loops: index <slug>: ...`).

```
cousin-loops run                     # the daemon, a tick every 30 s
cousin-loops run --interval 10 --ticks 1
cousin-loops status                  # exit 0 when healthy, 1 when down
cousin-loops fire wren morning-standup
cousin-loops requests
```

`run` takes an exclusive lock on the root (one clock per root): a
one-shot `--ticks 1` run beside an already-running daemon does not
borrow its tick, it exits 5, busy, at once, the same as a second
daemon started by mistake. Stop or wait for the running one first.

`systemd/cousin-loops.service` runs it for you ([operations](operations.md)).
If the daemon isn't running, nothing recurring happens: no heartbeats,
no loops, no one-shots, no timed flips, no index refresh (a search still
refreshes its own index). Chat and every CLI keep working.
`cousin-loops status` and the console both say "loops daemon has never
run" or "loops daemon down (last tick 312s ago)" rather than staying
quiet.

`fire` and the console's fire button don't fire anything themselves.
They write a request that the daemon picks up on its next tick, and
`requests` shows each one as `pending`, `done`, `failed` or `expired`.
A request nobody picks up within 10 minutes is marked expired, which
means the daemon is down or stuck.

What happens on each tick, for every cousin whose [runner](glossary.md#runner) is up (it holds
`run/runner.lock`; a cousin that's down gets nothing and catches up later):

1. Trigger files in the cousin home are delivered (see below).
2. The heartbeat, if it's due, and every due loop are sent together as
   one message. A single item goes as its bare prompt; several go under
   a `[Framework scheduler: N loops due this tick - handle in order]`
   line, one `### name` section each.
3. Each delivery is one row in the cousin's [inbox](glossary.md#inbox),
   and only once that row is stored does the loop count as fired. The
   daemon doesn't wait for the turn. If the row can't be stored, the
   loop stays due and is tried again next tick. So a loop can fire
   twice after a crash; write prompts that don't mind that.

A `daily_at` loop missed while the cousin was down fires once when it's
back, as long as that's still the same day. An interval loop fires once, not once per missed interval. A
`cron` loop only fires in a minute that matches, so a missed cron slot
is skipped.

The daemon's files, all under `data/` in the framework root:
`loops-state.json` (last tick, last fires), `loop-requests.db` and
`loops-fires.jsonl` (one line per fire, which the console uses to show
drift). Loop details are in [reference/loops.md](reference/loops.md).

### Heartbeats

The context heartbeat is built in. Every hour by default the daemon
checks the cousin's `CLAUDE.md`, `STATUS.md` and `MEMORY.md`, pastes any
that changed since the last beat into a prompt, and asks the cousin to
checkpoint with `cousin-memory activity` and reply with one line,
`Heartbeat at HH:MM`. If nothing changed it says so and points at
`cousin-memory search`. Which versions the cousin has seen is kept in
`data/heartbeat-mtimes.json` in the cousin home, updated only after the
beat was delivered.

```toml
[heartbeat]
context_beat_seconds = 3600    # 0 turns the heartbeat off
```

### Trigger files

Anything that can write a file can poke a cousin. Drop a file named
`<name>.ready` in the cousin home and the next tick handles it:

- `<loop name>.ready` fires that loop now. Its regular schedule isn't
  affected.
- `context-heartbeat.ready` sends a heartbeat now.
- `<anything>-message.ready` delivers the file's contents, as one line.
- Any other name is deleted and logged, never delivered.

```
echo "The deploy finished, check the logs." > cousins/wren/deploy-message.ready
```

The file is removed once it's been delivered. If delivery fails it
stays and is retried next tick.

### Flips from the daemon

Two kinds of flip are also driven from here:

- The daily flip. Every cousin gets one: its own `flip_at = "HH:MM"`
  under `[lifecycle]` in `cousin.toml`, else the install's
  `default_flip_at` from `config/harness.toml`, else 04:00. Set
  `flip_at = "never"` to opt a cousin out, and `cousin-loops flips` to
  see which cousin flips when and why. At most one cousin flips per
  tick, so they don't all roll over at once. A cousin whose session
  started after the day's flip time is not flipped that day, and
  neither is one whose runner is down.
- A timed flip, from the console's "flip in N minutes", with warnings
  delivered at 5 minutes, 1 minute and 30 seconds. A cousin whose
  runner is down at that time isn't flipped; the request fails.

What a flip does is in [cousins](cousins.md).

### Worker cousins

A cousin with `type = "worker"` under `[cousin]` has no session and
no heartbeat. When one of its loops is due, the daemon runs the command
in `config/worker-cmd` as a tracked job instead, with `{prompt}` and
`{home}` replaced:

```
my-agent --print --cwd {home} {prompt}
```

The exit code ends up on the job row, so a [worker](glossary.md#worker) that keeps failing
shows as failed in the Jobs view. Without `config/worker-cmd` the loop
stays due and the daemon logs what to write.

## One-shot schedules

For "do this later, once":

```
cousin-schedule add "in 30m" "Check whether the backup finished."
#   -> scheduled #3 for wren at 2026-09-18T15:32:00
cousin-schedule add "tomorrow 09:00" "Send ana the weekly summary."
cousin-schedule add "2026-10-01T08:00" "Renew the certificate."
cousin-schedule list            # pending ones
cousin-schedule list --all      # the last 50, fired and cancelled too
cousin-schedule cancel 3
```

Times: `in N` with `s`, `m` (the default), `h` or `d`; `tomorrow HH:MM`;
or a date and time like `2026-10-01T08:00`, `2026-10-01 08:00` or only
`2026-10-01`, in local time. A time in the past is refused.

The loops daemon delivers a due prompt on its next tick, as
`[cousin-schedule] <prompt>`, once the cousin is up. If the cousin is
down or delivery fails, the prompt stays pending and is tried again.
Without a daemon, `cousin-schedule tick` fires everything due by hand.
The store is `data/scheduled.db` under the framework root; each cousin
only sees and cancels its own.

## The tracker

Jobs are what's running now. The tracker is the list of things in
flight across the whole install, whoever holds them, including threads
with nothing running behind them for weeks.

```
cousin-tracker add "migrate the chat archive" --domain infra --tag q4
#   -> #1 migrate the chat archive [open] domain=infra
cousin-tracker state 1 active
cousin-tracker update 1 --notes "waiting on the disk swap" --state blocked
cousin-tracker update 1 --add-tag disk
cousin-tracker list --state blocked
cousin-tracker show 1 --json
cousin-tracker state 1 done
cousin-tracker delete 1
```

States are `open`, `active`, `blocked`, `done` and `dropped`, and any
state can move to any other. `--owner` defaults to the cousin in
`COUSIN_HOME`. `update --tag` replaces the tag list, `--add-tag` adds to
it. `list` filters by `--domain`, `--state`, `--tag` and `--owner` and
shows open work first, most recently touched first. Every command takes
`--json` and `--root`.

Ids are never reused: a deleted #3 stays gone, so a note that mentions
#3 keeps meaning the same thing. There's no history, only the current
state; if you want the trail, log a decision when you move an item.

The store is `data/tracker.db` under the framework root. The console's
Tracker view reads and edits the same file, and the CLI works with no
console running. Exit codes: 0 ok, 1 no such item, 2 a refused call
(bad state, blank title, nothing to update, no root).
