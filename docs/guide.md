# A guided tour of cousins-framework

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

Prerequisites: Python >= 3.11, `python3-venv`, `tmux` and `git`
(Ubuntu 24.04: `sudo apt-get update && sudo apt-get install -y
python3-venv tmux git`). The repository may be private, so the clone
needs GitHub access (a deploy key or a token). The complete new-machine
procedure, including the agent, the units and the uninstall, is
`docs/install.md`; this section is the short path.

```
git clone <repo> cousins-framework && cd cousins-framework
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[mcp]"                     # from the checkout; not on PyPI
python3 -m unittest discover -s tests

cousin-spawn testa --root "$PWD" --name Testa --role "a test cousin" \
    --voice "Plain and helpful. Answer the question first."
#   -> created testa at <checkout>/cousins/testa (chat port 8090)
```

`--root` is the checkout (it holds `templates/` and `cousins/`); every
command that needs the root takes `--root` or `FRAMEWORK_ROOT`
identically, and a command typed inside the checkout defaults to it.
The root is made absolute before it is written into the cousin's
files, because the agent runs from the cousin home, where a relative
path would name nothing. The voice is not optional: a cousin with no authored
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
timeout_s = 120
```

For a local Ollama: `curl -fsSL https://ollama.com/install.sh | sh`
(about 2.4 GB of disk even CPU-only), then `ollama pull
nomic-embed-text`. `timeout_s` must exceed the time one chunk takes to
embed: on a CPU without AVX that was 32 s, and a shorter timeout makes
every search wait it out and fall back to keyword; 30 is plenty with a
GPU.

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
```

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

**Auditing the prompt cache:** the provider's prompt cache is
prefix-matched, so a file that changes between turns and sits in the
prefix silently invalidates everything after it. `cousin-cache-audit`
reads the cousin's session transcripts through `config/harness.toml`
(absent: exit 2 naming the seam), takes the per-turn usage fields from
the assistant messages, and names the files under the home (and the
harness auto-memory directory, when configured) whose mtime falls
between two successive turns where the hit rate dropped. `--days`
bounds the window (default 7, 0 for all), `--json` prints the report as
one object, `--diagnose` adds the turn pair behind each suspect.

```
cousin-cache-audit --days 7
#   -> testa: 412 turn(s) over 7 day(s)
#      hit rate: 71.3%  [marginal]
#      per turn: min 0.0%  median 96.2%  max 99.8%
#      tokens: cache_read 9,812,004  input 210,331  cache_creation 3,740,116
#      drops: 6 (hit rate fell by more than 10 points between successive turns)
#      suspects (touched inside a drop window):
#        cousins/testa/STATUS.md  (fell 97.1% -> 2.4%)
#        cousins/testa/memory/MEMORY.md  (fell 95.8% -> 31.0%)
#      recommendation: move volatile content after the cache breakpoint; ...

cousin-cache-audit --diagnose
#        cousins/testa/STATUS.md  (fell 97.1% -> 2.4%)
#            between 2026-09-15T08:02:11.000Z and 2026-09-15T08:03:40.000Z, mtime ...
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
`cousin.toml`. A person on that chat page replies with `cousin-reply`
(`--image <file>` shows a picture on the reply: the file lands as
`<home>/chat/inbound/<reply id>.<ext>`, which the console attaches to that
message);
another cousin messages it with `cousin-chat send kestrel "..."`. The
distinction is load-bearing and the template spells it out - the wrong
path silently fails to deliver.

