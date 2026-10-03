# Glossary

The words these docs use in a sense of their own. Each entry says what the
word means here and where the page that covers it is.

### boot packet

the text a fresh session starts from: the framework law,
the self-portrait, the active state and recent memory. It
is rebuilt from disk at every flip and rollover. See
[reference/lifecycle.md](reference/lifecycle.md).

### console

the web interface over the whole install (`cousin-console`): the
fleet, chat, the reasoning pane, memory, jobs, settings. It is a view over the
stores and owns only its sessions and its users file. See
[console.md](console.md).

### cousin

a persistent agent with a home directory under `cousins/<slug>/`:
its `cousin.toml`, its identity files, its memory and its stores. It keeps its
identity across sessions; a session is a process it runs in, not the cousin.
See [cousins.md](cousins.md).

### distilled

the views under a cousin's `memory/distilled/`, rebuilt from
raw memory by `cousin-memory distill`. The boot packet reads them. Raw keeps
every entry; the distilled views keep what is live. See [memory.md](memory.md).

### event stream

See [stream](#stream).

### flip

ending a cousin's session and starting a fresh one on a new boot
packet (`cousin-flip`, the daily `flip_at`, or the console). Each session is a
**generation**. See [reference/lifecycle.md](reference/lifecycle.md).

### fold

a message that arrives while a turn runs, streamed into that turn
instead of waiting for the next one. Operator, person and peer chat fold;
schedules, meetings and loops queue to the turn's end. The `tmux` kind cannot
fold (its CLI queues typed input). See [reference/runners.md](reference/runners.md).

### generation

one session of a cousin, from one flip or rollover to the
next. The boot packet names its number.

### hive

cousins on other machines. The console machine runs the **queen**
(an HTTP API under `/hive/`); each other machine is a **node** that calls the
queen with its own token, never the other way round. See
[remote-cousins.md](remote-cousins.md).

### inbox

a runner cousin's durable, thread-keyed queue in SQLite in its
home. Every message, schedule, meeting turn and loop reaches a runner cousin
as an inbox row; the producer inserts it and wakes the runner. See
[reference/runners.md](reference/runners.md).

### kind

which runner a cousin runs on, its `[agent] runner`: `sdk` (the
Claude Agent SDK, first class), `tmux` (the host's interactive Claude Code CLI
in a tmux pane), `opencode`, or `fake` (tests). A cousin moves between kinds
with `cousin-migrate`. See [reference/runners.md](reference/runners.md).

### lane

how a cousin is driven: a **runner kind** (`sdk`, `tmux`, `opencode` or
`fake`). The **legacy tmux lane** (a cousin with no runner kind, typed into
by its chat server) was retired in 2.0.0; a cousin without a runner kind is
refused by name. See [cousins.md](cousins.md).

### loop

a recurring prompt or command from a cousin's `[[loops]]`, run by
the loops daemon (`cousin-loops`); heartbeats and daily flips are its too. See
[jobs-and-loops.md](jobs-and-loops.md).

### operator

the person a cousin answers to, from `[operator]` in its
`cousin.toml`. The operator's instructions are the cousin's highest authority.

### plugin

an install-level extension the framework runs but does not ship: a
directory with a `plugin.toml` declaring tools (an MCP server), a service
and a console page (a tab over the pane, or a strip over the chat), each optional, declared in `config/plugins.toml` and
turned on per cousin. See [plugins.md](plugins.md).

### raw memory

every memory entry as written, one line each under a
cousin's `memory/raw/`, with its truth level. Nothing is edited in place;
retiring a topic writes a new entry. See [memory.md](memory.md).

### review gate

the check that holds a batch of new memory entries out of
the distilled views until they are kept or dropped, by a second model on the
runner lane or by a person. See [memory.md](memory.md).

### rollover

a runner cousin ending its generation by itself, on context
pressure or age: it writes a handoff, and the next session starts from a
fresh boot packet. A flip is the same move started from outside. See
[reference/runners.md](reference/runners.md).

### runner

the process that drives one runner cousin (`cousin-runner`): its
inbox, its state machine (`idle`, `running`, `rate_limited`,
`rolling_over`, ...), its session and its event stream. See
[reference/runners.md](reference/runners.md).

### schedule

a one-shot future prompt to a cousin (`cousin-schedule`). See
[jobs-and-loops.md](jobs-and-loops.md).

### shared tier

files every cousin reads, under the framework root's
`shared/`. A cousin proposes a file; someone else promotes it; the proposer
and the reviewer are never the same; the install's
[house rules](house-rules.md) are seeded into it. See [memory.md](memory.md).

### side session

a second session in the same runner that answers one class
of threads (a meeting, a peer) while the primary session is busy, merging
through memory, never through context. Off unless `[agent.sessions]` turns it
on. See [configuration.md](configuration.md).

### stream

a runner cousin's event stream: every state change, turn, text,
thinking block and tool call, appended as JSONL and readable live
(`cousin-watch`, the console's reasoning pane). See
[reference/runners.md](reference/runners.md).

### supervisor

`cousin-supervisor`: one process that keeps the console, the loops daemon
and one runner per cousin up, in the Docker install and on a bare host
alike, restarts what crashes and stops them in order. See
[operations.md](operations.md).

### thread

everything between a cousin and one other party, in both
directions: `operator:<name>`, `person:<name>`, `peer:<slug>`,
`meeting:<id>`, `loop:<name>`, `schedule` or `system`. A reply goes back on the
thread it answers. See [chat.md](chat.md).

### tracker

the install-wide list of work in flight (`cousin-tracker`):
open, active, blocked, done or dropped, and whose it is. See
[commands.md](commands.md).

### truth level

what a memory entry rests on: `operator`, `framework`,
`tool`, `conclusion`, `hypothesis` or `obsolete`. Recall shows it beside the
entry. See [memory.md](memory.md).

### turn

one run of the model on a cousin's session, from the input it takes
to its result. A runner closes the inbox rows a turn answered when the turn
ends.

### worker

a cousin with `[cousin] type = "worker"`: no session and no
heartbeat; its loops run `config/worker-cmd` as tracked jobs. See
[jobs-and-loops.md](jobs-and-loops.md).
