# Configuration

Every file the framework reads from `config/`, every key in it, and the keys
of a [cousin](glossary.md#cousin)'s own `cousin.toml`. Read the first part when you set up an
install, and come back to the rest when you want to turn something on.

Only one file is required: `config/agent-cmd`. Everything else is optional.
When a file is missing, the thing it configures is off, and the code says so
where it matters instead of guessing a value. When a file is there but
broken, you get an error that names it. A typo never reads as "off".

A fresh checkout's `config/` holds only `*.example` files. Git ignores
everything else in there, so your real files (which can hold keys and
tokens) never get committed. Copy an example to its real name and edit it.

## Which ones you need

For a normal install with Claude Code:

```
printf '%s\n' "$HOME/.local/bin/claude --dangerously-skip-permissions --model {model} --effort {effort} --session-id {session_id}" > config/agent-cmd
cp config/harness.toml.claude-code.example config/harness.toml
cousin-console adduser ana                     # writes config/console-users.json
```

Then add these when you want what they do:

| file | turns on |
|---|---|
| `embedding.toml` | semantic memory search and proactive recall |
| `net-allowlist.json` | console and chat access from outside the private ranges |
| `external-peers.toml` | messaging cousins on another install |
| `hive.toml` | the console as queen for cousins on other machines |
| `media.toml` | image, voice and video generation |
| `shared-reviewers.json` | promoting memory into the [shared tier](glossary.md#shared-tier) |
| `outbound-filter.json` | blocking words from leaving in chat and media captions |
| `law.md` | a block of rules every cousin boots with |
| `worker-cmd` | loops for [worker](glossary.md#worker) cousins |
| `mcp-registry.toml` | your own default MCP tool list for new cousins |
| `accounts.toml` | [runner](glossary.md#runner) cousins on their own login, token or API key |

## The framework root

Everything below lives in `<root>/config/`, where the root is the directory
holding `cousins/`, `config/` and `templates/`. Normally that's the checkout.
Every command finds it the same way, first match wins:

1. `--root <dir>` on the command line
2. the `FRAMEWORK_ROOT` environment variable
3. `COUSIN_HOME`, if it points at `<root>/cousins/<slug>` and `<root>/config/`
   exists
4. the working directory, if it looks like a checkout (it has
   `templates/cousin-CLAUDE.template.md` and `config/`). This one only
   applies to commands you type, never to the chat server or the daemons.

Otherwise the command stops and tells you how to name the root.

## agent-cmd

One line: the command that runs your agent inside a cousin's tmux session.
Read by `cousin-spawn --start`, `cousin-flip` and the console's start button.
Without it no cousin can start, and those commands say so with the path.

```
<your home>/.local/bin/claude --dangerously-skip-permissions --model {model} --effort {effort} --session-id {session_id}
```

`--dangerously-skip-permissions` is what lets a cousin work unattended, and it
means the cousin can do anything your account can; see
[install](install.md#4-claude-code). Use an absolute path for the binary. systemd units and the tmux server don't
see your login shell's PATH. Before starting anything, the framework checks
that tmux and the first word of this line resolve.

Three placeholders are filled per start:

- `{session_id}`: a fresh id minted on every start, written back to the
  cousin's `cousin.toml` as `[runtime] session_id`.
- `{model}`: the cousin's `[runtime] model`, else `[agent] default_model` in
  `harness.toml`.
- `{effort}`: the cousin's `[runtime] effort`, else `[agent] default_effort`.

A placeholder with no value in either place is a start error naming both
files. There's no built-in vendor default.

## accounts.toml

`config/accounts.toml` names the accounts a runner cousin (`[agent] runner =
"sdk"` or `"tmux"` on a `claude-login` account, or `"opencode"` for an
`opencode` account) can run on. A cousin picks one with `[agent] account` in its
`cousin.toml`. A cousin that names none runs on `host`, the host's default
login in `~/.claude`, shared by every such cousin, exactly as before this file
existed. A cousin never obtains credentials itself: it runs on what it is
given, and `cousin-account` is operator-run.

The login lane is every `claude-login` account (the host's `~/.claude` and
each named one) and every `claude-token` account (a long-lived subscription
token from `claude setup-token`): putting a subscription's credentials into
the framework with `cousin-account login|token` carries the same terms risk
as any other use of the login lane, and it is the user's.

```toml
[accounts.fleet]
kind = "claude-login"            # its own login, in data/accounts/fleet/
# config_dir = "data/accounts/fleet"   (the default)

[accounts.nightly]
kind = "claude-token"            # a long-lived token from `claude setup-token`
# secret_file = ".secrets/accounts/nightly"   (the default: git-ignored)

[accounts.metered]
kind = "anthropic-key"           # an API key: metered billing, no login
# secret_file = ".secrets/accounts/metered"   (the default: git-ignored)

[accounts.keyed]
kind = "opencode"                # opencode on the providers whose keys you hold
providers = ["openai"]           # keys in <data_dir>/data/opencode/auth.json
# data_dir = ".secrets/accounts/keyed.opencode"   (the default: git-ignored)

[accounts.local]
kind = "opencode"                # opencode on a local OpenAI-compatible model
endpoint = "http://127.0.0.1:11434/v1"
endpoint_model = "qwen3-coder"
# endpoint_context = 65536       # the model's context window, in tokens (optional)
# endpoint_output = 8192         # its output bound (default: a quarter of the context)
```

| kind | credentials live in | keys |
|---|---|---|
| `claude-login` | its own `.credentials.json` in `config_dir` (default `data/accounts/<name>`), refreshed by the CLI | `config_dir` |
| `claude-token` | one line in `secret_file` (default `.secrets/accounts/<name>`) | `secret_file` |
| `anthropic-key` | one line in `secret_file` (default `.secrets/accounts/<name>`) | `secret_file` |
| `opencode` | opencode's own `auth.json` in `data_dir` (default `.secrets/accounts/<name>.opencode`), or none for a local endpoint | `data_dir`, and `providers` or `endpoint` plus `endpoint_model` (and optionally `endpoint_context`, `endpoint_output`) |

The rules, all checked when the file is read, and a broken entry is an error
that names the key, never a secret:

- A name matches `^[a-z0-9][a-z0-9_-]{0,31}$` and is not `host`.
- `kind` is one of the four above.
- `claude-login` takes `config_dir` and nothing else; the two secret kinds
  take `secret_file` and nothing else; `opencode` takes `data_dir`,
  `providers`, `endpoint`, `endpoint_model`, `endpoint_context` and
  `endpoint_output` and nothing else. An unknown key is refused.
- Every path is relative to the framework root. An absolute path, or one
  that leaves the root, is refused.
- An `opencode` account takes exactly one of `providers` or `endpoint`.
  `providers` is a non-empty list of distinct opencode provider ids (`openai`,
  `mistral`, ...). `opencode` names opencode's own hosted service (OpenCode
  Zen, its free models among them, as `opencode/<model>`); the runner keeps it
  disabled for every account that does not name it. Zen needs its own API
  key, and most of its free models let the vendor use the prompts for
  training: check the model's terms before a cousin runs on one. `endpoint` is an http(s) base URL with a
  host and no `user:password@`, and needs `endpoint_model`, the model id the
  endpoint serves; `endpoint_model` without `endpoint` is refused.
- `endpoint_context` (optional, with `endpoint` only) is the endpoint model's
  context window in tokens, a positive whole number; `endpoint_output` (only
  with `endpoint_context`) is its output bound, less than the context, default
  a quarter of the context capped at 32000 (opencode's own largest). The
  runner renders them as the model's `limit`: without it opencode reports no
  context limit for a local model, so neither the runner's context-pressure
  [rollover](glossary.md#rollover) nor opencode's own compaction happens (only the daily cadence
  applies). opencode compacts at the context minus the output, so an output
  near the context would compact every [turn](glossary.md#turn).
- An `endpoint` that names the Claude-subscription bridge (its package or
  proxy names, or port 3456, the bridge proxy's port) is refused: move a
  legitimate local proxy on 3456 to another port.

The secret files default to `.secrets/accounts/<name>` because the checkout's
`.gitignore` covers `.secrets/`: a secret is never visible to git and never
reaches a published tree. A secret file is read (by the runner when it
starts and connects, and by `cousin-account status`) as strictly as
`cousin-auth` reads its key file: the directory must be a 0700 directory of
yours, the file a regular 0600 file of yours, and a symlink is refused. A file
open to group or others refuses the start (exit 2, the message names the file
and the `chmod`). A missing secret file is not a configuration error but a
login to do: the runner starts, writes `data/login-required.json` (below) and
waits for the file to appear. Mint it with `cousin-account token <name>`, or
write the key.

The environment each kind gives the session. `cousin-runner` first removes
every variable that can pick the credentials or the provider from its own
environment, so nothing is inherited from the shell that started it:
`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`,
`CLAUDE_CODE_OAUTH_TOKEN`, `CLAUDE_CONFIG_DIR`, `AWS_BEARER_TOKEN_BEDROCK`,
`ANTHROPIC_FOUNDRY_API_KEY`, `ANTHROPIC_FOUNDRY_AUTH_TOKEN`,
`ANTHROPIC_AWS_API_KEY`, `CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_VERTEX`
and `CLAUDE_CODE_USE_FOUNDRY`.

| kind | sets | why |
|---|---|---|
| `claude-login` (named) | `CLAUDE_CONFIG_DIR=<root>/<config_dir>` | its own `.credentials.json`, its own refresh |
| `claude-login` (`host`) | nothing | the host's `~/.claude` |
| `claude-token` | `CLAUDE_CODE_OAUTH_TOKEN`, `CLAUDE_CONFIG_DIR=<root>/data/accounts/<name>` | the token and a config dir that holds no login (so a login cannot win over the token) |
| `anthropic-key` | `ANTHROPIC_API_KEY`, `CLAUDE_CONFIG_DIR=<root>/data/accounts/<name>` | the key and the same no-login dir (a CLI that finds a login and a key may bill the login) |
| `opencode` | `HOME=<data_dir>`, `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_CACHE_HOME`, `XDG_STATE_HOME` = `<data_dir>/config`, `/data`, `/cache`, `/state` | everything opencode writes (its config, its session database, its `auth.json`) stays in the account's data dir; no key is ever put in the environment |

A no-login directory that holds a login (a `.credentials.json` carrying one)
refuses the start: remove the file.

No subprocess scrub: a token or key account's variable reaches every command
the CLI starts, the cousin's own Bash included (`env` shows it). The CLI's
`CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1` would keep it out, but it also forces the
permission mode to `default` and requires the sandbox for every Bash call, so
a runner cousin could run no tool at all (1.18.2). Treat a key or token account
the way you treat the host login a cousin can already read: give it only to a
cousin you trust with it.

The `opencode` kind. The data dir and its four subdirectories are created
0700 when the account is first used (a looser mode is tightened; a symlink,
or a directory that is not yours, refuses the start). opencode keeps the
providers' keys in `<data_dir>/data/opencode/auth.json` (mode 0600, its
directory 0700). `cousin-account login <name> --provider <id>` puts one
there: an API key from stdin or `--key-file`, written directly (never
through chat), or an OAuth login with `--method <label>` through opencode's
own `auth login` (see [commands](commands.md)). That `auth.json` is held to the secret file rules: open to group or
others, a symlink or not yours refuses the start (exit 2, the message names
the file and the `chmod`); a missing one is a login to do. An Anthropic
OAuth login in it (a Claude subscription) refuses the start, and so does any
entry but an `api` key and another vendor's `oauth` login, and any entry
whose provider id says claude or anthropic, of any type (an Anthropic API
key included: ruling P9-1). Claude cousins
run on the Agent SDK and nowhere else (ruling P9-1): an opencode account that
names the `anthropic` provider, or an `endpoint_model`, `[agent] model` or
`small_model` whose id says claude or anthropic (in any case), is refused at
start (exit 2). `cousin-account status <name>`
reads presence only, with no process run: a `providers` account is logged
in when `auth.json` holds every provider it names, an `endpoint` account
by its configuration. The [lanes](glossary.md#lane) do not mix: an opencode cousin (`[agent]
runner = "opencode"`) runs on an `opencode` account only, never on `host`,
a `claude-login`, `claude-token` or `anthropic-key` account or an `[agent]
api_key_file`, and an `sdk` or `fake` cousin never runs on an `opencode`
account. Either mismatch refuses the start (exit 2).

One opencode account serves one cousin. Its data dir holds that cousin's
rendered config (with its MCP token), its policy files and opencode's own
session store, so the runner holds the account (a lock on the data dir) for
as long as it runs, and a second cousin on the same account is refused at
its start (exit 2, naming the cousin that holds it). Give each opencode
cousin its own account; two accounts may name the same provider keys.

Resume per kind: a `claude-login` account refreshes its own token, so a
restarted runner resumes its session through the CLI's own `--resume`; a
`claude-token` or `anthropic-key` account never refreshes, so it resumes from
the runner's session store.

`cousin-account list` shows every account (name, kind, where its credentials
live, never a secret); `cousin-account status <name>` says whether it is
logged in, with no model call; `cousin-account login|token <name> --via
<slug>` puts credentials into an account, and `cousin-account login <name>
--provider <id>` into an `opencode` one. See [commands](commands.md).

The login code capture: while `cousin-account login|token <name> --via <slug>`
waits for a code, it keeps `<root>/run/login-capture-<name>.json` (mode 0600
in a 0700 directory, outside every cousin home, so no home's backup, git or
chat history can hold a code; `.gitignore` covers `/run/`). The via cousin's
operator's next chat message is written there and taken by the flow within
half a second: one code, once. After that, and when the window closes without
a code, or the flow is killed, the file stays for an hour as a tombstone with
no code in it: every code-shaped message from that operator in that hour (a
second paste, a late code) is kept out of the chat and discarded, and any
other message passes as usual. A capture whose flow is gone is a tombstone
whatever its clock says, and a stored code never outlives its window.

Until the phase 6 container every cousin runs as your Unix user, so the model's own commands can read the account secrets, the account login directories and a pending login capture. The refusal inside a cousin and the policy's deny pattern are guardrails against a cousin RUNNING `cousin-account`, not a boundary around those files.

The refusal looks at the environment `cousin-account` runs in and at the
environment every ancestor process started with (best effort, where `/proc`
can be read). The terminal it wants is a usability check, not a safeguard: a
program can fake a terminal. The relay notice therefore ends with "If you did
not start this login from a host shell yourself, do not answer this."

### data/login-required.json

A runner whose account cannot answer (never logged in, a login that expired
or was revoked, a bad key or token, a missing secret file) or whose billing
stopped it does not exit and does not retry one turn at a time. It goes
`errored` with the detail `login_required` (or `billing`), puts the turn's
rows back in the queue, emits an `auth` event and writes
`<home>/data/login-required.json`:

| field | meaning |
|---|---|
| `host` | the host to log in on: `host_label` in harness.toml, else the hostname |
| `account` | the account's name (`host` for the host's own login) |
| `kind` | the account's kind |
| `reason` | `login_required` or `billing` |
| `detail` | what the CLI said, cut to 300 characters, never a secret |
| `since` | when this stop began (UTC); a new stop gets a new one |
| `action` | what to run: `cousin-account login <name> --via <slug>`, `cousin-account token <name> --via <slug>`, `cousin-account login <name> --provider <id>` for an `opencode` account (the key on stdin or with `--key-file`; `--method <label> --via <slug>` for an OAuth method), "write the key to <file>", `claude auth login` as the host user on the host, or for billing a check of the plan or credits |

`cousin-chat list` marks the cousin `LOGIN REQUIRED (account <name> on
<host>)` or `BILLING (account <name> on <host>)` while the file exists, and a
Telegram bridge on the home relays the action line once per `since`.

While it waits the runner spends no turn: every 1, 2, 4 ... 300 seconds it
looks at the account's credential file (its mtime and a hash, held in memory)
and at `claude auth status` (no model call), and reconnects only when the
credentials changed, the status went from logged out to logged in, or you
deleted the file. A revoked login still reads logged in, so for it only new
credentials (or deleting the file) move the runner. Deleting the file is the
manual retry, and the way out of a billing stop, where no credential changes.
A retry that fails for another reason (a secret file half written, a session
that is gone) puts that error into the file's `detail` and is tried again at
the next look; after three such failures in a row the runner gives up the
session on file and starts a fresh one, with the state digest.

A key or a token cannot refresh, so the first 401 of a turn ends it at once.
A `claude-login` account refreshes its own token at the CLI's next attempt, so
the runner lets the CLI finish; a refresh that fails still ends in a 401.

The file clears on the next good result after the retry, never at the
reconnect: a session's start cannot tell a live login from a revoked one.
When the stop came from the connect (a missing secret, a connect the login
refused) no row was put back, so the file stays until the next row the
cousin runs; `cousin-chat list` keeps saying LOGIN REQUIRED meanwhile, which
is accurate: nothing has proven the login yet.

## harness.toml

Where the agent harness keeps its own files, and a few things about how it
behaves. For Claude Code copy `harness.toml.claude-code.example`, which has
every value filled in. `harness.toml.example` is the same keys, commented,
for another harness.

Without the file, all of this is off: transcript mining at [flip](glossary.md#flip), the harness
memory collection in search, the console's token counts, the transcript-size
guard, `cousin-mcp approve`, the "needs attention" flag, the
`{model}`/`{effort}` defaults, and the `api_key` auth mode. A file that
doesn't parse is an error, not "off".

Top-level keys:

| key | default | meaning |
|---|---|---|
| `transcripts_dir` | none | where the harness writes session transcripts for a cousin. A path template, see below. Used by transcript mining at flip, the console's token counts and the size guard. |
| `auto_memory_dir` | none | the harness's own memory directory for a cousin. When set, it becomes the `harness` collection in `cousin-memory search`. |
| `mcp_logs_dir` | Claude Code's `~/.cache/claude-cli-nodejs/{home_encoded}/mcp-logs-{server}` | where the harness writes a log per session per MCP server. The boot packet and `cousin-mcp --last-connection` read it for the reason a cousin's MCP server failed, which the harness itself does not report. |
| `default_flip_at` | `04:00` | `"HH:MM"`, the daily flip time for every cousin that does not set its own. `"never"` makes no flip the default. One time for the whole fleet is fine: the daemon fires at most one flip per tick. |
| `flip_when_transcript_mb` | none (no guard) | a positive number of megabytes. When a live cousin's `<transcripts_dir>/<session_id>.jsonl` grows past it, the loops daemon asks for one flip, five minutes out. Needs `transcripts_dir`. |
| `settings_file` | none | the harness's settings JSON, the file that records project trust and approved MCP servers. `cousin-mcp approve` edits exactly this file. Without it, `approve` refuses and prints the edit to make by hand. |
| `attention_patterns` | `[]` | plain strings. When a running cousin's visible pane contains one, the pane is waiting on a person (a login or trust menu). The console shows "needs attention" and every delivery into that pane is skipped with a `tmux delivery SKIPPED` log line. |
| `host_label` | the hostname | a non-empty string: the host a login message names ("log in on <host>"), in `data/login-required.json`, `cousin-chat list` and the Telegram notice. Anything else is an error. |
| `busy_patterns` | `[]` | regular expressions. A match on the visible pane means the agent is mid-turn; `cousin-auth` and the console refuse to restart it unless forced. |

Path templates take `{home}` (the cousin home as is) and `{home_encoded}`
(Claude Code's project directory name for it: every `/` becomes `-`, so
`/a/b` is `-a-b`). A leading `~` is your home directory. If your checkout
path has characters other than letters, digits, `-` and `/`, compare with the
directory Claude Code actually made under `~/.claude/projects/` and write the
path out.

`[agent]`:

| key | default | meaning |
|---|---|---|
| `default_model` | none | what `{model}` renders to for a cousin without its own |
| `default_effort` | none | what `{effort}` renders to; one of `low`, `medium`, `high`, `xhigh`, `max` |
| `models` | a built-in list | the models the console's spawn dialog offers. Leave it unset to get the built-in list, which follows code updates. |
| `commit_attribution` | `true` | whether a commit or pull request a cousin makes carries the harness's own injected attribution (a Co-Authored-By trailer, a "Generated with Claude Code" line). A cousin's own `cousin.toml` `[agent] commit_attribution` overrides this. `true` keeps the harness's stock behaviour, since the framework is public and does not impose one operator's policy on every install; `false` turns it off for the SDK runner (`options.settings`, composed with anything else `options()` passes) and for the tmux lane (`includeCoAuthoredBy: false` and an empty `attribution` object written into `<home>/.claude/settings.json` by `apply_project_settings`, idempotently and without touching an operator's own keys in that file). The resolved value also rides the SDK runner's head `runner` [stream](glossary.md#stream) event, so it is observable without reading either toml file. |

`[agent.resume]`: how `agent-cmd` resumes a session instead of starting a new
one. Switching a running cousin's auth mode restarts it on the same session
through this. Without it, the switch refuses to restart.

| key | meaning |
|---|---|
| `session_arg` | the words in `agent-cmd` that start a session, e.g. `"--session-id {session_id}"`. Must appear in `agent-cmd` exactly. |
| `resume_arg` | what replaces them to resume, e.g. `"--resume {session_id}"` |

`[input_mode]`: for an agent with a vim-style input box. When the pane shows
`normal_marker`, the framework types `insert_keys` first so a chat line is
typed, not read as commands. Both keys are needed; without the table nothing
extra is typed.

`[auth.api_key]`: the `api_key` auth mode, where a cousin runs on an API key
from `<home>/.secrets/api-key.env` instead of the harness login (see
[cousins](cousins.md)). Without the table, `api_key` mode is refused.

| key | default | meaning |
|---|---|---|
| `key_env` | required | the environment variable the key is passed in (`ANTHROPIC_API_KEY` for Claude Code) |
| `config_dir_env` | required | the variable that points the harness at another config directory (`CLAUDE_CONFIG_DIR`) |
| `source_dir` | required | the harness's normal config directory (`~/.claude`) |
| `settings_file` | none | the harness's settings JSON (`~/.claude.json`) |
| `settings_name` | the file name of `settings_file` | the name of the copy inside the isolated directory |
| `isolated_dir` | `data/harness-api-key-config` | the isolated config directory; relative paths are under the root |
| `exclude` | `[]` | entries of `source_dir` not linked into the isolated directory |
| `strip_settings_keys` | `[]` | keys removed from the settings copy |
| `preserve_settings_keys` | `[]` | keys kept from the previous copy when it's rebuilt |
| `login_files` | `[]` | files that mean a login is present; the launch refuses |
| `login_file_keys` | `[]` | when set, a login file only counts as a login if its JSON has one of these keys |
| `login_settings_keys` | `[]` | keys in the settings copy that mean a login; the launch refuses |

In the default `claude` mode, both variables are removed from the agent's
environment.

## console-users.json

The console's logins. Never write it by hand:

```
cousin-console adduser ana     # asks for the password twice, at least 8 characters
```

The same command with an existing name resets that password. The file is
written atomically with mode 0600: `{"<user>": {"salt", "hash",
"iterations"}}`, PBKDF2-HMAC-SHA256 with a random salt per user and 200000
iterations.

- Missing: the console has no login. Anyone the network guard admits can use
  it, and the startup line says `(auth not configured: ...)`.
- Present with at least one user: every API route except login needs a
  session. There's no bypass for loopback or the LAN.
- Present but broken (bad JSON, empty, unreadable): the console closes.
  Every API route answers 503 naming the file and the startup line says
  `CLOSED`. `adduser` refuses to overwrite a broken file; restore it from a
  backup or delete it and add the users again.

Every user can do everything. There are no roles.

## net-allowlist.json

Who may connect to the console and the chat servers. By default: loopback
and the private ranges `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`. This
file only adds to that list:

```
{"allow": ["203.0.113.0/24"]}
```

Missing or unreadable: the defaults. A client outside the list gets 403.

## embedding.toml

The semantic leg of memory search. Read by `cousin-memory search`, the chat
server's proactive recall and the hive's recall. Missing: keyword search only,
quietly. Present but unreachable or without a `url`: keyword results plus a
notice saying so.

```
url = "http://localhost:11434/api/embeddings"
model = "nomic-embed-text"
timeout_s = 120
```

| key | default | meaning |
|---|---|---|
| `url` | required | the endpoint. It takes `{"model", "prompt"}` as JSON and returns `{"embedding": [...]}`; Ollama's `/api/embeddings` does. |
| `model` | `""` | sent as `model` |
| `timeout_s` | 10 | seconds per request. Set it above the time one chunk takes, or every search waits it out and falls back to keyword. 120 on a CPU-only box, 30 with a GPU. |
| `chunk_chars` | 2000 | long files are embedded in chunks this long |
| `chunk_overlap` | 200 | each chunk overlaps the previous one by this much |

`[recall]`, for proactive recall (the chat server searching memory for your
messages and adding a "possibly relevant" line):

| key | default | meaning |
|---|---|---|
| `min_chars` | 24 | shorter messages aren't searched |
| `min_score` | 0.45 | the similarity a hit needs to be mentioned |
| `top` | 3 | at most this many hits per message |

Keyword-only hits are never mentioned, unless the cousin sets
`[memory] recall_keyword_only = true`. A cousin opts out with
`[memory] proactive_recall = false`. More in [memory](memory.md).

## external-peers.toml

Cousins that live on another framework install, on this host or the LAN.
Read by `cousin-chat send` and `list`, and so by the MCP `send` tool.
Missing: only the cousins under `cousins/` are reachable.

```
[peers.kestrel]
url = "http://127.0.0.1:8085"
send_path = "/api/send"
```

| key | default | meaning |
|---|---|---|
| `url` | required | the peer's chat server, or its console, `http` or `https`, no credentials, query or fragment |
| `send_path` | `/api/send`, or `/peer/send` with a `token_file` | the route that takes the message; must start with `/` |
| `token_file` | none | outbound: the secret this install shares with that peer, in a file under the root (mode 0600). Each message to its console's `POST /peer/send` is signed with it; the secret itself is never sent |
| `sender` | none, required with `token_file` | outbound: the name that peer knows this install by (its own entry for us) |
| `inbound_token_file` | none | inbound: the secret shared with that peer, in a file under the root (mode 0600). This console's `POST /peer/send` checks that peer's signatures with it |
| `name` | the slug | how that peer's messages are shown here; never the operator's name or a local cousin's |
| `reach` | none, required for inbound | the local cousins that peer may write to; anything else answers as if it did not exist |

A peer reaches a cousin here through the console's `POST /peer/send`
(`docs/reference/console-api.md`): behind the network guard, with no
console login. Each message is signed (`Authorization: HMAC
<sender>:<hex>`, over the sender, the target, the send time, the id and
the message) with the secret the two installs share, so the secret never
crosses the network: a captured request can be neither altered nor sent
again after five minutes, and inside those five minutes its id is
refused. A peer may send 30 messages a minute. The entry is the identity,
one per install: every cousin there that sends to us is shown under the
same `name`, and `to` in their messages is our cousin's slug. An inbound
entry with no `reach`, an entry whose slug is a local cousin's, or a
secret file others can read is refused: the caller gets a plain `503`,
and the console's log says why (`chmod 600` it, add a `reach`). Keep a
private cousin out of every `reach`, and set `peer_visible = false` for it
too. Exchange a secret out of band, one per pair of installs, and rotate
it by replacing its file on both.

Every address the host resolves to must pass the network guard. A slug that
is also a local cousin is ignored (the local one wins, with a warning). A
malformed file is an error for every `cousin-chat` call. See
[chat](chat.md).

## hive.toml

Turns the console into the queen for cousins on other machines. Read by
`cousin-console` and `cousin-hive`. Missing, or `enabled = false`: no hive at
all, no `/hive/` routes, no remote cards, no build dialog. Present but wrong:
the same off state, plus a `hive off:` line in the console's journal naming
the problem.

| key | default | meaning |
|---|---|---|
| `enabled` | `false` | `true` or `false`, nothing else |
| `public_url` | required when enabled | the console as the nodes reach it, e.g. `http://192.0.2.10:8600`. Baked into every node archive the console builds. |
| `checkin_seconds` | 60 | how often a node checks in, at least 5. A node counts as online within 2.5 periods of its last checkin. |
| `home_cousin` | none | the local cousin a console-built node's `[tell-home: ...]` reaches, through the queen's authenticated `POST /hive/tell-home`. The build dialog's "home chat" switch needs it (or the legacy key below). |
| `home_chat_url` | none | legacy: a chat server that a node's `[tell-home: ...]` posts to directly, unauthenticated; used only when `home_cousin` is unset. |

The hive's database lives in `<root>/shared/hive/`. See
[remote cousins](remote-cousins.md).

## mcp-registry.toml

The default list of tools a cousin gets over MCP. `cousin-spawn` copies it
into every new home as `<home>/mcp-registry.toml`, with the `--operator` name filled
in. `cousin-mcp` reads the cousin's own copy, else `config/mcp-registry.toml`,
else the shipped `config/mcp-registry.toml.example`.

Don't edit the example. To change the default for your install, copy it to
`config/mcp-registry.toml`; to change one cousin, edit its home's copy.
Top-level keys: `ceiling` (default 12 enabled tools), `timeout` (120 seconds
per call), `max_output` (16000 characters). The `[tools.*]` shape is in
[MCP tools](mcp.md).

## media.toml

Media generation. Read by `cousin-image`, `cousin-voice` and `cousin-video`.
Missing, or a kind without a `url`: that kind is off, the command refuses
naming this file, and nothing leaves the box.

```
[image]
url = "http://192.0.2.20:8000/generate"
model = "some-model"
key_file = "config/media-key"
timeout_s = 120
```

One table per kind, `[image]`, `[voice]`, `[video]`:

| key | default | meaning |
|---|---|---|
| `url` | required | the provider endpoint |
| `model` | `""` | sent with the prompt |
| `key_file` | none | a file under the root holding a bearer token |
| `timeout_s` | 120 | seconds per request |

A request goes to the configured provider or fails; it's never rerouted. See
[media](media.md).

## shared-reviewers.json

Who may promote a proposed memory into the shared tier:

```
{"reviewers": ["ana"]}
```

Missing: promotion refuses and tells you to write this file. A reviewer can
never promote their own proposal, whatever the list says. See
[memory](memory.md).

## outbound-filter.json

Words that must not leave in a cousin's chat messages or media captions.
The framework ships no list of its own; without this file nothing is
filtered.

```
{
  "terms": ["project-x"],
  "protected": ["kestrel"],
  "trusted_peers": ["wren"],
  "surfaces": {"media": {"add": ["draft"]}}
}
```

| key | meaning |
|---|---|
| `terms` | blocked everywhere, matched as whole words, any case |
| `protected` | cousin slugs whose names are also blocked. Messages to a protected cousin aren't filtered, and a protected cousin's own messages pass when they go to you or to a trusted peer. |
| `trusted_peers` | slugs a protected cousin may talk to freely |
| `surfaces` | extra terms per surface, `chat` or `media`, under `add` |

A blocked `cousin-reply` or `cousin-chat send` sends nothing and exits 3,
naming the word. `COUSIN_FILTER_OVERRIDE=1` in the environment switches the
filter off for that command.

## law.md

Markdown that every cousin gets at the top of its boot packet, as the
"Framework Law" section. It's never cut for length. Missing: no law section.
That's an install choice, so it doesn't mark a boot as degraded. See
[lifecycle](reference/lifecycle.md).

## worker-cmd

One line, for worker cousins (`[cousin] type = "worker"`): cousins with no
tmux session that only run loops. When a worker's loop is due, the loops
daemon runs this command as a tracked job. `{prompt}` becomes the loop's
prompt and `{home}` the cousin's home.

```
/opt/worker/run-worker --home {home} {prompt}
```

Missing: the loop stays due and the daemon logs that this file is needed.
Nothing fires.

## Telegram

The Telegram bridge has no file in `config/` of its own. It's configured per
cousin in `cousin.toml` (below), and its bot token lives in a file under the
root that `[telegram] token_file` names, by convention
`config/telegram/<slug>.token` (mode 600). Keep it under `config/` so git
ignores it. Setup steps are in [telegram](telegram.md).

## Environment variables

| variable | meaning |
|---|---|
| `FRAMEWORK_ROOT` | the root, see above. The units set it. |
| `COUSIN_HOME` | the cousin a command acts for. Set in every cousin's tmux session; set it yourself to use `cousin-memory`, `cousin-job` and friends from a plain shell. |
| `COUSIN_SLUG` | set by the framework for session hooks, chat hooks and the MCP server |
| `COUSIN_TMUX_SOCKET` | a non-default tmux socket, read by the chat server and the watchdog. The console takes `--tmux-socket` instead. |
| `COUSIN_FILTER_OVERRIDE` | `1` switches the outbound filter off for one command |
| `COUSIN_SUPERVISED` | `1` in every process `cousin-supervisor` starts (with `PYTHONUNBUFFERED=1` and `FRAMEWORK_ROOT`); set by the [supervisor](glossary.md#supervisor), not by you. It is inherited by whatever those children launch in turn: on a bare host that includes the chat servers and tmux sessions the supervised console starts, so a tmux cousin started from that console sees it too. Only the console's restart route reads it (to report `supervised`) |
| `COUSIN_DEFAULT_RUNNER` | `sdk`, `fake` or `opencode`: the lane a new cousin gets when `cousin-spawn --runner` (or the console's `runner`) is not given, written to its `[agent] runner`. Unset or empty: the tmux lane, and nothing is written. Any other value is refused before anything is created |
| `COUSIN_DEFAULT_ACCOUNT` | the `[agent] account` a new runner cousin gets when `--account` is not given: `host` or one of `config/accounts.toml`'s (an unknown name is refused before anything is created). Ignored for a tmux cousin |
| `COUSIN_OPENCODE_BIN` | the `opencode` binary an opencode cousin's runner starts when its `[agent] opencode_bin` is not set. The image's `opencode` target sets it to its pinned binary, `/opt/opencode/bin/opencode` (also on its `PATH`); unset (the default image, a bare host): `opencode` on `PATH` |
| `COUSIN_POLICY_FILE` | set by an opencode cousin's runner for its `opencode serve`, not by you: the rendered policy file the plugin pack reads (`<data_dir>/cousin-policy.json`) |

## cousin.toml

Each cousin's own settings, at `cousins/<slug>/cousin.toml`. `cousin-spawn`
writes the first version; the console edits some keys in place. A cousin
exists when this file exists: there's no other registry.

What spawn writes:

```
[cousin]
slug = "wren"
name = "Wren"
role = "helps me around the house"

[chat]
port = 8090
tmux_session = "wren"

[operator]
name = "ana"
```

With `--runner` (or `COUSIN_DEFAULT_RUNNER`) it also writes `[agent] runner`,
and `account` when one is given (`--account` or `COUSIN_DEFAULT_ACCOUNT`):

```
[agent]
runner = "sdk"
account = "metered"
```

`[cousin]`:

| key | default | meaning |
|---|---|---|
| `slug` | required | lowercase, 2 to 32 characters, starts with a letter |
| `name` | the slug, capitalised | display name |
| `role` | `""` | one line; the console shows and edits it |
| `type` | `"cousin"` | `"worker"` for a cousin with no session that only runs loops through `worker-cmd` |
| `peer_visible` | `true` | `false` takes it out of other cousins' peer list, and it sees no peers either |
| `hidden` | `false` | hides the card in the console unless "show hidden" is on |

`[chat]`:

| key | default | meaning |
|---|---|---|
| `port` | picked by spawn, from 8090 up | the chat server's port. Required for chat. |
| `host` | `127.0.0.1` | the address the chat server binds and others reach it on |
| `tmux_session` | the slug | the tmux session the agent runs in and chat is typed into |

`[operator]`:

| key | default | meaning |
|---|---|---|
| `name` | none | the person this cousin works for. None is a valid answer: no operator. |

`[runtime]`:

| key | default | meaning |
|---|---|---|
| `model` | `[agent] default_model` | what `{model}` renders to |
| `effort` | `[agent] default_effort` | `low`, `medium`, `high`, `xhigh` or `max` |
| `session_id` | written on each start | the current session, from `{session_id}` |
| `auth` | `"claude"` | `"claude"` (the harness login) or `"api_key"`. Change it with `cousin-auth`. |

`[heartbeat]`:

| key | default | meaning |
|---|---|---|
| `context_beat_seconds` | 3600 | how often the loops daemon sends the cousin a heartbeat. The console accepts 60 seconds to 30 days. |

`[memory]`:

| key | default | meaning |
|---|---|---|
| `scope` | `"private"` | `private` or `shared`: whether the cousin may propose memories to the shared tier. Every cousin reads the shared tier and keeps its private memory either way. The retired `both` is read as `shared` |
| `proactive_recall` | `true` | `false` stops the chat server adding recall lines to your messages |
| `recall_keyword_only` | `false` | `true` lets keyword-only hits into recall lines when there's no embedding service |
| `review_batch` | `3` | when more than this many entries on authored topics were written since the review gate last looked (on the runner lane, after every turn), they are held for review, out of the memory views until kept (`cousin-memory review`) |
| `review_model` | the cousin's own model | the model the runner's review gate asks to keep or drop held entries (SDK lane) |

`[lifecycle]`:

| key | default | meaning |
|---|---|---|
| `flip_at` | the install's `default_flip_at` (`04:00`) | `"HH:MM"`. The loops daemon flips this cousin once a day at or after that time. `"never"` opts this cousin out. Leave it out and the install default applies, so a new cousin flips without being configured. `cousin-loops flips` prints the effective time and where it came from. |

`[[loops]]`, one table per loop:

| key | default | meaning |
|---|---|---|
| `name` | required | lowercase, starts with a letter, up to 32 characters, unique per cousin |
| `interval_seconds` | | every N seconds |
| `daily_at` | | `"HH:MM"` once a day |
| `cron` | | five-field cron |
| `days` | every day | with `daily_at`: three-letter weekdays, e.g. `["mon", "thu"]` |
| `prompt` | required | the text typed into the cousin when it fires |
| `enabled` | `true` | |
| `hidden` | `false` | hides it in the console's loops view |

Exactly one of `interval_seconds`, `daily_at` and `cron`. See
[jobs and loops](jobs-and-loops.md).

`[session]`:

| key | default | meaning |
|---|---|---|
| `start_hooks` | `[]` | commands `cousin-session start` runs, in order: each a string or `{name, cmd}` |
| `end_hooks` | `[]` | the same for `cousin-session end` |

`[telegram]`:

| key | default | meaning |
|---|---|---|
| `enabled` | off | must be `true` for the bridge to run |
| `token_file` | required | the bot token file, relative to the root |
| `operators` | required | `[{user_id = 123456, name = "ana"}]`. Only these Telegram users are served. |

The bridge refuses to start when any of these is missing. See [telegram](telegram.md).

### [agent] runner

`runner = "sdk"` puts the cousin on `cousin-runner`
(docs/design/agent-loop-runner.md) instead of a tmux session. Chat, schedules,
loops and meetings all reach it the same way now: each producer hands its item
to the delivery facade and reads back `delivery.accepted(outcome, home)` (a
durable [inbox](glossary.md#inbox) put is acceptance, `delivered` or a runner's `queued` row, never
a bare `failed`; a producer hands a runner cousin its item without waiting,
since the put is the acceptance, and waits on a tmux cousin's typed line as
before) and, where it needs to know whether the cousin is up,
`delivery.is_alive(home)` (a runner cousin's answer is whether a runner holds
its lock; a tmux cousin's is unchanged, tmux `has-session` or the chat port).
This is no longer experimental for those four producers. The console serves a
runner cousin's chat from its `chat.db` and shows its reasoning stream, with an
interrupt and a say box ([console](console.md)); `cousin-watch` shows the same
stream in a terminal. The runner starts its
session with no settings files and `bypassPermissions`; `policy.toml`
(`deny_tools`, `deny_bash_patterns`, `ask`, `outbound_filter`) is what stands
in for a settings file's deny rules, read once at start and enforced by a
`PreToolUse` hook, and a malformed one is a config error, exit 2, naming the
key. `runner = "fake"` is for tests. `runner = "tmux"` (phase 11) runs the
host's interactive Claude Code in a tmux pane on the framework's own socket,
fed by the same inbox; it runs on `host` or a named `claude-login` account only
(`claude-token` and `anthropic-key` accounts are refused, exit 2), and the pane
starts from a fixed allowlist of the runner's environment (`HOME`, `PATH`,
`USER`, `LOGNAME`, `SHELL`, `SSH_AUTH_SOCK`, `XDG_RUNTIME_DIR`,
`DBUS_SESSION_BUS_ADDRESS`, `LANG`, `LOCALE_ARCHIVE`, `TZ`, `COLORTERM`,
`TMPDIR`, every `LC_*`) plus the names `[agent] env_allow` lists (a list of
variable names; a `CLAUDE*` or `ANTHROPIC*` name is refused, since those never
reach the pane). The in-pane launcher also sets `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`,
`DISABLE_AUTOUPDATER=1` and `COUSIN_PANE_PID` (its own pid, which the exec
chain makes the CLI's too, so the pane hook and a crash recovery know which
process is this cousin's), and refuses the CLI's print-mode flags (`-p`,
`--print`, `--output-format`, `--input-format`, `--strict-mcp-config`): the
tmux kind's contract is an interactive pane, never a one-shot call. Absent: the tmux path, unchanged
(and `cousin-runner` refuses the cousin, exit 2, unless `--runner` is given; a
cousin.toml that does not parse is also the tmux path, and `cousin-runner`
refuses it the same way).

With a runner, delivery puts a row in the cousin's inbox (`data/inbox.db`) and
pokes it. Without waiting it reports `queued`. Waiting, it reports `delivered`
when the runner closes the row inside the wait, `failed` when the row's turn
failed or the row could not be stored, and `queued` when the wait ran out (the
row is kept; do not send it again). `delivered` means the model RECEIVED the
item, not that it answered it: the rows of an interrupted turn are delivered.

`model` names the model the runner asks for, and `effort` its effort (`low`,
`medium`, `high`, `xhigh` or `max`; anything else is exit 2 at start). A runner
reads only `[agent]`: `[runtime]` (model, effort, auth) is the legacy tmux lane's, and
`cousin-migrate` carries it over, validated (see [migrating](migrating.md)).
`account` names the account the
cousin runs on, one of `config/accounts.toml`'s (see
[accounts.toml](#accountstoml)); with none, the cousin runs on `host`, the
host's default login. The account is the only source of credentials:
`cousin-runner` removes every auth and provider variable (the list is under
[accounts.toml](#accountstoml)) from its own environment before the session
starts, so nothing is inherited from the shell, and checks the account before it takes the cousin's lock (an
unknown account, or a secret file that is open to others or malformed, is
exit 2; a missing secret file is let through as a login to do, see
[accounts.toml](#accountstoml)). The terms risk of running a cousin on a login is the
user's.

`cousin-spawn --runner sdk|fake|opencode|tmux [--account <name>]` (or the console's spawn
with `runner` and `account`) writes both keys when the cousin is created, and
`COUSIN_DEFAULT_RUNNER` / `COUSIN_DEFAULT_ACCOUNT` supply them when the flags
are left out (see [Environment variables](#environment-variables)); an
account without a runner is refused.

`auto_start` (default `true`) says whether `cousin-supervisor` starts this
runner cousin by itself when it starts or rescans (see
[commands](commands.md)). `auto_start = false` leaves it down until
`cousin-supervisor start <slug>`; only a literal `false` opts out. A tmux
cousin is never the supervisor's, whatever this says. A runtime stop (the
console's, `cousin-supervisor stop <slug>`) also keeps a runner cousin down
across a supervisor or container restart: it writes `<home>/run/held` (the
time and who asked), which the supervisor honours at every start and rescan,
and the next start removes it. `auto_start` is the lasting opt-out, the
marker the operator's last word.

`api_key_file` is deprecated: an implicit `anthropic-key` account named after
the cousin, whose secret file is this path (relative to the framework root),
read as strictly as any account secret (0600, in a 0700 directory). Move the
key to an account. `account` and `api_key_file` together is refused.

The runner waits at most 10 minutes for the next message of a turn (a stream
gone silent fails the turn) and puts no limit on a whole turn. These are
constructor defaults of the SDK runner (`idle_timeout_s`, `turn_timeout_s`),
not cousin.toml keys in this phase.

`commit_attribution` overrides `config/harness.toml [agent]
commit_attribution` for this cousin alone (tracker #112, see the table
above): `false` turns off the CLI's own injected attribution on this
cousin's commits and PRs, whatever the install default says; `true` or
absent falls back to the install default (itself `true` when unset).
The resolved value composes into the SDK runner's `options()` (and
`validate_account`'s), and into the tmux lane's
`<home>/.claude/settings.json` (`apply_project_settings`), and rides
the runner's head `runner` stream event.

`rollover_at_percent` (number, default 80) applies to the `sdk` and `opencode`
lanes only; the `tmux` kind has no automatic context-pressure rollover (only
an explicit flip, the daily cadence, or `flip_when_transcript_mb` moves it).
It is the context percentage at which
the runner rolls the cousin over at its next idle: it asks the model for its
handoff through the `handoff` tool, runs the `[session]` end hooks, starts a
new session on the same system prompt, then the start hooks, and sends the
state digest as the new session's first message. When the CLI reports its own
autocompact threshold, the runner also rolls over once the context is within
10,000 tokens of it, and an imminent compaction (the `PreCompact` hook) asks
for a rollover too. After a rollover, pressure waits until the percentage
drops 10 points below the threshold or 5 turns pass, whichever comes first.
One pending rollover per cousin: a second request joins the first, except a
long or multi-line reason (a bequest), which is never merged away. A handoff
the model does not write within 5 minutes becomes an emergency handoff built
from the session transcript and marked `degraded_state: true`; the generation
still ends.

The runner records `usage` on every `result` event in its stream (the SDK's
own `ResultMessage.usage` dict: at least `input_tokens`, `output_tokens`,
`cache_creation_input_tokens` and `cache_read_input_tokens`; `None` when the
result carried none, as a failed turn's synthetic result does), so cache
behaviour (a second turn of the same session reading the prompt cache rather
than rebuilding it) is visible per turn, not only in aggregate.

A runner cousin's home also holds its own state, none of it hand-edited:
`data/sessions.db` is the transcript (the SDK's `SessionStore` protocol over
SQLite). Who owns it goes by the account's kind: an `anthropic-key` or
`claude-token` account resumes through the store, which the framework owns; a
`claude-login` account resumes through the CLI's own `--resume`, and the store
only mirrors its transcript. There is no retention on it yet. `data/usage.db` is the per-turn usage and cost table
`usage.record` writes. `data/runner-session.json` is the session id (and
lane) to resume at the next start. `data/extract-cursor.json` is continuous
extraction's per-session cursor into the transcript. `data/generations/` is
one directory per past generation (`gen-0001`, ...), each a copy of
`STATUS.md`, `data/handoff.md` and `data/active-threads.md` as they stood at
that rollover.

A few other files in a cousin's home are configuration too:
`mcp-registry.toml` (its MCP tools), `.mcp.json` (its other MCP servers,
below), `chat-hooks.json` (patterns the chat
server reacts to, see [chat](chat.md)), `policy.toml` (below) and
`.secrets/api-key.env` (the key for `api_key` mode, written by `cousin-auth`).

### [agent.sessions]

Side sessions: a [thread](glossary.md#thread) kind that gets a session of its own, beside the
primary. Every kind not named stays in the primary session, which is also
the default with no table at all.

```toml
[agent.sessions]
peer = "own"        # peer chat is answered in a session of its own
person = "own"
meeting = "primary"
```

Keys are thread kinds (`person`, `peer`, `meeting`, `loop`, `schedule`);
values are `"primary"` or `"own"`. `operator` and `system` cannot be `"own"`:
the generation's work arrives on operator threads, and the system thread
carries the rollover, the state digest and the memory proposals. An unknown
kind, another value, or either of those two as `"own"` is a configuration
error: `cousin-runner` exits 2 naming it. Only `runner = "sdk"` has side
sessions; `runner = "fake"` or `runner = "opencode"` with a kind mapped to
`"own"` is refused the same way (exit 2, `side sessions need runner =
"sdk"`), before the runner or its server starts.

A kind mapped to `"own"` gets one side session for all its threads, in the
same `cousin-runner` process: the same system prompt, tools and working
directory as the primary (so the cached prompt prefix is shared), the same
memory, and its own context. It answers while the primary is busy: a peer
is not kept waiting behind a long operator task. The `handoff` tool is
refused in a side session; it never proposes memories and never runs the
`[session]` hooks; the rest (leaving STATUS.md and the generation to the
primary) is the side digest's instruction, not a guard. Its first turn
carries that digest as context: which session it is, the primary's state
and the kinds of threads it is on, the cousin's last activity note, then the
state digest, whose layers every session shares. It never carries a row's
words, a sender or a thread key from the primary's live turn. A side
session's `activity` note is written as `[<kind> session] ...`. It starts
over, with a fresh digest, at the context pressure `rollover_at_percent`
sets and whenever the primary moves to a new generation. Its session id is
kept in `data/runner-session-<kind>.json` and its event stream in
`data/stream/sdk-<kind>-<id>.jsonl`, headed by a `side_session` event, never
by the `runner` event that heads the primary's stream. A side session that
fails (its CLI does not start, a reconnect fails) is restarted inside the
same `cousin-runner` after a backoff; the primary and its running turn are
never stopped for it. A side session is not interruptible from the console
in this phase: an interrupt reaches the primary's live turn only. Each side
session is one more agent CLI process: measured at 300 to 480 MB of
resident memory per interactive CLI on the reference host.

### [agent] on the opencode lane

`runner = "opencode"` puts the cousin on `cousin-runner` with opencode as its
agent loop (`OpencodeRunner`, phase 9) instead of the Claude Agent SDK: the
same inbox, event stream, tools, policy and memory. It runs on a
`kind = "opencode"` account (see [accounts.toml](#accountstoml)) and these
`[agent]` keys. How the kinds compare, the per-runner contract table and
what this lane does not do yet are in [runners](reference/runners.md).

| key | default | meaning |
|---|---|---|
| `model` | required | `"<provider>/<model>"`, on a provider the account reaches: one of its `providers`, or `local/<endpoint_model>` for an `endpoint` account. No default: without it the runner refuses to start (exit 2), because opencode would otherwise pick a model from whatever provider it can reach |
| `small_model` | `model` | the model opencode uses for its own small calls (session titles); the same rules |
| `opencode_bin` | `COUSIN_OPENCODE_BIN`, else `opencode` on `PATH` | the `opencode` binary |
| `opencode_models_fetch` | `true` | `false` sets `OPENCODE_DISABLE_MODELS_FETCH=1`: opencode then does not fetch the models.dev catalog at start (an outbound request that carries no credential) |
| `shell_env` | `[]` | variable names the model's shell gets from the runner's own environment (for example `["SSH_AUTH_SOCK"]`), beside `USER` and `LOGNAME`. Not `HOME`, `XDG_*`, `OPENCODE_*`, a known credential, or a name shaped like one (`*SECRET*`, `*_PASSWORD`, `*_KEY`, `*_TOKEN`): exit 2 |

The runner starts one `opencode serve` on `127.0.0.1` with a fresh password,
in the cousin's home, with `HOME` and the four XDG directories in the
account's data dir. Its environment is an allowlist: `PATH`, `LANG`,
`LANGUAGE`, `LC_*`, `TZ`, `SSL_CERT_FILE`, `SSL_CERT_DIR`, the cousin's
`COUSIN_HOME` and `FRAMEWORK_ROOT`, `COUSIN_POLICY_FILE` (the plugin's policy
file), and the runner's own switches; nothing
else (no `OPENCODE_*` of the shell, no `*_API_KEY`) reaches it. The config
it reads is rendered at every start into `<data_dir>/opencode.runner.json`
(mode 0600): opencode's own hosted provider
disabled (unless the account names `opencode`), the model and small model above, `permission` allow-all (the
cousin's `policy.toml` is the policy), the plugin pack, and one MCP server,
the runner's own on loopback behind a per-start token, through which the
model reaches the framework's tools as `cousin_<tool>`.

opencode merges other config sources over that file. Measured on 1.18.31,
with the project config switched off: it merges `opencode.json` from its
global config dir, `<data_dir>/config/opencode/`, and from
`<data_dir>/.opencode/` (the account's `HOME`), and it loads plugins and
custom tools from the `plugin(s)/` and `tool(s)/` dirs there; it does not
read `.opencode/` or `opencode.json` in the cousin's home. So a runner
refuses to start (exit 2 when found at start-up, exit 3 at a restart) while
any of these exist: anything in `<data_dir>/config/opencode/` other than
opencode's own `.gitignore` and the plugin library below (`node_modules` and
a `package-lock.json` whose root names that library only), `<data_dir>/.opencode`,
or `<home>/.opencode`. The message names each path: remove it. After the
server starts, the runner reads the config opencode actually runs with
(`GET /config`) and runs no turn unless it holds exactly the plugin pack,
exactly the one MCP server, no provider beyond the account's, the models
`cousin.toml` names, and nothing the bridge guard refuses. It makes all
of these checks again (and reads `auth.json` again) before every turn,
because opencode can take a new plugin or provider while it runs
(`PATCH /global/config`, measured on 1.18.31): at the first mismatch the
row goes back to the queue and the runner gives up (exit 3), so its restart
reports the cause. A system-wide
managed config opencode may also read is outside the cousin's reach and is
checked the same way, through that effective config.

The model's shell does not get the server's environment as it is. opencode
merges the plugin pack's `shell.env` answer over it, so the runner has the
plugin set `HOME` to the cousin's home (not the account's data dir, where
`auth.json` and the rendered config live), set the four XDG variables,
`OPENCODE_SERVER_PASSWORD` and `OPENCODE_CONFIG` empty, and pass `USER`,
`LOGNAME` and the names `[agent] shell_env` lists (from the runner's own
environment, where set; `SSH_AUTH_SOCK` for work over ssh). Measured on
1.18.31 with a real `bash` call.

The server dies with its runner: it is started with a death signal
(`PR_SET_PDEATHSIG`, SIGKILL), so a runner killed without its teardown (the
supervisor's escalation, the OOM killer, a crash) takes it along. What the model runs
does not die with it by itself: opencode 1.18.31 starts the model's shell in
a session of its own, outside the server's process group, and ends it on a
normal stop but not when it is killed (measured). So every server start
carries a random marker in its environment (`COUSIN_OPENCODE_START`), which
everything it starts inherits; a stop kills the server's group and then
every process of the user carrying that marker. The runner also writes
`<data_dir>/opencode.pid` (the pid, its process group, its start time, the
boot and the marker) and, at its next start, kills what a runner killed
without its teardown left behind: the server's group when that pid is still
`opencode serve` with the same start time in the same boot, then everything
carrying its marker, the model's detached commands included. A recycled pid
or a file from another boot kills nothing; the system stream says
`opencode_leftover` with the pids when it killed any. So everything the
model starts from its shell dies at every stop or restart of its runner, a
service it detached and another cousin's runner it launched included (a
flip is not a stop: the rollover opens a new session on the same server, so
they keep running through it): start anything meant to outlive a turn outside the cousin (the
exceptions, a process that drops the marker, are in
[runners](reference/runners.md#known-gaps)). With any plugin
configured, opencode would first install its plugin library from npm into
its config dir and load no plugin until that ends (a network fetch, and a
start that hangs offline); the pack imports nothing, so the runner marks
the library present (`<data_dir>/config/opencode/node_modules` and
`package-lock.json`, an existing lock merged) and opencode installs
nothing. A config or an
environment that names the Claude-subscription bridge is refused before
anything starts. The runner checks that opencode reports that MCP server
connected before its first turn; when it does not, no turn runs (the runner
gives up, exit 3). The composed system prompt goes with every prompt;
opencode appends it to its own agent prompt.

The cousin's `policy.toml` is enforced by the plugin pack,
`plugins/opencode/cousin-policy.js` (see
[policy.toml on the opencode lane](#policytoml-on-the-opencode-lane)): the
runner renders the policy into `<data_dir>/cousin-policy.json` (mode 0600)
at every start and refuses to run turns (exit 3) until opencode's config
lists the plugin and the plugin has acknowledged this start's file
(`<data_dir>/cousin-policy.ack.json`). The listing alone proves nothing:
opencode lists a configured plugin even when it failed to load. The runner
records from opencode's event stream what the SDK lane's hooks record: each
tool call, with its arguments, as an activity line, a `task` call (opencode's
subagent tool) as a `subagent` job, a session checkpoint at the end of every
turn, and on `session.compacted` the pre-compact checkpoint and a rollover
at the next turn boundary. opencode sends `session.compacted` once the
compaction is done, so that checkpoint is taken after it, where the SDK's
`PreCompact` hook fires before: it records the state the compaction left, not
the transcript it summarised. A subagent's own tool calls (in its child
session) are vetoed by the plugin but not recorded.

A restart resumes the session
recorded in `data/runner-session.json` (lane `opencode`) while opencode
still holds it; otherwise a new session starts, with the state digest as its
first message when there is state to carry.

A rollover is the SDK lane's: the handoff is asked for in the old session
(the model calls `cousin_handoff`), then a new opencode session starts,
with the state digest as its first message; the old session stays in
opencode's store. Context pressure is the last answer's tokens against the
model's context limit as opencode reports it (`GET /config/providers`),
against `rollover_at_percent`; a model opencode reports no limit for has no
pressure rollover (the stream says so once) and only the daily cadence
applies. A local endpoint model has a limit only when its account names
`endpoint_context`. `cousin-spawn --runner opencode` writes no model: add `model` (and
the account) before the first start, or the runner refuses it.

### .mcp.json (the runner's MCP servers)

A runner cousin gets its own tools from the in-process `cousin` server, and
the claude.ai connectors from its login. Any other MCP server (a local
stdio server, an HTTP one such as Home Assistant's) goes in `<home>/.mcp.json`,
in the format Claude Code uses:

```json
{
  "mcpServers": {
    "notes": {"command": "/opt/notes-mcp", "args": ["--root", "/srv/notes"],
              "env": {"NOTES_TOKEN": "${NOTES_TOKEN}"}},
    "ha": {"type": "http", "url": "${HA_URL:-http://ha.lan:8123}/api/mcp",
           "headers": {"Authorization": "Bearer ${HA_TOKEN}"}}
  }
}
```

A stdio server takes `command`, `args` and `env` (`"type": "stdio"` may be
left out); an `http` or `sse` server takes `url` and `headers`. A key the
SDK's server config does not have (such as `headersHelper`) is dropped, and
the event below names it. The runner starts its session with no settings
files, so the CLI never reads this file itself: the runner reads it and
passes each server beside `cousin`, ordered by name, so the same file gives
the same tool list at every start.

- **`cousin` is reserved.** An entry named `cousin` is skipped, never
  started beside the runner's own server. That is the entry `cousin-spawn`
  writes for the tmux lane's `cousin-mcp`, so a migrated cousin's file is
  left as it is.
- **`${VAR}` and `${VAR:-default}`** work as in Claude Code, in `command`,
  `args`, `env` values, `url` and `headers` values. The runner passes them
  through unexpanded and the agent CLI expands them from its own
  environment, which is the runner's (the supervisor's or `cousin-runner`'s,
  not your shell's). This is on purpose: the SDK hands the servers to the CLI
  as a `--mcp-config` command-line argument, which the host's users can
  read, so only the `${NAME}` is on the command line and the value reaches
  the server through the CLI's environment. Keep the secret in the runner's
  environment and only its `${NAME}` in the file; a value written literally
  in the file is on the command line too. A variable that is unset and has
  no default skips that server, and the event names the variable. A
  reference to an account variable (`ANTHROPIC_API_KEY`,
  `CLAUDE_CODE_OAUTH_TOKEN` and the rest of the list under
  [accounts.toml](#accountstoml)) skips its server even with a default: the
  CLI's environment holds the cousin's own credential under those names,
  and a server is never handed it.
- **Never fatal.** A file that does not parse, or an entry that is not one of
  the three shapes (an unknown `type`, a missing `command` or `url`, a
  non-string value), is skipped; the cousin still starts with `cousin`.
- **One event.** At the first connect the stream gets an `mcp_config` event
  with each loaded server's name and type and each skipped entry's name and
  reason; no event when there is no file.
- **Deferred, not always loaded.** The `cousin` tools are always in the
  prompt; a user server's tools are deferred behind the CLI's tool search,
  so a large or changing tool list costs no prompt bytes (and moves no
  cached prefix) until the model looks one up. The contract says only that
  such servers may be present and that their tools are named
  `mcp__<server>__<tool>`.
- **policy.toml applies.** Their tools pass the same `PreToolUse` hook as
  any other: `deny_tools = ["mcp__ha__*"]` denies a whole server,
  `"mcp__ha__call_service"` one tool, and `ask` works the same way.
  `deny_bash_patterns` looks at a user server's tool too, when its input
  carries a `command` string.
- **When a change lands.** The file is read once per runner; a reconnect or
  a rollover offers the same set. Restart the runner (or wait for its next
  start) to pick up a change.

### policy.toml

`<home>/policy.toml`, read once when the runner starts and enforced by a
`PreToolUse` hook with no matcher (the CLI never consults `can_use_tool` under
`bypassPermissions`, so this is where a runner cousin's deny rules live
instead of a settings file's). The hook fires for every tool call of the
session, including the calls a subagent makes inside its own turn (the hook
input then carries `agent_id`), so a deny reaches a subagent too. Four keys,
all optional:

| key | default | meaning |
|---|---|---|
| `deny_tools` | `[]` | tool names the model may never call; exact name or a `prefix*`. To deny subagents, list both `Task` and `Agent` (the tool's older and newer names) |
| `deny_bash_patterns` | `[]` | regexes checked against the `command` string of any tool whose input carries one (`Bash`, `PowerShell`, `Monitor`, any other); never against a command a tool builds on the far side (an MCP server that shells out). The cousin's own `mcp__cousin__*` tools are skipped: their `command` is a verb such as `add` or `pass`, not a command line |
| `ask` | `[]` | tools that need operator approval; there is no approval surface yet, so they are enforced as `deny`, and the reason says so |
| `outbound_filter` | `true` | whether `reply` and `send` cross `config/outbound-filter.json` |

Two failure modes: the file absent means every tool is allowed, said once in
the event stream at start (`no policy.toml: every tool allowed`); the file
present but malformed (bad TOML, an unknown key, a non-list value, an
uncompilable regex) stops the runner at start, exit 2, naming the key. See
`templates/policy.toml.example` for a documented starting point.

What it is and is not:

- The patterns are a guardrail, not a sandbox. They stop a command line you
  can name; the same effect can be spelled another way (a script, another
  interpreter, an editor tool). When the risk is the tool, deny the tool.
- The file lives in the home it governs, so the model can rewrite it. A
  rewrite takes effect at the next start, never mid-session. An operator who
  needs it immutable makes it read-only to the cousin's user, or owns it.
- The CLI runs every `PreToolUse` callback that matches a call concurrently,
  and a deny from any of them wins; the order of the callbacks sequences
  nothing. The recorder (a subagent's or a background shell's job row) checks
  the policy itself and records nothing for a call the policy denies or asks
  about.
- Beside policy.toml, one rule is built in: a subagent's `reply` that names no
  `thread` is denied ("a subagent must name the thread it answers"), because
  the turn's implicit thread belongs to the turn the subagent runs in, not to
  the subagent.

#### policy.toml on the opencode lane

On `runner = "opencode"` the same file is enforced by the plugin pack
(`plugins/opencode/cousin-policy.js`, loaded by the config the runner
renders), in opencode's `tool.execute.before`: a denied call becomes a tool
error the model reads (`denied by policy: <reason>`) and the turn goes on,
as on the SDK lane. The decisions are `Policy.decide`'s, with these lane
differences:

- Tool names are written the SDK lane's way on both lanes. The plugin maps
  opencode's names before matching: `bash` `Bash`, `read` `Read`, `write`
  `Write`, `edit` `Edit`, `glob` `Glob`, `grep` `Grep`, `task` `Agent` (the
  subagent tool), `webfetch` `WebFetch`, `websearch` `WebSearch`,
  `todowrite` `TodoWrite`, `skill` `Skill`, and the framework's
  `cousin_<tool>` `mcp__cousin__<tool>`. A tool only opencode has keeps its
  own name (`apply_patch` edits files: deny it by that name if `Edit` and
  `Write` are denied; a [known gap](reference/runners.md#known-gaps)).
- `deny_bash_patterns` run as JavaScript regular expressions (with the `u`
  flag). A pattern Python accepts and JavaScript does not (`(?P<name>...)`,
  `(?i)`, `\A`, `\Z`) makes the plugin deny every call that carries a
  command; the runner says which pattern at start. Write patterns in the
  common subset.
- The policy file is read once, when opencode loads the plugin: a rewrite of
  `policy.toml` applies at the next start, as on the SDK lane.
- opencode's own permission config is allow-all and any interactive ask it
  still raises is rejected at once (an `error` event), so a turn never waits.
- The subagent `reply` rule above is not enforced on this lane.

