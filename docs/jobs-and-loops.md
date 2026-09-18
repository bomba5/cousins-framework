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
by every cousin, and the console's Jobs view reads it.

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
command's exit code. You don't call `done` for those. Don't point
`--log` at a file the command itself reads, or it will read its own
output forever.

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
`running`, `done`, `failed` and `cancelled`. `cancel` sends SIGTERM to
the job's process if it has one. `start --json` prints
`{"job_id": ..., "log_path": ...}` for scripts.

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
  until it actually finishes.
- **Background shells.** A `Bash` call with `run_in_background` becomes
  a `shell` job with the command. The hook wraps the command in an exit
  trap, so the row closes with the command's real exit code when it
  ends. If the home or root path has characters the trap can't quote
  safely, the command runs unwrapped and the row is left to the 24-hour
  reap.

Foreground Bash calls aren't recorded. The hook never blocks or fails a
tool call; its errors go to `data/job-hooks.log` in the cousin home.

A cousin spawned before these hooks existed gets them with:

```
cousin-spawn wren --repair-settings
```

## Loops

A loop is a prompt that gets typed into a cousin's session on a
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

One process fires everything: loops, heartbeats, one-shot schedules and
timed flips.

```
cousin-loops run                     # the daemon, a tick every 30 s
cousin-loops run --interval 10 --ticks 1
cousin-loops status                  # exit 0 when healthy, 1 when down
cousin-loops fire wren morning-standup
cousin-loops requests
```

`systemd/cousin-loops.service` runs it for you ([operations](operations.md)).
If the daemon isn't running, nothing recurring happens: no heartbeats,
no loops, no one-shots, no timed flips. Chat and every CLI keep working.
`cousin-loops status` and the console both say "loops daemon has never
run" or "loops daemon down (last tick 312s ago)" rather than staying
quiet.

`fire` and the console's fire button don't fire anything themselves.
They write a request that the daemon picks up on its next tick, and
`requests` shows each one as `pending`, `done`, `failed` or `expired`.
A request nobody picks up within 10 minutes is marked expired, which
means the daemon is down or stuck.

What happens on each tick, for every cousin whose chat server answers
on its port (a cousin that's down gets nothing and catches up later):

1. Trigger files in the cousin home are delivered (see below).
2. The heartbeat, if it's due, and every due loop are sent together as
   one message. A single item goes as its bare prompt; several go under
   a `[Framework scheduler: N loops due this tick - handle in order]`
   line, one `### name` section each.
3. Only when the text was actually typed in does the loop count as
   fired. If delivery fails, or the pane is sitting at a login prompt,
   the loop stays due and is tried again next tick. So a loop can fire
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
- `<anything>-message.ready` types the file's contents, as one line.
- Any other name is deleted and logged, never delivered.

```
echo "The deploy finished, check the logs." > cousins/wren/deploy-message.ready
```

The file is removed once it's been delivered. If delivery fails it
stays and is retried next tick.

### Flips from the daemon

Two kinds of flip are also driven from here:

- `flip_at = "HH:MM"` under `[lifecycle]` in `cousin.toml` flips that
  cousin once a day. At most one cousin flips per tick, so they don't
  all rebuild their boot packets at once.
- With `flip_when_transcript_mb` set in `config/harness.toml`, a cousin
  whose session transcript grows past that size gets a flip scheduled
  five minutes out, with warnings typed in at 5 minutes, 1 minute and 30
  seconds.

What a flip does is in [cousins](cousins.md).

### Worker cousins

A cousin with `type = "worker"` under `[cousin]` has no tmux session and
no heartbeat. When one of its loops is due, the daemon runs the command
in `config/worker-cmd` as a tracked job instead, with `{prompt}` and
`{home}` replaced:

```
my-agent --print --cwd {home} {prompt}
```

The exit code ends up on the job row, so a worker that keeps failing
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
