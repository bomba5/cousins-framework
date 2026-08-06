# Loops specification: recurring work, one owner

How recurring and scheduled work reaches cousins: the loops daemon,
the loop data model, heartbeats, worker firings, flip drivers, and
the one-shot tick. Written from a full behavioral inventory of the
source framework's scheduler; this is the contract the implementation
is written against and tested from.

The source taught one structural lesson above all the mechanics: it
ran one scheduler source file as TWO processes with duplicated,
unshared in-memory state, and that split produced its three worst
defects - a manual fire that deadlocked the dashboard, timed flips
that could never fire (scheduled in one process, walked in the
other), and loop edits that never reached the scheduler. Everything
below starts from refusing that topology.

## One owner

A single daemon - `cousin-loops` - owns all scheduler state: the
tick, fire timestamps, the timed-flip walker, and the one-shot
scheduler tick. Any UI is a view over what the daemon persists; the
filesystem remains the cousin registry. Nothing else fires loops, and
no other process holds a writable copy of scheduler state.

Manual fires and loop edits are REQUESTS: rows written to the request
store that the daemon consumes on its next tick. Cross-process dict
mutation - the current bug's exact shape - is impossible by
construction.

### Request consumption, pinned

The seam where the source's bug lived was a write nobody read; the
successor must be incapable of that silence:

- A request is visible as `pending` from the moment it is written;
  `cousin-loops requests` lists them, and any UI reads the same rows.
- The daemon marks each request `done` or `failed` (with the reason)
  when it consumes it. Consumption is recorded, never inferred.
- A request not consumed within its TTL (default 10 minutes) is
  marked `expired` by the next reader that notices - and an expired
  request is a loud symptom, because it means the daemon missed
  ticks. It is never silently dropped and never fires late as a
  surprise.

### When the daemon is not running

One owner makes the daemon a single point of failure for every loop,
flip driver, and one-shot - and its absence is emphatically NOT a
no-op, so it must never be silent:

- The daemon persists a `last_tick` timestamp on every tick.
- Every reader of loop state - the CLI, a UI, `cousin-schedule
  list` - compares `last_tick` to the tick interval and reports
  **"loops daemon down (last tick NNs ago)"** whenever it exceeds
  three intervals. An install that never started the daemon sees
  "loops daemon has never run" wherever loop state is displayed.
- What the absence costs is stated, not implied: no beats, no loop
  fires, no timed flips, no one-shot delivery. Messages, chat, and
  every CLI keep working - the framework degrades to exactly its
  M1 surface, visibly.

## The loop data model

`[[loops]]` lives in the cousin's own `cousin.toml` - configuration
lives with the cousin. Fields:

- `name` (required): `^[a-z][a-z0-9_-]{0,31}$`, unique per cousin.
- exactly ONE schedule form (validated - extra forms are an error,
  not silently dead):
  - `interval_seconds`: due when that much time has passed since the
    last fire;
  - `daily_at = "HH:MM"` with optional `days` weekday list: once per
    calendar day;
  - `cron`: five-field cron. Day-of-month and day-of-week combine
    with OR when both are restricted, as in real cron. **This
    deliberately differs from the source implementation, which ANDed
    them**; there are no installed compatibility constraints, and
    least-surprise wins while that is true.
- `prompt` (required, non-empty): delivered verbatim behind the
  provenance prefix.
- `enabled`: TRUTHINESS, default true. (`enabled = 0` disables. The
  source's identity-check-against-False left every other falsy value
  enabled.)

A malformed `cousin.toml` is a reported error naming the cousin -
never a silent no-loops-today. (The source returned an empty list on
any parse error, which silently disabled everything.)

No config key ships without its consumer - a STATED RULE of this
spec, not a one-off: a dead key is worse than dead code, because a
user will set it and believe something changed. The source carried
two (a token budget and a liveness cadence, both read by nothing);
neither enters this schema until the thing that reads it ships with
it.

## The tick

