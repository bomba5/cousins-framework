# Port plan: from the private framework to this one, phase by phase

**Status: design, 2026-09-17.** This page is the plan for bringing the private
framework's remaining subsystems into this repository so that this repository
becomes the everyday framework and the private instance is retired to a single
residual role. It is a working document: each phase gets a dated result line
when it lands, and the plan is corrected in place when reality disagrees.

## Scope

In scope, by the operator's word: everything the private framework runs that
this repository does not, except the exclusions below. Memory is the core
feature and goes first; "works perfectly" means hybrid retrieval (keyword and
semantic), embeddings through a configured provider, incremental self-healing
indexes, proactive recall into chat, and a boot packet whose durable layer is
regenerated from raw on every boot.

Out of scope, by the operator's word: the backlog tool, worker-type cousins,
the arc machinery (arcs, arsenal, prefetch), the persona leak filter, the media
engines (image, voice, video, their libraries, favourites, pre-render,
prompt-prefix stacks, the amplifier, the photo-sharing importer) and the media
gateway. The media provider seam this repository already has stays as it is:
off until configured.

Acceptance: one cousin is moved from the private instance to this framework
and works: boots from its packet, answers chat, recalls its own memory, flips.

Decided 2026-09-17: this framework runs on the same machine as the private
instance, on its own ports, until the cutover. Phase 5 (migration) is done by
hand by the operator with a runbook; no converter tooling is built.

## Ground rules that hold for every phase

1. **The gate is the referee.** Every commit passes `tests/gate/test_self.py`
   (address literals, home paths, secret shapes, opaque binaries) and, locally,
   the operator's denylist read from outside the tree. Nothing install-specific
   enters code: hosts, ports, paths, provider URLs and names live in `config/`
   and `cousin.toml`, each with a documented "absent means" line in
   `docs/configuration.md`, held by the existing coherence test.
