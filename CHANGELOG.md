# Changelog

The version lives in `pyproject.toml`. `cousin-version` prints it and
`cousin-version bump [major|minor|patch]` changes it. Newest first.

## Unreleased

### Added
- The `job` tool has a `run` command: `title`, `argv` (the command as an
  array, never a shell string), optional `desc` and `log` (relative to the
  home and confined to it). It is `cousin-job start shell --json [--desc D]
  [--home-log L] -- TITLE CMD`, the same launcher, on both lanes, with the
  title after `--` so no title is read as an option: the command runs detached in its own process group from the
  cousin's home, its output goes to the row's log, and the row closes with
  its exit code. It returns the job id and log path at once, and an answer
  from the launcher without a job id is an error naming what came back. An
  empty argv, a non-string element, a program starting with `-`, or a log
  outside the home or in `.secrets` is refused before a row exists. A cousin no longer needs `cousin-job`
  through Bash for a tracked long command, and the runner contract says so.
  An existing cousin's registry gains `run` at its next start or flip; the
  job tool's description and the `kind` text there keep their old wording,
  because the registry sync never changes a value a cousin already has.
- MCP registry: an `array` placeholder is checked (a JSON array of its
  `items` type, not empty unless optional), and a command's options go
  before a literal `--` in its argv.

- `cousin-job start --home-log REL`: a log path confined to the cousin's
  home with the console's own rule (home_files.resolve_in): absolute, `~`,
  `..` and `.secrets` are refused, exit 2, no row.

### Changed
- `cousin-job start ... --log PATH -- CMD` records the log path as absolute,
  so the console finds a relative one. `--log` itself stays unconfined: it
  is the operator's own flag.

## 1.19.0 - 2026-09-24

### Added
- The console's runner pane highlights what it shows: the event kind in the
  accent colour, text in white, tool calls and results in grey, diffs in diff
  colours, and JSON and markdown rendered. Untrusted text stays linear: every
  inline markdown pattern is bounded, markdown parsing stops after 20,000
  characters (the rest is shown as is), and JSON above 64 KB is not parsed.
- `[agent] effort` sets a runner cousin's effort (`--effort` on the CLI); an
  unknown level is exit 2 at start.
- `cousin-migrate` carries `[runtime] model`, `effort` and `auth` into
  `[agent]`. `plan` prints the runner's bundled CLI version, and a carried
  model or effort is written only after `--validate` ran one turn with it on
  the cousin's own account: a model the runner's CLI can't run is never
  written. `auth = "api_key"` becomes an `anthropic-key` account made from the
  cousin's key file; rollback removes only what apply made. `check` reports a
  `[runtime]` / `[agent]` mismatch.