A cousin still running on a different framework instance (on this
host or the LAN, not under this install's `cousins/`) is reachable
once `config/external-peers.toml` names its chat server
(`config/external-peers.toml.example` documents every key):

```
cat > config/external-peers.toml <<'EOF'
[peers.wren]
url = "http://127.0.0.1:8085"
send_path = "/api/send"      # POST {"user": <sender name>, "message": <text>}
EOF
cousin-chat list
#   -> testa        port=8090   (self)
#      kestrel      port=8091
#      wren         url=http://127.0.0.1:8085 (external)
cousin-chat send wren "the descaling schedule moved to Fridays"
#   -> {"id": 12, "ok": true, "to": "wren"}
```

The MCP `send` tool routes by that same list, so `to = "wren"`
works from inside the agent too. The peer's address must pass the
network guard (loopback and the private ranges, plus
`config/net-allowlist.json`); the request goes direct, never through a
proxy or a redirect. A slug that is neither a local cousin nor an
external peer is still an error, and a local cousin of the same slug
wins over the file.

## 4. Track work: jobs and the console

```
cousin-job start subagent "map the auth module"
cousin-job list --mine
cousin-job done 1 "mapped; notes in data/"

# a shell command as a job: runs detached, logs to <root>/data/job-logs/,
# and closes itself with the command's exit code
cousin-job start shell "rebuild the index" -- cousin-memory reindex
cousin-job tail 2 -f
```

Jobs live in a database the module owns, so they survive anything.
A cousin's harness hooks also record every subagent and every
background shell as a job on their own, closed at completion.

Jobs are what is running right now; the tracker is what is in flight
across the whole framework, whoever holds it - the operator's list of
open threads, blocked threads and what closed:

```
cousin-tracker add "migrate the chat archive" --domain infra --tag q4
cousin-tracker state 1 active
cousin-tracker update 1 --notes "waiting on the disk swap" --state blocked
cousin-tracker list --state blocked
cousin-tracker show 1 --json
cousin-tracker state 1 done
```

Five states, `open|active|blocked|done|dropped`; the owner defaults to
the cousin whose `COUSIN_HOME` is set, so each cousin's adds attribute
themselves. `list` puts open work first and closed work last. Ids never
recycle: a deleted #3 stays gone, so a note that says #3 keeps meaning
the same thing. The store is `<root>/data/tracker.db` and the console
serves it; the shapes are in `docs/tracker-spec.md`. To watch the
fleet in a browser:

```
cousin-console --port 8600
```

The console renders what the framework persists - the fleet with its
cards and editors, chat with the live terminal pane, jobs, memory and
the shared-tier review, loops with drift, tokens, the tracker - and
submits every command through the same library a CLI uses. It is a
view, never a source of truth: close it, restart it, never run it, and
nothing is lost but the page. Out of the box it is open to every
address the network guard admits and says so on its account panel; to
put a login in front of it:

```
cousin-console adduser ana      # password from a prompt, never argv
```

That writes `config/console-users.json`, and from then on every API
route needs a session, with no address-based bypass (the routes and
the auth model are `docs/console-spec.md`). `cousin-ui` is the
retired name: it prints a pointer and runs the console for one
release.

A first session, end to end: open the page, sign in, and the fleet
card for `testa` shows `running` when its tmux session is up and
`chat: ok` when its chat server answers `/health` with its slug. Type
in the chat view and the message goes through the console to testa's
own chat server, which delivers it to the session; the history you
read back is that server's, not a copy. Open the pane and the browser
shows testa's terminal from `capture-pane`, keystrokes going back
through the same injection lock the chat server uses. Jobs, loops,
memory and the tracker each render the store their CLI writes. Stop
the console and start it again: every tab logs in once more and finds
everything where it was. The same walk runs in process on every test
run (`tests/console/test_console_e2e.py`), and running the console as
a unit with its first user is `docs/operations.md` section 8.

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
the loop due rather than lying that it fired. Anything that can write
a file can also trigger a loop: drop `<name>.ready` in the cousin
home (`morning-standup.ready`, `context-heartbeat.ready`, or
`testa-message.ready` carrying one literal line) and the next tick
delivers it and removes the file. With `flip_when_transcript_mb` in
`config/harness.toml` the daemon also requests a flip for a cousin
whose session transcript outgrows it; see `docs/loops-spec.md`. A
one-shot future prompt:

```
cousin-schedule add "in 30m" "Remember to restart the indexer."
```

The loops daemon fires it on the first tick after its time, with a
`[cousin-schedule]` prefix, once the cousin is alive; a failed
delivery stays pending and retries next tick. `cousin-schedule tick`
does the same by hand when no daemon runs.

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

Inside one generation a session still has a start and an end, and a
cousin can list what to run at each in its own `cousin.toml`:

```toml
[session]
start_hooks = ["cousin-cycle inc --start"]
end_hooks = [
  {name = "sync-state", cmd = "cousin-sync-state"},
  "cousin-cycle inc --end",
]
```

```
cousin-session start
#   -> running session start (1 hook(s))
#        [ok]   step-1
#      session start done: 1 ok, 0 failed, 0 skipped
cousin-session end
#   -> running session end (2 hook(s))
#        [ok]   sync-state
#        [ok]   step-2
#      session end done: 2 ok, 0 failed, 0 skipped
cousin-session status             # the last run and both hook lists, as JSON
#   -> {"last_end": "2026-09-17T19:31:33", "last_run": {"phase": "end",
#       "results": [{"name": "sync-state", "rc": 0,
#                    "output": "state synced: 1 open, 0 parked, 0 closed", ...
```

Every hook sees `COUSIN_HOME`, `COUSIN_SLUG` and `SESSION_PHASE`. A hook
that fails is reported (`[FAIL] step-2 (rc=3)`) and the rest still run;
the command exits 1 so a wrapper can notice. `--skip NAME` leaves one
out. Beside these, `hooks/` ships three scripts for the agent harness
itself - a pre-compaction checkpoint, a stop checkpoint and a start
banner - that read only `COUSIN_HOME` and write only under `data/`;
`docs/session-hooks.md` has the wiring and what each one writes.
### Login or API key: cousin-auth

A cousin's agent authenticates in one of two modes, set per cousin in
`cousin.toml [runtime] auth` and read at every start (so a flip or a
restart keeps it):

- `claude` (the default): the harness's own login. The key variable
  and the config-dir variable are removed from the agent's
  environment, so a key exported somewhere upstream cannot quietly
  switch the cousin to metered billing.
- `api_key`: a key from the cousin's own
  `<home>/.secrets/api-key.env` (one line `ANTHROPIC_API_KEY=<key>`
  for Claude Code; file 600, directory 700, refused otherwise). The
  key reaches the agent only through its environment: a small
  launcher in front of the agent command reads the file at exec time,
  so no argv (tmux's included) and no log line ever holds it.

The billing lesson that shapes the second mode: with its own login
present AND `ANTHROPIC_API_KEY` set, Claude Code bills the LOGIN (it
warns "Both claude.ai and ANTHROPIC_API_KEY set"). A key in the
environment is not enough. So `api_key` mode also points the agent at
an isolated config directory (`<root>/data/harness-api-key-config/`)
that symlinks everything in `~/.claude` except the login and
account-bound files, plus a copy of `~/.claude.json` without the
account block; a launch refuses if that directory holds a login.
Transcripts are symlinked through, so both modes share the same
sessions. The names involved (the variables, the files, the account
keys) are in `config/harness.toml [auth.api_key]`; the Claude Code
preset has them.

```
cousin-auth testa                        # the mode and the key file's state
#   -> testa: auth claude (modes: claude, api_key)
#      key: not set (<checkout>/cousins/testa/.secrets/api-key.env)
cousin-auth testa --key-stdin < key.txt  # writes the key file, 600 (a tty prompts, no echo)
#   -> key written to .../testa/.secrets/api-key.env (ends WXYZ)
cousin-auth testa api_key                # switch; a running agent restarts on the SAME session
#   -> testa: auth claude -> api_key; restarted on session 1b4e...
cousin-auth testa claude --no-restart    # set it, apply at the next start
```

A switch restarts a running agent with the harness's resume
(`config/harness.toml [agent.resume]`: the preset swaps
`--session-id {session_id}` in `config/agent-cmd` for
`--resume {session_id}`), so the conversation carries over. It refuses
a cousin that is mid-turn (its pane matches `busy_patterns`, Claude
Code's spinner line) unless `--force`, and it checks everything (key
file, isolated directory, resume possible) before it changes
`cousin.toml` or kills anything. The console's cousin inspector has
the same control: a mode select, and a key field that sends the key
once and afterwards shows only "key set" and its last four characters.
The first `api_key` start of Claude Code may ask "Do you want to use
this API key?" once; answer it in the pane (the console flags it as
needing attention). The answer is kept across rebuilds of the isolated
directory.

### Which version is running: cousin-version

The framework's version lives in `pyproject.toml` only; it is
`cousin_lib.__version__` at runtime, `GET /api/version` in the console
(public, no login), and the dim `v0.1.0 <commit>` beside the brand in
the console's top bar. The console reads both when it starts, so a
checkout that was bumped or pulled but not restarted still shows the
old values. `CHANGELOG.md` has one entry per version.

```
cousin-version                    # the running version, with the commit in a git checkout
#   -> 0.1.0 (0a119f6)
cousin-version bump minor         # edits pyproject.toml in place, nothing else
#   -> 0.1.0 -> 0.2.0 (<checkout>/pyproject.toml)
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

A cousin moving over from the previous framework brings its chat history
with `cousin-chat-import` (chat server stopped): old messages keep their
ids and pictures, rows already in the new store move after them, and a
second run is refused.

```
cousin-chat-import testa --old-home /old/instance/cousins/testa/files --root .
#   -> {"imported": 566, "renumbered": 4, "images": 157, ...}
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

To put a whole cousin on the other machine rather than a token, build
a node archive. It is copy-over, never push: the builder mints the
token in the queen's store, renders the node's identity, and writes
one tarball you move by hand and install there.

```
# on the queen machine:
cousin-spawn-node kestrel --root . \
    --queen-url http://queen.example.invalid:8101 \
    --name Kestrel --role "watches the greenhouse" --out /tmp/build
#   -> built /tmp/build/kestrel-node.tar.gz   (it holds the token: private)

# on the node machine (python3 and outbound reach to the queen, nothing else):
tar xzf kestrel-node.tar.gz && cd kestrel-node && ./install.sh
```

The node runs its own small chat server in the same shapes as every
cousin's (`/health`, `/api/send`, `/api/history`), recalls from and
remembers to the queen every turn, and answers messages other cousins
send it over the bus. Its brain is the command in `node.env
AGENT_CMD`; until you set one it is a placeholder that greets, echoes
and still remembers, so a fresh node is never dead on arrival. The
whole procedure, the archive's contents and the failure behaviour are
in `docs/deploying-a-node.md`.

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
published tree in the first place. On a checkout that also hosts a
live install, `cousin-gate --git-visible` scans only what git would
publish (tracked files and untracked ones `.gitignore` does not
exclude). The gate also runs inside this framework's own test suite on
every commit, over its own tree in exactly that mode, so "the gate
runs on every commit" is enforced, not just asserted, and a live
`cousins/` under the checkout does not fail the suite.

## 12. Running it unattended

Two fleet-wide maintenance commands exist for a timer to run. The
compaction sweep drives `cousin-memory compact` over every cousin home
in turn, never stopping at one failure, and exits non-zero if any
failed so a unit's journal shows it:

```
cousin-sweep compact --target both --root .
#   -> [sweep] testa index: rc=0 {"ok": true, "retired": [], ...}
#      [sweep] testa raw: rc=0 raw: {"folded_days": 0, ...}
#      [sweep] 2 run(s), 0 failed
```

The tool-surface manifest is the list of `cousin-*` commands with the
first line of each one's `--help`, derived from the package's own entry
points (nothing typed by hand); the boot packet quotes it as its Tool
Surface layer, so a fresh generation knows its tools without
re-discovering them, and boots degraded until the file exists:

```
cousin-tool-surface --root .
#   -> wrote ./data/tool-surface.md (25 tools)
```

The chat server that `cousin-spawn --start` and `cousin-flip` launch
runs detached with no supervisor, so a third timer command watches it.
`cousin-chat-watchdog` makes one pass over the fleet: a running cousin
whose port is free gets its server spawned (log appended to
`<home>/data/chat-server.log`), a port that is occupied but does not
answer `/health` with the cousin's slug is an alert and nothing more
(it never kills), and a stopped cousin is skipped. `--dry-run` shows
the decisions:

```
cousin-chat-watchdog --dry-run --root .
#   -> [chat-watchdog] testa: ok
#      [chat-watchdog] testb: spawn (would spawn cousin-chat-server --home ./cousins/testb)
#      [chat-watchdog] testc: skip
cousin-chat-watchdog --root .
#   -> [chat-watchdog] spawned testb on :8091 health=ok
```

`systemd/` holds unit templates for the loops daemon, the three timers
and a per-cousin chat server; `docs/operations.md` walks the install
from a cold clone and lists what to check when a cousin goes quiet.

## 13. The same CLIs as tools: MCP

A cousin's harness can be handed the CLI surface as tools over the
Model Context Protocol, so arguments travel as JSON and reach each CLI
as an argv list - no shell between the model and `cousin-memory
decide`, so backticks and `$(...)` in a decision body arrive
byte-identical. Every tool is a CLI that already exists; the adapter
adds no capability and no inbound path. Spawn provisions it: every new
home carries `mcp-registry.toml` (the default registry, four coarse
tools, with the operator from `--operator` filled in) and a
`.mcp.json` pointing the harness at `cousin-mcp`. Nothing runs until
the harness accepts that registration.

```
cousin-mcp --registry config/mcp-registry.toml.example --selftest
#   -> registry: config/mcp-registry.toml.example (4 tools, ceiling 12, timeout 120s, output cap 16000 chars)
#        memory    cousin-memory                activity, decide, recall, search
#        send      cousin-chat, cousin-reply    operator, peer (operators: none configured)
#        job       cousin-job                   done, fail, list, show, start
#        schedule  cousin-schedule              add, cancel, list
#        cousin-memory -> beside the interpreter
#        cousin-chat -> beside the interpreter
#        cousin-reply -> beside the interpreter
#        cousin-job -> beside the interpreter
#        cousin-schedule -> beside the interpreter
#      mcp sdk: absent; the MCP SDK is not importable; serving needs the extra: pip install -e ".[mcp]" from the checkout, ...
#      selftest ok: 4 schema(s) built
```

The selftest needs no SDK: it loads the registry, builds every schema,
and says where each command resolves - beside the interpreter this
`cousin-mcp` runs under first, then on PATH - and exits 1 if one
resolves nowhere. Serving does need the SDK, as the optional extra:

```
pip install -e ".[mcp]"                  # from the checkout, in its venv
cousin-mcp approve testa --root "$PWD"
#   -> approved ./cousins/testa in ~/.harness-settings.json: trusted, "cousin" enabled; ...
```

Spawn also writes the cousin's own harness project settings,
`<home>/.claude/settings.json`: its session hooks, the job-tracking
hook that records subagents and background shells in `cousin-job`,
and `"cousin"` in `enabledMcpjsonServers`. For a cousin spawned
before that:

```
cousin-spawn testa --root "$PWD" --repair-settings
```

The same command rewrites the `cousin` entry of the home's `.mcp.json`
(its command, `--registry` path and identity env, all absolute),
keeping any other server or env key there; run it on a cousin spawned
with a relative `--root` by an older release.

`approve` records the harness's acceptance in the settings file
`config/harness.toml settings_file` names and edits nothing else; with
no seam it refuses and prints the edit to make by hand. `send` is the
one tool with a resolution rule: a known peer slug goes to `cousin-chat
send`, a configured operator name to `cousin-reply`, anything else is
an error naming what the cousin knows - never a default. One call from
a shell, to see what the model would see:

```
export COUSIN_HOME=$PWD/cousins/testa FRAMEWORK_ROOT=$PWD
cousin-mcp --call memory '{"command": "search", "query": "descaling"}'
#   -> 1. [memory] .../upkeep.md  "...needs [descaling] every 200 shots."
```

The contract, the registry shape and the stated limits are in
`docs/mcp-spec.md`.

## Where to go deeper

Every subsystem above has a contract under `docs/`:
`spawn-and-template-spec.md`, `chat-server-spec.md`,
`lifecycle-spec.md`, `lifecycle-surgery.md`, `session-hooks.md`,
`loops-spec.md`, `memory-tiers.md`, `media-spec.md`, `telegram-spec.md`, `hive-spec.md`, `mcp-spec.md`,
`ui-spec.md`, `operator-interface.md`, `configuration.md`,
`operations.md`, `install.md`, and `gate.md`. The specs say exactly what each feature does, including in
its unconfigured state; this guide is the way in.
