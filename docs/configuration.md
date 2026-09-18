# Configuration

Everything operator- or install-specific is configuration, read from
`<framework root>/config/`. Every seam is OPTIONAL: absent, the
framework runs with that capability off, and the absence is a no-op or
a stated degradation, never a silent default that becomes somebody's
value. This page lists every file the code reads; a test asserts the
list stays complete, so a new seam cannot ship undocumented.

A fresh checkout's `config/` holds only the shipped `*.example` files
(and is gitignored apart from them); copy the ones you need to their
real names and add the rest by hand.

**The framework root** (which contains `cousins/`, `config/`, and
`templates/`) is named the same way by every entry point that needs
it: an explicit `--root` flag wins, else the `FRAMEWORK_ROOT`
environment variable, else the root `COUSIN_HOME` names (a home lives
at `<root>/cousins/<slug>`, so its grandparent counts when it holds a
`config/` directory; a cousin's own shell often carries only its
home), else - for a command you type - the working directory when it
is a checkout (it holds `templates/cousin-CLAUDE.template.md` and
`config/`), else a loud error naming the channels. `cousin-spawn`, `cousin-flip`,
`cousin-console`, `cousin-mcp`, `cousin-sweep`, `cousin-tool-surface`
and `cousin-chat-watchdog` take `--root` identically; a flag is
discoverable from `--help`, the env var suits a service unit. Library
code (the chat server, memory search) never guesses from its working
directory. The root is made absolute before it is written anywhere, so
`--root .` is safe, but `--root "$PWD"` reads better in a script.

