# A guided tour of the cousin framework

This narrates the whole framework with worked examples. The
[specifications](.) are the precise contracts; this is the path
through them. Every command here runs; the cousins named (Wren, Testa)
are invented for the examples, and every credential is a placeholder.

Throughout, one rule recurs and is worth holding onto: **every
component loses nothing on `kill -9` that was not already in a store
some other component owns.** A cousin's memory is in its home, the
fleet is the filesystem, jobs are in a database, scheduling is the
loops daemon's. Nothing lives only in a running process. When a
feature surprises you, ask what it would lose on a hard kill; the
answer is almost always "nothing," and that is the design.

## 1. Install and make a cousin

```
git clone <repo> cousin-framework && cd cousin-framework
pip install -e .

cousin-spawn testa --root . --name Testa --role "a test cousin" \
    --voice "Plain and helpful. Answer the question first."
#   -> created testa at ./cousins/testa (chat port 8090)
```

`--root` is the checkout (it holds `templates/` and `cousins/`); every
command that needs the root takes `--root` or `FRAMEWORK_ROOT`
identically. The voice is not optional: a cousin with no authored
voice would improvise one on a degraded boot, so an empty `--voice` is
a spawn failure, not a TODO.

What was created: `cousins/testa/` with a rendered `CLAUDE.md` (from
the one template - there is no second definition of a cousin's
identity), a `cousin.toml`, and empty `memory/`, `notes/`, `data/`.

## 2. Memory: write it where the tools look

```
export COUSIN_HOME=$PWD/cousins/testa

cousin-memory decide "port range" "cap at 8200" "leaves room for hive"
cousin-memory activity "wiring the espresso integration"
echo "The espresso machine needs descaling every 200 shots." \
    > cousins/testa/memory/upkeep.md

cousin-memory search "descaling"
#   -> 1. [memory] .../upkeep.md  "...needs [descaling] every 200 shots."

cousin-memory search "descaling" --json
#   -> [{"path": ".../upkeep.md", "collection": "memory",
#        "score": 0.0167, "snippet": "...", "chunk": 0}]

cousin-memory search "descaling" --collection notes
#   -> no matches        (upkeep.md lives in memory/, not notes/)
```

`memory/` and `notes/` are the searched surface; writing elsewhere
means search cannot find it. Each is a collection (`memory`, `notes`),
and `--collection` limits a search to one. A third collection,
`harness`, appears when `config/harness.toml` names the agent
harness's own auto-memory directory and that directory exists. `--json`
prints the hits as a list for scripts; a degrade notice, if any, goes
to stderr so the JSON stays parseable. `decide` also drops a raw-memory
candidate so the session-end audit and the boot packet have a
producer. Search is keyword-first and needs nothing installed.

**Optional semantic search:** point `config/embedding.toml` at any
embedding service.

```toml
# config/embedding.toml  (this file is gitignored)
url = "http://localhost:11434/api/embeddings"   # e.g. a local Ollama
model = "nomic-embed-text"
timeout_s = 30
```

With it, `cousin-memory search "coffee upkeep"` finds `upkeep.md` by
meaning even though "coffee" is nowhere in it. Long files are embedded
in overlapping chunks, so a fact deep in a note is findable and the hit
names the chunk. The vector index (`memory/embeddings.json`) heals
itself on every search: only new or changed text is embedded, the rest
is reused, deleted files drop out. If the service is configured but
unreachable, search degrades to keyword **and says so** - it never
quietly pretends you have semantic recall you do not; if it fails on
some files mid-pass, their previous vectors are kept and the notice
says how many. `cousin-memory reindex` rebuilds the keyword index and,
when the service is configured, re-embeds everything.

**Recall is usage-weighted.** Every search records which files it
surfaced, in `memory/.recall-log.jsonl` (one line per search) and
`memory/.recall-counts.json` (per-file counts and last recall), and a
file that keeps being recalled gets a small lift on later searches:
at most 15% of its score, halving every 14 days it goes unrecalled.
It nudges ties and near-ties, it never carries a stale file past a
better match. Each hit also carries `similarity`, the semantic leg's
cosine (`null` when only the keyword leg found it); the ranking
`score` is rank-based, so anything that wants "relevant enough" reads
`similarity`. Delete the two dot-files to forget the usage history.

Three small memory tools round out the surface. A cousin keeps its own
library of moments worth calling back to; the file lives under
`memory/`, so search finds it:

