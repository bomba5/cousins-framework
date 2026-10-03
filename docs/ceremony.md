# What the ceremony buys

A [cousin](glossary.md#cousin) does work nobody asked for: it reads a state digest when a session
starts, answers a heartbeat every hour, writes a handoff before each new
session, and gets memory recalled, proposed and reviewed around its [turns](glossary.md#turn).
That bookkeeping is what lets it pick up tomorrow where it stopped today,
but it is paid for in model turns and tokens on your account. This page lists
each ritual: what it costs, what breaks without it, and the knob that turns it
down or off.

Every number here comes from the code (a constant, a budget or a default),
named so you can check it. What your own cousins actually spend is in the
console: the **Tokens** page (per cousin and day, with the prompt-cache hit
rate; [console](console.md#tokens)), and each turn's `result` event in the
reasoning pane or `cousin-watch <slug>`, which carries that turn's usage.

## A new cousin's first minutes

A cousin you have just spawned and started spends its first turns on
bookkeeping before you say anything:

1. **The state digest.** Spawn writes a `STATUS.md`, so the [runner](glossary.md#runner)'s first
   session starts with a digest as its first message (`SdkRunner._has_state`
   in `cousin_lib/runner/sdk.py`): one model turn.
2. **The first heartbeat.** The loops daemon has never beaten this cousin,
   so its next tick (within 30 seconds) sends a heartbeat at once, carrying
   `CLAUDE.md`, `STATUS.md` and `MEMORY.md` in full, up to 6000 characters
   each (`_compose_beat` in `cousin_lib/loops.py`): a second model turn.

After that, the hourly heartbeat is the only thing that wakes it on a
schedule. A new cousin does not [flip](glossary.md#flip) on its first day: the daily flip skips a
session that started after that day's flip time.

## At a glance

| ritual | model turns | knob (default) |
|---|---|---|
| [system prompt](#the-system-prompt) | none of its own; input on every turn, mostly cached | none |
| [state digest](#the-state-digest) | 1 per new session | none (size); how often a session starts |
| [heartbeat](#the-heartbeat) | 1 per beat | `[heartbeat] context_beat_seconds` (3600; 0 is off) |
| [rollover and the daily flip](#rollover-and-the-daily-flip) | 2 per [rollover](glossary.md#rollover) (the handoff, then the digest) | `[agent] rollover_at_percent` (80), `[lifecycle] flip_at` (the install's `default_flip_at`, `04:00`) |
| [recall](#recall) | none | `[memory] proactive_recall` (true) |
| [memory proposals](#memory-proposals) | 1 each, at most 6 a day | none |
| [review gate](#the-review-gate) | 1 separate call per batch | `[memory] review_batch` (3), `[memory] review_model` (the cousin's model) |
| [mining and distilling](#mining-and-distilling) | none | none |
| [recording](#recording) | none | none |
| [session hooks](#session-hooks) | none | `[session]` (none) |

The keys are in the cousin's `cousin.toml` unless named otherwise;
[configuration](configuration.md) has each one.

## The system prompt

**Costs.** It is input on every [turn](glossary.md#turn): the framework law
(`config/law.md`), the framework contract, the cousin's authored identity
(the authored parts of `CLAUDE.md` and its committed self-portrait), the
operator rules from the [shared tier](glossary.md#shared-tier) and the operator's
standing instructions to this cousin ([memory](memory.md#truth-levels)), appended to the agent CLI's own preset
(`compose_system_prompt` in `cousin_lib/runner/prompt.py`). None of it is
ever cut. The contract is about 6,100 characters (about 1,500 tokens) with
every tool of the shipped `config/mcp-registry.toml.example` enabled
(`cousin_lib/runner/contract.py`); the law, the identity and the rules are as
long as you write them. The prompt is kept byte for byte the same across
sessions, so after the first turn it is read from the prompt cache, which
costs a fraction of fresh input. What changes it, and so writes the cache
again once: an edit to the law, a rule, a standing instruction, the authored
identity or the self-portrait, a tool enabled or disabled in the cousin's registry, or a new
framework minor version. A running session keeps the prompt it started with;
an edit lands at the next session.

**Without it.** No law, no description of the cousin's tools, no identity:
a cousin with no authored identity on disk is told so and marked degraded.

**Knob.** None. An empty or missing `config/law.md` leaves the law out, no
`kind: rule` entry in `shared/` leaves the rules out, and a tool disabled in
the cousin's `mcp-registry.toml` (`enabled = false`) drops its part of the
contract.

## The state digest

**Costs.** One model turn at the start of every new session that has state
to carry (a `STATUS.md` or a `data/handoff.md`). The digest is at most 31,400
characters (about 7,850 tokens): 8000 tokens at 4 characters each, less 600
characters kept for its header (`DIGEST_MAX_CHARS` in
`cousin_lib/runner/prompt.py`, `TOTAL_MAX_CHARS` and `LAYER_BUDGETS` in
`cousin_lib/boot.py`). Each layer has a floor and a ceiling, in characters:

| layer | floor | ceiling |
|---|---|---|
| operator calibration | 3,200 | 8,000 |
| active state (STATUS.md open loops, the handoff) | 2,000 | 6,000 |
| task packet (active threads, reasoning capsules) | 2,000 | 8,000 |
| tool trace summary (last 24 hours) | 2,000 | 6,000 |
| memories | 4,000 | 16,000 |
| shared reference | 1,600 | 6,000 |

Over the total, layers are cut to their floor in this order until it fits:
memories, trace summary, calibration, task packet, active state, shared
reference. [lifecycle](reference/lifecycle.md#the-boot-packet) describes
each layer.

**Without it.** A new session starts knowing nothing of the last one: no open
loops, no `active-threads.md`, no memories. A digest that cannot be built is replaced by
the last handoff, marked degraded.

**Knob.** None on its size. How often it is paid is how often a session
starts: see [rollover](#rollover-and-the-daily-flip).

## The heartbeat

**Costs.** One model turn per beat, every `context_beat_seconds` (default
3600, one an hour) while the cousin's runner runs, day and night
(`tick` in `cousin_lib/loops.py`). The prompt is a fixed text plus each of
`CLAUDE.md`, `STATUS.md` and `MEMORY.md` that changed since the last beat,
inline, up to 6000 characters each (`_BEAT_INLINE_CAP`); when none changed it
is a few lines. It asks the cousin to checkpoint its state with
`cousin-memory activity`. A beat that is due with other loops goes in the
same turn. A stopped cousin gets none.

**Without it.** Nothing re-reads the cousin's own files between your
messages: an edit you make to `STATUS.md` or `CLAUDE.md` reaches it only when
it next looks, and its activity checkpoint is written only when it thinks to.
A cousin that is only ever spoken to works the same otherwise.

**Knob.** `[heartbeat] context_beat_seconds`, default 3600. The console
accepts 60 seconds to 30 days; `0` in `cousin.toml` turns it off. Spawn sets
it with `--heartbeat SECONDS`.

## Rollover and the daily flip

**Costs.** A [rollover](glossary.md#rollover) ends a session and starts the
next. It is two model turns: the handoff (the cousin writes its state through
the `handoff` tool, with up to 300 seconds, `HANDOFF_DEADLINE_S`) and the new
session's state digest. A rollover happens:

- on context pressure: after a turn that left the context at
  `rollover_at_percent` (default 80), or within 10,000 tokens of the point
  where the agent CLI would compact on its own (`pressure_due` in
  `cousin_lib/runner/rollover.py`); after one, pressure cannot trigger
  another until the context drops 10 points below the threshold or five turns
  pass;
- once a day at the cousin's flip time (the daily flip), unless its session
  started after that time that day;
- when you flip it by hand, or reincarnate or transplant it.

**Without it.** A session grows until the agent CLI compacts it itself, which
summarizes away detail the framework did not choose and writes no handoff.
Every turn re-sends the whole context, so a long session makes each turn
dearer even with the cache.

**Knob.** `[agent] rollover_at_percent`, default 80: higher means fewer
rollovers and longer, dearer sessions. `[lifecycle] flip_at`, default the
install's `default_flip_at` in `config/harness.toml`, itself `04:00`;
`"never"` (or `"off"`, `"none"`, `"no"`) turns the daily flip off for one
cousin or, in `default_flip_at`, for every cousin without its own. The
pressure rollover has no off switch.

## The handoff

**Costs.** Part of the rollover's first turn: one `handoff` tool call that
writes STATUS.md's open loops, `data/active-threads.md`, what the session
learned (as memories) and, last, `data/handoff.md`. With the rest of the
rollover it also archives those files under `data/generations/gen-NNNN/`.

**Without it.** The next session starts from whatever the last one left on
disk. If the cousin does not call the tool in time, the runner writes an
emergency handoff from the last 2000 characters of the session, marked
`degraded_state: true`, and the generation still ends.

**Knob.** None: it is the rollover.

## Recall

**Costs.** No model turn. Before an operator's or a person's chat message
reaches the `sdk` kind, the runner searches the cousin's memory and adds one
`[fw-recall]` line naming up to three matching memory files
(`cousin_lib/runner/hooks.py`, `cousin_lib/memory_search.py`). It waits at most
4 seconds (`RECALL_BUDGET_S`) and goes on without recall past that. With an
embedding service, the search embeds the message (at most 6000 characters of
it) and may bring up to 24 changed chunks of the index up to date. Messages
shorter than 24 characters, and other kinds of row (loops, heartbeats, peers),
get no recall.

**Without it.** The cousin sees only what it remembers to search for.

**Knob.** `[memory] proactive_recall`, default true. Without an embedding
service recall is off unless `[memory] recall_keyword_only` is true (default
false). `config/embedding.toml` `[recall]` sets `min_chars` (24),
`min_score` (0.45) and `top` (3); [memory](memory.md) has the details.

## Memory proposals

**Costs.** One model turn each, on the `sdk` kind. After a turn whose text
states a conclusion with a decision word in it ("decided", "the rule is",
"root cause" and a few more) and that saved no memory itself, the runner
queues a short message naming up to three such sentences and asking the
cousin whether to keep them (`propose_turn` in
`cousin_lib/runner/extract.py`). It runs after everything else queued (the
lowest priority), never follows its own proposal turn, and is capped at six
per cousin per rolling 24 hours (`PROPOSAL_CAP`).

**Without it.** A conclusion the cousin did not save survives only as a
mined `episode:` line, not as a memory it chose to keep.

**Knob.** None.

## The review gate

**Costs.** On the `sdk` kind, when a cousin writes more than `review_batch`
memories on its own topics since the gate last looked, the new entries are
held, and a separate model call reviews them in the background: up to 20
entries per call, each shown in at most 600 characters, with no tools, a
180-second timeout and up to two tries per entry (`cousin_lib/review_gate.py`,
`cousin_lib/runner/sdk.py`). It never holds up the cousin's next turn. Its
usage is recorded as `review-gate`.

**Without it.** A burst of memory writes enters the [distilled](glossary.md#distilled) views and the
next digest unreviewed.

**Knob.** `[memory] review_batch`, default 3: a higher number holds less
often, and a very high one effectively turns the gate off. `[memory]
review_model` picks a cheaper model for the review (default: the cousin's
own).

## Mining and distilling

**Costs.** No model turns. After every turn the `sdk` and `tmux` runners pick
sentences out of the turn by keyword and write them to raw memory, at most 24
per rolling 24 hours (`WINDOW_CAP` in `cousin_lib/runner/extract.py`).
Building a digest first regenerates `memory/distilled/` from raw memory (six
files of at most 40 lines each; `cousin_lib/distill.py`); its time grows with
the cousin's history.

**Without it.** The digest's memories layer and the proposals have nothing
fresh to read.

**Knob.** None. `cousin-sweep` (weekly, through its systemd timer) compacts
raw memory ([operations](operations.md)).

## Recording

**Costs.** No model turns. Every tool call is recorded by the runner's hooks
(a job row for each subagent and background shell in `data/jobs.db`, an
activity line, the trace ledger), which costs a little disk and time per
call. One part does reach the model: the digest's tool trace summary, at most
30 calls from the last 24 hours within its layer's budget.

**Without it.** No job list in the console or in `cousin-job list`, no
record of what the cousin ran, and a digest that cannot say what the last
session did.

**Knob.** None.

## Session hooks

**Costs.** Nothing by default: spawn writes no `[session]` table. Hooks you
add run as shell commands at each session's start and end
([cousins](cousins.md#session-hooks)).

**Knob.** `[session] start_hooks` and `end_hooks`, empty by default.