2. **Ported means rewritten against the spec, then tested, then diffed.** Each
   module is read from the source, its behaviour written down as tests first
   (the source's own tests are the starting list), then implemented here. The
   provenance ledger (`docs/provenance.md`) gets a row per file.
3. **Zero third-party runtime dependencies stays true.** Where the source used
   an external package (the MCP adapter pins one), the dependency is optional
   and declared as an extra; the core suite never needs it.
4. **Tests run without services.** Embedding, chat, tmux, agents are faked at
   the boundary the way the current suite does it.
5. **Phase order is fixed; scope inside a phase is not.** A phase ships when
   its checklist is green, in one or more commits, and the next phase does not
   start before.

## Phase 0: memory to full parity (the core)

Target: a cousin's memory here is at least as good as in the source, and better
where the source was fragile.

Behaviours to port (source module in parentheses, names generic):

- **Hybrid search** (`memory_search`): keyword FTS5 leg always; semantic leg
  when `config/embedding.toml` names a provider; reciprocal-rank fusion of both;
  `--json`, `--top`, collection filter (memory files, notes, the harness's own
  auto-memory directory when present).
- **Incremental self-heal**: every search checks source mtimes and re-embeds
  only new or changed files, drops deleted keys, keeps prior vectors when the
  provider is down, treats an empty index over non-empty sources as stale.
  Chunking of long files with overlap. Atomic index writes.
- **Usage-weighted recall** (`_reinforce`): a recall log and counts; frequently
  surfaced files get a bounded score bonus; decays.
- **Proactive recall** in the chat server: for operator messages above a
  minimum length, run a search and append a short "possibly relevant" line to
  the injected message; per-cousin opt-out in `cousin.toml [memory]`;
  thresholds in config, not code.
- **Durable layer producer** (`_distill`): regenerate the six distilled files
  from raw deterministically; curated text above a marker survives; bounded;
  idempotent; the boot assembler runs it before reading the floor.
- **Raw fold** (`_raw_fold`): daily raw files older than the hot window fold
  into monthly gzip archives byte-identically, with a per-topic digest left in
  place; stable digest ids.
- **Reasoning capsules** (`_capsule`, `cousin-reason`): conclusion, evidence,
  rejected alternatives, confidence, truth level; surfaced in the boot packet.
- **Corrections capture** (`_corrections`): detect operator corrections in
  injected messages, record them, surface the last N in the calibration layer.
- **Transcript mining at flip** (`_transcript_mine`): read the dying session's
  harness transcript for conclusions and dead ends into raw candidates; the
  transcript location is a config seam.
- **Callbacks** (`callback`), **backup** (`backup_memory`, VACUUM INTO
  snapshots), **state sync** (`sync_state`, STATUS.md to `data/state.json`,
  newest section wins), **decide --stdin** three-chunk form, decisions log
  rotation, `consolidate` that promotes.

Config seams added: `config/embedding.toml` gains `chunk_chars`,
`chunk_overlap`, `recall.min_chars`, `recall.min_score`, `recall.top`;
`config/harness.toml` names where the harness writes session transcripts and keeps its auto-memory directory
(absent: transcript mining is off, said once at flip).

Checklist: the source's memory tests re-expressed here (about 120 functions),
plus a live acceptance run: a cousin home with real memory files, a real
embedding provider, search returns the planted fact by meaning, not by word.

## Phase 1: lifecycle and operations

- Daily flip driver and the size guard (force-flip when the session transcript
  exceeds a configured size), both as CLIs plus systemd unit *templates* under
  `systemd/` with placeholders, installed by hand or by a documented script.
- Prompt-cache audit CLI. Session bookend macros from `cousin.toml [session]`.
- Reincarnate and transplant (identity surgery with memory continuity), with
  the lifecycle doc.
- Harness hooks (pre-compact checkpoint, stop checkpoint, session-start banner)
  as shell files under `hooks/`, referenced from the template.
- Ready-file watcher (the "trigger-file seam" the loops spec marks as not
  shipped) and the compaction sweep across all cousins.
- Tool-surface manifest generator (`--help` first lines into a shared file read
  at boot).

## Phase 2: chat and the harness adapter

- Chat-pattern hooks (regex to shell or inject handlers from a per-cousin JSON
  file) and the chat-server watchdog. Presence and engagement tracking were
  dropped from scope by the operator on 2026-09-17; inbound image attachments
  already ship here.
- The MCP adapter: the cousin's CLI surface over stdio, registry-driven,
  argv lists only, per-cousin provisioning at spawn, harness pre-approval as a
  documented manual or scripted step. The MCP package is an optional extra.

## Phase 3: the console

Decision pending with the operator: port the source console as-is (browser
compiled React, no build step, roughly thirteen thousand lines including the
backend) or rebuild a leaner console on this repository's stdlib server.
Recommendation: port as-is minus the media, backlog and arc views, because it
keeps every view in daily use and the gate can strip install specifics from
data files. Either way the contract stays the one in `docs/ui-spec.md`: a view
that dies loses nothing.

Views in scope: cousins (cards, inspector, role and CLAUDE.md editors, loops
editor, spawn, dismiss with archive, flip), chat (bubbles, reactions, inline
media display, the live terminal pane over SSE with input), jobs, loops with
drift, memory, tokens, tracker, settings and account, restart panels. The
scheduler role (heartbeats, loop dispatch, timed flips) becomes the loops
daemon this repository already has, not a second copy of the backend.

## Phase 4: hive nodes and the tracker

- Node builder (`spawn-node`): a self-contained node archive with a minted
  token and rendered identity, the node template with its own small chat
  server and queen-backed brain, the deploy-abroad guide.
- The in-flight work tracker (framework-wide, domain and state and tags).

## Phase 5: migration (by hand)

The procedure is .

- No converter is built. A runbook lists what to carry per cousin home:
  `cousin.toml` (rewritten by hand to this repository's shape), memory files
  (raw, distilled, decisions, notes: same shapes, copied), and what is left
  behind (the source chat database schema differs; history stays readable on
  the source instance).
- Cutover: stop the source cousin, spawn here, copy memory, boot, verify chat
  and recall, point the operator's surfaces at the new port.
- The residual role of the private instance is documented in the operator's
  own notes, not here.

## Sizing (working estimate, corrected as phases land)

| phase | source lines to read | new tests (est.) | days |
|---|---|---|---|
| 0 | ~3,000 | 120 | 3-4 |
| 1 | ~2,500 | 80 | 2-3 |
| 2 | ~2,500 | 70 | 2-3 |
| 3 | ~13,000 | 60 | 5-7 |
| 4 | ~900 | 30 | 1-2 |
| 5 | runbook only | 0 | by hand |

## Results log

- **2026-09-17, phase 0 landed** (commits d56bfe6..HEAD on main): hybrid search with chunking, per-chunk
  incremental vectors and RRF; usage-weighted recall; proactive recall in the chat server (cosine threshold,
  keyword-only opt-in); the durable-layer distiller and lossless raw fold, run by the boot assembler;
  reasoning capsules (`cousin-reason`); corrections capture; transcript mining at flip; callbacks, backup,
  state sync; `decide --stdin`; `consolidate` promotes. Suite 608 tests green, gate clean. Acceptance on a
  scratch cousin with a real embedding provider: a query with no shared words surfaced the right note, the
  chat server delivered the recall line into the terminal while the stored message stayed bare, a halt was
  captured as a correction, the dry-run flip showed the transcript-mining stage, and the boot packet carried
  the distilled decision.
- **2026-09-17, phase 1 landed** (c8ade59..HEAD on main): ready-file triggers and a transcript-size guard as
  tick steps of the loops daemon (one owner; the daily flip stays the daemon's `flip_at` driver); session
  bookends (`cousin-session`) and the three harness hooks; reincarnate and transplant with snapshots and an
  audit log; the prompt-cache audit; the compaction sweep; the tool-surface manifest read into the boot
  packet; systemd unit templates with placeholders; the operations guide. Suite 756 tests green, gate clean.
  Acceptance on the scratch cousin: bookend hooks ran and were recorded; the hooks wrote their checkpoints;
  the tool surface listed 29 scripts and reached the packet; the sweep ran; the cache audit read a real
  6 MB transcript (98.6 percent hit rate); three daemon ticks consumed a heartbeat and a message ready file,
  delivered the message into the pane, and the size guard queued one flip request with its 5-minute warning.
- **2026-09-17, phase 2 landed** (ac49703..HEAD on main): chat-pattern hooks evaluated by the chat server
  (shell handlers detached and reaped, inject handlers as their own `fw-hook` line, outside paths refused);
  the chat-server watchdog with its unit template; the MCP adapter (stdlib core, registry by console-script
  name, `--selftest`, `approve` through the harness settings seam, the 1.x SDK as an optional extra) and
  per-cousin provisioning at spawn (`--operator`). Presence and engagement were dropped by the operator.
  Suite 909 tests green, gate clean. Live run on the scratch install: a message matching two hooks produced
  the injected line in the pane and the shell hook's marker file; the watchdog respawned a killed chat
  server and got a healthy answer; a real MCP stdio client listed four tools and logged a decision through
  the adapter. Two defects the live run caught: the chat server had no `__main__` guard, so every `-m`
  launcher (spawn, watchdog) reported success having started nothing; and the 2.x MCP SDK changed the
  server API, so the extra pins 1.x.
- **2026-09-17, phases 3 and 4 landed** (dbf079e..HEAD on main): the console API contract (61 routes),
  a shared route registry, backend A (fleet, jobs, loops, memory, shared review, auth, admin, tracker) and
  backend B (chat proxy, live pane, events stream, static), frontend A and B (the React console ported minus
  media, backlog, GPU controls, presence and engagement, the game; a tracker view written fresh), an
  end-to-end test through the HTTP surface, the console unit, the ui-spec rewrite; the tracker store and
  CLI; the hive node builder and node template with the deploy guide; the migration runbook. Suite 1186
  tests green, gate clean. Live: the console served on the scratch install, the operator logged in from a
  browser, listed the fleet, sent a chat message that reached the cousin's pane; one bug found and fixed on
  the way (the unread dot compared two differently spelled keys).
