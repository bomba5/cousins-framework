# Loops reference

The loop data model and exactly how the loops daemon decides what to deliver and when. Read it when a loop didn't fire (or fired twice) and you want to know why. For setting loops up, read [jobs and loops](../jobs-and-loops.md).

One process, `cousin-loops run`, owns all scheduling: loops, context heartbeats, trigger files, one-shots and scheduled [flips](../glossary.md#flip). Nothing else fires anything. The console, the CLIs and scripts only read its state or leave requests for it to pick up on its next tick.

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
cron = "0 17 * * fri"
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

A loop that doesn't validate (bad name, no schedule or two, empty prompt) is skipped and the reason is reported: on the daemon's stderr every tick, and in the `errors` list of the console's loops view. A `cousin.toml` that doesn't parse at all is reported the same way, naming the file. The other loops of that cousin and every other cousin still run.

Saving loops from the console (or `loops.save_cousin_loops`) validates the whole list first and refuses it naming the bad entry (`loops[2]: duplicate name 'digest'`), then rewrites only the `[[loops]]` tables, keeps the rest of the file, and checks the result parses back to the same thing before it replaces the file. `days` is lowercased on the way.

## When a loop is due

| form | due when |
|---|---|
| `interval_seconds = N` | at least N seconds since its last fire. Never fired counts as fired at 0, so a new interval loop fires on the first tick |
| `daily_at = "HH:MM"` | the clock is past HH:MM today, today is in `days` (when set), and it hasn't fired today |
| `cron` | the current minute matches, and it hasn't fired since that minute started |

Missed time is caught up late rather than skipped, but only once. A `daily_at` loop whose cousin was down at 07:30 fires when the cousin comes back that day. An interval loop that missed five intervals fires once, not five times. A cron loop has no catch-up: if the daemon wasn't ticking during the matching minute, that run is gone.

Cron fields take `*`, numbers, `a-b` ranges, `,` lists and `/step`. Day of week is 0 to 7 with both 0 and 7 meaning Sunday. Names like `mon` or `jan` aren't understood. When both day of month and day of week are restricted, either one matching is enough, like real cron: `0 9 1 * mon` fires on the 1st and on every Monday.

## The tick

Every tick does this, in this order:

1. **Flips.** Walk the pending timed flips: send the warnings that are due, run the ones whose time has come. Then, if no timed flip ran this tick, at most one daily `flip_at` flip. See [Flips](#flips).
2. **Cousins.** For each cousin, one at a time:
   - a [worker](../glossary.md#worker) cousin runs its due loops as jobs (see [Workers](#worker-cousins)) and that's all;
   - a cousin that isn't alive is skipped. A legacy tmux cousin (no `[agent] runner`) is alive when its chat server accepts a connection on `127.0.0.1:<port>`, so a tmux pane whose chat server is dead gets nothing. A [runner](../glossary.md#runner) cousin (`[agent] runner` set, whatever the kind - `sdk`, `tmux`, `opencode` or `fake`) is alive when its runner holds `run/runner.lock`;
   - its trigger files are delivered, one delivery each;
   - the context heartbeat (if due) and every due loop are collected and delivered together, as one message.
3. **Requests.** Consume pending manual fires.
4. **One-shots.** Deliver due `cousin-schedule` jobs.
5. **Transcript guard.** Maybe queue one flip for a cousin whose session transcript got too big.
6. **Housekeeping.** Mark pending requests older than their TTL as `expired`, write `last_tick`, save the state.

A failure inside one cousin (an exception, a bad file) is reported and the walk moves on to the next cousin. A broken one-shot store or transcript guard is reported and doesn't cost the loops their tick. Errors go to the daemon's stderr, prefixed `cousin-loops:`.

## Delivery

A legacy tmux cousin (no `[agent] runner`) gets everything typed into its tmux session through the same injector the chat server uses (paste, wait, Enter, check, one retry; see [the chat API](chat-api.md#what-the-cousin-sees)). That includes its guard: if the pane shows one of `attention_patterns` from `config/harness.toml`, nothing is typed and the delivery counts as failed.

A runner cousin, including one on the `tmux` runner kind (its own tmux pane, driven by `TmuxRunner`, not the legacy [lane](../glossary.md#lane)'s send-keys), gets each delivery as one row in its [inbox](../glossary.md#inbox) (`data/inbox.db`), on [thread](../glossary.md#thread) `loop:daemon` with source `loop`. The row is kept before the daemon moves on, so the put is the delivery: the daemon never waits for the [turn](../glossary.md#turn).

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
- a one-shot: `[cousin-schedule] <prompt>`;
- a flip warning: `[cousin-flip] wrap up tool calls - flip in 5 minutes`, and so on.

Nothing is recorded as fired until delivery reports success. If typing fails, the loops stay due, the heartbeat's file changes stay unreported, the trigger file stays, the one-shot stays pending, and the next tick tries again. The flip side: if the daemon dies between typing and saving, the same thing is delivered again. So everything here is at least once. Write loop prompts that don't mind running twice now and then; heartbeats already don't.

## Context heartbeats

Every non-worker cousin gets a context heartbeat, whether or not it has loops. The period is `[heartbeat] context_beat_seconds` in `cousin.toml`, default 3600. Zero or less turns it off (the console only lets you set 60 to 2592000).

The heartbeat checks the modification times of `CLAUDE.md`, `STATUS.md` and `MEMORY.md` in the home against `data/heartbeat-mtimes.json`. Each file that changed is pasted in (up to 6000 characters) as the current contents; if none changed it says so. Then it asks the cousin to checkpoint with `cousin-memory activity`, log a decision if something real changed, and answer `Heartbeat at HH:MM`. The mtimes file is only written after a successful delivery, so a failed beat reports the same changes next time.

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

A request still pending after its TTL is marked `expired` with `daemon missed ticks: request outlived its TTL`. It never fires late. The console runs the same expiry check, so you see expired rows even while the daemon is down.

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

Step 4 of each tick delivers every pending job whose time has come, as `[cousin-schedule] <prompt>`. A job for a cousin that's down stays pending, quietly, until the cousin is back. A failed delivery keeps the job pending and reports it. The job is marked `fired` only after delivery, so after a crash you may get it twice but never zero times.

`cousin-schedule tick` fires due jobs by hand, for an install without the daemon. It doesn't check whether the cousin is alive, and it marks a job fired even when the typing was skipped (a pane showing an attention pattern), so prefer the daemon.

## Worker cousins

A cousin with `[cousin] type = "worker"` has no tmux session and no heartbeat. When one of its loops is due, the daemon runs the command in `config/worker-cmd` as a tracked job instead:

```
my-agent --print --cwd {home} {prompt}
```

The template is split like a shell line, then `{prompt}` and `{home}` are replaced in each word. Each run is a job titled `worker <slug>|<loop>` with its log under the jobs log directory; its exit code ends up in the job row, and a loop whose last job failed shows as `failed` in the console. Starting the job counts as the fire. Without `config/worker-cmd` the loop stays due and every tick reports `worker loop wren|digest due but no worker command configured`.

Workers don't get trigger files, and a manual fire for a worker fails (there's no session to type into). Their loops fire on schedule only.

## Flips

A flip isn't a loop. The daemon drives it three ways, all through the same `flip()` call ([lifecycle](lifecycle.md#the-flip)):

**Daily.** Every cousin, at its own `[lifecycle] flip_at = "HH:MM"` in `cousin.toml`, else the install's `default_flip_at`, else 04:00; `flip_at = "never"` opts one out and `cousin-loops flips` prints the effective time and its source. Once a day, as soon as the clock passes that time (late rather than skipped). At most one daily flip per tick, and none on a tick where a timed flip ran, so when several cousins share a time they flip one per tick instead of all building boot packets at once. The date of the last daily flip is kept in the state file, so a failed flip isn't retried that day; it's reported. Workers are skipped. An unparsable `flip_at` is reported every tick.

**Timed.** A `flip` request with a `fire_at`, from the console's "flip in N minutes" or the transcript guard. While it waits, the cousin gets warnings at five minutes, one minute and 30 seconds:

```
[cousin-flip] wrap up tool calls - flip in 5 minutes
[cousin-flip] finalize your handoff now - flip in 1 minute
[cousin-flip] write data/handoff.md - flip in 30 seconds
```

At `fire_at` it flips and marks the request `done`, or `failed` with the error. Cancelling (console, or `loops.cancel_request`) marks it `cancelled`. Only one timed flip per cousin can be pending.

**Transcript guard.** With `flip_when_transcript_mb` and `transcripts_dir` set in `config/harness.toml`, every tick measures each live non-worker cousin's session transcript (`<transcripts_dir>/<session_id>.jsonl`, session id from `[runtime]`). For the biggest one over the limit it queues a timed flip five minutes out with a reason like `transcript over 40 MB (41.3 MB)`. One cousin per tick, never a second while one is pending for that cousin. Cousins without a session id or a transcript are skipped. Setting the limit without `transcripts_dir` gets reported every tick.

Timed and daily flips run whether or not the cousin's chat server is up.

## Files

| path | what |
|---|---|
| `data/loops-state.json` | `last_tick`, `last_beat` per slug, `last_fires` per `slug\|loop`, `last_flips`, warning and report bookkeeping. The daemon's memory; losing it means every interval loop fires on the next tick |
| `data/loops-fires.jsonl` | one `{"ts", "cousin", "loop"}` line per delivered loop fire. The console's drift chart reads it. Heartbeats aren't in it |
| `data/loop-requests.db` | the request queue |
| `data/scheduled.db` | one-shots |
| `<home>/data/heartbeat-mtimes.json` | the heartbeat's view of the identity files |
| `<home>/*.ready` | trigger files |

All `data/` paths above are under the framework root, except the last two, which are in the cousin's home.

## Drift

The console's drift number for a loop is how much longer than the interval the last gap between two fires was: `max(0, (last fire - fire before) - interval_seconds)`. The drift chart plots the same thing for the last 80 gaps from `loops-fires.jsonl`. Drift is only meaningful for interval loops (daily and cron loops have interval 0 there). The usual causes are the 30-second tick itself, a cousin that was down, and deliveries that failed and were retried.