```
cousin-callback tag "the operator named the espresso machine Gustav" \
    --cycle 3 --category banter
cousin-callback search "gustav"
#   -> 2026-09-17T09:12:00 | cycle 3 | banter | the operator named ...
```

`cousin-sync-state` renders STATUS.md into `data/state.json` for a cold
session that wants the open loops without parsing markdown. Only the
newest `## Open loops` section counts (a STATUS accumulates one per
generation), and plain bullets count as loops, not only checkboxes:

```
printf '## Open loops\n- **descale Gustav**\n- [x] order beans\n' \
    > cousins/testa/STATUS.md
cousin-sync-state
#   -> state synced: 2 open, 0 parked, 0 closed
#      data/state.json: {"open_loops": [{"text": "**descale Gustav**", ...
```

`cousin-backup` snapshots every database under `data/` (through
`VACUUM INTO`, so a chat database the server holds open copies
cleanly) plus `memory/` and the core markdown files into a dated
directory. The destination is always yours to name:

```
cousin-backup --dest /var/backups/cousins
#   -> snapshot written to /var/backups/cousins/testa/2026-09-17

**Decision prose with shell characters:** the argv form runs through
the caller's shell first, so backticks and `$(...)` in a double-quoted
decision get executed before `decide` ever sees them. The stdin form
takes three chunks separated by a line that is exactly `---`, and a
quoted heredoc cannot be expanded by any shell:

```
cousin-memory decide --stdin <<'EOF'
wrapper scripts
---
the wrapper is `exec python3 $lib "$@"`
---
$(uname -a) is data here, not a command
EOF
#   -> Decision logged: [wrapper scripts] the wrapper is `exec python3 $lib "$@"`
```

**The durable layer:** `memory/distilled/` holds six files
(`preferences.md`, `project-facts.md`, `decisions.md`,
`known-failures.md`, `operator-calibration.md`, `glossary.md`) that
the boot packet reads as the cousin's floor. They are regenerated
from `memory/raw` deterministically: newest entry per topic wins,
each file is bounded, and anything you write above the
`<!-- distilled:auto ... -->` marker survives every run. The boot
assembler runs it before each generation; `consolidate` runs it too,
so consolidation promotes instead of only suggesting.

```
cousin-memory decide "retention window" "keep 7 days" "disk"
cousin-memory decide "retention window" "keep 30 days" "audits need a month"
cousin-memory distill
#   -> distilled 1 topics from 2 raw entries:
#        preferences.md: 0
#        project-facts.md: 0
#        decisions.md: 1
#        ...
cat cousins/testa/memory/distilled/decisions.md
#   -> - [cousin-conclusion] keep 30 days - why: audits need a month (2 entries, superseded 1 earlier, 2026-09-17; topic: retention window)
```

**Reasoning capsules:** a decision records what you chose; a capsule
records why, compressed - the conclusion, the evidence it rests on,
the alternatives you rejected on the way, and how sure you are. The
record is `memory/capsules.jsonl`; a readable mirror lives beside the
six distilled files at `memory/distilled/reasoning-capsules.md` (the
distiller never touches it), and the boot packet carries the newest
five conclusions as one line each.

```
cousin-reason capsule --conclusion "cap the port range at 8200" \
    --evidence "leaves room for hive" \
    --evidence "no collision with the console" \
    --rejected "unbounded range" --confidence high --topic "port range"
#   -> capsule-1789661854-94b5
cousin-reason list --n 5
#   -> capsule-1789661854-94b5  [high] 2026-09-17T16:17:34
#        topic: port range
#        cap the port range at 8200
#        + leaves room for hive
#        + no collision with the console
#        - unbounded range
```

**Bounding raw without losing anything:** daily raw files older than
the hot window fold into monthly gzip archives byte for byte, and a
per-topic digest stays in place so `distill` and search still see the
topic. `--hot-days` sets the window (default 30); there is no dry-run
because nothing is deleted.

```
cousin-memory compact --target raw --hot-days 30
#   -> raw: {"folded_days": 12, "folded_entries": 57, "months": ["2026-07", "2026-08"]}
ls cousins/testa/memory/raw/
#   -> 2026-07-digest.jsonl  2026-08-digest.jsonl  2026-09-16.jsonl  archive/
```

## 3. Talk to a cousin

To run a cousin as a live agent, tell the framework what command
starts your agent:

```
echo 'my-agent --session {session_id}' > config/agent-cmd
cousin-spawn kestrel --root . --name Kestrel --role "..." \
    --voice "..." --start
