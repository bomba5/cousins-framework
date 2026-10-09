# Loops reference

The loop data model and exactly how the loops daemon decides what to deliver and when. Read it when a loop didn't fire (or fired twice) and you want to know why. For setting loops up, read [jobs and loops](../jobs-and-loops.md).

One process, `cousin-loops run`, owns all scheduling: loops, context heartbeats, trigger files, one-shots, scheduled [flips](../glossary.md#flip) and the outbox's retries. Nothing else fires anything. The console, the CLIs and scripts only read its state or leave requests for it to pick up on its next tick.

```sh
cousin-loops run                # the daemon: a tick every 30 s, forever
cousin-loops run --interval 10 --ticks 1
cousin-loops status             # "ok", or why not; exit 1 when down
cousin-loops requests           # the request queue, newest first
cousin-loops fire wren digest   # queue a manual fire
```

The systemd unit `cousin-loops.service` runs `cousin-loops run --interval 30`.

## A loop

Loops live in the [cousin](../glossary.md#cousin)'s own `cousin.toml`, one `[[loops]]` table each:

```toml
[[loops]]
name = "digest"
daily_at = "07:30"
days = ["mon", "tue", "wed", "thu", "fri"]
prompt = "Write the morning digest and post it to ana."
enabled = true

[[loops]]
name = "inbox-check"
interval_seconds = 1800
prompt = "Check notes/inbox.md and triage anything new."

[[loops]]
name = "weekly-review"
cron = "0 17 * * 5"
prompt = "Review the week in memory/raw and update STATUS.md."
```

| field | meaning |
|---|---|
| `name` | required. `^[a-z][a-z0-9_-]{0,31}$`, unique within the cousin |
| `interval_seconds` | due when this many seconds have passed since the last fire. A positive integer |
| `daily_at` | `"HH:MM"` local time, once per calendar day |
| `cron` | five-field cron, local time |
| `days` | optional list of three-letter weekdays (`mon`..`sun`). Only `daily_at` uses it |
| `prompt` | required, not blank. Delivered as written |
| `enabled` | default true. Any false-ish value (`false`, `0`) turns it off |
| `hidden` | only the console reads this: it hides the loop from the list |

Exactly one of `interval_seconds`, `daily_at` and `cron`. None or two is an error, not a guess.

A loop that doesn't validate (bad name, no schedule or two, empty prompt, a `cron` field that doesn't parse) is skipped and the reason is reported: on the daemon's stderr every tick, and in the `errors` list of the console's loops view. A `cousin.toml` that doesn't parse at all is reported the same way, naming the file. The other loops of that cousin and every other cousin still run.

Saving loops from the console (or `loops.save_cousin_loops`) validates the whole list first, each `cron` expression included, and refuses it naming the bad entry (`loops[2]: duplicate name 'digest'`), then rewrites only the `[[loops]]` tables, keeps the rest of the file, and checks the result parses back to the same thing before it replaces the file. `days` is lowercased on the way.

## When a loop is due

| form | due when |
|---|---|
| `interval_seconds = N` | at least N seconds since its last fire. Never fired counts as fired at 0, so a new interval loop fires on the first tick |
| `daily_at = "HH:MM"` | the clock is past HH:MM today, today is in `days` (when set), and it hasn't fired today |
| `cron` | the current minute matches, and it hasn't fired since that minute started |

Missed time is caught up late rather than skipped, but only once. A `daily_at` loop whose cousin was down at 07:30 fires when the cousin comes back that day. An interval loop that missed five intervals fires once, not five times. A cron loop has no catch-up: if the daemon wasn't ticking during the matching minute, that run is gone.

Cron fields take `*`, numbers, `a-b` ranges, `,` lists and `/step`. Day of week is 0 to 7 with both 0 and 7 meaning Sunday. Names like `mon` or `jan` aren't understood. When both day of month and day of week are restricted, either one matching is enough, like real cron: `0 9 1 * 1` fires on the 1st and on every Monday.

## The tick

Every tick does this, in this order:

1. **Flips.** Walk the pending timed flips: send the warnings that are due, run the ones whose time has come. Then, if no timed flip ran this tick, at most one daily `flip_at` flip. See [Flips](#flips).
2. **Cousins.** For each cousin, one at a time:
   - a [worker](../glossary.md#worker) cousin runs its due loops as jobs (see [Workers](#worker-cousins)) and that's all;
   - a cousin that isn't alive is skipped. A cousin with no `[agent] runner` is refused by 2.0.0 and gets nothing. A [runner](../glossary.md#runner) cousin (`[agent] runner` set, whatever the kind - `sdk`, `tmux`, `opencode` or `fake`) is alive when its runner holds `run/runner.lock`;
   - its trigger files are delivered, one delivery each;
   - the context heartbeat (if due) and every due loop are collected and delivered together, as one message.
3. **Requests.** Consume pending manual fires.
4. **One-shots.** Deliver due `cousin-schedule` jobs.
5. **Index, outbox and meetings.** Queue the memory index refresh for the cousins that need it, send again what the outbox holds that is due ([chat](../chat.md#cousins-on-another-install)), and run the meetings' tick.
6. **Housekeeping.** Mark pending requests older than their TTL as `expired`, write `last_tick`, save the state.

A failure inside one cousin (an exception, a bad file) is reported and the walk moves on to the next cousin. A broken one-shot store, index refresh, outbox or meetings store is reported and doesn't cost the loops their tick. Errors go to the daemon's stderr, prefixed `cousin-loops:`. After each tick the daemon also writes what each step did to `data/health.json`, a count of consecutive failures per component that `cousin-health` prints ([operations](../operations.md#health)).

## Delivery

A cousin with no `[agent] runner` gets nothing: 2.0.0 refuses it by name ([migrating](../migrating.md#a-cousin-with-no-runner)).

A runner cousin, whatever its kind (the `tmux` kind included, which drives its own tmux pane through `TmuxRunner`), gets each delivery as one row in its [inbox](../glossary.md#inbox) (`data/inbox.db`), on [thread](../glossary.md#thread) `loop:daemon` with source `loop`. The row is kept before the daemon moves on, so the put is the delivery: the daemon never waits for the [turn](../glossary.md#turn).

What the cousin gets:

- one due loop, no heartbeat: the prompt, as written;
- several things due at once:

  ```
  [Framework scheduler: 2 loops due this tick - handle in order]

  ### context-heartbeat
  Context heartbeat. ...

  ### digest
  Write the morning digest and post it to ana.
  ```

  The heartbeat always comes first, then the loops in file order;
- a manual fire: `[Framework scheduler: manual fire]` then `### <name>` and the prompt;
- a trigger file: `[Framework scheduler: ready-file trigger]` then `### <name>` and the prompt;
- a one-shot: `[cousin-schedule] #<id>, set <time>, due <time>, fired <time> (on time | N min late). ...`, a blank line, then the prompt (see [One-shots](#one-shots));
- a flip warning: `[cousin-flip] wrap up tool calls - flip in 5 minutes`, and so on.

Nothing is recorded as fired until the row is in the inbox. If the put fails (the inbox can't be opened or written), the loops stay due, the heartbeat's file changes stay unreported, the trigger file stays, the one-shot stays pending, and the next tick tries again. The flip side: if the daemon dies between the put and saving its state, the same thing is delivered again. So everything here is at least once. Write loop prompts that don't mind running twice now and then; heartbeats already don't. A one-shot is the exception for a runner cousin: its put carries the key `schedule:<id>`, the inbox keeps one row per key, and the repeat after a crash finds the first row and marks the job fired without a second delivery.

## Context heartbeats

Every non-worker cousin gets a context heartbeat, whether or not it has loops. The period is `[heartbeat] context_beat_seconds` in `cousin.toml`, default 3600. Zero or less turns it off (the console only lets you set 60 to 2592000).

The heartbeat checks the modification times of `CLAUDE.md`, `STATUS.md` and `MEMORY.md` in the home against `data/heartbeat-mtimes.json`. A changed `STATUS.md` is pasted in as its live `## Open loops` section only (the first bare `## Open loops` heading or, in an older home, the suffixed section right below an empty one: the handoff's own reader, `cousin_lib/status_sections`), up to 6000 characters and then a pointer to the file; a `STATUS.md` with no such section gets a pointer. A changed `CLAUDE.md` or `MEMORY.md` is named with its path and size, never pasted: the identity file is in the system prompt already, and `MEMORY.md` is history. If none changed it says so. Then it asks the cousin to checkpoint with `cousin-memory activity`, log a decision if something real changed, and answer `Heartbeat at HH:MM`. The mtimes file is only written after a successful delivery, so a failed beat reports the same changes next time.

The heartbeat shows up in the console as a loop called `context-heartbeat`, and you can fire it by that name (`cousin-loops fire wren context-heartbeat`, a trigger file, or the console's fire button). A fired heartbeat is a real one: it resets the clock for the next.

## Trigger files

Drop a file named `<name>.ready` in a cousin's home and the next tick picks it up. Files are taken in name order, only for live cousins, and never for workers.

| file | what's delivered |
|---|---|
| `<loop name>.ready` | that loop's prompt, as an extra fire. The loop's own schedule isn't touched |
| `context-heartbeat.ready` | a real heartbeat |
| `<anything>-message.ready` | the file's contents, trimmed, as one line. An empty file is removed and nothing is sent |
| anything else | removed, with a line naming it on stderr. A disabled loop's name counts as anything else |

The file is removed once its delivery succeeds. If delivery fails the file stays and the next tick tries again; the failure is reported once, not every tick.

```sh
echo "the nightly backup failed, check /var/log/backup.log" > cousins/wren/backup-message.ready
```

Anything that can write a file can trigger a cousin: a shell hook, cron, another cousin's job.

## Manual fires and the request queue

A manual fire is a row in `data/loop-requests.db` that the daemon consumes on its next tick. The console's fire button and `cousin-loops fire` both write one.

| field | meaning |
|---|---|
| `id`, `ts` | row id, when it was written |
| `kind` | `fire` or `flip` |
| `cousin` | the slug |
| `payload` | JSON: `{"loop": "<name>"}` for a fire, `{"fire_at": <unix>, "reason": "..."}` for a flip |
| `ttl_seconds` | 600 for a fire; delay plus 600 for a timed flip |
| `status` | `pending`, then `done`, `failed`, `expired` or `cancelled` |
| `consumed_at`, `reason` | when it finished, and why when it didn't work |

A fire request is `done` when delivered, `failed` with `no such loop 'x'` for an unknown or disabled loop, or `failed` with `delivery failed`. A manual fire doesn't move the loop's schedule, except for `context-heartbeat`.

A request still pending after its TTL is marked `expired` with `daemon missed ticks: request outlived its TTL`. It never fires late. Only the daemon's tick runs that check, so while the daemon is down an over-age request still shows as `pending`.

The daemon only claims the kinds it knows (`fire` and `flip`). Anything else sits pending until it expires.

## Is the daemon running?

`cousin-loops status`, the console and everything else that shows loop state compare `last_tick` in `data/loops-state.json` with the tick interval (30 s):

| state | message |
|---|---|
| never ticked | `loops daemon has never run` |
| last tick more than 90 s ago | `loops daemon down (last tick NNs ago)` |
| otherwise | `ok` |

The 90 seconds are three ticks of 30 s whatever `--interval` you run with, so a daemon on a slower interval reads as down between ticks.

With the daemon down there are no heartbeats, no loop fires, no trigger files, no one-shots and no scheduled flips. Chat, jobs and every CLI keep working.

## One-shots

`cousin-schedule` stores a single prompt for later in `data/scheduled.db`, shared by the install and scoped per cousin. The cousin is taken from `COUSIN_HOME`.

```sh
cousin-schedule add "in 30m" "check whether the upload finished"
cousin-schedule add "tomorrow 06:30" "remind ana about the dentist"
cousin-schedule add "2026-10-01T09:00" "start the quarterly review"
cousin-schedule list [--all]
cousin-schedule cancel 12
```

`when` is `in N[s|m|h|d]` (a bare number means minutes), `tomorrow HH:MM`, or a local date or datetime (`YYYY-MM-DD`, `YYYY-MM-DD HH:MM`, with `T` or a space, seconds optional). A time in the past is refused.

A cousin holds at most 20 pending one-shots. `add` refuses the 21st (`<slug> already has 20 pending scheduled prompts (the cap); cancel one first`) until one fires or is cancelled, so a runaway loop of a cousin scheduling itself stops there. The `schedule` tool and the console's `POST /api/cousins/<slug>/schedules` refuse it the same way.

Step 4 of each tick delivers every pending job whose time has come. A job for a cousin that's down stays pending, quietly, until the cousin is back. A failed delivery keeps the job pending and reports it. The job is marked `fired` only after delivery, so after a crash you may get it twice but never zero times.

A due job arrives as one header line, a blank line, then the prompt as it was written:

```
[cousin-schedule] #12, set 2026-10-01 21:10 CEST, due 2026-10-02 06:30 CEST, fired 2026-10-02 06:30 CEST (on time). Your own scheduled prompt: nobody is waiting on this turn unless the prompt says so.

remind ana about the dentist
```

The times are local, to the minute. A job fired less than two minutes after its time is `on time`; later, it says `N min late` (whole minutes). One that fires 30 minutes or more late (the cousin or the daemon was down) is still delivered, never dropped, and its header adds: `It fired late (the cousin or the daemon was down), so what it was set for may already be settled: check before acting on it.`

`cousin-schedule tick` fires due jobs by hand, for an install without the daemon. It delivers the same header and prompt, without the `[cousin-schedule] ` prefix (the message comes in on the `schedule` thread). It doesn't check whether the cousin's runner is up: it puts each due prompt into the cousin's inbox and marks the job fired once the put succeeded, so a cousin that is down gets it when its runner starts. A cousin with no `[agent] runner`, or an inbox that can't be written, keeps the job pending and the error is printed.

## Worker cousins

A cousin with `[cousin] type = "worker"` has no session and no heartbeat. When one of its loops is due, the daemon runs the command in `config/worker-cmd` as a tracked job instead:

```
my-agent --print --cwd {home} {prompt}
```

The template is split like a shell line, then `{prompt}` and `{home}` are replaced in each word. Each run is a job titled `worker <slug>|<loop>` with its log under the jobs log directory; its exit code ends up in the job row, and a loop whose last job failed shows as `failed` in the console. Starting the job counts as the fire. Without `config/worker-cmd` the loop stays due and every tick reports `worker loop wren|digest due but no worker command configured`.

Workers don't get trigger files, and a manual fire for a worker fails (there's no session to deliver to). Their loops fire on schedule only.

## Flips

A flip isn't a loop. The daemon drives it two ways, both through the same `flip()` call ([lifecycle](lifecycle.md#the-flip)):

**Daily.** Every cousin, at its own `[lifecycle] flip_at = "HH:MM"` in `cousin.toml`, else the install's `default_flip_at`, else 04:00; `flip_at = "never"` opts one out and `cousin-loops flips` prints the effective time and its source. Once a day, as soon as the clock passes that time (late rather than skipped), for a session that started before it: the point is that a session is at most a day old, unless it did nothing (below). A cousin whose current session started at or after the day's flip time (spawned or started since, or rolled over since) is not flipped that day, and neither is one that has never started a session; its day is marked done and the next day's flip time is its first. An idle generation is not flipped either: when every inbox row since the generation started is upkeep (heartbeats, its boot, a flip, memory proposals), the flip would cost a handoff and a boot and carry nothing, so the session goes on, its day is marked done, the tick reports it in `idle_flips` and the daemon prints it. It flips at the first daily point after it does any work, and at once when the system prompt or the tool list it recorded (`runner-session.json`'s `snapshot`) no longer matches what a start would serve, so a new rule still reaches it. A home with no inbox database is never idle. When the session started is `data/generation-started.json`, which the framework writes at every generation start: a [rollover](../glossary.md#rollover) or flip, and a runner's fresh start of its session (a first boot moves no generation). A home from before that file falls back to the last change of `data/generation.txt`. At most one daily flip per tick, and none on a tick where a timed flip ran, so when several cousins share a time they flip one per tick instead of all rolling over at once. The date of the last daily flip is kept in the state file, so a failed flip isn't retried that day; it's reported. Workers are skipped. An unparsable `flip_at` is reported every tick.

**Timed.** A `flip` request with a `fire_at`, from the console's "flip in N minutes". While it waits, the cousin gets warnings at five minutes, one minute and 30 seconds:

```
[cousin-flip] wrap up tool calls - flip in 5 minutes
[cousin-flip] finalize your handoff now - flip in 1 minute
[cousin-flip] write data/handoff.md - flip in 30 seconds
```

At `fire_at` it flips and marks the request `done`, or `failed` with the error. Cancelling (console, or `loops.cancel_request`) marks it `cancelled`. Only one timed flip per cousin can be pending.

Neither kind starts a stopped cousin. A daily flip skips a cousin whose runner is down and marks its day done. A timed flip whose cousin is down at `fire_at` ends `failed` with `runner not running; start it before flipping`. Either way nothing is queued for the next start, so a flip never undoes an operator's stop.

## Files

| path | what |
|---|---|
| `data/loops-state.json` | `last_tick`, `last_beat` per slug, `last_fires` per `slug\|loop`, `last_flips`, warning and report bookkeeping. The daemon's memory; losing it means every interval loop fires on the next tick |
| `data/loops-fires.jsonl` | one `{"ts", "cousin", "loop"}` line per delivered loop fire. The console's drift chart reads it. Heartbeats aren't in it |
| `data/health.json` | per component: ok or failing, consecutive failures, since when, the last error ([operations](../operations.md#health)) |
| `data/loop-requests.db` | the request queue |
| `data/scheduled.db` | one-shots |
| `<home>/data/heartbeat-mtimes.json` | the heartbeat's view of the identity files |
| `<home>/*.ready` | trigger files |

All `data/` paths above are under the framework root, except the last two, which are in the cousin's home.

## Drift

The console's drift number for a loop is how much longer than the interval the last gap between two fires was: `max(0, (last fire - fire before) - interval_seconds)`. The drift chart plots the same thing for the last 80 gaps from `loops-fires.jsonl`. Drift is only meaningful for interval loops (daily and cron loops have interval 0 there). The usual causes are the 30-second tick itself, a cousin that was down, and deliveries that failed and were retried.
