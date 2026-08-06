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
```

`memory/` and `notes/` are the searched surface; writing elsewhere
means search cannot find it. `decide` also drops a raw-memory
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
meaning even though "coffee" is nowhere in it. If the service is
configured but unreachable, search degrades to keyword **and says
so** - it never quietly pretends you have semantic recall you do not.

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
`lifecycle-spec.md`, `loops-spec.md`, `memory-tiers.md`,
`media-spec.md`, `telegram-spec.md`, `hive-spec.md`,
`ui-spec.md`, `operator-interface.md`, `configuration.md`, and
`gate.md`. The specs say exactly what each feature does, including in
its unconfigured state; this guide is the way in.