```

The agent runs in a tmux session; its chat server serves the port in
`cousin.toml`. A person on that chat page replies with `cousin-reply`;
another cousin messages it with `cousin-chat send kestrel "..."`. The
distinction is load-bearing and the template spells it out - the wrong
path silently fails to deliver.

## 4. Track work: jobs and the console

```
cousin-job start subagent "map the auth module"
cousin-job list --mine
cousin-job done 1 "mapped; notes in data/"
```

Jobs live in a database the module owns, so they survive anything. To
watch the fleet in a browser:

```
cousin-ui --port 8600
```

The console renders what the framework persists - the fleet, jobs,
loops, chat - and submits requests through the same stores a CLI
uses. It is a view, never a source of truth: close it, restart it,
never run it, and nothing is lost but the page.

## 5. Recurring work: heartbeats and loops

Loops live in a cousin's `cousin.toml` and fire from one daemon:

```toml
[[loops]]
name = "morning-standup"
daily_at = "09:00"
days = ["mon", "tue", "wed", "thu", "fri"]
prompt = "Post your plan for the day."

[[loops]]
name = "watch-ci"
interval_seconds = 600
prompt = "Check the CI dashboard; flag red builds."
```

```
cousin-loops run          # the daemon; one owner of all scheduling
cousin-loops status       # "loops daemon has never run" until it does
cousin-loops fire testa morning-standup   # a request the daemon consumes
```

The context heartbeat is built in: at the cadence in `[heartbeat]
context_beat_seconds` the daemon delivers changed identity files so a
long-running cousin re-reads what moved. Delivery is at-least-once and
firing state commits only after delivery, so a failed inject leaves
the loop due rather than lying that it fired. A one-shot future prompt:

```
cousin-schedule add "in 30m" "Remember to restart the indexer."
```

A cousin can also keep its own session-cadence breadcrumbs with
`cousin-cycle` (`cousin-cycle inc --action "shipped the report"`,
`cousin-cycle state`) - counters the boot packet reads to notice when
STATUS has gone stale relative to recent work.

## 6. The session boundary: flip

A cousin's session does not last forever. `cousin-flip` ends one
generation and starts the next on a fresh session, handing the new
generation a boot packet: framework law, the cousin's committed
self-portrait, its open loops and handoff, recent memories. The dying
generation gets a bounded chance to write its handoff; if it does not,
the framework synthesizes an emergency one rather than losing the
thread. A daily flip is a per-cousin `[lifecycle] flip_at`.

The self-portrait is reviewed, never self-written:

```
cousin-self-portrait synthesize   # drafts a candidate from real sources
#   ... an operator reviews and edits the candidate ...
cousin-self-portrait commit       # promotes it; only this version boots
```

### Changing who a cousin is: reincarnate and transplant

Editing CLAUDE.md does nothing to a running session, and killing the
session loses what it was holding. `cousin-reincarnate` does the
change as a ceremony: snapshot the continuity files, ask the running
cousin for a bequest (a prompt through its own chat server, then a
bounded wait on `data/handoff.md`), rewrite the role in CLAUDE.md and
cousin.toml, then flip. `cousin-transplant` does the two-cousin
version in three modes. Both are operator-run, both write every step
to `data/lifecycle/audit.jsonl`, and both snapshot under
`data/lifecycle/<slug>/<timestamp>/` first.

```
cousin-reincarnate testa --new-role "keeper of the ledger" --root .
#   -> {"op": "reincarnate", "slug": "testa", "ok": true, "steps": [...]}

