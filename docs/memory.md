# Memory

How a [cousin](glossary.md#cousin) remembers: where each kind of memory lives, what writes
it, what a fresh session reads back, and how to search, share and
remove it. Read this when you want a cousin to keep something, or when
you're trying to work out why it forgot.

## The short version

All of a cousin's memory lives in its home, `cousins/<slug>/`. Other
cousins can't see it. The cousin writes to it with `cousin-memory`:

```
export COUSIN_HOME=$PWD/cousins/wren

cousin-memory decide "port range" "cap at 8200" "leaves room for the hive"
cousin-memory remember "espresso machine" "descale every 200 shots" \
    --level operator --cite "chat #412, 2026-09-17"
cousin-memory search "descaling"
#   -> 1. [0.017] [memory] .../cousins/wren/memory/upkeep.md
#         # Upkeep The espresso machine needs [descaling] every 200 shots.
```

Every `cousin-memory` command needs `COUSIN_HOME` (or `--home`). Without
it the command refuses instead of guessing, because a guessed home means
reading and writing some other cousin's memory.

When a new session starts (a spawn, a [flip](glossary.md#flip), a restart), the framework
hands the cousin a boot packet built from the layers below. So what a
cousin "knows" at the start of a session is: its identity files, its
open loops and handoff, the [distilled](glossary.md#distilled) views of its raw memory, its last
few reasoning capsules and recent raw entries, and the head of its
`MEMORY.md`. Anything else it has to search for.

## The layers

Everything below is relative to the cousin home.

| Layer | Where | Written by | Read by |
|---|---|---|---|
| Active state | `STATUS.md`, `data/handoff.md`, `data/handoff-manual.md`, `data/active-threads.md`, `data/session-checkpoint.md`, `data/pre-compact-checkpoint.md` | the cousin; the checkpoint files by the harness hooks | boot packet, heartbeat, session start hook |
| Index | `MEMORY.md` | the cousin | boot packet (the first 1000 characters), heartbeat |
| Raw entries | `memory/raw/YYYY-MM-DD.jsonl` | `decide`, `remember`, transcript mining after each [turn](glossary.md#turn), a transplant merge | distiller, boot packet |
| Monthly digests | `memory/raw/YYYY-MM-digest.jsonl` | the raw fold | distiller, boot packet |
| Raw archive | `memory/raw/archive/YYYY-MM.jsonl.gz` | the raw fold | search (an entry also in a monthly digest is indexed once), explorer |
| Distilled views | `memory/distilled/*.md` | the distiller | boot packet |
| Decisions | `data/decisions.jsonl` | `decide` | `consolidate`, the boot packet's staleness warning (a compatibility log: `recall` reads raw memory, and the one-time backfill (triggered by recall, search, consolidate) copies the decisions only this log holds into raw) |
| Memory and notes files | `memory/**/*.md`, `notes/**/*.md` | the cousin | search |
| Reasoning capsules | `memory/capsules.jsonl`, mirrored to `memory/distilled/reasoning-capsules.md` | `cousin-reason capsule` | boot packet, search (the mirror) |
| Corrections | `data/corrections.jsonl` | the chat send path, from your messages | boot packet (calibration layer) |
| Raw entries | `memory/raw/*.jsonl`, `memory/raw/archive/*.jsonl.gz` | `cousin-memory decide` and `remember`, the transcript miner, the jobs ledger, framework events | distill, **search**, `recall` |
| Harness auto-memory | the directory `config/harness.toml` names in `auto_memory_dir` | the agent harness itself (switched off by the `sdk` and `tmux` [runner](glossary.md#runner) kinds) | search (a file whose imported copy is current is found as the copy), explorer, `import-auto` |
| Imported auto-memory | `memory/imported/auto/*.md`, `.manifest.json`, `.baseline.json` | `cousin-memory import-auto --apply` | search (collection `memory`), `import-auto --verify` |
| Search indexes | `memory/fts_index.db`, `memory/vectors.db` | search, `reindex` | search |
| Recall log | `memory/.recall-log.jsonl`, `memory/.recall-counts.json` | every search | search ranking, explorer |
| Trash | `memory/.trash/` | removals from the console explorer | `cousin-memory trash restore` |
| Legacy | `legacy/` | a migration (the old home, archived whole) | the explorer only |

A few notes on the ones that aren't obvious.

**Active state** is what the cousin is doing right now. `STATUS.md`
holds the open loops, `data/handoff.md` is what the last generation
told the next one, and `data/active-threads.md` is one bullet per thread in flight.
The `handoff` tool writes them when a flip ends a session (its `active_threads`
list when the cousin gives one).
`data/handoff-manual.md` is for a handoff written by hand; the
framework never writes it. The two checkpoint files come from the
Claude Code hooks in `hooks/` (see [cousins](cousins.md)).
If decisions were logged after `STATUS.md` last changed, the boot
packet warns the cousin that STATUS may be stale.

**The open loops are one section.** The live section is the first bare
`## Open loops` heading on a line of its own, up to the next `#` or `##`
heading: the section the handoff writes (its `status` is the body; the
framework writes the heading, drops a leading "Open loops" heading the
model wrote itself and demotes a `#` or `##` heading inside to `###`).
The digest, the boot packet, `data/state.json`, the session-end baseline
and the checkpoints all read that section and nothing else. A suffixed
heading (`## Open loops (current as of gen 4)`) is history the cousin
kept, never the live section, and so is `### Open loops archive`. One
exception reads homes written before the handoff normalised its
`status`: when the bare section is empty and the very next heading is a
suffixed open-loops heading, that section is read as the live one, until
the next handoff writes the bare section.

**`MEMORY.md`** is an index: one line per topic file, like
`- [Upkeep](upkeep.md) - descaling schedule, 2026-09-17`. It loads every
session, so keep it short. Links are relative to the home or to
`memory/`.

**Raw entries** are the append-only record every writer feeds. One JSON
object per line:

```json
{"timestamp": "2026-09-18T15:08:49+00:00", "topic": "espresso machine",
 "content": "descale every 200 shots", "truth_level": "L0_OPERATOR",
 "source": "remember", "cite": "chat #412, 2026-09-17"}
```

**Corrections** are captured when a chat message is stored (the console,
`cousin-chat`, the Telegram bridge): when a message from
the configured operator contains "stop", "don't", "no", "actually",
"instead" and similar, it's recorded with its class, and the boot
packet shows the recent ones next to the operator calibration. Only the
operator named in `cousin.toml [operator]` counts; a peer cousin's "no"
isn't calibration.

**Harness auto-memory** is the agent's own memory directory (for Claude
Code, `~/.claude/projects/<encoded home>/memory`). The framework never
writes it. It only searches and displays it, and only when
`config/harness.toml` sets `auto_memory_dir` and the directory exists.

## Writing memory

```
cousin-memory decide TOPIC DECISION REASONING [--level L] [--cite SRC]
cousin-memory remember TOPIC FACT [--level L] [--cite SRC]
cousin-memory activity "what I'm doing now"
cousin-memory recall [KEYWORD] [--last N]
```

`decide` appends to `data/decisions.jsonl` and also writes a raw entry
(`"source": "decision"`, content `<decision> - why: <reasoning>`), so a
decision reaches the distilled views. `remember` writes only the raw
entry, for a fact that isn't a decision. `activity` overwrites
`data/last-activity.txt` with a timestamped one-liner. `recall` reads
raw memory through the same index `search` uses: with a keyword it
prints the best-ranked entries (with an embedding service configured,
only those the keyword matched or that clear `[recall] min_score`),
without one the newest entries you wrote (the framework's own log,
topics starting `episode:`, `job:` or `framework:`, is left out),
oldest first either way. A decision prints with its `Why:` line, a
folded month's digest of one too. Times are the raw entry's own: UTC for
what `decide` writes now. `recall` is not a search you made, so it
changes no recall weighting. `data/decisions.jsonl` is still written,
for the boot packet's staleness warning and the hooks; the first
`recall` or search in a home copies every decision that only that log
(or one of its rotated archives) holds into raw, once, with its original
time (most old ones carry a local time without an offset), and marks it
done in `data/.decisions-backfilled`. `consolidate` counts raw only, so
a decision is counted once. A decision line you removed through the
console's trash is not brought back by the backfill: a trashed raw
twin counts as already handled. If the backfill itself fails (a
read-only or full `data/`, a bad byte in the log), `search` and `recall`
still answer from what raw memory already holds; one line goes to
stderr and no mark is written, so the next read tries again.
`consolidate` runs the backfill unguarded, since it is a command you run
and a loud failure there is the right one.

If the decision text has backticks or `$(...)` in it, don't pass it as
arguments: your shell runs them before `decide` sees the text. Use the
stdin form with a quoted heredoc, three chunks split by a line that is
exactly `---`:

```
cousin-memory decide --stdin <<'EOF'
wrapper scripts
---
the wrapper is `exec python3 $lib "$@"`
---
$(uname -a) is data here, not a command
EOF
```

A cousin using the MCP `memory` tool gets this for free: arguments go
over JSON, with no shell in between ([mcp](mcp.md)).

Plain Markdown files are memory too. Anything the cousin writes under
`memory/` or `notes/` is searchable. Writing somewhere else in the home
means search won't find it.

One writer at a time: the writes to the file-backed stores (the raw day
file, `data/decisions.jsonl` and its rotation, the decisions backfill, the
recall counts and log, the distilled views with the read of raw behind them,
the extraction and proposal cursors, `data/last-activity.txt`, the raw fold
(`compact --target raw`: a month's read, archive and removal of its day
files) and the trash's check-and-replace of a raw or decisions file) hold an
flock on `data/.memory-write.lock` while they read and write. Two sessions
of one cousin (see `[agent.sessions]` in [configuration](configuration.md)),
the cousin's own `cousin-memory` commands and the [runner](glossary.md#runner)'s per-turn miner can
all write at once, and none of them loses the other's entry. The raw fold
needs it although it folds only days older than the hot window: the
decisions backfill writes a decision into the raw file of its own day, which
is often one of those. Outside the lock: `cousin-memory import-auto` (an
operator act, into `memory/imported/`) and the `MEMORY.md` index compaction
(`compact --target index`), which write no store the lock covers.

## Truth levels

Every raw entry carries a truth level, so the cousin (and you) can tell
what you said from what it guessed.

| Level | `--level` | What it's for |
|---|---|---|
| `L0_OPERATOR` | `operator` | Something you said. Needs `--cite`. |
| `L1_FRAMEWORK` | `framework` | Something the framework observed: a flip happened, a loop fired. |
| `L2_TOOL` | `tool` | A measured result: what a command printed, what a job returned. |
| `L3_COUSIN_CONCLUSION` | `conclusion` | The cousin's own conclusion. The default. |
| `L4_COUSIN_HYPOTHESIS` | `hypothesis` | A guess the cousin hasn't checked yet. |
| `L5_OBSOLETE` | `obsolete` | Something that used to be true and was superseded. Kept for history. |

`--level operator` without `--cite` is refused. It's the strongest claim
a memory can make, so it has to say where you said it: a chat message
id, a quote, a date. Operator entries go to
`memory/distilled/operator-calibration.md`, which the boot packet
carries as its own layer, ahead of everything the cousin concluded on
its own.

```
cousin-memory remember "deploy window" "never on Fridays" \
    --level operator --cite "chat #1180"
cousin-memory decide "cache size" "try 512 MB" "matches the last profile" \
    --level hypothesis
```

Older entries may carry short forms: `operator-stated` reads as L0,
`cousin-conclusion` as L3, and an entry with no level as L3. The boot
packet's required actions remind every cousin to record what you told
it at L0 with a citation and its guesses at `hypothesis`.

L0 and L3 are mostly written by the cousin itself. The other levels
fill themselves in:

- **L1 framework.** The framework notes what it changes about a cousin,
  under topics like `framework:flip`, `framework:session`,
  `framework:model`, `framework:auth`: flips (new generation, session,
  clean or emergency handoff), starts and stops, model, effort and auth
  mode changes, chat history imports, role changes, memory transplants,
  and a flip that died halfway.
  Nothing is written when nothing changed, and there are no periodic
  entries.
- **L2 tool.** When a cousin's job finishes as done or failed, its
  title, exit code and summary land in that cousin's memory under
  `job:<title>`. Repeat runs of the same job fold into one line.
  Cancelled jobs and jobs with no owning cousin are skipped.
- **L4 hypothesis.** When the runner mines a turn's transcript, a
  sentence that hedges ("probably", "might", "I suspect", "I think",
  "likely", "maybe", "not sure", "seems") is kept as a hypothesis under
  `episode:<id>:hypothesis` instead of as a conclusion.
- **L5 obsolete.** When something stops being true, retire the topic:

  ```
  cousin-memory obsolete "backups" --why "moved the NAS snapshot to 03:00"
  ```

  The topic drops out of the distilled files the next session boots
  from, but the history stays in raw. A reason is required. A topic
  with no raw entries is refused unless you add `--force` (it suggests
  close matches). Any later entry on the same topic brings it back. The
  console explorer has the same thing as a "mark obsolete" button on
  each raw entry, and the MCP memory tool has an `obsolete` command.

## Valid time, tensions and the review gate

Raw memory is append-only, so when a claim was true is worked out from
raw rather than written into it. Every entry is valid from when it was
written (or its own `valid_from`) until an obsolete mark covers it (or
its own `valid_to`). `cousin-memory history <topic>` lists a topic's
claims with an id each and `live` or `valid to <time>`. To retire one
claim and keep the topic, pass its id:

```
cousin-memory obsolete "spare keys" --why "the tin moved to the shed" --entry 3f9a1c0b27de
```

`cousin-memory tensions` lists the authored topics that have two or
more live claims saying different things, such as a correction written
beside the claim it corrects. It does not judge which one is right: you
settle a tension by retiring the claim that is no longer true. The
console has the same list at `GET /api/memory/{slug}/tensions`.

When more than `[memory] review_batch` new entries on authored topics
(default 3) have been written since the review gate last looked, the gate
holds all of them. On the `sdk` runner kind it looks after every [turn](glossary.md#turn) (the other kinds do not run it); the
count is per cousin, so entries from a turn that crashed or ended in an
error are caught at the next look. A held entry stays in raw and search
still finds it, but it stays out of the distilled views and out of the
recent memory a new session starts with, until it is kept or dropped. On
the `sdk` kind a second model reviews the batch in the background after
the turn. Anything it does not settle, or everything if the review fails,
waits for you:

```
cousin-memory review                       # what is held
cousin-memory review --keep 3f9a1c0b27de 81d2e4f09a3c
cousin-memory review --drop 5c7e9b1d2a40 --why "a duplicate of the backups topic"
```

Run the verdicts from your own shell: `--keep` and `--drop` refuse to run
inside a cousin's own process tree, so a cousin cannot release what it
wrote itself. That guard is best effort: it looks at the process's
ancestors, so a process detached from the tree (`setsid`, `nohup`) is not
recognised, and any raw line that records a release releases the entry.
A new session's boot packet says how many entries are held. Each verdict records who gave it. A drop retires the entry
the same way `obsolete --entry` does. The framework's own log
(`episode:`, `job:`, `framework:`) is not gated: its writers already cap
how much they write.

## The distilled views

`memory/distilled/` holds six files the boot packet reads as the
cousin's long-term floor:

- `preferences.md`
- `project-facts.md`
- `decisions.md`
- `known-failures.md`
- `operator-calibration.md`
- `glossary.md`

They're rebuilt from raw every time a state digest is built (a
flip), every time the cousin is started or resumed, by the loops
daemon within a tick of any raw write (it compares the raw files
against `memory/.last-distill`, which every distill touches),
and by `cousin-memory distill` and `cousin-memory consolidate`. No model is
involved and the result is the same every run. Entries are grouped by
topic and the newest entry per topic becomes the line; older entries
show up as a count:

```
- [L3_COUSIN_CONCLUSION] keep 30 days - why: audits need a month (2 entries, superseded 1 earlier, 2026-09-17; topic: retention window)
```

Which file a topic lands in: L0 entries go to `operator-calibration.md`.
Otherwise it's a whole-word match on the topic and source, for example
"bug", "failed" or "lesson" go to `known-failures.md`, "preference",
"tone" or "style" to `preferences.md`, "host", "api" or "config" to
`project-facts.md`, "glossary" or "term:" to `glossary.md`. Everything
else goes to `decisions.md`. Each file keeps the top 40 lines
(`--max-lines` changes it), ranked by entry count then recency, except
that the framework's own log (topics starting `episode:`, `job:` or
`framework:`) ranks after everything you wrote: it fills only the lines
your memory leaves. Search finds those entries either way.

You can write in these files. Everything above the line
`<!-- distilled:auto - lines below are regenerated from memory/raw; edit above this line only -->`
is kept as-is on every run. Everything below it is rewritten. An empty
file holds the stub `_(empty - awaiting distillation)_`.

`consolidate` also prints topics with three or more entries in raw
memory (which holds every decision too), as candidates for a proper topic file in
`memory/` with a line in `MEMORY.md`.

## Reasoning capsules

A decision records what the cousin chose. A capsule records why, short:
the conclusion, the evidence, what it ruled out, and how sure it is.

```
cousin-reason capsule --conclusion "cap the port range at 8200" \
    --evidence "leaves room for hive" \
    --evidence "no collision with the console" \
    --rejected "unbounded range" --confidence high --topic "port range"
cousin-reason list --n 5
```

The record is `memory/capsules.jsonl`. The readable mirror,
`memory/distilled/reasoning-capsules.md`, is what search finds; the
distiller never touches it. The boot packet carries the last three
capsules in full and the newest five conclusions as one line each.

## Keeping it bounded

Memory only grows, so three things keep it in check. None of them
deletes anything.

```
cousin-memory compact --target raw --hot-days 30
cousin-memory compact --target index --budget 24000 --hot-days 7 --dry-run
cousin-sweep compact --target both      # every cousin, for a timer
```

- **Raw fold** (`--target raw`). Daily raw files older than
  `--hot-days` (default 30) are appended byte for byte to
  `memory/raw/archive/<YYYY-MM>.jsonl.gz` and removed. Each month keeps
  one digest entry per topic in `memory/raw/<YYYY-MM>-digest.jsonl`, so
  the distiller still sees the topic. There's no dry run because nothing
  is lost.
- **Index compaction** (`--target index`). When `MEMORY.md` is over
  the byte budget (default 24000), the oldest pointer lines are removed
  until it fits. A line only goes if it has a date, is older than
  `--hot-days` (default 7), isn't marked `[pin]`, and its file exists in
  `memory/` and is in the search index, so the topic stays findable. A
  snapshot goes to `memory/.compact-snapshots/` first (kept 14 days).
  It exits 1 when there's no `MEMORY.md`.
- **Decisions rotation.** When `data/decisions.jsonl` passes 1 MB, all
  but the newest 200 lines move to
  `data/decisions-archive-YYYYMMDD.jsonl`. Automatic, on the next
  `decide`.

The `cousin-sweep` timer in `systemd/` runs both compactions over every
cousin; see [operations](operations.md).

## What the boot packet reads

The boot packet is what a new session starts from, in two parts. The
system prompt, never cut, holds:

1. Framework law (`config/law.md`), not memory, but first.
2. The operator rules: the [shared tier](glossary.md#shared-tier)'s `kind: rule`
   entries in full.
3. The identity: the authored parts of `CLAUDE.md` and the committed
   self-portrait.

The state digest, the session's first message, has a ceiling of about
8000 tokens and holds:

4. Operator calibration: `operator-calibration.md`, plus recent
   corrections.
5. Active state: the open loops from `STATUS.md` and the latest
   `data/handoff.md`.
6. Task packet: `data/active-threads.md` and the last three capsules.
7. Tool trace summary.
8. Retrieved memories: the other five distilled files, the newest
   capsule conclusions, recent raw entries (up to the last 60 lines
   from the newest 14 raw files) and the head of `MEMORY.md`.
9. Shared reference: a one-line index of the rest of the shared tier.

When the digest is too big, the memories are cut first. A
layer that's missing (no identity, no active
state) is named at the top as degraded, so the cousin knows it's
booting short. An empty memories layer is normal for a new cousin and
isn't flagged. The step-by-step is in
[reference/lifecycle.md](reference/lifecycle.md).

Between boots, the loops daemon's context heartbeat re-sends
`CLAUDE.md`, `STATUS.md` and `MEMORY.md` whenever they change
([jobs and loops](jobs-and-loops.md)).

## Search

```
cousin-memory search "descaling"
cousin-memory search "descaling" --top 10 --collection notes
cousin-memory search "descaling" --json
cousin-memory reindex
```

Search covers four collections: `memory` (`memory/**/*.md`), `notes`
(`notes/**/*.md`), `raw` (the raw store, one unit per entry: every
`memory/raw/*.jsonl` line and every entry in the monthly archives) and
`harness` (the harness auto-memory directory, when configured).

A raw entry is indexed on its own rather than as part of its day file,
because a day mixes unrelated topics, which ranks badly and embeds
worse. A `raw` hit's `path` is the file with the entry's line number
appended, `memory/raw/2026-09-21.jsonl#43`, so two entries in one file
are two hits. The same memory is indexed once however many files hold
it: `raw_fold` keeps a month in both `<YYYY-MM>-digest.jsonl` and
`archive/<YYYY-MM>.jsonl.gz`, and the twins differ only in metadata.

This is what makes `distill` a convenience rather than the only door.
Before it, an entry reached recall solely as one truncated line per
topic, capped at 40 lines per distilled file. `--collection` limits it to one. `--json` prints the
hits as a list with `path`, `collection`, `score`, `similarity`,
`snippet` and `chunk`; any warning goes to stderr so the JSON stays
clean. The trash is never searched.

The keyword leg is always there: SQLite FTS5 in `memory/fts_index.db`,
with nothing to install. In Docker the semantic leg is on from the first
start too (below); on a bare host search is keyword only until you add an
embedding service. Every cousin's indexes
(keyword and, with an embedding service, vectors) are kept level with its
files by the loops daemon: each home is checked every 5 minutes and only
what changed is embedded, one home at a time, so the embedding service is
never hit by several homes at once. A search still refreshes the index
itself if it finds it behind, so you rarely need `reindex`.

### Semantic search

With an embedding service, search also finds things by meaning.

**In Docker** there is nothing to do. `compose.yml` runs Ollama as the
`embeddings` service, which pulls `nomic-embed-text:v1.5` on its first
start, and the framework's entrypoint writes `config/embedding.toml`
pointing at it (`http://embeddings:11434/api/embeddings`, `timeout_s =
120`) when the file is absent; a file you wrote is never overwritten.
Until the model is pulled, or when the service is down, search gives
keyword results plus a notice. Your own Ollama instead, or
none: [install](install.md#your-own-ollama-or-none).

**On a bare host**, copy `config/embedding.toml.example` to
`config/embedding.toml`:

```toml
url = "http://localhost:11434/api/embeddings"
model = "nomic-embed-text"
timeout_s = 120
```

The service gets `{"model": ..., "prompt": ...}` and must answer
`{"embedding": [...]}`. Ollama does that out of the box:

```
curl -fsSL https://ollama.com/install.sh | sh     # about 2.4 GB, even CPU-only
ollama pull nomic-embed-text
```

`timeout_s` (default 10) has to be longer than one chunk takes to
embed, or every search waits it out and falls back to keyword. On a
CPU without AVX that was 32 seconds, so use 120 on a CPU-only box; 30
is plenty with a GPU. Files are embedded in chunks of `chunk_chars`
(default 2000) overlapping by `chunk_overlap` (default 200); a last
piece that would add fewer new characters than the overlap joins the
chunk before it instead of standing alone. A hit says which chunk
matched.

The vectors live in `memory/vectors.db`. Each search embeds only
new or changed chunks and drops deleted files, so the first search
after a lot of writing is slow and the rest are quick. `reindex`
rebuilds the keyword index and, with a service configured, re-embeds
everything.

Only one refresh runs per cousin at a time (a lock on
`memory/.embeddings.lock`). A search that finds another refresh
running doesn't start a second one: it ranks against the index as it
stands and says so in its notice. `reindex` waits its turn instead. A
long refresh saves what it has every 32 embedded chunks, so a pass
that dies partway (a killed process, a proactive recall past its
budget) leaves its work behind for the next one.

On a CPU-only host an embedding service can take every core for each
request. The `[options]` table in `config/embedding.toml` is passed to
the service as is; for Ollama, `num_thread` caps the cores one
embedding uses:

```toml
[options]
num_thread = 8
```

Keyword and semantic results are merged by rank. Each hit's `score` is
that merged rank score, and `similarity` is the semantic cosine (`null`
when only the keyword side found it). Use `similarity` if you want a
"relevant enough" cutoff.

If the service is configured but down, or the file is broken, search
still returns keyword results and prints a notice saying so. It never
pretends you have semantic search when you don't. Without the file
there's no notice, since you never asked for it.

### Recall weighting

Every search records the files it returned in
`memory/.recall-log.jsonl` and `memory/.recall-counts.json`. Files that
keep coming up get a small boost on later searches: at most 15%, halving
for every 14 days the file isn't recalled. It breaks ties, it doesn't
beat a better match. Delete the two files to reset it.

### Importing the agent CLI's own memory

The `sdk` and `tmux` runner kinds switch the agent CLI's own auto-memory
off (`CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`), so framework memory is the only
one. What the CLI kept before that [folds](glossary.md#fold) in once:

```
cousin-memory import-auto                      # a dry run: what would be copied, nothing written
cousin-memory import-auto --apply --verify     # copy into memory/imported/auto/, then check it
```

Every `*.md` in the directory `config/harness.toml` names in
`auto_memory_dir`, the CLI's own `MEMORY.md` index included, is copied
to `memory/imported/auto/<name>` with its frontmatter kept and three
keys added: `imported_from`, `imported_sha256`, `imported_at`. Backups
and databases in that directory are listed and skipped. Running it
again copies only what changed since; it never overwrites a copy you
edited (a conflict, to merge by hand) and never brings back a copy you
removed (`dropped`). Once a file's copy is current, search finds the
copy instead of the original, so one memory is one hit, and the copy
inherits the original's recall weighting. The imported index is
searchable like any memory file; nothing puts it in your context by
itself.

A manifest it cannot read (missing is fine; unreadable or not valid
JSON is not) refuses instead of guessing: `import-auto` prints one
`ERROR:` line naming the manifest on stderr, writes nothing, and exits
2. Search stays tolerant of the same manifest: it treats it as empty
rather than erroring, so a corrupt manifest degrades recall, never
crashes it. A copy on disk with no row in the manifest is a conflict to
merge by hand, unless it is byte-for-byte what this import would have
written (a run that died after copying a file but before saving the
manifest converges instead of blocking forever).

The check is before and after. Just before `--apply` writes, it replays
the newest 50 (`--sample N`) of your own logged searches
(`memory/.recall-log.jsonl`) that surfaced a file from that directory
and keeps what each surfaces now; `--verify` replays the same queries
and reports any that lost a memory, as itself or as its copy. Both first
bring the search indexes fully up to date, so they compare the import,
not an index still filling in. It exits
1 on a loss and 2 when nothing was compared (no baseline yet, or no
logged query ever reached that memory). A `--verify` over a manifest it
cannot read exits 2 the same way as `--apply`, without comparing
anything. Neither replay counts as a recall, so neither changes the
ranking it measures.

### Memory proposals on the SDK lane

After a turn in which the cousin reached a decision (a sentence the
transcript miner keeps that also says "decided", "agreed", "from now
on", "the fix is" and the like) and recorded nothing with the memory
tool, the runner queues one question: those sentences, and whether any
is worth keeping. It runs when nothing else is waiting, never about its
own turn, and at most `PROPOSAL_CAP` times a rolling day
(`cousin_lib/runner/extract.py`). The answer is an ordinary `remember`
or `decide`, under a topic the cousin chose. A step that fails while
building the proposal (a broken store, a corrupt cursor or cap file)
never falls back to silence: it surfaces as the `propose` event's own
`error`, the same event a normal turn's proposal (or its absence) uses,
and the turn still delivers.

### The review gate on the SDK lane

After every turn, whether it ended in an answer or an error, and once
when it starts, the runner asks the review gate what was written on
authored topics since it last looked (`cousin_lib/review_gate.py`). Over
`[memory] review_batch`, the entries are held and a second model reviews
them in the background, at most 20 entries per call: each call has no
tools and keeps no session, and runs on
the cousin's own account, on `[memory] review_model` or else the
cousin's own model (`SdkRunner._model_review`). The next message does
not wait for it. Its usage is recorded like a turn's. The outcome is a
`review_gate` event in the runner's [stream](glossary.md#stream): how many were held, kept,
dropped and still pending, and the error if the review failed (`cancelled`
when the runner stopped first). The reviewing model may keep an entry recorded at
operator level but never drop one: a drop cannot be undone, so that
drop is yours, and the entry stays held until you give it. Anything the review did not settle stays
held for `cousin-memory review`; a later start of the runner offers it to
the reviewer once more. On the `tmux` and `opencode` kinds nothing runs
the gate after a turn.

### Proactive recall in chat

When you (the operator) send a cousin a message of at least 24
characters, the SDK runner's prompt hook searches the cousin's memory and
adds one line to what the cousin sees (the other runner kinds do not
recall):

```
[fw-recall] possibly relevant from your memory: Upkeep (memory:upkeep.md) - cousin-memory search for details; ignore if not.
```

Titles and paths only, never file contents, and your stored message is
untouched. With semantic search configured, a hit needs a similarity of
at least 0.45. Without it nothing is appended, unless the cousin sets
`recall_keyword_only = true`, which lets any keyword hit through. The
thresholds are the `[recall]` table in `config/embedding.toml`
(`min_chars = 24`, `min_score = 0.45`, `top = 3`). Per cousin, in
`cousin.toml`:

```toml
[memory]
proactive_recall = true        # false turns it off for this cousin
recall_keyword_only = false
```

## The shared tier

A cousin's memory is private. To share a file with every cousin, it
goes through review: a cousin proposes, someone else promotes. The
proposer and the reviewer can never be the same person or cousin, and
no setting changes that.

The files live under the framework root:

- `shared/<file>.md` is the canonical, shared copy.
- `shared/proposed/<slug>__<file>.md` are pending proposals.
- `shared/audit.jsonl` logs every propose, overwrite, promote and
  reject.

First name your reviewers in `config/shared-reviewers.json`. Without it
every promotion is refused.

```json
{"reviewers": ["ana"]}
```

Then:

```
# as the cousin: propose a file (body on stdin)
cousin-shared propose norms.md --slug wren --reason "house rules" < memory/norms.md
cousin-shared list
cousin-shared diff norms.md --slug wren

# as a reviewer
cousin-shared promote norms.md --proposer wren --by ana
cousin-shared reject norms.md --proposer wren --by ana --reason "too specific"

# anyone
cousin-shared read norms.md
```

Replacing an existing proposal needs `--force`. A rejected proposal is
deleted, but the reason stays in the audit log. Exit codes: 0 ok, 1 not
found (or a proposal already exists), 2 usage, 3 refused by the review
rule.

Names are resolved before they're compared: a cousin's slug and its
display name are the same principal, so a cousin can't approve its own
proposal by signing with the other name. If a reviewer's name collides
with a cousin's display name, the promotion is refused.

A cousin can also nominate several files at once:

```
cousin-memory propose-shared            # dry run: what would go
cousin-memory propose-shared --commit   # write the proposals
```

This only looks at `memory/project_*.md` and `memory/reference_*.md`
(hyphen forms too) that have a line `shareable: true` in their first 500
characters, skips anything already shared or pending, and only runs for
a cousin whose `cousin.toml` says:

```toml
[memory]
scope = "shared"     # may nominate; the default "private" never does
```

Every cousin's boot packet carries the canonical shared tier (the
rules in its system prompt, the index in its state digest), whatever the cousin's own `scope` (scope decides what a cousin
nominates, not what it reads). An entry whose frontmatter has
`kind: rule` is quoted in full: it is an operator rule the whole fleet
follows. Every other entry is one line, its file name and
`description`, and the cousin reads it with `cousin-shared read` when it
is relevant. Pending proposals never reach a packet. The shared tier is
not part of `cousin-memory search`.

```
---
name: reference_first-principles
description: Every cousin reasons from first principles
shareable: true
kind: rule
---
```
 In the console, the Memory view
lists pending proposals with their diffs and has approve and reject
buttons. With console logins set up, the reviewer is whoever is signed
in ([console](console.md)).

## The explorer and the trash

The console's Memory view (pick a cousin) shows the layers above with
counts and ages, raw entries you can filter by truth level, topic, text,
source, date and tier, the decisions log with the raw entries each
decision wrote, and some health notes: topics with many entries, files
never recalled, `MEMORY.md` links that point at nothing, whether the
distilled views are behind raw, and how many daily files are waiting to
be folded.

From there you can remove a single raw entry, a decision (together with
its raw copy), or a file under `memory/` or `notes/`. A file under
`legacy/` can be removed only when you say so explicitly. The generated
layers can't be removed directly: to change a distilled view, remove the
raw entries behind it; the gzip archive stays whole.

Nothing is destroyed. A removal moves the line or file into
`memory/.trash/<id>/` with a `manifest.json` saying where it came from,
and appends a line to `memory/.trash/audit.jsonl`. When raw changed, the
distilled views are rebuilt right away. Search drops a removed file on
its next run. If `MEMORY.md` pointed at the file, the explorer flags the
dead link; fixing the index is up to the cousin.

Restore from the trash layer in the explorer, or from a shell:

```
cousin-memory trash
#   -> 20260918T134746-314733  ana  memory/raw/2026-09-04.jsonl:3
cousin-memory trash restore 20260918T134746-314733
#   -> restored memory/raw/2026-09-04.jsonl:3 (distilled views regenerated)
```

A raw line is addressed by its line number plus a short hash of its
text. If the file changed since the explorer read it, the removal looks
for the hash; if the line is gone the console answers 409 instead of
removing the wrong one.

## Small extras

- `cousin-callback tag "ana named the espresso machine Gustav" --cycle 3 --category banter`
  keeps moments worth calling back to in `memory/callbacks.md`;
  `cousin-callback search gustav` finds them.
- `cousin-sync-state` renders the live `## Open loops` section of
  `STATUS.md` (the bare heading; a suffixed one is history) into `data/state.json` for scripts that don't want to
  parse Markdown.
- `cousin-backup --dest DIR` snapshots each cousin's databases, `memory/`
  and core Markdown files ([operations](operations.md)).
