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

- Presence (is the operator watching this tab), engagement cursor, chat-pattern
  hooks (regex to shell or inject handlers from a per-cousin JSON file), the
  chat-server watchdog, inbound image attachments in chat (display only; no
  generation).
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