Every tick, in order, with PER-COUSIN exception isolation - one
failing cousin skips that cousin, never the rest of the walk:

1. Walk pending timed-flip requests (warnings at T-5m/T-1m/T-30s,
   fire at T-0 via `flip()` under its concurrency marker).
2. For each registered, framework-managed cousin: liveness-gate
   (a lingering pane with a dead chat server must not receive
   fires), collect due work - the context beat first, then loops in
   file order - and deliver it COALESCED: one injection carrying all
   due sections, each under its `### name` header.
3. Consume manual-fire and edit requests.
4. Fire due one-shots from the scheduler store (the daemon is the
   framework-native driver the one-shot tick was waiting for; the
   orphan semantics are unchanged).
5. Persist state - outside any lock that a reader also takes.

Ticks are inline, never per-tick threads (the source's zombie-tick
wedge taught that); a watchdog exits the process on a wedged tick and
state reload on restart prevents refiring what already fired.

Catch-up is late-is-better-than-skipped: a `daily_at` loop missed
while a cousin was down fires on return, once; an interval loop gets
ONE catch-up fire, not one per missed interval.

## Delivery: at-least-once, stated per firing type

Fire state - including heartbeat file-delta state - commits only
AFTER delivery reports success. A failed injection leaves the loop
due, the delta unconsumed, and a log line; a crash between delivery
and commit refires. This makes every firing type at-least-once, and
each type's duplicate cost is stated:

- **Beats** are idempotent by construction (a repeated delta prompt
  re-reports the same changes); a duplicate beat is noise-free.
- **Loop prompts** may repeat after a crash; loop authors write
  prompts that tolerate a repeat, and the spec says so where loops
  are documented.
- **One-shots** keep the scheduler's existing contract: duplicate
  over lost, always.

All terminal delivery goes through the injection module - the single
injector, like the single tmux site.

## Heartbeats

The context beat delivers the file-delta prompt: changed identity
files (CLAUDE.md, STATUS.md, MEMORY.md) inlined with a per-file cap,
against ONE mtime-state file per cousin, committed after delivery.
Default cadence is one value, stated here (3600s), and the template,
docs, and code agree - the source shipped three different defaults.

## Worker cousins

A cousin with `type = "worker"` has no tmux session and no beats; a
due loop runs the worker command template from host configuration
(`config/worker-cmd`, `{model}`/`{prompt}`/`{home}` placeholders -
binary and trust model are the install's call, never hardcoded). Each
firing is a tracked job; the EXIT CODE is inspected and recorded, and
a worker whose firings fail looks failed everywhere loop state is
shown. (The source marked worker fires successful before the
subprocess ran; a worker failing every firing looked perfectly
healthy.)

## Flip drivers

A flip is not a prompt; it is not a loop action-type. The daemon
carries two flip drivers, both consuming `flip()` under its
concurrency marker:

- `flip_at = "HH:MM"` per cousin: the daily flip, staggered across
  cousins so boot packets never assemble simultaneously.
- Timed-flip requests (operator-initiated, with the T-minus warning
  ladder) through the request store - which is what makes them
  actually fire, unlike the source.

## Cycle counters

`cousin-cycle inc/state/reset` maintains per-cousin counters and
breadcrumbs at `data/cycle.json`; the boot staleness check is its
consumer. The source's overlay and milestone fields had zero readers
and do not ship - the dead-code rule.

## The trigger-file seam (not implemented)

The source framework contained a watcher protocol - drop
`<loop>.ready` in a cousin's home to trigger that loop - that turned
out to have no live producer anywhere in its tree. It does not ship.
The seam stays named because an adopter will plausibly want it: if a
trigger-file mechanism is added, it belongs INSIDE the daemon (one
owner), fires through the same delivery path, and commits its
dedup state after delivery like everything else.

## Consciously excluded

Remote-host cousins (the fleet module's problem), engagement pushes,
chat watchdogs, and byte-guard flip policies stay outside this spec;
none of them may fire loops except through the daemon's request
store.
