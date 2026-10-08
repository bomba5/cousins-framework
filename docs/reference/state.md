# What is authoritative

Every durable store the framework writes, what kind of state it is, who
writes it, what it is rebuilt from, and which store wins when two
disagree. A new store gets a row here: `tests/test_state_doc.py` fails
for a `.db` or `.jsonl` name in `cousin_lib` that this page does not
name.

## The levels

| Level | What it is | Rule |
|---|---|---|
| raw event | an append-only log: nothing rewrites a line | the record; everything else is built from it or beside it |
| transactional | rows or a small file updated in place, under a lock or a transaction | the record of its own domain |
| projection | rebuilt from a lower level by a named function | never authoritative: on conflict the source wins, and a rebuild fixes it |
| context | text built for the model (the system prompt, the [boot packet](../glossary.md#boot-packet) digest, checkpoints) | never authoritative, never read back as state |
| authored | written by the [cousin](../glossary.md#cousin) or the operator (identity, STATUS, notes) | authoritative for what it says; the framework rewrites only the parts named below |
| config | settings | read at start; see [configuration](../configuration.md) for what applies when |

Projections and context may be stale. Stale beats invisible: a view that
lags its source is shown with what it has, and the source decides.

## A cousin's memory

| Store (under the home) | Level | Written by | Rebuilt from / wins |
|---|---|---|---|
| `memory/raw/YYYY-MM-DD.jsonl` | raw event | `memory._append_raw` (remember, decide, record_event, obsolete), extraction, the review gate, dreaming, job results | **the memory record.** Lines leave only through trash or the monthly [fold](../glossary.md#fold) |
| `memory/raw/YYYY-MM-digest.jsonl` | projection | `raw_fold.fold_raw` | from the folded day files; the archive is the record |
| `memory/raw/archive/YYYY-MM.jsonl.gz` | raw event | `raw_fold` | byte copies of folded day files |
| `memory/distilled/*.md` | projection | `distill.distill` | from raw; text above the auto marker is authored and kept |
| `memory/capsules.jsonl` | raw event | `capsule.write_capsule` | the record; `distilled/reasoning-capsules.md` is its mirror and boot reads the jsonl |
| `memory/fts_index.db`, `memory/vectors.db` | projection | `memory_search` | from memory, notes and raw; a cache, rebuilt when stale |
| `memory/.recall-log.jsonl`, `.recall-log-archive.jsonl` | raw event | `reinforce.record` | the log of searches |
| `memory/.recall-counts.json` | transactional | `reinforce.record` | an aggregate of the recall log, never rebuilt from it; re-earned, not memory |
| `data/recall-receipts.jsonl` | raw event | `memory_search` | what proactive recall returned |
| `memory/.dream-ledger.json` | transactional | `dream_memory` | dreaming's cursor and open attempt |
| `data/dreams/*.jsonl`, `data/dreams/journal/*.jsonl` | raw event | `dreaming`, `dream_memory` | every pass and every change, for undo |
| `memory/.trash/` and its `audit.jsonl` | transactional + raw event | `memory_trash` | removed memory, restorable |
| `memory/imported/auto/` | transactional | `memory_import` | a copy of the agent CLI's own memory files; the copy is searched when current |
| `data/decisions.jsonl` | raw event | `memory.decide` | a compatibility log: every decision is also a raw entry, and raw wins (recall and consolidate count raw only) |
| `data/corrections.jsonl` | raw event | `corrections` | the operator's corrections |
| `data/review-gate.json`, `data/extract-cursor.json`, `data/propose-cursor.json`, `data/proposals.json` | transactional | `review_gate`, `runner/extract` | cursors; the held and released entries live in raw |
| `MEMORY.md`, `memory/**/*.md`, `notes/**/*.md` | authored | the cousin | its own knowledge files |

Memory written from outside the session is newer than the digest the
session booted with, and newer wins (the [runner](../glossary.md#runner) adds it to the next
prompt). Validity (`valid_until`, obsolete marks) is derived from raw and
never written back.

## A cousin's active state and identity

| Store (under the home) | Level | Written by | Rebuilt from / wins |
|---|---|---|---|
| `STATUS.md` | authored | the cousin; the handoff rewrites only the live `## Open loops` section | **authoritative for open loops**; the first copy of a section is current, later copies are history |
| `data/handoff.md` | transactional | the `handoff` tool (written last), or an emergency handoff marked degraded | where the last generation stood |
| `data/active-threads.md` | transactional | the `handoff` tool | the [threads](../glossary.md#thread) in flight at the handoff |
| `data/state.json` | projection | `sync_state.write_state` | from STATUS.md, at a handoff or `cousin-sync-state` only; STATUS wins |
| `data/session-checkpoint.md`, `data/pre-compact-checkpoint.md` | context | `runner/checkpoints` | built from last-activity, state.json or STATUS, and decisions |
| `data/last-activity.txt` | transactional | `memory.note_activity` | the latest activity line |
| `data/generations/gen-NNNN/` | raw event | `rollover.archive_generation` | copies of STATUS, handoff and threads at each [rollover](../glossary.md#rollover) |
| `data/generation.txt`, `data/generation-started.json` | transactional | `boot` | the counter and when the generation started |
| `CLAUDE.md`, `self-portrait.md` | authored | the operator, template sync for the framework part | boot reads only the committed portrait, never `.self-portrait-candidate.md` |
| `data/run/system-prompt.md` | context | `runner/prompt.system_prompt_option`, at every connect | from the law, the contract, identity, shared rules and the operator's rules |
| `cousin.toml`, `.mcp.json`, `mcp-registry.toml`, `policy.toml`, `chat-hooks.json` | config | the operator, the console, `spawn`, template sync | read at start; a runner reports an edit made since |

When decisions were logged after STATUS.md last changed, the boot digest
says so (STALE WARNING): verify STATUS against them. The STATUS a session
reads at boot is the anchor; the digest and the checkpoints are context
built from it.

## A cousin's runner and transport

| Store (under the home) | Level | Written by | Rebuilt from / wins |
|---|---|---|---|
| `data/inbox.db` | transactional | the runner's [inbox](../glossary.md#inbox), fed by chat, loops, meetings, peers, [flips](../glossary.md#flip) | the delivery queue: queued, claimed, done |
| `data/chat.db` | transactional | the chat API, the `reply` tool, Telegram | **authoritative for chat**; a reply is committed here before its inbox row closes |
| `data/sessions.db` | transactional | `runner/session_store` | the SDK transcript; a login account resumes through the CLI's own transcript instead |
| `data/stream/*.jsonl` | raw event | `runner/stream` | every runner event; the reasoning pane and runner status are projections of it |
| `data/runner-session.json` | transactional | each runner's `_save_session` | the session to resume, and the snapshot it started with |
| `data/usage.db` | raw event | `usage` | one cost row per [turn](../glossary.md#turn) |
| `data/turn-tools.jsonl` | transactional | `runner/tool_ledger` | the live turn's tool calls, reset each turn |
| `data/activity/*.log` | raw event | `activity.record` | one line per tool call |
| `data/artifacts-private.json` | transactional | `artifacts` | the private rows' paths, for their owner only |
| `data/telegram-bridge.json` | transactional | `telegram` | offsets and cursors; chat.db owns the messages |

## The install

| Store (under the root) | Level | Written by | Rebuilt from / wins |
|---|---|---|---|
| `data/jobs.db`, `data/job-logs/` | transactional + raw event | `jobs` | the job record; a closed job also lands as an L2 entry in its owner's raw memory |
| `data/artifacts.db` | transactional | `artifacts` | path, sha256, size, job, commit |
| `data/tracker.db` | transactional + raw event (`history`) | `tracker` | the backlog |
| `data/meetings.db` | transactional | `meetings` | meetings and their transcripts |
| `data/scheduled.db` | transactional | `schedule` | one-shot prompts |
| `data/loop-requests.db`, `data/loops-state.json`, `data/loops-fires.jsonl` | transactional + raw event | `loops` | timed flips and fires; the daemon's state |
| `data/outbox.db`, `data/inbound-seen.db` | transactional | `outbox`, `peer_inbound` | messages to and from other installs, deduplicated by id |
| `data/trace-ledger.db` | raw event | `trace` | the tool trace the boot packet summarizes |
| `data/audit-violations.db` | transactional | `audits` | session-end audit findings |
| `data/health.json` | transactional | `health` | the last health pass |
| `data/lifecycle/audit.jsonl`, `data/system/audit.jsonl`, `data/accounts/audit.jsonl` | raw event | `lifecycle`, the console | what was done to cousins, the system and accounts |
| `data/tool-surface.md` | projection | `tool_surface` | from the installed entry points |
| `data/console-sessions.json`, `config/console-users.json`, `data/console-prefs/` | transactional | the console | the only state the console owns; everything else it shows is a view |
| `shared/*.md` | authored | promotion through `shared_tier` | the canonical [shared tier](../glossary.md#shared-tier) |
| `shared/proposed/`, `shared/audit.jsonl` | transactional + raw event | `shared_tier` | proposals and their history |
| `shared/hive/hive.db` | transactional | the hive | tokens, the hive inbox, the shared corpus and nodes |
| `config/*` | config | the operator | [configuration](../configuration.md) |

Runtime files (`run/`, locks, sockets, pid files) hold no state worth
keeping: a restart rebuilds them.
