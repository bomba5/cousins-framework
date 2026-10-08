# Lifecycle reference

What a new session starts from, what a [flip](../glossary.md#flip) does step by step, and how reincarnate and transplant work underneath. Read it when a flip went wrong or a [cousin](../glossary.md#cousin) woke up confused. For the everyday version, read [cousins](../cousins.md).

A cousin's session only lasts so long before its context is full. The flip ends the session (a "generation") and starts a fresh one. Every cousin runs on a [runner](../glossary.md#runner) kind, so its flip is a [rollover](../glossary.md#rollover): the runner ends the generation itself. The files in the home are what carry over; what the fresh session reads first is built from them.

## The boot packet

A new session starts from two texts the runner builds from the home. The **system prompt** holds the framework law, the framework contract (the tools the cousin has), the cousin's authored identity, the operator rules and the operator's standing instructions to this cousin. It is never cut and stays the same from one generation to the next ([runners](runners.md)). The **state digest** is the new session's first message: everything that changes. Together they carry the boot packet's layers, described below. The digest looks like this:

```
STATE DIGEST FOR COUSIN: wren
Generation: 12
state_hash: 7c21e9aa0b13
memory_snapshot: 2026-09-18T07:30:02+00:00
DEGRADED layers: task_packet

## 1. Operator Calibration
...
## 5. Retrieved Memories
...
## 6. Shared Reference
...
```

`state_hash` is the first 12 hex of SHA-256 over STATUS.md plus `data/handoff.md`. Compare it across generations to see what changed. The `DEGRADED` line only appears when a layer is missing something the cousin should have (see the table), so the cousin boots knowing it. Operator Calibration and Shared Reference are left out when they are empty, and the numbers close up.

### The layers

| layer | where | built from | when it's empty |
|---|---|---|---|
| Framework Law | system prompt | `<root>/config/law.md`, the same for every cousin; seeded from `templates/law.md` on the [supervisor](../glossary.md#supervisor)'s first start | left out. The system prompt is never cut, and `boot.fit()` treats the law as a hard layer it never cuts either |
| Operator rules | system prompt | `<root>/shared/*.md`, the canonical tier only: entries with `kind: rule`, in full. Never pending proposals | left out, fine: a fresh install has no [shared tier](../glossary.md#shared-tier) |
| Identity | system prompt | the authored parts of `CLAUDE.md` (the title line, `## Identity`, `## Voice` and what's below the template marker, minus the template's own text), then `<home>/self-portrait.md`, the committed portrait only (a candidate waiting for review doesn't count) | a fixed note that no identity is on disk and none should be invented, **degraded** (`identity`) |
| Standing instructions | system prompt | the cousin's L0 entries whose topic carries a preferences word ("rule:", "feedback", "preference", "prefers", "tone", "register", "style"; `distill.standing_instruction`), the newest entry per topic in full, sorted by topic, under "# Your operator's standing instructions" | left out |
| Operator Calibration | digest | the curated text above the marker in `memory/distilled/operator-calibration.md`, the other L0 entries (one line per topic, the view's line) newest first, then the last 15 corrections from `data/corrections.jsonl`, newest first. Standing instructions are not repeated here. The portrait's own calibration section is in the identity already | left out |
| Active State | digest | the live `## Open loops` section of STATUS.md, the bare heading the handoff writes (a suffixed `## Open loops (...)` heading is history; see [memory](../memory.md)) (or the first 1500 characters when there's no such section), then the first 1500 characters of `data/handoff.md`. A stale warning goes on top when decisions were logged after STATUS.md was last changed | `(no active state - degraded boot)`, **degraded** |
| Current Task Packet | digest | `data/active-threads.md` (first 1500 characters) and the last three reasoning capsules from `memory/distilled/reasoning-capsules.md` | `(no in-flight tasks - check STATUS.md)`. Only **degraded** if Active State is empty too |
| Recent Tool Trace Summary | digest | the cousin's traced CLI calls from the last 24 hours, newest first, up to 30 | `(no substantive tool traces in last 24h)`, fine |
| Retrieved Memories | digest | the [distilled](../glossary.md#distilled) files in `memory/distilled/` (preferences, project facts, decisions, known failures, glossary; calibration has its own layer), the last five capsule conclusions from `memory/capsules.jsonl`, the last 60 entries from the newest 14 files in `memory/raw/` (standing instructions left out: the system prompt has them), and the first 1000 characters of MEMORY.md | empty, fine: a new cousin has no memories |
| Shared Reference | digest | every other canonical shared entry as one line (file and description), with how to read one (`cousin-shared read <file>`) | left out, fine |

Before it builds the digest, the runner regenerates `memory/distilled/` from `memory/raw/` (the same as `cousin-memory distill`), so the digest always has a fresh view. If that fails, the digest uses whatever distilled files are already there, and if the digest can't be built at all, the last handoff goes in its place, marked degraded.

The stale warning in Active State reads like this and means: don't trust STATUS.md blindly.

```
> STALE WARNING: STATUS.md mtime is 30.5h old; 4 decision(s) logged after. Verify against data/decisions.jsonl before acting on it.
```

### Size

The system prompt is never cut. The digest has to fit in 8000 tokens, counted as 4 characters per token (32000 characters), less 600 characters kept for its header. Each layer has a floor and a ceiling (`boot.LAYER_BUDGETS`):

| layer | min tokens | max tokens |
|---|---|---|
| calibration | 800 | 2000 |
| active_state | 500 | 1500 |
| task_packet | 500 | 2000 |
| trace_summary | 500 | 1500 |
| memories | 1000 | 4000 |
| shared (the Shared Reference) | 400 | 1500 |

First every layer is cut to its ceiling. The ceilings add up to more than the total, so if the digest is still too big, layers are cut to their floor one at a time in this order until it fits: memories, trace_summary, calibration, task_packet, active_state, shared. That is `boot.TRUNCATE_ORDER` (memories, trace_summary, calibration, task_packet, active_state, shared, self_portrait) without the layers the digest doesn't carry. A cut layer ends with `... (truncated, <layer>, budget hit)` and the marker counts inside the budget. The calibration layer is the exception: it gives way by whole entries, newest kept, and each of its two parts (the calibration, the corrections) ends with `- ... N more not shown` for what it left out.

Degraded is decided before any cutting, from each layer's own rule in the table, never by searching the text.

## The flip

```sh
cousin-flip wren                # prints the result as JSON; exit 0 when ok
cousin-flip wren --dry-run      # checks the cousin can be flipped; no rollover
```

The console's flip button, a timed flip and a daily `flip_at` all call the same function ([loops](loops.md#flips)). A cousin never flips itself.

`cousin-flip`, the loops daemon and the lifecycle commands run in another process than the runner, so they put a `flip` row into the cousin's [inbox](../glossary.md#inbox) (the same row `Runner.rollover` puts when the context fills up) and wait for its answer, up to the handoff deadline plus a minute. One rollover is pending per cousin: a second request joins the first, except a long or multi-line reason (a bequest), which is never merged away. The runner claims the row between [turns](../glossary.md#turn), never in the middle of one.

The row's body is the reason, and the model reads it in its handoff request: `cousin-flip` by hand, `max_age` from the daily `flip_at` cadence, `timed flip` from a timed flip, the bequest text from reincarnate.

What the runner does with the row, on the `sdk` kind (the other kinds follow the same shape; [runners](runners.md) lists where they differ):

1. **Handoff.** Ask the model for its handoff through the `handoff` tool, which writes STATUS.md's open loops, `data/active-threads.md`, what the session learned, and LAST `data/handoff.md`. If the model doesn't call it within 300 seconds, ends its turn without it, or the turn fails, the runner writes an emergency handoff itself (`# EMERGENCY HANDOFF (framework-generated)`, `degraded_state: true`, the reason, and the last 2000 characters of the session) and the generation still ends. A rate limit or a needed login postpones the rollover instead.
2. **End hooks.** Run the `[session]` end hooks (see [session hooks](#inside-a-generation-session-hooks)).
3. **Archive.** Copy STATUS.md, `data/handoff.md` and `data/active-threads.md` to `data/generations/gen-NNNN/` for the generation that's ending.
4. **Final mine.** Mine what is left of the old session's transcript into raw memory (the runner mines every [turn](../glossary.md#turn); the opencode kind does not mine yet, see [runners](runners.md#known-gaps)).
5. **New session.** Start a fresh session on the same system prompt. If that fails, the rollover fails: the old session is kept and the row is closed `failed`, naming where it stopped.
6. **Generation.** Add one to `data/generation.txt` (a missing file counts as 0). From here nothing fails the rollover: a step that goes wrong is named in the answer and the row still closes as done.
7. **Start hooks**, then the **state digest** as the new session's first message, ahead of any chat that queued meanwhile.

The result is JSON with one `rollover` stage carrying the runner's answer:

```json
{"slug": "wren", "ok": true, "lane": "runner",
 "stages": [{"stage": "rollover", "ok": true, "inbox_id": 41,
             "reason": "cousin-flip", "handoff": "clean", "generation": 12,
             "old_session": "...", "digest": "built", "coalesced": false}]}
```

On failure there's an `error` string.

A runner that is not running is handled two ways on purpose. `cousin-flip` and a timed flip refuse (`runner not running; start it before flipping`; a timed flip's request ends `failed` with it) and queue nothing, and the daily cadence skips the cousin and marks its day done. So a flip never undoes an operator's stop by leaving a rollover to fire at the next start. `cousin-reincarnate` and `cousin-transplant` queue the row anyway: the new role or the moved memory must take effect at the next start.

The daily cadence keeps its stagger: the loops daemon flips at most one cousin per tick.

`--dry-run` doesn't touch the runner: it loads the cousin's `cousin.toml` and answers ok with the rollover stage skipped (`{"stage": "rollover", "skipped": "dry-run"}`). A cousin with no runner kind is refused, as a real flip would be.

### A clean stop

The console's stop button stops the cousin's runner and holds it down (`<home>/run/held`); a restart resumes the session. A cousin with no `[agent] runner` is refused (409).

A clean stop is the runner's own SIGTERM path. The console asks the [supervisor](../glossary.md#supervisor) to stop the cousin's `runner:<slug>` child without waiting for it; the runner gives the turn it is on up to 30 s (`STOP_TIMEOUT_S`), then stops on its own (the supervisor kills it 5 s after that). There is no separate handoff prompt: the [rollover](../glossary.md#rollover) and the turn-end mining cover that. The route answers 202 `stopping` while the supervisor works it, or 200 `stopped` when there was nothing running to stop; anything the supervisor itself refuses comes back as a 502 naming the reason, and a runner started by hand with no supervisor running is a 503. A `clean` field in the body of `POST /api/cousins/<slug>/stop` must be a boolean, and changes nothing: every stop is this one.

### When a flip dies halfway

The `flip` row is durable. A runner that stops or dies before or during the rollover leaves the row in its inbox and finishes the rollover at its next start; a `cousin-flip` that was waiting answers `runner not running; rollover queued as inbox row N`. Once the handoff is written, the `sdk` kind records that in `data/rollover.json` until the row closes, so a runner killed after it goes on from there: the old session gets no second handoff turn (the row's answer says `"handoff": "kept"`), and a start whose new session already existed makes no boot of its own before the rollover's. A handoff the model never writes doesn't hold anything up: after the 300-second deadline the runner writes the emergency handoff and the generation ends. A rollover that failed before the new session existed kept the old one; its row says where it stopped, so fix that and flip again.

## Reincarnate

Give a cousin a new role and keep its memory.

```sh
cousin-reincarnate wren --new-role "keeps the house paperwork in order" [--root R]
```

1. **Snapshot.** Copy MEMORY.md, STATUS.md, CLAUDE.md, cousin.toml, self-portrait.md and the whole `memory/` tree (without the search indexes, which rebuild) to `<root>/data/lifecycle/wren/<UTC timestamp>/`.
2. **Bequest.** The request to Wren, to write in her own voice who she is now, what is in flight and a bequest to her successor, becomes the rollover's reason. She reads it in her handoff request and answers it in the `handoff` tool's `position` field, within the handoff deadline (300 seconds); the rollover follows either way. The step records `"carried": true`.
3. **Rewrite.** In CLAUDE.md, the title line `# <Name> - <role>` gets the new role, and so does the body of a `## Role` section if there is one. Everything else in the file stays as it was. `[cousin] role` in cousin.toml is replaced.
4. **Flip.** The next generation boots with the new role and the old memory. The row is queued even when the runner is stopped (see [The flip](#the-flip)), so the new role takes effect at the next start.

A cousin with no `[agent] runner` is refused before anything is touched.

## Transplant

Move memory or identity between two cousins.

```sh
cousin-transplant --donor wren --recipient kestrel --mode soul-donation [--root R]
```

Both are snapshotted as above, the mode is applied, then both are flipped, donor first. There is no bequest step; each cousin is rolled over, and queued if its runner is stopped. A cousin with no `[agent] runner` is refused before anything is touched.

| mode | Kestrel ends up with | Wren afterwards |
|---|---|---|
| `soul-donation` | Wren's MEMORY.md and `memory/` in place of her own; her own identity files | unchanged |
| `body-swap` | Wren's CLAUDE.md, self-portrait and `[cousin]` name and role; her own memory | Kestrel's former identity; her own memory |
| `merge` | her own MEMORY.md, then `## Memories inherited from Wren (<date>)`, then Wren's; `memory/raw/` merged (files she lacked copied, shared files gain the lines she didn't have) | unchanged |

The slug and the session always stay where they are: a body swap changes who lives at an address, not the address.

Nothing is ever deleted: the donor stays, and the recipient's old files are in its snapshot.

## Records and rollback

Every step of both commands appends a line to `<root>/data/lifecycle/audit.jsonl` with `ts`, `op`, `step` and the step's details. A reincarnation writes `snapshot`, `bequest`, `rewrite`, `flip`, `done`; a transplant two `snapshot`s, `apply`, two `flip`s and `done`. `done` carries `ok`.

Both commands print their result as JSON. Exit codes: 0 when every flip worked, 1 when a flip failed (the files have already been changed), 2 when the command refused before touching anything (unknown slug, empty role, unknown mode, same cousin twice, a cousin with no `[agent] runner`, no root).

To undo: stop the cousin, copy the snapshot's files back over the home (`memory/` as a whole), start it and flip it. For a transplant, do both cousins from their own snapshots. The audit log has each snapshot path.

The loops daemon keeps delivering while these run; nothing stops a heartbeat landing in the middle.

## Inside a generation: session hooks

Two smaller things run at the edges of a session, not between generations.

**`cousin-session`** runs the hooks listed in the cousin's `cousin.toml`:

```toml
[session]
start_hooks = ["cousin-cycle inc --start",
               {name = "activity", cmd = "cousin-memory activity 'session opened'"}]
end_hooks = [{name = "sync-state", cmd = "cousin-sync-state"}]
```

```sh
cousin-session start [--skip NAME]
cousin-session end [--skip NAME]
cousin-session status
```

It needs `COUSIN_HOME`. Each entry is a command string (named `step-N` by position) or a table with `cmd` and an optional `name`. They run in order through `sh`, in the home, with `COUSIN_HOME`, `COUSIN_SLUG` and `SESSION_PHASE` set, 300 seconds each at most (a timeout is rc 124). A failing hook is reported and the rest still run; the exit is 1 if any failed. An entry without `cmd` or a broken cousin.toml is exit 2 and nothing runs. The last run is saved in `data/session.json`.

**Harness hooks** are three shell scripts in `hooks/` that spawn wires into the cousin's `.claude/settings.json` (`cousin-spawn <slug> --repair-settings` redoes it):

| script | harness event | what it does |
|---|---|---|
| `session_init.sh` | SessionStart | prints a banner: slug, home, time, which identity files and checkpoints exist |
| `pre_compact.sh` | PreCompact | writes `data/pre-compact-checkpoint.md` (current activity, last five decisions, identity file sizes) |
| `session_checkpoint.sh` | Stop | writes `data/session-checkpoint.md` (activity, open and in-progress STATUS items, last five decisions) |

They take the home as their first argument (else `COUSIN_HOME`), write only under `<home>/data/`, and always exit 0. The boot packet doesn't read these checkpoints; they're for the cousin to read after a compaction or at the next start.

## Keeping MEMORY.md small

MEMORY.md is read every session, so it has a size budget. `cousin-memory compact` retires old pointer lines from it, oldest first, until it's under 24000 bytes:

```sh
cousin-memory compact --dry-run      # what would go
cousin-memory compact [--budget 24000] [--hot-days 7]
```

A pointer line is `- [title](file.md) ... 2026-08-01 ...`. It's only retired when it's older than the hot window (7 days), doesn't contain `[pin]`, has a date it can read, and its file still exists under `memory/` and is in the search index, so nothing becomes unfindable. It stops as soon as the file fits. Before changing anything it saves a copy to `memory/.compact-snapshots/MEMORY-<timestamp>.md` (kept 14 days). `--target raw` does something else: it folds old daily raw files into monthly archives (see [memory](../memory.md)).
