# Lifecycle reference

What goes into a boot packet, what a [flip](../glossary.md#flip) does step by step, and how reincarnate and transplant work underneath. Read it when a flip went wrong or a [cousin](../glossary.md#cousin) woke up confused. For the everyday version, read [cousins](../cousins.md).

**2.0.0:** every cousin is a [runner](../glossary.md#runner) kind, and its flip is a [rollover](../glossary.md#rollover): see [Runner lane](#runner-lane). The boot packet and the flip steps below are the retired legacy tmux [lane](../glossary.md#lane)'s; 2.0.0 refuses a cousin with no `[agent] runner` before any of them run. They stay here as the record, and the packet's layers are the ones the runner's state digest shares.

A cousin's session only lasts so long before its context is full. The flip ends the session (a "generation") and starts a fresh one, and the boot packet is the text the fresh session gets typed in first so it knows who it is and what it was doing. The files in the home are what carry over; the packet is built from them.

## The boot packet

`boot.assemble(slug, home)` builds it. The flip writes it to `data/boot-packet-gen-NNNN.md` and types it into the new session. It looks like this:

```
BOOT PACKET FOR COUSIN: wren
Generation: 12
identity_hash: 3f9a0c1b2d4e
state_hash: 7c21e9aa0b13
memory_snapshot: 2026-09-18T07:30:02+00:00
DEGRADED layers: calibration, tool_surface

## 1. Framework Law
...
## 2. Shared Rules and Fleet Memory
...
## 9. Tool Surface
...

## 10. Required Boot Actions
...
```

`identity_hash` is the first 12 hex of SHA-256 over the self-portrait plus the law; `state_hash` the same over STATUS.md plus `data/handoff.md`. Compare them across generations to see what changed. The `DEGRADED` line only appears when a layer is missing something the cousin should have (see the table), so the cousin boots knowing it.

### The layers

| # | layer | built from | when it's empty |
|---|---|---|---|
| 1 | Framework Law | `<root>/config/law.md`, the same for every cousin | empty section. Not marked degraded: it's an install problem, not the cousin's |
| 2 | Shared Rules and Fleet Memory | `<root>/shared/*.md`, the canonical tier only: entries with `kind: rule` in full, every other entry as one line (file and description). Never pending proposals | empty, fine: a fresh install has no [shared tier](../glossary.md#shared-tier) |
| 3 | Cousin Self-Portrait | `<home>/self-portrait.md`, the committed portrait only (a candidate waiting for review doesn't count) | `(no committed self-portrait yet - ...)`, **degraded** |
| 4 | Operator Calibration | `memory/distilled/operator-calibration.md`; else the "Operator Calibration" section of the self-portrait (unless it still says TODO). Then the last 15 corrections from `data/corrections.jsonl`, newest first | `(no operator calibration distilled yet - degraded)`, **degraded** (also when there are corrections but no calibration) |
| 5 | Active State | the `## Open loops` section of STATUS.md (or the first 1500 characters when there's no such section), then the first 1500 characters of `data/handoff.md`. A stale warning goes on top when decisions were logged after STATUS.md was last changed | `(no active state - degraded boot)`, **degraded** |
| 6 | Current Task Packet | `data/active-threads.md` (first 1500 characters) and the last three reasoning capsules from `memory/distilled/reasoning-capsules.md` | `(no in-flight tasks - check STATUS.md)`. Only **degraded** if Active State is empty too |
| 7 | Recent Tool Trace Summary | the cousin's traced CLI calls from the last 24 hours, newest first, up to 30 | `(no substantive tool traces in last 24h)`, fine |
| 8 | Retrieved Memories | the [distilled](../glossary.md#distilled) files in `memory/distilled/` (preferences, project facts, decisions, known failures, glossary; calibration is in layer 4), the last five capsule conclusions from `memory/capsules.jsonl`, the last 60 entries from the newest 14 files in `memory/raw/`, and the first 1000 characters of MEMORY.md | empty, fine: a new cousin has no memories |
| 9 | Tool Surface | `<root>/data/tool-surface.md`, written by `cousin-tool-surface` (or its timer) | `(no tool-surface manifest ... - degraded; run cousin-tool-surface ...)`, **degraded** |
| 10 | Required Boot Actions | fixed text | never empty |

Before it reads layer 8, `assemble` regenerates `memory/distilled/` from `memory/raw/` (the same as `cousin-memory distill`), so the packet always has a fresh view. If that fails, the packet uses whatever distilled files are already there.

The stale warning in layer 5 reads like this and means: don't trust STATUS.md blindly.

```
> STALE WARNING: STATUS.md mtime is 30.5h old; 4 decision(s) logged after. Verify against data/decisions.jsonl before acting on it.
```

Layer 10 tells the new session to rebuild its objective, pick the next action from active threads, check whether it booted degraded, and carry on without announcing the restart. It also gives the four pre-exit writes, in the order the flip and the clean stop ask for: reconcile STATUS.md, write `data/active-threads.md`, save what the session learned with `cousin-memory remember` / `decide`, and LAST `data/handoff.md`, whose write ends the session. Finally it says how to record things you told it at level L0, with `cousin-memory remember ... --level operator`.

### Size

The whole packet has to fit in 8000 tokens, counted as 4 characters per token (32000 characters). Each layer has a floor and a ceiling:

| layer | min tokens | max tokens |
|---|---|---|
| law | 500 | 800 |
| self_portrait | 800 | 1500 |
| calibration | 300 | 800 |
| active_state | 500 | 1500 |
| task_packet | 500 | 2000 |
| trace_summary | 500 | 1500 |
| memories | 1000 | 4000 |
| tool_surface | 300 characters | 1500 characters |

First every layer is cut to its ceiling. The ceilings add up to more than the total, so if the packet is still too big (the limit is the total minus the fixed text and some room for headers), layers are cut to their floor one at a time in this order until it fits: tool_surface, memories, trace_summary, calibration, task_packet, active_state, self_portrait. Law is never cut in that second pass; it only has its ceiling. A cut layer ends with `... (truncated, <layer>, budget hit)` and the marker counts inside the budget.

Degraded is decided before any cutting, from each layer's own rule in the table, never by searching the text.

## The flip

```sh
cousin-flip wren                # prints the result as JSON; exit 0 when ok
cousin-flip wren --dry-run      # preflight and packet size, no restart
cousin-flip wren --confirm      # the new session posts one line in chat when it's ready
```

The console's flip button, a timed flip, a daily `flip_at` and the transcript guard all call the same function ([loops](loops.md#flips)). A cousin never flips itself.

Step by step:

1. **Concurrency guard.** If `data/.flip-in-progress.json` exists and was written less than 480 seconds ago (the handoff wait plus three minutes), another flip is running: refuse. An older marker is from a flip that died; it's ignored and overwritten.
2. **Preflight.** Nothing is touched yet. It checks that `config/agent-cmd` exists, that its `{model}` and `{effort}` placeholders can be filled (from the cousin's `[runtime]`, else `config/harness.toml [agent]`), that tmux and the agent's executable are on the PATH, and that the cousin's auth mode is usable (for `api_key`, the key file). Any failure stops the flip here with the old session still running. This runs on `--dry-run` too.
3. **Marker.** Write `data/.flip-in-progress.json` with the start time.
4. **Capture.** If the tmux session is alive, grab the last 100 lines of the pane.
5. **Handoff.** Type this into the live session:

   ```
   [cousin-flip in progress] Four pre-exit writes required, in this order:
   1. Reconcile STATUS.md ...
   2. Write data/active-threads.md, one bullet per in-flight thread.
   3. Save what this session learned that is not in memory yet:
      cousin-memory remember (...) and cousin-memory decide (...).
   4. LAST, write your handoff to data/handoff.md (...). Its write is
      the signal that you are done: the session ends right after.
   Then stop working. The framework is rebuilding your boot packet.
   ```

   Then wait up to 300 seconds for `data/handoff.md` to change, with one nudge at 150 seconds. If it never changes, the framework writes an emergency handoff itself (`# EMERGENCY HANDOFF (framework-generated)`, `degraded_state: true`, the reason, and the last 2000 characters of the pane). No live session means no prompt and no wait.
6. **Session-end audit.** Check which of STATUS.md, `data/handoff.md` and `data/active-threads.md` were written since the previous packet; the misses go to `<root>/data/audit-violations.db`. If `active-threads.md` is missing or old, write a baseline from STATUS.md's open loops. A file the cousin wrote this session is never overwritten.
7. **Transcript mining.** With `config/harness.toml` and a session id in `[runtime]`, read the dying session's transcript (`<transcripts_dir>/<session_id>.jsonl`) and save the cousin's own conclusion-like sentences as raw memory candidates for the distiller. If anything goes wrong this step is skipped with a reason; it never fails the flip.
8. **Archive.** Copy `data/handoff.md`, `data/active-threads.md`, the old boot packet and the pane capture to `data/generations/gen-NNNN/` for the generation that's ending.
9. **Generation.** Add one to `data/generation.txt` (a missing file counts as 0).
10. **Assemble.** Build the new packet and write `data/boot-packet-gen-NNNN.md`.
11. **Kill and respawn.** Kill the tmux session, mint a new session id (lowercase letters, digits and dashes), put it into the agent command's `{session_id}` placeholder if it has one, and start the cousin the normal way. The id is written to `[runtime] session_id` in `cousin.toml` even when the command doesn't use it.
12. **Inject.** Wait 8 seconds for the agent to come up, then type the packet in, preceded by `[cousin-flip] boot packet follows. Do not announce the respawn.` (with `--confirm`: post one line to chat when oriented).
13. **Done.** Remove the marker.

The flip counts as ok only if the new session id was saved: a flip that lost it didn't really work, whatever else did. The result is JSON:

```json
{"slug": "wren", "ok": true, "new_generation": 12, "boot_packet_tokens": 5210,
 "degraded_sections": ["tool_surface"],
 "stages": [{"stage": "preflight", "ok": true},
            {"stage": "capture", "alive": true, "transcript_chars": 6120},
            {"stage": "prompt_handoff", "sent": true},
            {"stage": "wait_handoff", "wrote_clean": true, "nudged": false},
            {"stage": "audit_before_exit", "violations": 0},
            {"stage": "active_threads_baseline", "wrote": false, "reason": "fresh-skip"},
            {"stage": "transcript_mine", "mined": 7},
            {"stage": "archive", "path": ".../data/generations/gen-0011"},
            {"stage": "assemble_packet", "path": ".../data/boot-packet-gen-0012.md"},
            {"stage": "respawn", "ok": true},
            {"stage": "persist_identity", "ok": true},
            {"stage": "inject_packet", "tokens": 5210}]}
```

On failure there's an `error` string and the stages up to the one that failed.

`--dry-run` skips the concurrency guard and the marker, runs preflight, skips the handoff, mining, archive and respawn, and builds the packet without writing it, so you see `new_generation`, `boot_packet_tokens` and `degraded_sections` for what a real flip would produce. Building the packet does regenerate `memory/distilled/`, so that's the one thing a dry run writes.

### A clean stop

The console's stop button on a runner cousin stops its runner and holds it down (`<home>/run/held`); a restart resumes the session. A cousin with no `[agent] runner` is refused (409).

The console runs a clean stop in the background (HTTP 202, the row turns `stopped` when it is done; the handoff wait is up to 300 seconds). A cousin that isn't running stops at once. `{"clean": false}` on `POST /api/cousins/<slug>/stop` stops at once without the handoff, and restart stays immediate: it applies a setting and comes straight back. In code the clean stop is `cousin_lib.flip.close_session`.

That is the legacy [lane](../glossary.md#lane)'s clean stop. On the [runner](../glossary.md#runner) lane (any `[agent] runner` kind) a clean stop is not `close_session` at all: it is the runner's own SIGTERM path. The console asks the [supervisor](../glossary.md#supervisor) to stop the cousin's `runner:<slug>` child without waiting for it; the runner gives the turn it is on up to 30 s (`STOP_TIMEOUT_S`), then stops on its own (the supervisor kills it 5 s after that) (no marker, no pane capture, no separate handoff prompt - the runner's own [rollover](../glossary.md#rollover) and turn-end mining cover that). The route answers 202 `stopping` while the supervisor works it, or 200 `stopped` when there was nothing running to stop; anything the supervisor itself refuses comes back as a 502 naming the reason.

### When a flip dies halfway

Nothing recovers a crashed flip automatically. A flip that fails after step 3 (say the respawn failed) leaves the marker behind. The console shows that cousin's flip as `stale_marker`. Look at what's there: if the tmux session is gone, `cousin-flip` it again (after 480 seconds the old marker no longer blocks), or start it from the console. The packet the flip built is in `data/boot-packet-gen-NNNN.md` if you want to paste it by hand.

### Runner lane

A cousin with `[agent] runner` set (`sdk`, `tmux`, `fake` or `opencode`) is not flipped through the legacy tmux lane's steps: its flip is a rollover, whatever the runner kind - including the `tmux` runner kind, which drives its own tmux pane through `TmuxRunner` rather than the legacy lane's send-keys. `cousin-flip`, the loops daemon and the lifecycle commands run in another process than the runner, so they put a `flip` row into the cousin's [inbox](../glossary.md#inbox) (the same row `Runner.rollover` puts; a pending one is joined, not doubled) and wait for its answer, up to the handoff deadline plus a minute. None of the steps above run: no marker, no pane capture of the legacy kind, no pending boot, no transcript mining of the legacy kind (the SDK and tmux runners mine every [turn](../glossary.md#turn); the opencode runner does not mine yet, see [runners](runners.md#known-gaps)).

The row's body is the reason, and the model reads it in its handoff request: `cousin-flip` by hand, `max_age` from the daily `flip_at` cadence, `timed flip` from a timed flip, the bequest text from reincarnate. The result has `"lane": "runner"` and one `rollover` stage carrying the runner's answer.

A runner that is not running is handled two ways on purpose. `cousin-flip` and the daily cadence refuse (`runner not running; start it before flipping`) and queue nothing, so a flip never undoes an operator's stop by leaving a rollover to fire at the next start. `cousin-reincarnate` and `cousin-transplant` queue the row anyway: the new role or the moved memory must take effect at the next start.

The daily cadence keeps its stagger: the loops daemon flips at most one cousin per tick, runner or tmux.

`--dry-run` on a runner cousin is ok with the rollover stage skipped.

## Reincarnate

Give a cousin a new role and keep its memory.

```sh
cousin-reincarnate wren --new-role "keeps the house paperwork in order" [--timeout 300] [--root R]
```

1. **Snapshot.** Copy MEMORY.md, STATUS.md, CLAUDE.md, cousin.toml, self-portrait.md and the whole `memory/` tree (without the search indexes, which rebuild) to `<root>/data/lifecycle/wren/<UTC timestamp>/`.
2. **Bequest.** Post a message to Wren's own chat server (`/api/send`, as user `framework`) asking her to write `data/handoff.md` in her own voice for her successor. Wait up to `--timeout` seconds (300) for the file to change. If the chat server is down or Wren stays quiet, that's written down and it carries on; the flip asks for a handoff again anyway.
3. **Rewrite.** In CLAUDE.md, the title line `# <Name> - <role>` gets the new role, and so does the body of a `## Role` section if there is one. Everything else in the file stays as it was. `[cousin] role` in cousin.toml is replaced.
4. **Flip.** The next generation boots with the new role and the old memory.

On the runner lane there is no chat prompt: the bequest (the same request, pointed at the `handoff` tool's `position` field) is carried as the rollover's reason, so the model reads it in its handoff request and answers it through `handoff`. The step records `"carried": true`. The row is queued even when the runner is stopped (see [Runner lane](#runner-lane)).

## Transplant

Move memory or identity between two cousins.

```sh
cousin-transplant --donor wren --recipient kestrel --mode soul-donation [--root R]
```

Both are snapshotted as above, the mode is applied, then both are flipped, donor first. There is no bequest step; a runner cousin is rolled over, and queued if its runner is stopped.

| mode | Kestrel ends up with | Wren afterwards |
|---|---|---|
| `soul-donation` | Wren's MEMORY.md and `memory/` in place of her own; her own identity files | unchanged |
| `body-swap` | Wren's CLAUDE.md, self-portrait and `[cousin]` name and role; her own memory | Kestrel's former identity; her own memory |
| `merge` | her own MEMORY.md, then `## Memories inherited from Wren (<date>)`, then Wren's; `memory/raw/` merged (files she lacked copied, shared files gain the lines she didn't have) | unchanged |

The slug and tmux session always stay where they are: a body swap changes who lives at an address, not the address.

Nothing is ever deleted: the donor stays, and the recipient's old files are in its snapshot.

## Records and rollback

Every step of both commands appends a line to `<root>/data/lifecycle/audit.jsonl` with `ts`, `op`, `step` and the step's details. A reincarnation writes `snapshot`, `bequest`, `rewrite`, `flip`, `done`; a transplant two `snapshot`s, `apply`, two `flip`s and `done`. `done` carries `ok`.

Both commands print their result as JSON. Exit codes: 0 when every flip worked, 1 when a flip failed (the files have already been changed), 2 when the command refused before touching anything (unknown slug, empty role, unknown mode, same cousin twice, no root).

To undo: stop the cousin, copy the snapshot's files back over the home (`memory/` as a whole), and flip it. For a transplant, do both cousins from their own snapshots. The audit log has each snapshot path.

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
