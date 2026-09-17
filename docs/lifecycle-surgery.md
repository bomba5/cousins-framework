# Lifecycle surgery: reincarnate and transplant

How a cousin changes who it is without losing what it was holding.
`docs/lifecycle-spec.md` covers the boot packet and the flip; this
document covers the two operations built on top of the flip, both
shipped in `cousin_lib.lifecycle`.

## The problem

A running cousin reads its CLAUDE.md once, at session start. Editing
the file changes nothing until the session ends, and ending the
session drops the working state: the half-finished thread, the
current register, the thing it was about to do. So "change this
cousin's role" and "keep what this cousin is holding" pull against
each other.

The flip already solves half of it: the dying generation gets a
bounded window to write its handoff, and the next generation boots
from that handoff plus its durable memory. Reincarnate and transplant
add the other half: change the identity or the memory files between
the bequest and the respawn, under a snapshot, with every step on
record.

## Shared mechanics

**Registry.** Cousins resolve through the filesystem
(`FrameworkConfig.list_cousins`): a slug exists iff
`cousins/<slug>/cousin.toml` exists under the root. No console has to
be running. An unknown slug is a refusal (exit 2) before anything is
touched.

**Snapshot.** Before any mutation, the continuity files are copied to
`<root>/data/lifecycle/<slug>/<timestamp>/`: `MEMORY.md`, `STATUS.md`,
`CLAUDE.md`, `cousin.toml`, `self-portrait.md` when present, and the
whole `memory/` tree minus rebuildable search indexes. The snapshot
lives under the framework root, never beside the home: a snapshot
inside the tree it guards dies with that tree.

**Audit.** Every step appends one JSON line to
`<root>/data/lifecycle/audit.jsonl` with `ts`, `op`
(`reincarnate` or `transplant`), `step`, the slugs involved, and the
step's own fields. A reincarnation writes `snapshot`, `bequest`,
`rewrite`, `flip`, `done`; a transplant writes two `snapshot` rows,
`apply`, two `flip` rows, `done`. The `done` row carries the
operation's `ok`.

**Flip.** Restarts go through `flip.flip` (see the lifecycle spec):
the flip has its own handoff window, session-end audit, generation
archive, and verified identity persistence. The operation's `ok` is
the flip's `ok`. The flip is an injectable seam (`do_flip`) so the
choreography is testable without a terminal.

**Root.** `--root R`, else `FRAMEWORK_ROOT`, else a loud error naming
both (`FrameworkConfig.resolve`). The flip reads the root from the
environment; the lifecycle commands set it from `--root` when it is
not already set.

## cousin-reincarnate

```
cousin-reincarnate <slug> --new-role "<text>" [--root R] [--timeout N]
```

One cousin: new role, same memory. Steps, in order:

1. **Snapshot** the home as above.
2. **Bequest.** The prompt goes to the cousin through its own chat
   server's `/api/send`, so it lands in the chat history and the
   terminal the same way an operator's message does. Then a bounded
   wait (default 300 s, `--timeout`) for `data/handoff.md`'s mtime to
   move. A silent cousin or an unreachable chat server is a recorded
   outcome (`sent`, `wrote`, `reason` in the step), never a failure:
   the flip that follows prompts for a handoff again and synthesizes
   an emergency one if that also goes unanswered.
3. **Rewrite** the role. In CLAUDE.md the title line the template
   renders as `# <Name> - <role>` gets the new role, and a `## Role`
   section's body is replaced when the file has one; everything else
   is preserved byte for byte. In `cousin.toml` the `[cousin] role`
   key is replaced by a targeted line edit, re-parsed, and renamed
   into place.
4. **Flip.** The new generation boots from the rewritten identity.

Output is the structured result as JSON on stdout. Exit 0 when the
flip succeeded, 1 when it did not, 2 on a refusal (unknown slug,
empty role, no root).

## cousin-transplant

```
cousin-transplant --donor A --recipient B --mode MODE [--root R]
```

Two cousins. Both are snapshotted, the mode is applied, then both are
flipped, donor first so the recipient boots last into its new
interior. The donor is never deleted; what to do with it afterwards
is the operator's call.

| mode | recipient's CLAUDE.md, self-portrait, name/role | recipient's MEMORY.md and memory/ | donor afterwards |
|---|---|---|---|
| `soul-donation` | kept | replaced by the donor's (the old ones live in the snapshot) | unchanged |
| `body-swap` | swapped with the donor's | kept in place | carries the recipient's former body |
| `merge` | kept | recipient's MEMORY.md, then a dated heading `## Memories inherited from <Donor> (<date>)`, then the donor's; `memory/raw` unioned (missing files copied, shared files gain the donor's lines they lacked) | unchanged |

Slug, chat port, and tmux session always stay with the slot; a body
swap changes who lives at a slot, never where the slot is.

Refusals (exit 2, nothing touched): unknown donor or recipient, a mode
outside the three, donor and recipient the same slug.

## Rollback

Every mutation is preceded by a snapshot. To undo: stop the cousin,
copy the snapshot's files back over the home (`memory/` whole), then
`cousin-flip` it. For a transplant, do this for both parties from
their own snapshot directories. The audit log names each snapshot
path.

## Stated limits

- **The bequest is advice.** Nothing forces the cousin to write it;
  the recorded `wrote: false` is the signal, and the flip's emergency
  handoff is the floor.
- **Identity drift compounds.** Each reincarnation hands the next
  generation a copy of a copy. Small role changes survive cleanly;
  wholesale rewrites read like someone else's diary, whatever the
  handoff says.
- **Merge is the fragile mode.** Two timelines in one MEMORY.md is
  readable but not reconciled; the distiller sees both and has to
  weigh them. Prefer it over soul-donation when losing the
  recipient's own history would matter; prefer soul-donation when a
  clean interior matters more.
- **No lock against recurring work.** The loops daemon keeps
  delivering while an operation runs; the flip's concurrency guard
  covers a second flip, not a heartbeat landing mid-surgery.
