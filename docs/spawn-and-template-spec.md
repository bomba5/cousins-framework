# Spawn and template specification

How a cousin comes to exist: the `cousin-spawn` CLI, the cousin
template, and the contract between them. This document is written from a
full behavioral inventory of the source framework's spawn path and is
the contract the implementation is written against and tested from.

The source framework taught one lesson above all the mechanics: **two
definitions of a cousin's identity will drift, and the drift lands in
bedrock.** Its template was wired to nothing while an inline generated
document diverged from it for a month - losing the voice section whose
absence had already caused a documented incident, contradicting the
peer-reply doctrine, and baking mutable facts into files meant to be
permanent. Everything below follows from refusing that state.

## The single-source rule

- `templates/cousin-CLAUDE.template.md` is the ONLY definition of a new
  cousin's CLAUDE.md. No other code path may emit one.
- The renderer substitutes `{{NAME}}`, `{{SLUG}}`, `{{PORT}}`,
  `{{ROLE_ONE_LINE}}`, `{{ROLE_PARAGRAPH}}`, `{{VOICE_GUIDE}}`, and
  strips HTML comments: template comments are guidance for template
  editors, and persisting "replace this before saving" into a cousin's
  bedrock is drift-bait.
- **Any `{{` remaining in rendered output is a spawn failure**, not a
  TODO. An unsubstituted `{{VOICE_GUIDE}}` means the cousin has no
  authored register on bedrock and will improvise one the first time its
  higher identity layers are absent. The renderer enforces this; the
  test suite enforces it again on every example cousin in the tree.
- The template carries doctrine every cousin needs from day one:
  identity framing, in-character vs out-of-character chat handling with
  the peer-reply pitfall stated, the CLI surface that actually ships,
  memory and decision logging, hard rules, and the `## Voice` anchor
  with its authored-never-improvised invariant. Cousin-specific material
  is APPENDED below a marked seam, never edited into the doctrine.
- Bedrock carries no mutable facts: no spawn timestamps, no model
  catalogues, no tool inventories that rot. Those live in configuration
  or docs that are allowed to change.

## Operator references in the template