### Fixed
- A runner cousin is told, in the fixed part of its contract, to reply, send,
  remember and schedule through its tools, not through the terminal CLIs an
  identity file may still name (#95). It applies at each cousin's next
  rollover; a restart resumes the recorded prompt.
- `validate` (`cousin-runner --check-auth --validate`, `cousin-migrate
  --validate`) fails on an error inside the turn, a non-success result or an
  HTTP error status, not only on a result flagged as an error: the "model not
  supported" 400 used to pass. It never runs on credentials inherited from the
  invoking shell.
- A contract test no longer races the runner: it waits for the turn's result
  event, not only for the row.

### Changed
- Requires `claude-agent-sdk` 0.2.159 (bundled CLI 2.1.281), the first that
  runs `claude-opus-5-5`.

## 1.18.2 - 2026-09-24

### Fixed
- A runner cousin on an `anthropic-key` or `claude-token` account can use
  its tools again. The account no longer sets
  `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1`: with it the bundled CLI forced the
  permission mode to `default` (every tool call asked, and a runner has
  nobody to answer) and required the sandbox for every Bash call. Measured
  on CLI 2.1.277 and 2.1.281. The account's key or token now reaches the
  commands the CLI starts, the cousin's own Bash included
  (docs/configuration.md, "No subprocess scrub").
- The live key-lane proof is now `test_a_key_account_runs_its_tools`: a
  Bash call on a key account must succeed with no permission prompt. It
  fails with the scrub set and passes without it.

## 1.18.1 - 2026-09-24

### Fixed
- The inbound gate refuses a sender named like one the framework writes
  itself (`fw-hook`, `runner`, `framework`, `unknown`, `system`,
  `schedule`; `delivery.FRAMEWORK_SENDERS`): a peer or node named
  `fw-hook` was threaded on `system` as a framework hook. `403`.
- `POST /peer/send` answers every refusal before the signature verifies
  with the same `401 {"error": "unauthorized"}`: a known peer's slug no
  longer answers a malformed body with `400` or an unusable entry with
  `503`, so a caller cannot list the configured peers. The no-reach and
  local-slug refusals come after the signature and keep their generic
  `503`; the console's log says why in every case.
- The shipped hive node logs `tell-home dropped` when the queen does not
  take a tell-home or no home is configured; it never retries one, and
  `docs/reference/hive-api.md` now says so.
- `docs/reference/hive-api.md`: a tell-home is shown under the name the
  operator minted the node's token with, and the node's `[tell-home: ...]`
  goes to `POST /hive/tell-home` when `TELL_HOME=1`.
- A runner cousin gets a chat image over 3.7 MB (4.9 MB once encoded,
  under the API's 5 MB cap) as a text block with its full path for the
  model's Read tool, which downsizes it, instead of an inline image the
  API refused (a phone photo failed its turn).
- A runner cousin's own tools (memory, send, reply, job, schedule,
  meeting, handoff) are always loaded: the CLI had deferred them behind
  its tool search, so a cousin had to search before its first call.

## 1.18.0 - 2026-09-24

### Added
- `POST /peer/send` on the console: another install's cousin writes to one
  of these with a message signed by the secret the two installs share
  (`Authorization: HMAC <sender>:<hex>`; `config/external-peers.toml`
  `inbound_token_file`, a 0600 file): the secret never travels. Behind the
  network guard, no console session. `reach` (required) limits where a
  peer may write, and anything outside it answers like a cousin that does
  not exist; `name` is how it is shown. The sending side signs with
  `token_file` and `sender`.
- `POST /hive/tell-home` on the queen: a node's `[tell-home: ...]` with its
  own token, to `config/hive.toml`'s `home_cousin` only, under the name its
  token was minted with (never the name it checks in with). A node built
  with home chat gets `TELL_HOME=1`; `home_chat_url` stays as the legacy,
  unauthenticated path.
- Both go through one gate (`cousin_lib/peer_inbound.py`): a plain sender
  name that is never the cousin's operator or a local cousin; a `msg_id`
  and a finite `sent_at` within five minutes, an id delivered once (a
  repeat is a `409`); control characters stripped; 30 messages a minute
  per sender; 16000 characters at most.
- `cousin-migrate plan` warns (`warn 2.0.0 ...`) about every key a cousin
  or the install still carries that 2.0.0 will reject, from one table
  (`cousin_lib/removed_keys.py`).

### Changed
- A message to a local runner cousin (`cousin-chat send`, the runner's
  `send` tool, the console's peer route) is written into its chat and inbox
  in the sender's own process: a runner cousin needs no chat server to be
  reached. A tmux cousin is still reached through its chat server.
- `cousin-reply` and the media `chat` commands store the reply themselves
  (`chat_api.reply`, what the chat server's reply route runs), with or
  without a chat server.
- The Telegram bridge reads replies from the cousin's chat store, not over
  HTTP, and a runner cousin's bridge needs no `[chat] port`.

### Fixed
- Control characters in a chat message or a sender's name are no longer
  typed into a tmux cousin's pane (the chat server's `/api/send` included).

## 1.17.0 - 2026-09-24

### Added
- `cousin-migrate plan|apply|rollback|check <slug>`: one running cousin from
  the tmux lane to the SDK runner as recorded operator steps (a clean stop,
  the auto-memory import, `[agent] runner = "sdk"`, the supervisor's start
  plus the cousin's chat server, a check that both stay up), the prior
  `cousin.toml` kept in `data/migration.json` byte for byte. `rollback`
  undoes exactly the steps that ran and starts tmux on a fresh boot packet.
  `apply` and `rollback` need `--yes`; nothing else migrates a cousin.
  `check` measures the week on the runner: inbox rows not done after an
  hour, tool calls with no recorded result, failed recorder hooks, the chat
  server. The runbook and the fleet's order are in `docs/migrating.md`.
- Valid time for memory claims, derived from raw: `cousin-memory history
  <topic>` lists each claim's id and when it was valid; `cousin-memory
  obsolete <topic> --entry <id>` (and the console's obsolete route with
  `entry`) retires one claim and keeps the topic.
- `cousin-memory tensions` (and `GET /api/memory/{slug}/tensions`): authored
  topics with two or more live claims of different content, to settle.
- A review gate: when more than `[memory] review_batch` (default 3) entries
  on authored topics were written since it last looked, they are held out
  of the distilled views, the boot packet and the runner's digest until
  kept. On the SDK lane it looks after every turn and at start, and a
  second model (`[memory] review_model`, default the cousin's own) reviews
  in the background (a `review_gate` event). `cousin-memory review` lists
  what is held; its `--keep`/`--drop` are the operator's, refused inside a
  cousin's own process tree.

### Changed
- `cousin-chat-watchdog` judges a runner cousin as running by its runner's
  lock, not a tmux session, so it brings a runner cousin's chat server back
  after a reboot or a crash (the supervisor runs none).
- The monthly raw fold no longer puts an entry-level obsolete mark, a
  retired or held entry, or a review gate record into a month's digest, and
  distill reads a topic with such entries from the whole history.

### Downgrade
- Raw memory written by 1.17.0 reads differently under 1.16.x and earlier: an
  entry-level obsolete mark that is its topic's newest line retires the
  whole topic, and held entries and the gate's records are distilled.
  Nothing in raw changes, and upgrading again restores the views.

## 1.16.0 - 2026-09-24

### Added
- `cousin-supervisor run` keeps an install's daemons up in one process:
  the console, the loops daemon and one `cousin-runner` per runner cousin
  (`[agent] runner = "sdk"` or `"fake"`, unless `[agent] auto_start =
  false` or a stop holds it; tmux cousins are never its). Every child's
  output goes to its stdout prefixed with the child's name (`console | `,
  `runner:wren | `). A child that exits is restarted after 1, 2, 4 ... up
  to 60 seconds, and five exits inside a minute mark it `failing` and leave
  it down with one loud line; a configuration exit (2) is `failing` at
  once, and the console's own restart (exit 75) comes back at once. Exit 5
  is busy, for a runner and for the loops daemon (another runner holds the
  cousin's lock, another loops daemon the install's): the child waits in
  `backoff`, retried after 1, 2, 4 ... 60 seconds with one line per
  attempt, and a busy exit is never counted toward `failing`, so it starts
  as soon as the holder is gone, however long that takes. It
  reaps every child and orphan, so it is a correct PID 1. On SIGTERM it
  stops the runners together (35 seconds each to finish the turn:
  `runner.main.STOP_TIMEOUT_S` plus 5), then the loops daemon, then the
  console, and exits 0; SIGHUP rescans `cousins/` without touching a
  healthy child. One supervisor per install (`run/supervisor.lock`).
- `cousin-supervisor status [--json] | start <slug> | start --name
  console|loops | stop <slug> [--no-wait] | stop --name console|loops |
  reload` talk to the running supervisor over `run/supervisor.sock`; a slug
  only ever names a runner cousin, `--name` the console or the loops
  daemon. `stop` answers once the child is down, `--no-wait` at once.
  `run/supervisor.json` holds the same status for readers that want a
  file, read only while a supervisor holds the lock. The console's fleet
  row gains `supervisor: {state}`, null when no supervisor runs (unknown,
  never stopped).
- A stop of a runner cousin holds it: the supervisor writes
  `<home>/run/held` (the time and who asked) and `start` removes it, and a
  held cousin is not started by a new supervisor, so the stop survives a
  supervisor restart, a `docker compose down` and `up`, an upgrade and a
  reboot until the next `start`, as a stopped tmux cousin stays stopped.
- A runner cousin's start and stop go through the supervisor: the console's
  start, stop and restart routes and `cousin-spawn --start` ask it instead
  of tmux and need no `config/agent-cmd`. The console's stop does not wait:
  it answers 202 `stopping` (200 `stopped` when nothing ran), and its
  restart answers 202 and starts the runner again in the background once it
  is down. With no supervisor running, a start answers 503 (the console,
  a restart's start half included) or exits 1 (`cousin-spawn --start`); a
  stop answers 200 with `"runner": "not running", "supervisor": "not
  running"` and still holds: it writes `<home>/run/held` itself and says
  `"held": true`, so a supervisor started later leaves the cousin down
  until `start` (a restart whose start is refused removes that hold
  again). A stop the supervisor refused is a 502 with its reason in the
  console, never `stopped`.
- `cousin-loops run` holds `run/loops.lock` for its life: a second loops
  daemon on the same root (a second clock) exits 5 (busy) with "another
  loops daemon holds <path>", and the first keeps running. A supervisor's
  loops child that meets such a holder waits for it and becomes the clock
  once it is gone.
- `cousin-spawn --runner sdk|fake [--account <name>]`, and `runner` and
  `account` in the console's `POST /api/cousins`, create a runner cousin;
  `COUSIN_DEFAULT_RUNNER` and `COUSIN_DEFAULT_ACCOUNT` supply them when left
  out (unset: a tmux cousin, as before). The console's spawn dialog sends
  neither, so those two variables decide for it.
- The framework as a Docker image: `Dockerfile` (the source tree installed
  in place at `/opt/framework` with the `sdk` extra, pip removed from the
  image (the venv's and the base image's), uid 10001, all state on the
  `/data` volume, `HOME=/data/home`, a health check on `/api/version`,
  `cousin-supervisor` as the command), `docker/entrypoint.sh` (makes the
  volume a framework root on every start, links `templates/` into the
  image, refreshes `config/*.example`, creates only `config/harness.toml`,
  installs the API key secret privately and declares `[accounts.api-key]`,
  warns while the console has no user) and an allowlist `.dockerignore`,
  so a checkout that is also a live root never leaks its state into the
  build.
- `compose.yml`: the `framework` service with the console on
  `127.0.0.1:8600`, the `framework-data` volume, `COUSIN_DEFAULT_RUNNER=sdk`
  and a 45 second stop grace period; the `embeddings` profile (Ollama).
  `compose.api-key.yml` is the API key lane, turned on with `cp
  compose.api-key.yml compose.override.yml`; `secrets/` and
  `compose.override.yml` are git-ignored.
- `systemd/cousin-supervisor.service` (`TimeoutStopSec=75`): the supervisor
  as one user unit in place of the console and loops units, never beside
  them. `systemd/README.md` gives the migration, which carries the old
  console's `--host` and `--port` into a drop-in, and the way back.
- CI builds the image, runs the runner contract suite inside it and fails
  over 180 MB compressed (`docker/image-size.sh`, `docker save | gzip -6`;
  the image is 162.2 MB).
- `docs/install.md` leads with the Docker install; `docs/operations.md`
  covers the container (what runs, holds, logs, the volume, backup,
  upgrade, extending the image, both auth lanes).

### Changed
- `cousin-runner` exits 5, not 2, when another runner holds the home's
  lock; 2 is configuration only.
- `cousin-backup` snapshots `data/inbox.db` first, then the other
  databases, then the runner's event stream (`data/stream/*.jsonl`, each
  copy cut to its last complete line), then the runner's state files as
  plain copies (`data/runner-session*.json`, `generation.txt`,
  `extract-cursor.json`, `propose-cursor.json`, `proposals.json`). A
  snapshot taken mid-turn restores to a runner that answers the row at
  least once: never lost, possibly answered twice.
- The console's restart route reports `supervised` under `cousin-supervisor`
  too (`COUSIN_SUPERVISED`, set for every child it starts).
- `docs/reference/loops.md` says how the loops daemon treats a runner
  cousin: alive while its runner holds the lock, and every delivery one
  inbox row.

### Fixed
- `python -m cousin_lib.loops run` ran nothing and exited 0: the module had
  no `__main__` block (the `cousin-loops` script calls it directly).
- A runner cousin's chat server no longer refuses to start without a tmux
  binary: its deliveries are inbox rows, so a node's `[tell-home]` and a peer's
  message reach the runner on a host without tmux.
- The console API reference said the restart route exits 0; it exits 75.
- `cousin-runner` retries its lock for about a second before it exits 5
  (#79): `is_running` probes by taking the same lock for microseconds (the
  loops tick, the fleet poll, the console's stream), and a runner starting
  inside a probe was refused. This closes the race for every prober.
- `hold_loops_lock` closes its own fd in every process forked from the
  daemon (an at-fork handler, tolerant of a reused fd number): a worker
  loop's job runs through `jobs._spawn_tracked`, which forks twice with
  no exec, and flock locks are shared across fork, so the job-runner
  child used to inherit the daemon's copy of `run/loops.lock` and hold it
  open past the daemon's own exit - no clock until the job ended.
- A `start` (the console's route, `cousin-spawn <slug> --start`) on a
  runner cousin no longer trusts a stale `delivery.is_alive` read while
  the supervisor is stopping it: a runner can keep its lock for up to
  ~35s after a no-wait stop, and `supervisor.is_held` is true for that
  whole window (the stop writes it at once). A start in that window used
  to read the lock as live and answer "already running" without asking
  the supervisor at all, then the runner went down and stayed held with
  nothing said. It now asks the supervisor whenever the cousin is held,
  and reports its true answer - "still stopping", or a fresh start once
  it is down.
- `spawn.stop_cousin` with no supervisor running answered a runner
  "not running" without checking; a runner started by hand (bypassing
  `cousin-supervisor`) still holds its lock, and the stop now checks
  `delivery.is_alive` and reports "running" truthfully instead. It still
  cannot signal a runner with no supervisor to ask, so it only holds it
  down for the next one, as before.
- `docs/jobs-and-loops.md` did not say that `cousin-loops run --ticks 1`
  beside an already-running daemon exits 5, busy (the same lock a second
  daemon takes); `systemd/README.md` said a second loops daemon left the
  supervisor's loops child `failing` - it is `backoff` (busy, retried
  against the holder forever, never counted toward `failing`).

## 1.15.0 - 2026-09-24

### Added
- Side sessions on the SDK lane: `[agent.sessions]` in cousin.toml maps a
  thread kind to `"primary"` (the default) or `"own"`. A kind mapped `"own"`
  gets one session of its own for all its threads, in the same
  `cousin-runner` process and over the same inbox, with the primary's
  system prompt, tools and working directory (the cached prefix is shared)
  and the same memory. It answers while the primary is busy: a peer is no
  longer kept waiting behind a long task. Its first turn carries a side
  digest (the primary's state, the kinds of threads it is on and since
  when, the cousin's last activity note, then the state digest); it never
  carries a row's words, a sender or a thread key from the primary's live
  turn. The `handoff` tool is refused in a side session; it never proposes
  memories, never runs the `[session]` hooks, and starts over at context
  pressure or when the primary moves to a new generation. A side session
  that fails is restarted inside the process after a backoff; the primary
  and its running turn are never stopped for it. `operator` and `system`
  are always the primary's; an unknown kind or value is exit 2. Its session
  id is kept in `data/runner-session-<kind>.json`, its event stream in
  `data/stream/sdk-<kind>-<id>.jsonl`, headed by a `side_session` event.
  `runner = "fake"` refuses side sessions. A side session is not
  interruptible from the console in this release.
- One memory writer at a time per home: an flock on
  `data/.memory-write.lock`, held by the file-backed memory writes (raw,
  the decisions log and its rotation, the decisions backfill, the recall
  counts, the distilled views with their read of raw, the extraction
  cursors, the activity note, the raw fold's read-archive-remove of a
  month, the trash's check-and-replace), across threads and processes, on
  a lock file opened read-only (another user's lock file never shuts a
  cousin out). Before it, two writers at once could lose a decision from
  the log during its rotation, a recall count, or an extraction cursor, and
  a decision the backfill wrote into an old day file while `compact
  --target raw` folded that day was lost for good.

### Changed
- `data/last-activity.txt` is written through a temporary file, so a reader
  never sees it empty mid-write; a side session's note reads
  `[<kind> session] ...`.
- `data/generation.txt` is bumped through a temporary file and a rename.
- The SDK lane's pre-compact checkpoint shows the tail of the calling
  session's own event stream, not the newest stream file: with side
  sessions that file could be another session's, and a side session's
  checkpoint would have carried the primary's operator rows (and the
  reverse).
- `cousin-runner --once` exits 4 when any session waits for a login, not
  only when the primary does, and 3 when a side session cannot start.

## 1.14.0 - 2026-09-24

### Added
- A runner cousin's reasoning pane in the console: `GET /api/cousins/<slug>/stream`
  (its primary event stream as SSE, live, starting at the newest 200 events;
  frame ids are `<session>:<seq>`, so a reconnect resumes after its last event,
  and one after a runner restart gets a `session` frame and the new stream),
  `POST .../interrupt` (ends the running turn from outside the runner's
  process, past its first answer too) and `POST .../say` (a message to the
  running turn on the operator's thread; a login code is diverted, never
  delivered). The chat page shows it in place of the tmux pane.
- `cousin-watch <slug> [--follow] [--tail N | --after N] [--json]`: the same
  stream in a terminal, from the newest 200 events by default.
- An interrupt is an inbox row (source `interrupt`, priority 0), taken on every
  poll of a live turn: it closes `delivered` when it interrupted, `failed` when
  the CLI refused (the turn goes on) or when no turn was running (then it never
  runs as a turn); a tmux cousin never gets one typed into its pane. Contract
  items `interrupt_row` and `interrupt_row_idle`.
- `cousin-runner` writes a `runner` event first in its stream (kind, pid, the
  items it declares unsupported); the primary stream is the newest file headed
  by one, so a side session's stream never stands in for it. `runner/status.py`
  reads a runner cousin's state from its own stores, and the fleet row carries
  it as `runner`.
- The tokens view's prompt-cache hit rate per cousin and per day
  (`cache_read / (cache_read + cache_creation + input)`, a result with no
  usage left out): `/api/tokens` rows gain `cache`.

### Changed
- The console serves a runner cousin's chat (history, search, send, archive,
  reactions) from its `chat.db`, through `server/chat_api.py`, the library the
  chat server now answers with too: the same bodies on both lanes.
- A runner cousin's fleet row: `status` from the runner's lock, `chat:
  "console"`, `active` a live turn, `pid` the runner's, `lastMsgTs` read from
  `chat.db` read-only; no chat server call and no tmux call for it.
- `policy.toml`'s `ask` is enforced as deny with no approval surface yet; the
  deny reason, the docs and the example no longer promise one in a phase.

### Fixed
- The reasoning stream carries a thinking block's text (bounded at 8000
  characters, `truncated` when cut); since phase 2 it held only its length.

## 1.13.1 - 2026-09-24

### Fixed
- A hit strong in both the keyword and semantic legs could vanish at a
  small `top`. `search()` asked each leg for exactly `top` candidates
  before fusion, so a document ranked just past the cut in BOTH legs
  never reached `_fuse`, although its fused score would have beaten a
  single-leg hit inside the cut (measured on a real replay: fused
  0.0326, rank 3 at top=10, absent at top=5). `search()` now asks each
  leg for `max(20, 4 * top)` candidates and still cuts the fused
  result to `top`.
- `captures_for` (and its sibling `read_capture`) called `.get()` on
  whatever `json.loads` returned; a capture file that held valid JSON
  that was not an object (a list, a number) raised `AttributeError`.
  `captures_for` runs on every operator message (`server/inbound.py`
  `divert_login_code`), so a single malformed capture file broke login
  diversion for every account. Both readers now treat non-object JSON
  like unparsable data: skipped, never raised.

## 1.13.0 - 2026-09-24

### Added
- `cousin-memory import-auto`: folds the agent CLI's own auto-memory,
  its `MEMORY.md` index included, into `memory/imported/auto/` with its
  provenance (`imported_from`, `imported_sha256`, `imported_at` in each
  file's frontmatter). A dry run unless `--apply`; idempotent through a
  manifest; a copy edited since the import is never overwritten, a copy
  removed is never brought back. A manifest that exists but cannot be
  read or parsed refuses instead of guessing: one `ERROR:` line on
  stderr naming the path, nothing written, exit 2; `search` stays
  tolerant of the same manifest (reads it as empty). A copy on disk with
  no row in the manifest is a conflict to merge by hand, unless it is
  exactly what this import would have written, which converges instead
  of blocking on a run that died between copying and saving the
  manifest. `--apply` first replays the cousin's own logged queries that
  reached that memory as a baseline; `--verify` replays them again and
  exits 1 on a lost memory, 2 when nothing was compared, including a
  manifest it cannot read (no comparison is attempted). Both bring the
  search indexes fully current first. An imported copy inherits its
  original's recall weighting, and search indexes it as its original's
  exact text (the provenance lines stripped), so both legs rank it the same.
- On the SDK lane, after a turn in which the cousin reached a decision
  and recorded none, the runner queues one `propose` row asking whether
  to keep it: the lowest priority, never about its own turn, at most
  `PROPOSAL_CAP` a rolling day. A `propose` event records each outcome;
  a step that fails while building the proposal (a broken store, a
  corrupt cursor or cap file) surfaces on that same event's `error`
  field instead of silently producing no proposal, and the turn still
  delivers.

### Changed
- `cousin-memory recall` (and the runner's `memory recall` tool) reads raw
  memory through the search index: a fact written by `remember` is
  recallable, not only a decision. With an embedding service, a hit must
  match the keyword or clear `[recall] min_score`. Without a keyword it
  lists the newest entries the cousin wrote, the framework's own log left
  out. A decision prints as before, a digested one too; times are now
  the raw entry's own (UTC for new decisions, where `decisions.jsonl`
  had local time); the empty result reads `No memories found`. Recall
  no longer counts as a search in the recall weighting.
- The first `recall` or search in a home copies every decision that only
  `data/decisions.jsonl` or its rotated archives hold into raw, once,
  with its original timestamp (marked in `data/.decisions-backfilled`);
  the log is still written but no longer recalled from, and
  `consolidate` counts raw only, so a decision is counted once. A raw
  line removed through the console's trash is never brought back by the
  backfill: a trashed twin counts as already handled.
- The distilled views rank the framework's own log (topics starting
  `episode:`, `job:`, `framework:`) after every authored topic.
- The SDK lane starts the agent CLI with `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`.
- A search finds a harness auto-memory file whose imported copy is current
  as the copy only.
- The `memory` tool's description in `config/mcp-registry.toml.example`
  says what recall now does. It reaches new homes; an existing home reads
  its own `mcp-registry.toml` (copied at spawn, never rewritten by a
  template sync) and keeps the old wording until that file is updated,
  which is also when its cached prompt re-creates once.

### Fixed
- The runner's proactive recall and memory tool read the runner's own
  framework root instead of whatever `FRAMEWORK_ROOT` names, and so
  does `refresh_if_stale` for its keyword index.
- A raw memory in the `[fw-recall]` line is named by its topic, not by the
  date of the file it sits in.
- A failing decisions backfill (a read-only or full `data/`, a bad byte
  in the log, a truncated or corrupt gzip archive under `memory/raw/archive/`)
  no longer silences `search` or `recall`: it prints one stderr line and
  writes no mark, so the next read retries; `consolidate` keeps the
  unguarded, loud failure, since it is a command a person runs. The same
  truncated or corrupt archive no longer crashes a search or recall
  outright either: the raw index and a raw hit's own lookup skip it like
  any other unreadable file.

## 1.12.0 - 2026-09-24

### Added
- The composed system prompt on the SDK lane: the law, a contract generated
  from the tool registry and the version, the authored identity (a missing
  one is a named degraded state, never an improvised persona) and the shared
  operator rules, byte-stable across generations so a rollover and a restart
  keep the prompt cache; passed as the `claude_code` preset with
  `exclude_dynamic_sections` and `snapshot`. The volatile layers (active
  state, task packet, retrieved memories, tool trace, calibration, the shared
  index) ride a state digest under the boot packet's own budgets and
  truncation order (`boot.fit`, shared with the tmux lane) as the first
  message of a generation.
- The framework owns the transcript: `SqliteSessionStore` over
  `data/sessions.db` implements the SDK's session store protocol (its
  conformance suite runs in the tests); a key-lane cousin resumes through
  the store, a login-lane cousin through the CLI's own resume; the first
  init after a resume must name the saved session or the runner starts fresh
  from the digest, loudly. A restart (`data/runner-session.json`) resumes
  the session and costs no generation.
- Usage per result into `data/usage.db` (cost as the per-client difference),
  the console token view reads both lanes; every turn is mined into raw
  memory, deduplicated against the flip miner and capped per rolling day
  (`extract.py`); mirror errors are recorded.
- Rollover as one awaited structured `handoff` call (STATUS open loops,
  threads, memories, the handoff file, in the ritual's order), the session
  bookends, a new session on the same prompt bytes, the digest first; on
  context pressure with hysteresis, at the daily cadence (`max_age`, the
  stagger kept), on `cousin-flip`, reincarnate and transplant (a bequest
  rides the handoff request); coalesced, never interrupting a live turn,
  never losing a row, an unanswered handoff ends in an emergency handoff
  from the real transcript, a failure before the new session ends
  `errored -> idle` with the generation unmoved.
- `rate_limited` is a state: nothing is claimed while a limit holds, a limit
  during the handoff postpones the rollover.
- Accounts: `config/accounts.toml` names `claude-login`, `claude-token` and
  `anthropic-key` accounts; `[agent] account` picks one; the environment per
  kind (`CLAUDE_CONFIG_DIR` under the account, the secret from a 0600 file
  under `.secrets/accounts/`, `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1` for the
  secret kinds, twelve auth and provider variables scrubbed from the shell);
  `cousin-account list|status|login|token`: the operator logs in through the
  framework from a host shell (the CLI's own flow in a pty, the URL relayed
  through chat, the code captured once under `<root>/run/`, never stored in
  chat.db, a late or second code diverted); a cousin never obtains a
  credential. A dying login is detected in the turn (the typed
  authentication error, a 401 retry, the 401 result, a refused connect),
  written to `data/login-required.json`, shown by `cousin-chat list` and the
  Telegram bridge, waited out without a turn and without failing a row, and
  the runner reconnects when the credential changes;
  `cousin-runner --check-auth [--validate]` (exit 4).

### Changed
- `boot.py`'s budget engine is `boot.fit`, its shared tier `boot.shared_parts`;
  `trace` readers take an explicit root. `Inbox` gains `claim_id`, `open_rows`,
  `replace_body`, `done_if_queued`. `flip.flip` takes `reason` and
  `queue_if_stopped`; the loops daemon names its reasons. `api_key_file` is
  deprecated for `[agent] account`. The `handoff` tool's schema is the
  structured one (position, next_action, status, active_threads, learned).
- The event stream gains `usage`, `extract`, `rollover`, `rate_limit`,
  `auth` and the `system` subtypes `resumed`, `fresh`, `resume_failed`,
  `connect_failed`.

### Fixed
- Two timing-based runner tests (the mid-turn fold, the fake runner's state
  stream) now wait for the event they assert instead of sleeping.
- The usage tests evaluate today's date per call, so a suite that crosses
  UTC midnight no longer compares against yesterday.

## 1.11.0 - 2026-09-23

### Added
- In-process tools on the SDK lane: the runner serves every registry tool
  (`memory`, `send`, `job`, `schedule`, `meeting`, and any other the cousin's
  `mcp-registry.toml` enables) as an in-process MCP server built from the one
  registry, so a tool exists on both transports or on neither, plus two tools
  that exist only there: `reply`, the only writer of the chat surface, which
  routes by the live turn (one thread implicit, two must be named, a named
  operator or person thread always reachable), and `handoff`. A registry
  command with no in-process handler stops the runner at start with the list.
  No process is spawned per tool call.
- In-process hooks: recording (job rows, activity lines) through the new
  `cousin_lib/recording.py`, memory recall off the loop with a 4 s budget and
  the chat server's own gates (one implementation, `memory_search.recall_context`),
  Stop and PreCompact checkpoints, `waiting_permission` on permission requests,
  and `policy.toml` (deny_tools, deny_bash_patterns on any tool's command, ask,
  outbound_filter) enforced as a PreToolUse hook that fails closed; a subagent
  must name the thread it replies to. Matched PreToolUse callbacks run
  concurrently in the CLI; the recorder consults the policy itself.
- Prompt-cache guards: every `result` event carries the SDK's `usage`;
  `session_init` carries the tools and MCP servers the CLI offered; tool
  definitions are byte-stable across connects (tested across interpreters);
  an opt-in live check requires turn 2 to read back what turn 1 cached.
- Producers on the runner: `delivery.accepted()` (a durable put is
  acceptance) and `delivery.is_alive()` (liveness from `run/runner.lock`);
  schedules, loops and meetings hand a runner cousin its items without
  waiting; a Telegram message for a runner cousin is stored and delivered by
  the bridge; chat hooks are one library call every send path makes.
  `cousin-runner` exports `COUSIN_HOME` and `FRAMEWORK_ROOT`, refuses an
  unservable registry with rc 2, and holds one lock per cousin.

### Changed
- `memory` (decide, remember, recall, activity) and `schedule` (add, list,
  cancel) are library functions the CLI calls, byte-identical output.
- `cousin-schedule tick` keeps a job pending when its deliverer says the
  delivery was not accepted.
- `[agent] runner = "sdk"` is no longer experimental for chat, schedules,
  loops and meetings; the composed prompt and the console views arrive in
  phases 4 and 5.
- The master plan takes the accepted review's order and, after 2.0.0, a
  wishlist; phases 4, 5, 7 and 10 gain the breakdown ledger, the cache hit
  rate in the tokens view, valid-time claims with a tensions view, a review
  gate for bulk memory writes, and a glossary.

## 1.10.0 - 2026-09-23

### Added
- The runner: a cousin can run with no terminal. `cousin-runner --home <home>`
  claims items from a durable thread-keyed inbox (`data/inbox.db`) in source
  priority order, drives one long-lived Claude Agent SDK client, appends
  every SDK message and state change to `data/stream/<session>.jsonl`, and
  marks each item done by the turn that consumed it. A message that arrives
  mid-turn from the operator is folded into the running turn and closed by
  its one result, and only once the CLI has echoed it (`replay-user-messages`):
  a row the CLI did not echo before the result belongs to the next turn and
  is closed by that turn's result. `FakeRunner` is the reference
  implementation; one contract suite (18 items: receipts, priority, folding,
  interrupt, outcomes, failure recovery, stop, the event stream) tests both.
  `cousin.toml [agent] runner = "sdk"` switches a cousin over and routes
  `deliver()` to the inbox; every tmux cousin is untouched and the key is
  experimental until phase 3 moves the producers. The auth lane is the
  presence of the key in the session environment and nothing else (the
  login lane scrubs `ANTHROPIC_*` from the runner's environment), and
  `apiKeySource` from every session init is recorded so a cousin on the
  wrong lane is visible. One runner per cousin, held by `run/runner.lock`.
  A failed turn is drained to its result so the next turn starts in sync,
  reconnecting with `resume` as the fallback; a runner that cannot connect
  exits 3 for a supervisor to restart. Optional extra `sdk`.
- Live proofs, opt in with `COUSIN_LIVE_SDK=1`: two related messages through
  `deliver()` answered in one session, and a message sent during the final
  answer neither lost nor misattributed.

### Changed
- `Inbox` grew `requeue`, `unfinished` and `get`; the master plan's locked
  interfaces say so, and it takes the operator-accepted review: the twelve
  unplaced parity rows placed, `max_age` as the flip cadence, phase 7 tasks
  1-2 ticked, and the order 2, 3, 4, 7(3-6), 5, 6, 7(7-8), 8, 9, 10.
## 1.9.1 - 2026-09-23

### Changed
- `claude-opus-5-5` heads the built-in model catalogue (`DEFAULT_MODELS`),
  probed live on five running cousins the day it was released. The spawn
  dialog's default follows the catalogue head when `default_model` is unset.

## 1.9.0 - 2026-09-22

### Fixed
- A bounded foreground embedding pass is no longer silent. `ensure_index`
  reported `incomplete`, but `search()` branched on `busy` and `failed` only,
  so the flag was produced and never displayed: a semantic leg that had ranked
  against 24 of 416 chunks returned `notice=None` and its hits came back
  looking like a complete result over the whole corpus. Measured on a cold
  home 2026-09-22, 5.8% of the corpus, by the peer that hit it. Hits from 6%
  of a corpus presented as complete are how a confident wrong file gets cited

### Added
- `ensure_index`'s report carries `ranked` (current chunks whose stored vector
  still matches their text) and `total` (current chunks in the corpus), so the
  notice can say how much of the corpus the semantic leg actually saw instead
  of only that it saw some. A carried-over or failed chunk keeps its old
  vector and is NOT counted: it is ranked against text that no longer exists,
  which is a worse failure than being unranked, not a better one. Both are
  None on the busy path, where the pass did no work and the coverage is
  unknown

## 1.8.0 - 2026-09-21

### Fixed
- A search no longer pays for a whole backfill. `search()` asks for a bounded
  embedding pass (24 chunks) and the loops daemon finishes the rest;
  `ensure_index(wait=False)` meant "do not queue behind another pass", not "do
  not do the work", so with the lock free a single query ran every embedding
  itself. Harmless while a cousin had a few hundred chunks; indexing the raw
  store multiplied that by about ten and a cousin's first query after the
  change sat over three minutes with the embedding service pinned

### Changed
- The vector index is SQLite (`memory/vectors.db`, one row per chunk, the
  vector a float32 blob) instead of one JSON object read and parsed in full on
  every search. Measured on a real cousin: loading the index went from 1283 ms
  to 69 ms and the file from 16.4 MB to 4.3 MB, against the 938 ms embedding
  call the index exists to serve. An existing `embeddings.json` is imported
  once on first read and removed
- A vector store too damaged to open is replaced rather than fatal: the index
  is a cache of what the sources say

## 1.7.0 - 2026-09-21

### Added
- Memory search indexes the raw store (`memory/raw/*.jsonl` and its monthly
  archives), one unit per entry, as the `raw` collection. It holds what
  `cousin-memory decide` and `remember`, the flip's transcript miner, the jobs
  ledger and framework events write, and nothing indexed it: `*.md` only. An
  entry reached recall solely through `distill`, which keeps one truncated
  line per topic and caps each file at 40 lines. Measured on a real cousin:
  904 entries over 789 topics survived as 139 lines, so 82% of topics could
  not be found

### Changed
- One result slot is reserved for a curated topic file (`memory/**/*.md`, or
  the harness auto-memory) when the ranking would drop every one of them.
  Indexing the raw store took curated files from 13 of 45 top-three slots to
  2 on a real corpus, because BM25's length normalisation puts a short entry
  above a long file that names the term once. The reserved slot is the last,
  so the best match is never displaced; an explicit `--collection` is never
  overridden

### Fixed
- The same entry is indexed once however many files hold it. `raw_fold` keeps
  a month in both `<YYYY-MM>-digest.jsonl` and `archive/<YYYY-MM>.jsonl.gz`,
  and the twins carry the same topic and content under different metadata, so
  one memory returned as two hits (344 duplicates on the benchmark corpus)
## 1.6.3 - 2026-09-21

### Changed
- Every producer that reaches a cousin (chat, reactions, chat hooks, loops,
  schedules, meetings, the flip, a pending boot) goes through
  `cousin_lib.delivery.deliver()` with a typed, thread-keyed item. The text
  reaching the pane is byte-identical; this is the seam the agent loop runner
  plugs into (`docs/design/agent-loop-runner.md`)
- The memory recall line travels to the cousin as the item's context instead
  of being glued onto the message text. What the cousin reads is unchanged

## 1.6.2 - 2026-09-21

### Fixed
- The boot packet's MCP line claimed a scope it did not always have. A cousin
  with no persisted `runtime.session_id` (hand-made, or on its first flip) fell
  back to the newest log file, which is the pre-1.6.0 reading and may belong to
  an older generation. The line now says it could not be scoped instead of
  claiming the generation that just died (#54)

### Documentation
- The 1.5.1 WAL entry stated a property the build only permits; it now names the
  conditions under which WAL does not apply

## 1.6.1 - 2026-09-21

### Fixed
- The boot packet's MCP warning silently depended on the flip assembling the
  packet before persisting the new session id. Nothing stated or tested that
  order, and inverting it would have made the warning scope to a session with
  no log and go quiet forever. Named at the persist site and guarded by
  `tests.test_flip.TestAssembleSeesTheDyingSessionId`, which was proven to go
  red under the inversion (#54)

## 1.6.0 - 2026-09-21

MINOR rather than PATCH for one reason: `cousin-mcp --last-connection` gains
a third state on its public interface. Everything else here is a fix.

### Changed
- `cousin-mcp --last-connection` reports three states instead of two:
  `connected`, `FAILED`, and `no outcome recorded`. Exit 2 now also covers an
  attempt whose outcome was never written down, which previously exited 1 as
  a failure
- `mcp_logs.last_connection()` returns `state` (`connected` | `failed` |
  `unrecorded`) in place of `ok`, plus `reason` and `earlier`

### Fixed
- The boot packet's MCP line no longer predicts the session that is booting.
  It names the session it is about, reports that session's attempt time, and
  is scoped to the generation that just died whenever a `runtime.session_id`
  is on file; see 1.6.2 for what it says when none is (#54)
- An attempt whose outcome nobody recorded reads as unrecorded, not as a
  failure, and a failure in wording the parser has no literal for keeps its
  reason instead of printing "no reason recorded". Measured over 5908 harness
  logs: 93 files were reported FAILED with no reason; 80 held the reason and
  13 held no outcome at all, one of them since April (#54)
- The warning no longer claims "Nothing retries it": a harness session
  reconnects inside itself, which 186 of those logs record (#54)

## 1.5.1 - 2026-09-20

### Fixed
- WAL mode on `jobs.db`, `scheduled.db`, `hive.db` and the memory indexes,
  best-effort: `sqlite_util.wal` swallows a `sqlite3.Error` and SQLite converts
  the journal on the first WRITE, so a database on a network filesystem, or one
  nobody has written since, stays in the rollback journal (#38)
  <!-- wording corrected in 1.6.2: the original entry stated the property
       without the condition the build merely permits -->

## 1.5.0 - 2026-09-20

### Added
- `cousin-mcp --last-connection`: why a cousin's MCP server failed, with the
  server's own stderr (#36)
- The boot packet reports a failed MCP connection
- `mcp_logs_dir` in `config/harness.toml`

## 1.4.0 - 2026-09-20

### Added
- Every cousin flips daily, whether or not its `cousin.toml` says so
- `default_flip_at` in `config/harness.toml`; `flip_at = "never"` opts a
  cousin out
- `cousin-loops flips`: each cousin's flip time and where it comes from

## 1.3.1 - 2026-09-20

### Fixed
- Additive column migrations raced between two openers of the same database
- The console reports a serving thread that outlives `stop()`

## 1.3.0 - 2026-09-20

### Changed
- A tool that does not validate is skipped with a warning instead of killing
  the MCP server. Unparseable TOML and the tool ceiling stay fatal (#35)
- `cousin-mcp --selftest` exits 1 when a tool was skipped

## 1.2.2 - 2026-09-20

### Fixed
- An added registry table goes in with its own tool, not at the end of the file

## 1.2.1 - 2026-09-20

### Fixed
- `cousin-spawn <slug> --sync-template` crashed instead of running

## 1.2.0 - 2026-09-20

### Fixed
- The MCP registry sync is additive at every level. A command or property
  added inside a tool block a cousin already had never reached it (#32)

## 1.1.0 - 2026-09-19

### Fixed
- Closing a shell job ends its processes, not just the runner
- A finished job whose process group still has processes is shown as a leak

## 1.0.3 - 2026-09-19

### Fixed
- The flip tests no longer leave a chat server on :8100
- Install docs and README quick start, from a clean Ubuntu 24.04 re-test

## 1.0.2 - 2026-09-19

### Fixed
- The console fits on a phone: no view, inspector or modal is cut off

## 1.0.1 - 2026-09-19

### Fixed
- The tokens page reads every transcript in the window, not only today's
- Each message counts once; totals were roughly doubled

## 1.0.0 - 2026-09-19

### Changed
- **Breaking:** `cousin-meeting teach` is removed; the template sync replaces it
- Every start and flip syncs the framework part of `CLAUDE.md` from the
  template. `## Identity` and `## Voice` stay the cousin's own

### Added
- `cousin-spawn <slug> --sync-template`, with `--apply`

## 0.12.1 - 2026-09-19

### Fixed
- The console pane no longer flickers while a cousin works
- Telegram first-time setup with no operator configured

## 0.12.0 - 2026-09-19

### Added
- A Telegram panel per cousin in the console, and the bridge starts and stops
  with its cousin

## 0.11.0 - 2026-09-19

### Added
- `cousin-reply --image` and `--video` attach to the reply (#24)

### Fixed
- Telegram attachments over the upload limit, and rejected sends log the reason

## 0.10.0 - 2026-09-19

### Changed
- Every cousin's memory index is kept fresh by the loops daemon

## 0.9.0 - 2026-09-19

### Added
- Telegram carries images, video and voice both ways (#13)

## 0.8.2 - 2026-09-19

### Fixed
- A failed Telegram relay no longer loses the message; the bridge saves its
  position

## 0.8.1 - 2026-09-19

### Fixed
- The boot packet gives the same pre-exit order as the rest of the docs

## 0.8.0 - 2026-09-19

### Added
- Delete a meeting, turn numbers in every turn line, and an opening notice

## 0.7.0 - 2026-09-19

### Changed
- `[memory] scope` is `private` or `shared`; `both` is read as `shared`

## 0.6.0 - 2026-09-19

### Added
- Meetings: a chat shared by the user and several cousins, in rounds.
  `cousin-meeting`, a console page, and docs

## 0.5.0 - 2026-09-19

### Added
- Boot packet layer 2, shared rules and fleet memory

## 0.4.0 - 2026-09-19

### Changed
- Sidebar groups are saved per console user on the server

### Fixed
- The daily flip skips a stopped cousin instead of starting it

## 0.3.0 - 2026-09-18

### Added
- Memory fills in its own truth levels: framework changes at L1, finished jobs
  at L2, hedged sentences at L4
- `cousin-memory obsolete TOPIC --why ...` retires a topic (L5)
- The documentation is rewritten from scratch

## 0.2.0 - 2026-09-18

### Added
- Remote cousins on other machines, with the console as the hive queen
- Truth levels on memory writes, and a layered memory explorer
- A read-only file browser per cousin
- Markdown and Mermaid in chat; images, video and audio
- `config/external-peers.toml` for cousins on another install

### Fixed
- Chat is not typed into a pane showing a menu or a prompt
- The console pane cursor and mouse behave

## 0.1.0 - 2026-09-18

The first numbered release: cousins with their own memory, chat and
lifecycle; the loops daemon; per-cousin auth; the web console; optional
media, Telegram and remote cousins; systemd units and backups.
