# Memory

How a cousin remembers: where each kind of memory lives, what writes
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

When a new session starts (a spawn, a flip, a restart), the framework
hands the cousin a boot packet built from the layers below. So what a
cousin "knows" at the start of a session is: its identity files, its
open loops and handoff, the distilled views of its raw memory, its last
few reasoning capsules and recent raw entries, and the head of its
`MEMORY.md`. Anything else it has to search for.

## The layers

Everything below is relative to the cousin home.

| Layer | Where | Written by | Read by |
|---|---|---|---|
| Active state | `STATUS.md`, `data/handoff.md`, `data/handoff-manual.md`, `data/active-threads.md`, `data/session-checkpoint.md`, `data/pre-compact-checkpoint.md` | the cousin; the checkpoint files by the harness hooks | boot packet, heartbeat, session start hook |
| Index | `MEMORY.md` | the cousin | boot packet (the first 1000 characters), heartbeat |
| Raw entries | `memory/raw/YYYY-MM-DD.jsonl` | `decide`, `remember`, transcript mining at a flip, a transplant merge | distiller, boot packet |
| Monthly digests | `memory/raw/YYYY-MM-digest.jsonl` | the raw fold | distiller, boot packet |
| Raw archive | `memory/raw/archive/YYYY-MM.jsonl.gz` | the raw fold | nothing automatic (`zcat` it) |
| Distilled views | `memory/distilled/*.md` | the distiller | boot packet |
| Decisions | `data/decisions.jsonl` | `decide` | `recall`, `consolidate`, the boot packet's staleness warning |
| Memory and notes files | `memory/**/*.md`, `notes/**/*.md` | the cousin | search |
| Reasoning capsules | `memory/capsules.jsonl`, mirrored to `memory/distilled/reasoning-capsules.md` | `cousin-reason capsule` | boot packet, search (the mirror) |
| Corrections | `data/corrections.jsonl` | the chat server, from your messages | boot packet (calibration layer) |
| Harness auto-memory | the directory `config/harness.toml` names in `auto_memory_dir` | the agent harness itself | search, explorer |
| Search indexes | `memory/fts_index.db`, `memory/embeddings.json` | search, `reindex` | search |
| Recall log | `memory/.recall-log.jsonl`, `memory/.recall-counts.json` | every search | search ranking, explorer |
| Trash | `memory/.trash/` | removals from the console explorer | `cousin-memory trash restore` |
| Legacy | `legacy/` | a migration (the old home, archived whole) | the explorer only |

A few notes on the ones that aren't obvious.

**Active state** is what the cousin is doing right now. `STATUS.md`
holds the open loops (the boot packet quotes the newest `## Open loops`
section), `data/handoff.md` is what the last generation told the next
one, and `data/active-threads.md` is one bullet per thread in flight.
The flip asks the cousin to write all three before it ends a session.
`data/handoff-manual.md` is for a handoff written by hand; the
framework never writes it. The two checkpoint files come from the
Claude Code hooks in `hooks/` (see [cousins](cousins.md)).
If decisions were logged after `STATUS.md` last changed, the boot
packet warns the cousin that STATUS may be stale.

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

**Corrections** are captured by the chat server: when a message from
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
`data/last-activity.txt` with a timestamped one-liner. `recall` prints
past decisions, optionally filtered by a keyword in the topic, decision
or reasoning.

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
  a flip that died halfway, and a dead chat server getting restarted.
  Nothing is written when nothing changed, and there are no periodic
  entries.
- **L2 tool.** When a cousin's job finishes as done or failed, its
  title, exit code and summary land in that cousin's memory under
  `job:<title>`. Repeat runs of the same job fold into one line.
  Cancelled jobs and jobs with no owning cousin are skipped.
- **L4 hypothesis.** When a flip mines the old session's transcript, a
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

## The distilled views

`memory/distilled/` holds six files the boot packet reads as the
cousin's long-term floor:

- `preferences.md`
- `project-facts.md`
- `decisions.md`
- `known-failures.md`
- `operator-calibration.md`
- `glossary.md`

They're rebuilt from raw every time a boot packet is assembled (a
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
(`--max-lines` changes it), ranked by entry count then recency.

You can write in these files. Everything above the line
`<!-- distilled:auto - lines below are regenerated from memory/raw; edit above this line only -->`
is kept as-is on every run. Everything below it is rewritten. An empty
file holds the stub `_(empty - awaiting distillation)_`.

`consolidate` also prints topics with three or more entries across the
decisions log and raw, as candidates for a proper topic file in
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

The boot packet is the text a new session starts from. It has eight
layers in a fixed order, a hard ceiling of about 8000 tokens, and a
list of required actions at the end. The memory it pulls in:

1. Framework law (`config/law.md`), not memory, but first.
2. The committed self-portrait.
3. Operator calibration: `operator-calibration.md` (or the portrait's
   calibration section), plus recent corrections.
4. Active state: the open loops from `STATUS.md` and the latest
   `data/handoff.md`.
5. Task packet: `data/active-threads.md` and the last three capsules.
6. Tool trace summary.
7. Retrieved memories: the other five distilled files, the newest
   capsule conclusions, recent raw entries (up to the last 60 lines
   from the newest 14 raw files) and the head of `MEMORY.md`.
8. The list of `cousin-*` commands and what each one does.

When the packet is too big, the command list and then the memories
are cut first, and law never. A
layer that's missing (no self-portrait, no calibration, no active
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

Search covers three collections: `memory` (`memory/**/*.md`), `notes`
(`notes/**/*.md`) and `harness` (the harness auto-memory directory,
when configured). `--collection` limits it to one. `--json` prints the
hits as a list with `path`, `collection`, `score`, `similarity`,
`snippet` and `chunk`; any warning goes to stderr so the JSON stays
clean. The trash is never searched.

Out of the box search is keyword only: SQLite FTS5 in
`memory/fts_index.db`, with nothing to install. Every cousin's indexes
(keyword and, with an embedding service, vectors) are kept level with its
files by the loops daemon: each home is checked every 5 minutes and only
what changed is embedded, one home at a time, so the embedding service is
never hit by several homes at once. A search still refreshes the index
itself if it finds it behind, so you rarely need `reindex`.

### Semantic search

Add an embedding service and search also finds things by meaning.
Copy `config/embedding.toml.example` to `config/embedding.toml`:

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
(default 2000) overlapping by `chunk_overlap` (default 200), and a hit
says which chunk matched.

The vectors live in `memory/embeddings.json`. Each search embeds only
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

### Proactive recall in chat

When you (the operator) send a cousin a message of at least 24
characters, its chat server searches the cousin's memory and appends one
line to what the cousin sees:

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

Every cousin's boot packet carries the canonical shared tier in its
layer 2, whatever the cousin's own `scope` (scope decides what a cousin
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
- `cousin-sync-state` renders the newest `## Open loops` section of
  `STATUS.md` into `data/state.json` for scripts that don't want to
  parse Markdown.
- `cousin-backup --dest DIR` snapshots each cousin's databases, `memory/`
  and core Markdown files ([operations](operations.md)).