The template must render correctly in a null-operator install. It names
no person: operator authority is phrased against `cousin.toml
[operator]` ("the operator configured there, if any, is the ultimate
authority"), and examples use the `<operator name>` placeholder form in
prose rather than interpolating a human being into bedrock. A missing
operator is a configuration state, never a defaulted name.

## `cousin-spawn`

`cousin-spawn <slug> --root <framework root> --name <display name>
--role <one line> [--role-paragraph <text>] [--voice <text>]
[--port N] [--operator <name>] [--model <name>]
[--effort low|medium|high|max] [--heartbeat SECONDS]
[--memory-scope private|shared|both] [--start]`

The four optional runtime flags land in `cousin.toml` as `[runtime]
model` and `effort`, `[heartbeat] context_beat_seconds` and `[memory]
scope`; each is validated before anything is written, and a flag left
out writes no key (the documented default applies, never a copied-out
value). The console's spawn dialog sends the same four fields.

### Effects, in order

1. **Validate**: slug against `^[a-z][a-z0-9_-]{1,31}$`; a cousin
   exists iff `cousins/<slug>/cousin.toml` exists (a bare directory
   without one is reported as an orphan with its path, not treated as a
   cousin); the template must render completely with the provided
   values, checked BEFORE anything is written.
2. **Allocate a port** if not given: take the first port in the
   configured range that is neither claimed nor live. The scan range
   decides where to LOOK; the claimed set decides what to SKIP, and the
   claimed set is every port in any `cousin.toml` under the root
   **whether or not it falls inside the range** - a hand-configured
   port outside the range must never become allocatable by someone
   widening the range later, because a port collision between two
   running cousins is silent misdelivery, not a startup error. A live
   bind test backs the scan. **Exhaustion is an error.** There is no
   sentinel value; a cousin without a working chat port is a spawn
   failure, not a degraded success.
3. **Create the home** under `cousins/<slug>/files/`: `memory/`,
   `data/`, `notes/`, `scripts/` - exactly the directories the shipped
   tools read, nothing speculative.
4. **Write `cousin.toml`** to a temporary file, re-parse it with a TOML
   reader, and rename into place. A config that cannot be read back is
   never persisted. Fields: `[cousin] slug/name/role`,
   `[chat] port/tmux_session`, `[operator]` only if `--operator` was
   given.
5. **Render and write** `CLAUDE.md`, plus minimal `STATUS.md` and
   `MEMORY.md` skeletons. Never overwrite an existing file.
6. **Provision the MCP adapter** (`docs/mcp-spec.md`): write
   `mcp-registry.toml` (the install's default registry - the edited
   `config/mcp-registry.toml` if one exists, else the shipped example -
   with its `operators` line filled from `--operator`) and `.mcp.json`
   (the harness registration: `cousin-mcp --registry <that file>` over
   stdio with `COUSIN_HOME`, `COUSIN_SLUG`, `FRAMEWORK_ROOT` in its
   env), each only if absent. The same function is safe to run on a
   cousin that predates it.
   Then **write the harness project settings**
   (`cousin_lib.harness_settings`): `<home>/.claude/settings.json`,
   created or merged (keys it does not own are kept, a second run
   writes the same bytes, a file that is not a JSON object is refused
   and left alone). It carries `"cousin"` in `enabledMcpjsonServers`
   (the approval of the adapter for this project) and this cousin's
   hooks: the three bookend scripts under the checkout's `hooks/`, each
   by absolute path with the home written into the command, so a hook
   never depends on the agent's environment or working directory.
   Folder trust stays the operator's act (`cousin-mcp approve <slug>`
   edits the harness's user-wide file). An existing cousin is brought
   up to date with `cousin-spawn <slug> --repair-settings` (creates
   nothing, safe to repeat). The harness merges hook lists across its
   settings scopes: a user-wide hook still fires in a cousin session
   alongside these.
7. On any failure after step 3: **remove everything this run created**.
   A failed spawn leaves no orphan tree and does not block the slug.
   This includes the partial state where `cousin.toml` was already
   written - the state that squats a slug in practice, since existence
   of that file is what makes a cousin real to the registry. The
   boundary is creation, not startup: once every create step has
   succeeded, the cousin exists, and a subsequent `--start` failure
   KEEPS the home (exit 1) - a valid cousin that failed to launch is
   restartable, not an orphan.

### `--start`

Optional and separate: creating a cousin and running one are different
operations. `--start` creates the tmux session (named by
`[chat] tmux_session`) and launches the configured agent command in it,
then starts the chat server. The agent command - binary, flags, trust
model - is **host configuration** (`config/agent-cmd`), not framework
code: what it means to "run an agent" differs per install and per trust
posture, and hardcoding any vendor's binary path or permission flags
into the framework was one of the source's portability failures. The
command may carry `{session_id}`, `{model}` and `{effort}`
placeholders; all three render at this one site (`docs/configuration.md`
says where each value comes from), so a per-cousin model or effort is
a `cousin.toml [runtime]` fact and the install-wide fallback is a
`config/harness.toml [agent]` fact - the command line itself stays one
line for the whole install.

There is exactly ONE tmux-session-creation site in the codebase, and
`cousin-spawn --start` calls it. The source framework had two (spawn
and respawn), with duplicated environment dicts and constants that
drifted; any future respawn/flip machinery must call the same function.

### Failure reporting

Exit codes are the interface: 0 created (and started, if asked),
1 partial-start (home created and kept, start failed - stated loudly),
2 validation or configuration error (nothing written). A failure is
never reported as success with the error folded into a status string;
that pattern cost the source framework silent half-spawns that its own
UI could not distinguish from health.

## The example cousin

`examples/wren/` is a complete rendered output of the template - a
fictional cousin invented for this repository, not a scrubbed copy of a
real one. It exists so the template's contract is exercised by a real
artifact in the tree: the suite asserts Wren's CLAUDE.md contains a
filled `## Voice` section and no unsubstituted placeholder, which is
the executable form of the incident this design descends from.

## Stated limits

- **Spawn does not compose a boot packet.** In the source framework a
  cousin's first full identity load happens at its first respawn, not
  at spawn; v1 keeps that asymmetry honest by stating it: a fresh
  cousin boots from its rendered CLAUDE.md alone. The boot-packet
  layer arrives with the lifecycle module.
- **Spawn does not start services beyond the chat server.** Heartbeats,
  schedulers, and watchdogs are install-level concerns.
- **Concurrent spawns are not serialized.** Port allocation re-checks
  liveness at bind time, but two simultaneous spawns racing for the
  same slug resolve by filesystem semantics, not by a lock. Single
  operator, single host is the v1 posture.

## Consciously excluded

Respawn/flip, reincarnation, transplant, remote-node spawning, and
UI-driven creation ship with their own modules later; each will consume
this CLI (or its library form) rather than reimplementing any part of
the sequence above.