| file | read by | absent means |
|---|---|---|
| `config/agent-cmd` | `cousin-spawn --start`, `cousin-flip` | no agent starts; spawn/flip that need it error with this path named |
| `config/worker-cmd` | `cousin-loops` (worker cousins) | a worker loop stays due and names this path; nothing fires |
| `config/law.md` | boot packet assembly | no framework-law layer; a missing law file is an install matter, never a per-cousin degradation |
| `config/net-allowlist.json` | the network guard (chat server, UI) | loopback + RFC1918 only; the file only ever ADDS CIDRs |
| `config/console-users.json` | `cousin-console` (the web console's login) | auth is not configured: the console is open to every address the guard admits, and `GET /api/auth/me` says so |
| `config/outbound-filter.json` | the outbound content filter | no extra protected terms; the framework ships no vocabulary of its own |
| `config/embedding.toml` | `cousin-memory search`, proactive recall in the chat server | keyword search only, silently - nothing was promised; proactive recall stays silent unless the cousin opts in with `cousin.toml [memory] recall_keyword_only = true` |
| `config/harness.toml` (for Claude Code: copy the shipped `config/harness.toml.claude-code.example`) | transcript mining at flip, the harness auto-memory search collection, the console's token counts, the loops daemon's transcript-size guard, `cousin-mcp approve` (`settings_file`), the console's "needs attention" flag (`attention_patterns`), the `agent-cmd` `{model}`/`{effort}` defaults and the console's spawn catalogue (`[agent]`), `cousin-auth` (`busy_patterns`, `[agent.resume]`, `[auth.api_key]`) | all off; mining says so once at flip, the guard is silent, `approve` refuses with the manual edit spelled out, a `{model}`/`{effort}` placeholder needs the cousin's own `[runtime]` value or the start fails naming this file |
| `config/mcp-registry.toml` (edited copy of the shipped `config/mcp-registry.toml.example`) | `cousin-spawn` (copied into every new home as `mcp-registry.toml`), `cousin-mcp` when no `--registry` is given and the cousin home carries none | the shipped example is the default; the adapter itself is off until a home carries a `.mcp.json` the harness has approved |
| `config/external-peers.toml` (copy of `config/external-peers.toml.example`) | `cousin-chat send`/`list`, and so the MCP `send` tool's peer path | only the cousins under `cousins/` are reachable; a malformed file is an error for every `cousin-chat` call |
| `config/shared-reviewers.json` | `cousin-shared` promotion | promotion refuses with remediation - never a defaulted approver |
| `config/hive.toml` (copy of `config/hive.toml.example`) | `cousin-console` (the console as the hive's queen), `cousin-hive serve` and `cousin-hive nodes` (checkin period) | no hive: no `/hive/` route (404), no remote cousin cards, no build dialog, no socket, no token; a present-but-unusable file is the same off state plus a stderr line naming the problem |
| `config/media.toml` | `cousin-image`/`cousin-voice`/`cousin-video` | media generation is off; the CLIs refuse naming this file, nothing leaves the box |

## Formats

- `agent-cmd`, `worker-cmd`: one command line. `agent-cmd` may carry
  three placeholders, each rendered at the single spawn site
  (`spawn.start_cousin`, so a plain start and a flip render alike):
  `{session_id}` (minted fresh per start and written back to
  `cousin.toml [runtime] session_id`), `{model}` and `{effort}`
  (`cousin.toml [runtime] model` / `effort` for that cousin, else
  `harness.toml [agent] default_model` / `default_effort` below). A
  placeholder with no value in either file is a spawn error naming
  both files; nothing guesses a vendor default. `worker-cmd` carries
  `{prompt}` and `{home}`. The binary and its trust posture are
  yours - the framework hardcodes neither. Name the binary by absolute
  path: units and the tmux server do not see your login shell's PATH.
  `cousin-spawn --start` and `cousin-flip` check, before anything is
  created or killed, that tmux and the command's first word resolve.
  For Claude Code (installed to `~/.local/bin` by its installer):

  ```
  /home/<you>/.local/bin/claude --dangerously-skip-permissions --model {model} --effort {effort} --session-id {session_id}
  ```

  `--dangerously-skip-permissions` lets an unattended cousin run every
  tool without asking; leave it out to answer the prompts yourself in
  the pane. Log Claude Code in once interactively before the first
  start (`docs/install.md` step 4).
- `law.md`: markdown; the framework law every cousin boots with.
- `net-allowlist.json`: `{"allow": ["203.0.113.0/24", ...]}`. Extends
  the defaults; can never remove loopback.
- `console-users.json`: `{"<user>": {"salt": "<hex>", "hash": "<hex>",
  "iterations": N}}`, PBKDF2-HMAC-SHA256 with a random 16-byte salt per
  user and 200000 iterations. Never written by hand: `cousin-console
  adduser <name>` prompts for the password and writes the file
  atomically with mode 0600; the same command with an existing name
  resets that user. Present with at least one user, every `/api/*`
  route but login and `me` needs a session cookie and there is no
  address-based bypass. There is no scope field: no per-user
  authorization is enforced, so none is stored (`docs/console-spec.md`).
- `outbound-filter.json`: the filter's per-surface additions;
  see the outbound filter module for the shape.
- `embedding.toml`: `url`, `model`, `timeout_s` (seconds per
  embedding request; 10 when absent). Set it above the time one chunk
  takes to embed, or every search waits the full timeout and then
  degrades to keyword: with Ollama and `nomic-embed-text` on a CPU
  without AVX one 560-character chunk took 32 s, so 120 is the
  documented value for CPU-only hosts and 30 is plenty with a GPU. The
  chat server's proactive recall embeds each qualifying operator
  message within the same limit. The endpoint takes
  `{"model", "prompt"}` and returns `{"embedding": [...]}`; front any
  service with that contract. A configured-but-unreachable service
  degrades to keyword AND says so - it never quietly pretends.
  Optional search keys: `chunk_chars` (default 2000) and
  `chunk_overlap` (default 200) split long files into overlapping
  chunks before embedding. Optional `[recall]` table for proactive
  recall in the chat server: `min_chars` (default 24, shorter
  operator messages are not searched), `min_score` (default 0.45,
  the semantic similarity a hit needs to be mentioned; keyword-only
  hits under a configured seam are never mentioned, and without the
  seam nothing is unless `cousin.toml [memory] recall_keyword_only =
  true`), `top` (default 3, the most hits one
  message may surface). Thresholds live here, not in code; a
  per-cousin opt-out is `cousin.toml [memory] proactive_recall =
  false` (see `docs/chat-server-spec.md`).
- `harness.toml`: `transcripts_dir`, `auto_memory_dir`, each a path
  template with `{home}` (the cousin home) and `{home_encoded}` (the
  harness's project-dir encoding of it: every `/` becomes `-`, so
  `/a/b` is `-a-b`); a leading `~` is the user's home. The Claude Code
  preset (`config/harness.toml.claude-code.example`) sets
  `transcripts_dir = "~/.claude/projects/{home_encoded}"`, its
  `/memory` subdirectory as `auto_memory_dir`, and `settings_file =
  "~/.claude.json"`. `transcripts_dir` is where the harness writes a
  session's transcript; `auto_memory_dir` is the harness's own memory
  directory for that cousin. A key left out is None, never a guessed
  location. Unparsable is loud: the file promised something.
  `flip_when_transcript_mb` (optional, a positive number of
  megabytes) arms the loops daemon's transcript-size guard: a live
  cousin whose `<transcripts_dir>/<session_id>.jsonl` exceeds it gets
  ONE timed-flip request, five minutes out, through the request
  store (see `docs/loops-spec.md`). It needs `transcripts_dir`; set
  without it, the daemon reports a dead key on every tick. Absent:
  no guard. Zero, negative, or not a number: loud, like unparsable.
  `settings_file` (optional): the harness's own settings JSON, the
  file that records per-project MCP approval; `cousin-mcp approve
  <slug>` edits exactly this file and nothing else. Absent: `approve`
  refuses and prints the edit to make by hand. `~` is expanded.
  `attention_patterns` (optional, a list of strings): pane text that
  means the agent is waiting on a person rather than working, such as
  the harness's login menu; the console's fleet row carries the first
  one a running cousin's pane shows (`attention`) and the card says
  "needs attention". Every injection into a cousin's pane (chat
  delivery, loops, schedule, flip) reads the visible pane first and
  skips, with a "tmux delivery SKIPPED" log line, while it shows one:
  typed text in a login or trust menu selects options. Absent:
  nothing is flagged and nothing is skipped. Not a list of
  non-empty strings: loud.
  `[agent]` (optional table): `default_model` and `default_effort`
  are what the `agent-cmd` placeholders `{model}` and `{effort}`
  render to for a cousin whose `cousin.toml [runtime]` sets neither
  (`effort` is one of `low`, `medium`, `high`, `xhigh`, `max`; another value
  is loud); `models` is the catalogue the console's spawn dialog
  offers (`GET /api/spawn/options`), a list of strings. Absent table:
  no default model, no default effort (a placeholder then needs the
  cousin's own value or the spawn fails naming this file), and a
  built-in catalogue of three names that never reaches an agent on
  its own.
  `busy_patterns` (optional, a list of regular expressions): searched
  in a running agent's visible pane; a match means it is mid-turn,
  and `cousin-auth` and the console refuse to restart it unless
  forced. `[agent.resume]` (optional): `session_arg` and `resume_arg`,
  each carrying `{session_id}`; an auth-mode switch restarts a running
  agent on the same session by swapping the first, found verbatim in
  `agent-cmd`, for the second. Absent: the switch refuses to restart
  and says so. `[auth.api_key]` (optional): the `api_key` auth mode
  (`cousin.toml [runtime] auth`, see `cousin-auth` in
  `docs/guide.md`): `key_env` (the variable the key is handed over
  in), `config_dir_env` (the variable that points the harness at
  another config dir), `source_dir` (the harness's normal config
  dir), `settings_file` / `settings_name` (its settings JSON and the
  name of the copy), `isolated_dir` (default
  `data/harness-api-key-config`, relative to the root), `exclude`
  (entries of `source_dir` not linked), `strip_settings_keys`,
  `preserve_settings_keys`, `login_files` and `login_settings_keys`
  (present in the isolated dir = a login: the launch refuses).
  Absent: `api_key` mode is refused, and the default mode strips
  nothing.
- `shared-reviewers.json`: `{"reviewers": ["name-or-slug", ...]}`. A
  reviewer may never be the proposer; that boundary is enforced at the
  promote site regardless of what this file says.
- `mcp-registry.toml`: the MCP tool registry (`ceiling`, `timeout`,
  `max_output`, `[tools.<name>]` with `command`, `properties`,
  `commands`); the full shape is in `docs/mcp-spec.md`. Commands are
  console-script names resolved beside the interpreter first, then
  on PATH. The example ships with `operators = []`; spawn fills that
  one line per cousin.
- `hive.toml`: `enabled` (bool; anything but `true` is off),
  `public_url` (required when enabled: the queen as NODES reach it,
  `http://<lan-ip>:<console-port>`; it is baked into every archive
  the console builds and prefixes the one-time download link),
  `checkin_seconds` (default 60, at least 5: how often a node checks
  in; a node is online within 2.5 periods of its last checkin),
  `home_chat_url` (optional: the chat server a console-built node's
  `[tell-home: ...]` marker posts to; the build dialog's "home chat"
  switch needs it). See `docs/hive-spec.md`.
- `media.toml`: per-kind sections `[image]` / `[voice]` / `[video]`,
  each `url`, `model`, optional `key_file`, `timeout_s`. See
  `docs/media-spec.md`. A configured provider or an inert refusal -
  never a silent reroute to a vendor you did not choose.

## The rule these share

No configuration key ships without a consumer, and no consumer reads a
key this page does not list. A dead config key is worse than dead
code: a user sets it and believes something changed. The test suite
holds both halves - the code never reads an undocumented `config/`
file, and the specs never promise a key nothing reads.
