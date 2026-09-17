# Migration runbook: moving one cousin from the private instance to this framework

**Status: the phase 5 procedure, done by hand, one cousin at a time.** The
operator decided (2026-09-17) that no converter is built; this page is what
the hand does. Every step is reversible until the last one, and the source
instance keeps serving the cousin until you stop it there.

## Before you start (once per install)

1. Install this framework on the target host: `pip install -e .` in the
   checkout (add `[mcp]` if cousins use the MCP adapter), set
   `FRAMEWORK_ROOT` to the root that will hold `cousins/`, `config/`, `data/`.
2. `config/`: `agent-cmd` (the real agent binary line with `{session_id}`),
   `harness.toml` (transcripts and auto-memory directories, the size guard),
   `embedding.toml` (the embedding provider; keyword-only without it),
   `console-users.json` via `cousin-console adduser <name>`,
   `shared-reviewers.json` if the shared tier is used, `net-allowlist.json`
   only if a non-RFC1918 range must reach the console or chat servers.
3. Units: render `systemd/*.service|timer` with the placeholders (see
   `systemd/README.md`), enable `cousin-loops`, `cousin-console`,
   `cousin-chat-watchdog.timer`, `cousin-sweep.timer`, `cousin-tool-surface.timer`.
4. Run `cousin-tool-surface` once (the boot packet reads it) and
   `cousin-gate --root <root>` with the operator's denylist to confirm the
   install itself is clean.

## Per cousin

1. **Stop it on the source** (its tmux session and chat server) after it has
   written its handoff; keep the source home untouched as the fallback.
2. **Spawn here** with the same slug, name and role: `cousin-spawn <slug>
   --root <root> --name <Name> --role "<role>" --voice "<voice>" --operator
   <operator name>`. This writes `cousin.toml` in this framework's shape,
   `CLAUDE.md` from the template, the MCP registry and `.mcp.json`. Do NOT
   copy the source `cousin.toml`: it carries tables this framework does not
   read (`[runtime]`, `[heartbeat]`, `[budget]`, `[modules]`, `[engagement]`).
   Port its `[[loops]]` entries by hand into the new file; port `[telegram]`
   and `[session]` if used.
3. **Copy memory**, same shapes on both sides: `memory/raw/`,
   `memory/distilled/` (regenerated at first boot anyway), `notes/`,
   `MEMORY.md`, `STATUS.md`, `data/decisions.jsonl`, `data/capsules.jsonl`
   if present, `data/corrections.jsonl`, `data/active-threads.md`,
   `data/handoff.md`. Skip `memory/embeddings.json` and `memory/fts_index.db`
   (rebuilt by the first search) and the source `data/chat.db` (the chat
   schema differs; history stays readable on the source instance).
4. **Rewrite CLAUDE.md**: keep the template's doctrine sections that spawn
   produced and append the cousin's own sections (voice, identity invariants,
   loops it runs, household hard rules) from the source file. The source's
   tool table names commands this framework does not ship (media, arcs,
   backlog, workers); drop those rows.
5. **Auto-memory**: if the harness keeps a per-project memory directory, the
   new home has a new encoded path; copy the directory across so the cousin
   keeps its feedback and reference files, then let `config/harness.toml
   auto_memory_dir` point at the pattern.
6. **Tracker items** do not carry over: the state vocabulary changed
   (`open|active|blocked|done|dropped`); re-add the live ones with
   `cousin-tracker add`.
7. **Start and verify**: `cousin-spawn --start` (or the console's start
   button); then, in order: `/health` on its chat port answers with the slug;
   `cousin-memory search "<a fact from its notes>"` finds it by meaning;
   a chat message from the console reaches the pane with a recall line;
   `cousin-flip <slug> --dry-run` shows every stage; the boot packet
   (`cousin-flip` for real, once) carries the distilled layer and the
   tool surface.
8. **Only then** retire the source cousin: remove it from the source
   console, leave the source home on disk for a week.

## What the old instance keeps

The media path and its gateway, the arc machinery, the backlog and worker
cousins stay on the source instance for the one cousin that uses them; that
is the operator's decision, not a limitation of this page.
