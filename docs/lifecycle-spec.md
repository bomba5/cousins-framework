# Lifecycle specification: boot packets and the flip

How a cousin's identity survives a session boundary: the layered boot
packet, and the flip that ends one generation and starts the next.
Written from the full behavior of the source framework's boot and flip
machinery; this is the contract the implementation is written against
and tested from.

Three incidents shaped the source and their lessons are load-bearing
here: per-layer budget maxima that summed past the documented total
ceiling; a truncation marker appended after the slice that made the
overflow loop spin forever; and degraded-layer detection by substring
scan that flagged healthy cousins every morning. Each got a rule below.

## The boot packet

`assemble(slug, home, generation)` deterministically composes a
cold-start packet within a hard total budget (8k tokens at 4 chars per
token), from these sections in order:

1. **Framework law** - `<root>/config/law.md`. The repo ships a short
   generic default. A missing law file is an install problem, never a
   per-cousin degradation.
2. **Self-portrait** - the cousin's committed text self-portrait. The
   packet reads only the committed version; candidates awaiting review
   do not boot.
3. **Operator calibration** - the distilled calibration file when
   present, else the self-portrait's calibration section. Absent means
   degraded: a persona-anchored cousin booting without calibration
   should know it.
4. **Active state** - STATUS.md open-loops plus the latest handoff,
   with a staleness warning when decisions were logged after STATUS's
   last edit: the next session anchors on STATUS as authoritative, and
   silent drift there poisons the whole orientation.
5. **Task packet** - active-threads.md. Its fallback string is a
   cross-reference, not a gap (see degraded rules).
6. **Tool trace summary** - reserved. The trace ledger is not in v1;
   the section renders empty at zero cost. The rule that allows this
   and forbade the source's worker tick is one rule: accommodate an
   absent subsystem only where absence is a no-op, never where it
   produces recurring output.
7. **Retrieved memories** - recent raw-memory entries (the decide
   bridge is their producer) plus the memory index head. Empty is the
   legitimate starting condition of a new cousin, never degraded.
8. **Required boot actions** - immutable, never truncated.

Identity audit hashes (self-portrait + law; status + handoff) head the
packet for drift detection across generations.

### Budget rules

- Per-layer min/max budgets exist, but the TOTAL ceiling governs: the
  composed packet including headers and the required-actions block
  stays under the ceiling, whatever the per-layer maxima sum to.
- Truncation on overflow walks a fixed priority order (memories first,
  law never), truncating each victim to its minimum. The truncation
  marker counts INSIDE the budget it truncates to - a marker appended
  beyond the slice re-triggers the overflow loop on the same victim
  forever.

### Degraded-layer rules

Detection is per-layer explicit logic, never a substring scan over
section text. Empty memories: fine. Task-packet fallback while active
state has content: fine. Trace section empty: fine, by design. The
self-portrait missing or uncommitted, calibration absent, active state
absent: degraded, named in the packet header so the cousin boots
knowing it.

## The flip

`flip(slug)` ends a generation and starts the next on a fresh session
identity. Stages, in order, each reported in a structured result:

0. **Concurrency guard then preflight.** A fresh in-progress marker
   refuses a second flip (a concurrent flip double-bumps the
   generation and injects into the first flip's new session); a stale
   marker passes through. Preflight validates binaries and config with
   zero side effects and runs even under dry-run - a dry-run that
   skips preflight lies about what a real flip would do.
1. **Capture** the live pane tail before anything destructive.
2. **Handoff prompt** to the live cousin: reconcile STATUS, write the
   handoff, write active-threads. Bounded wait on the handoff file's
   mtime with one halfway nudge; on timeout the framework synthesizes
   an EMERGENCY handoff from observable state (pane tail), marked
   degraded - a real signal loss, never the normal flow.
3. **Session-end audit** (cousin_lib.audits) with the prior packet's
   mtime as session start, then the active-threads baseline
   remediation.
3b. **Transcript mining** (`cousin_lib.transcript_mine`). With
   `config/harness.toml` present, the dying session's transcript
   (`<transcripts_dir>/<session_id>.jsonl`, the OLD id, read before
   the new one is minted) is mined for the cousin's own
   conclusion-like and dead-end sentences, each written as a raw
   candidate (`topic: episode:<id prefix>`, `source:
   flip-transcript`, `L3_COUSIN_CONCLUSION`), capped, deduplicated,
   for the raw -> distill pipeline to judge. Recorded as a stage
   `{"stage": "transcript_mine", "mined": n}` or
   `{"stage": "transcript_mine", "skipped": "<reason>"}`
   (`dry-run`, `config/harness.toml absent`, no persisted session
   id, or the error text). Best-effort by construction: it can
   never fail the flip.
4. **Archive** the ending generation (packet, handoff, pane tail)
   under `data/generations/`.
5. **Generation bump** - a counter file, initialized at spawn.
6. **Assemble and write** the new packet to
   `data/boot-packet-gen-NNNN.md`.
7. **Kill and respawn.** The respawn calls `spawn.start_cousin` - the
   codebase's single tmux-creation site. Session identity is minted as
   a fresh UUID and rendered into the agent command via an optional
   `{session_id}` placeholder in the host's agent-cmd config; an agent
   with no session concept omits the placeholder. The command's
   `{model}` and `{effort}` placeholders render at the same site from
   the cousin's `[runtime]` (else the install's `[agent]` defaults),
   and preflight checks that render BEFORE the kill: a placeholder
   nothing defines fails the flip with nothing destroyed. The id is minted and
   written back into `cousin.toml` EVEN WHEN the placeholder is
   absent - the generation record is more useful with it, and an
   undefined dead state is how a later reader concludes the field is
   unused and deletes it. The id's character set is constrained to
   `[a-z0-9-]` at the mint site: a generated value substituted into a
   command string stays safe by construction, not by the reason it
   happened to be safe today. The write-back uses the same atomic
   re-parsed write the rest of the framework uses.
8. **Inject** the packet after the session settles, prefaced with the
   do-not-announce rule: the seam should be invisible unless the
   operator explicitly asked for a confirmation, in which case the
   flipped cousin posts one line to its own chat surface.
9. **Clear the marker.** The result's ok reflects whether the session
   id actually persisted - a flip that lost its identity write did not
   succeed, whatever else worked.

Crash recovery is the operator: a stale marker is reported, never
auto-recovered. Cousin death is rare enough that a human in the loop
is correct, and the source framework wrote that down as a decision
rather than an accident.

## Stated limits

- **The packet is advice, not enforcement.** Nothing verifies the
  cousin performed the required boot actions; the session-end audit
  catches the write-side rituals only.
- **Flip requires the chat-era layout.** A cousin created by hand
  without the spawn skeleton may lack the files the packet reads;
  every section is absent-tolerant, so the packet degrades rather
  than fails.

## Consciously excluded from v1

Reincarnation, transplant, timed/daily flip drivers, the trace
ledger, and correction-capture calibration ship with their own
modules; each has a named seam above. Transcript mining landed
as stage 3b. The bookends a cousin runs inside one generation
(`cousin-session`) and the harness-side checkpoint hooks are a
separate, smaller contract: `docs/session-hooks.md`.
Timed/daily flip drivers, the trace ledger, and correction-capture
calibration ship with their own modules; each has a named seam above.
Transcript mining landed as stage 3b. Reincarnation and transplant
landed as `cousin_lib.lifecycle`, built on the flip: see
`docs/lifecycle-surgery.md`.
