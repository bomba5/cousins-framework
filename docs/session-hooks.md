# Session bookends and harness hooks

Two small contracts at the edges of one generation of a cousin. The
flip (`docs/lifecycle-spec.md`) is the boundary BETWEEN generations;
this is what happens at the start and end of a session INSIDE one, and
what the agent harness can run on the cousin's behalf when it compacts
or stops.

## Unconfigured state

- No `[session]` table in `cousin.toml`, or no `start_hooks` /
  `end_hooks` key: `cousin-session start|end` runs zero hooks, records
  the run, exits 0. Nothing is implied by absence.
- The shell hooks under `hooks/` are files, not behaviour: nothing
  runs them until the operator wires them into the harness. Run
  without `COUSIN_HOME` they say so on stdout, write nothing, exit 0.

## `cousin-session`

```
cousin-session start [--skip NAME ...]
cousin-session end   [--skip NAME ...]
cousin-session status
```

Context is `COUSIN_HOME` (required; exit 2 without it) and, optionally,
`COUSIN_SLUG`. Hooks come from the cousin's own `cousin.toml`:

```toml
[session]
start_hooks = [
  "cousin-cycle inc --start",
  {name = "activity", cmd = "cousin-memory activity 'session opened'"},
]
end_hooks = [
  {name = "sync-state", cmd = "cousin-sync-state"},
  "cousin-cycle inc --end",
]
```

- An entry is a command string or a table with `cmd` and an optional
  `name`. Unnamed entries are `step-N` by position in the list (the
  position counts named entries too, so a name never shifts a number).
  An entry that is neither is refused by name: exit 2, nothing runs.
  The table is parsed by the same TOML reader as the rest of the file;
  a bracket or comma inside a quoted command is just text.
- Hooks run in file order, each through the shell (`sh -c`), with the
  cousin's home as working directory and a copy of the caller's
  environment plus:
  - `COUSIN_HOME` - the home, always set explicitly;
  - `COUSIN_SLUG` - the environment's value if set, else
    `[cousin] slug`, else the home's directory name;
  - `SESSION_PHASE` - `start` or `end`.
  A relative path in a command resolves against the home, not the
  framework checkout.
- A hook that exits non-zero is reported with its code and the tail of
  its output, and the remaining hooks still run. A hook that runs
  longer than 300 seconds is killed and reported as rc 124. The
  phase's exit is 0 if every hook that ran succeeded, 1 if any failed.
- `--skip NAME` (repeatable) leaves a named hook out; it is reported
  as skipped and does not affect the exit code.
- Every run, including an empty one, is recorded at
  `<home>/data/session.json`: `last_start`, `last_end`, `running`
  (true between a start and an end), and `last_run` with the phase,
  time, per-hook `rc` and output tail, and the ok/failed/skipped
  counts. The write is atomic (temp file then rename); a torn or
  missing file reads as a fresh state.
- `status` prints that record as JSON plus the configured `start_hooks`
  and `end_hooks`, so "what would run" and "what last ran" are one
  command.

The source framework's session command also carried a cosplay overlay,
a media favourites drain and a did-you-post-audio nudge. They were one
install's habits, not a bookend contract, and do not ship; anything of
the kind is a hook in the table.

## The harness hooks

Three POSIX shell scripts under `hooks/`, executable in the tree. Each
reads `COUSIN_HOME` (required) and `COUSIN_SLUG` (optional, else the
home's directory name) and nothing else, writes only under
`<home>/data/`, and never exits non-zero: a hook that breaks the
harness costs more than the checkpoint it was writing. The two that
write answer on stdout with a one-line JSON object carrying a
`systemMessage`, the shape agent harnesses read back from a hook.

| hook | when the harness runs it | writes | says |
|---|---|---|---|
| `hooks/pre_compact.sh` | before compacting the conversation | `data/pre-compact-checkpoint.md`: current activity (`data/last-activity.txt`), the last five decisions (`data/decisions.jsonl`), line counts of the identity files present | read the checkpoint after compaction, then search memory |
| `hooks/session_checkpoint.sh` | when the session stops | `data/session-checkpoint.md`: activity, the open (`- [ ]`) and in-progress (`- [~]`) items from STATUS.md (first twenty), the last five decisions | the next session reads it first |
| `hooks/session_init.sh` | when a session starts | nothing | a banner: slug, home, time, which of CLAUDE.md / MEMORY.md / STATUS.md / PROFILE.md exist and how long they are, which checkpoints under `data/` exist |

Absent inputs are stated, not skipped: a checkpoint written with no
STATUS.md says "No STATUS.md" under that heading, so the reader knows
the file was missing rather than empty. The decision bullets are
formatted with `python3` from PATH; without one, the raw JSON lines
are listed instead.

Wiring is the operator's, per harness: point the harness's
pre-compaction, stop and session-start hook settings at these scripts
with `COUSIN_HOME` in the hook's environment. The framework does not
edit harness settings and has no opinion on which harness it is; the
scripts only assume a POSIX `sh`, `date`, `wc`, `grep`, `tail`.

## Where the pieces meet

`cousin-session start` is the cousin's own ritual and runs what the
cousin's operator listed; `hooks/session_init.sh` is the harness's
banner and runs whether or not the cousin remembers to. Both can be
present. The boot packet (`docs/lifecycle-spec.md`) does not read
`data/session.json` or the checkpoints; they are for the cousin's
eyes at its next turn, which is why the banner names them.