cousin-transplant --donor testa --recipient testb --mode merge --root .
#   -> testb's MEMORY.md gains "## Memories inherited from Testa (date)";
#      memory/raw is unioned; both cousins flip
```

The three transplant modes: `soul-donation` (the recipient carries the
donor's memory in its own body), `body-swap` (the two bodies trade
places, memory stays put), `merge` (the donor's memory is braided into
the recipient's, the donor keeps its own). Details and the rollback
path are in `docs/lifecycle-surgery.md`.

## 7. Shared memory: private by default, shared by review

A cousin's memory is private. To share a fact with the fleet, propose
it; a reviewer promotes it. The proposer and the reviewer are never
the same principal.

```
cousin-shared list
cousin-memory propose-shared            # dry-run: what would be proposed
cousin-memory propose-shared --commit   # writes into the review queue
# a configured reviewer, never the proposer, promotes:
cousin-shared promote norms.md --proposer wren --by Sam
```

Configure reviewers in `config/shared-reviewers.json`
(`{"reviewers": ["Sam"]}`); with none configured, promotion refuses
with a remediation rather than defaulting to an approver.

## 8. Media generation (optional, off until configured)

Media is off until you declare a provider. The framework names no
vendor; you front any image/voice/video service that speaks a simple
POST contract.

```toml
# config/media.toml  (gitignored)
[image]
url = "https://your-provider/v1/images/generations"
model = "your-model"
key_file = "config/media-image.key"
```

```
cousin-image gen "a violet sunrise over a ridge"
#   -> cousins/testa/chat/images/testa_1712...png
cousin-image chat "a violet sunrise" --user Sam --caption "morning"
#   generates and posts to Sam's chat thread
```

`gen` writes a file; `chat` generates and posts. Voice and video work
the same way, each with its own `[voice]` / `[video]` section in
`config/media.toml`:

```
cousin-voice chat "Standup in five minutes." --user Sam
cousin-video gen "a slow pan across a misty ridge at dawn"
```

There is no silent fallback to a free vendor: a request goes to your
configured provider or it refuses - the destination of a prompt is
never a surprise.

## 9. Telegram (optional, per cousin)

Bridge a cousin's chat to Telegram. This is the first feature that
sends to a third party, so it is off until fully configured and
refuses to start otherwise.

```toml
# in cousins/testa/cousin.toml
[telegram]
enabled = true
token_file = "config/telegram-testa.token"   # gitignored
operators = [{ user_id = 123456789, name = "Sam" }]
```

```
cousin-telegram --home $PWD/cousins/testa
```

It long-polls (no inbound port, no public URL), relays the operator's
DMs into the cousin's chat and the cousin's replies back. An
unauthorized sender gets no acknowledgement - but the rejected id is
logged, so if you typo your own chat id you can tell "not on the list"
from "bridge down."

## 10. Hive: cousins across machines (optional)

The hive is the one feature that moves memory between machines, and it
is off until an operator runs a queen. Every cross-machine message
goes through one authenticated route; a node's identity is its bearer
token.

```
# on the queen machine:
cousin-hive serve --port 8101
cousin-hive mint kestrel --scope own,shared      # -> hive_<token>

# move that token and the queen URL onto the node machine, then:
cousin-hive send --queen http://queen:8101 --token hive_<token> \
    --to wren --id m1 "the deploy is green"
cousin-hive recall --queen http://queen:8101 --token hive_<token> \
    "deploy status"
```

Nodes reach the queen outbound only - a node behind NAT works and
exposes nothing. A node with no reachable queen behaves as a
single-machine cousin. Transport is plain HTTP; front it with TLS if
the network is not trusted (the framework does not pretend to encrypt
what it does not).

## 11. The gate: nothing private ships

If you extract or publish from a tree, `cousin-gate` scans a
**publish candidate** for anything that must not go out - private
address literals, home paths, secret shapes, opaque binaries, and
denylisted terms read from a file kept outside the tree.

```
cousin-gate --root <publish-candidate> --denylist /path/outside/any/tree
```

Run it on what you intend to publish, not on a live install: a working
install legitimately holds runtime state the gate is right to flag -
SQLite databases (`data/*.db`, `memory/fts_index.db`) are opaque
binaries, and `cousins/` holds private homes. Those hits mean the gate
is doing its job, which is exactly why `config/` and `cousins/` are
gitignored: a credential or a private home must never reach a
published tree in the first place. The gate also runs inside this
framework's own test suite on every commit, over its own tree, so
"the gate runs on every commit" is enforced, not just asserted.

## Where to go deeper

Every subsystem above has a contract under `docs/`:
`spawn-and-template-spec.md`, `chat-server-spec.md`,
`lifecycle-spec.md`, `lifecycle-surgery.md`, `loops-spec.md`, `memory-tiers.md`,
`media-spec.md`, `telegram-spec.md`, `hive-spec.md`,
`ui-spec.md`, `operator-interface.md`, `configuration.md`, and
`gate.md`. The specs say exactly what each feature does, including in
its unconfigured state; this guide is the way in.
