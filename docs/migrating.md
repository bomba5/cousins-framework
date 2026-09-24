# Migrating a cousin

How to move a cousin you already have into an install: its home, its memory,
its chat history, and, if you ran cousins on other machines under an older
framework, the hive's tokens and memory. It assumes the target install is
done and working ([install](install.md)).

There are two cases, and they go differently (moving a cousin already
here from the tmux lane to the SDK runner is its own section,
[below](#from-the-tmux-lane-to-the-sdk-runner)):

- **The cousin already runs on this framework**, on another machine or in
  another root. The home has the right shape. You copy it across.
- **The cousin comes from an older framework** whose home looks different.
  You spawn a fresh cousin here and carry the memory and chat into it.

Either way, nothing on the source side is touched until the very end. Keep
the old home around until you're happy with the new one.

## Before you start

Stop the cousin on the source, after it has written its handoff (a flip or
its session-end routine does that). Both halves: the tmux session and the
chat server.

```
tmux kill-session -t wren
kill "$(cat <old home>/data/chat-server.pid)"
```

Then make an archive of the whole old home, and of its Claude Code
transcript directory (`~/.claude/projects/<encoded old home>/`). After that
the source can go away without losing anything:

```
mkdir -p ~/migration && chmod 700 ~/migration
tar -czf ~/migration/wren-home.tar.gz -C <old home>/.. "$(basename <old home>)"
tar -czf ~/migration/wren-transcripts.tar.gz -C ~/.claude/projects <encoded old home>
```

The archives can hold keys and private memory. Keep them private.

## From another install of this framework

Copy the home into `cousins/<slug>` under the new root, with the cousin
stopped on both sides:

```
rsync -a <old home>/ "$FRAMEWORK_ROOT"/cousins/wren/
```

That carries everything: `cousin.toml`, `CLAUDE.md`, memory, notes, the chat
store (`data/chat.db`) with its pictures under `chat/`, loops, hooks and
the `api_key` secret if there is one.

Then fix what points at the old place:

```
cousin-spawn wren --repair-settings     # rewrite .claude/settings.json and .mcp.json with the new paths
cousin-mcp approve wren                 # trust the new path in ~/.claude.json
```

- **The chat port.** Check `[chat] port` in `cousin.toml` against the other
  cousins here (`grep -h '^port' cousins/*/cousin.toml`) and anything else
  listening on the machine. Change it if it collides.
- **Claude Code's own memory.** Claude Code keeps a memory directory per
  project, named after the project path, and the path just changed. If
  `config/harness.toml` sets `auto_memory_dir`, copy the old directory to
  the new name so the cousin keeps it:

  ```
  mkdir -p ~/.claude/projects/<encoded new home>
  cp -a ~/.claude/projects/<encoded old home>/memory ~/.claude/projects/<encoded new home>/
  ```

  The encoding is the path with every `/` turned into `-`, so
  `/srv/fw/cousins/wren` becomes `-srv-fw-cousins-wren`.

What doesn't come with the home is the old root's own state: job history,
pending one-shot schedules, tracker items, loop requests (all in the old
root's `data/`), and the shared memory tier (`shared/`). Re-add what still
matters with `cousin-tracker add` and `cousin-schedule add`. Shared memory
that was promoted there can be proposed again from the cousin's own memory;
see [memory](memory.md).

Now go to [checking it](#checking-it).

## From an older framework

Spawn the cousin fresh, with the same slug, name and role, so it gets a
`cousin.toml`, `CLAUDE.md`, MCP registry and settings in this framework's
shape:

```
cousin-spawn wren --name Wren --role "helps me around the house" \
    --voice "Short, plain and honest." --operator ana \
    --model claude-opus-5 --effort high
cousin-mcp approve wren
```

Don't copy the old `cousin.toml` over it. Keys this framework doesn't read
would sit there looking like they do something. Instead, carry over by hand
what you had: `[[loops]]`, `[session]` hooks, `[telegram]`, `[lifecycle]
flip_at`, the heartbeat (`--heartbeat` or `[heartbeat]
context_beat_seconds`). Every key is in [configuration](configuration.md#cousintoml).

### Memory

Copy these from the old home into the new one, where they exist. They have
the same shape here:

| from the old home | what it is |
|---|---|
| `MEMORY.md`, `STATUS.md` | the memory index and the open loops |
| `memory/raw/` | the raw memory, one JSONL file per day |
| `memory/distilled/` | rebuilt from raw on the first boot anyway, but it keeps curated text above its marker |
| `memory/capsules.jsonl`, `memory/callbacks.md` | reasoning capsules and callbacks |
| `notes/` | notes |
| `data/decisions.jsonl` | the decisions log |
| `data/corrections.jsonl` | corrections |
| `data/active-threads.md`, `data/handoff.md` | what was in flight when it stopped |
| `self-portrait.md` | the self-portrait |
| `scripts/` and other working folders | whatever the cousin kept |

Skip `memory/embeddings.json` and `memory/fts_index.db`. They're search
indexes, and the first `cousin-memory search` rebuilds them (with
`config/embedding.toml` set, the first search after a big copy is slow).

Carry Claude Code's per-project memory across too, the same way as in the
section above.

### CLAUDE.md

Keep the one spawn wrote: it has the framework's chat and tool instructions.
Append the cousin's own sections from the old file (its voice, what it must
never do, the loops it runs, the household rules). If the old file has a
table of commands, drop the rows for commands this framework doesn't have;
`data/tool-surface.md` lists the ones it does.

### Chat history

With the new chat server stopped:

```
cousin-chat-import wren --old-home <old home>
```

`--old-home` is the old home or an unpacked archive of it; it needs
`data/chat.db` and `chat/`. The command refuses while the cousin's chat
server answers. What it does:

- Every old message keeps its id, so replies still quote the right message.
- Messages already in the new store (say, a hello you sent to test it) move
  to ids after the last old one, with their replies, reactions and pictures.
- Old pictures are copied to `chat/inbound/<id>.<ext>`, where the console
  looks for them. A picture whose file is gone is counted and the message
  kept.
- Old audio and video links are kept as text on the message.
- The new store is backed up first as `data/chat.db.pre-import-<stamp>`, and a
  marker, `data/.chat-imported.json`, holds the report.

It prints a report:

```
{
  "imported": 566,
  "renumbered": 4,
  "images": 157,
  "images_missing": 2,
  "media_links_kept": 11,
  ...
}
```

It refuses a second run. `--force` skips that check, but a forced second run
treats the first import as "already in the store" and adds the history
again after it. To redo an import, restore `data/chat.db.pre-import-<stamp>`
over `data/chat.db` and delete the marker first.

This is for the older framework's chat store. Between two installs of this
framework, `data/chat.db` and `chat/` travel with the home copy and need no
import.

### Tracker items

Items from an old tracker don't carry over. Re-add the live ones with
`cousin-tracker add`.

## Hive data from an older framework

If you had cousins on other machines under an older framework's queen, the
queen's tokens and memory can come across, so a deployed node only needs its
queen URL changed. On the machine where this install's console runs:

```
cousin-hive import-legacy --tokens <old queen>/tokens.json \
    --memory-dir <old queen store> --shared-slugs kestrel
#   tokens: 2 added, 0 already present
#   memory: 812 added, 0 already present, 0 unusable
```

- `--tokens` is the old queen's `tokens.json` (token to `{slug, scope}`).
  Each token is imported with the same string. Scopes map to `own` and
  `shared`; a missing scope means both, and any other word is dropped and
  reported.
- `--memory-dir` is the old store directory with `<slug>/memory.jsonl` in
  it. Each record becomes a memory row for that slug with its original time
  and kind, in `own` scope.
- `--shared-slugs` lists the slugs whose memory was the old shared corpus;
  theirs goes in as `shared`.
- The old embeddings aren't kept (different model). Rows are embedded again
  as they're recalled, when `config/embedding.toml` is set.
- It's safe to run twice: rows already imported are skipped.

A token string that already belongs to another slug here is not imported,
and the output names it. The hive database lives in `<root>/shared/hive/`,
so `FRAMEWORK_ROOT` has to point at this install. Turn the hive on in
`config/hive.toml` if it isn't, then point each node at the new queen URL.
See [remote cousins](remote-cousins.md).

## Accounts for runner cousins

Existing cousins keep working with no change: a cousin that names no account runs on `host`, the host's default login in `~/.claude`, shared by every such cousin, exactly as before; a cousin with `api_key_file` runs on an implicit key account. One change is not silent: an `api_key_file` is now read as strictly as `cousin-auth`'s key file, so a key file at mode 0644, or in a directory open to group or others, now REFUSES to start (exit 2, the message names the file and the `chmod`): `chmod 600` the file and `chmod 700` its directory. Move a cousin to its own login with an `[accounts.<name>]` entry, the login command, and `account = "<name>"` in its `cousin.toml`; a token or key account keeps its secret in `.secrets/accounts/<name>` unless `secret_file` says otherwise.

Moving a cousin from `host` to a named login account moves where the CLI
keeps its local transcripts (they live under the account's config dir), so
the first resume after the move is `resume_failed`, then a fresh session
with the digest: expected, and it costs a conversation, never state. In a
phase 6 container, `host` (the host's `~/.claude`) lives outside the volume;
a containerised cousin runs on a named account whose config dir is inside
it.

See [configuration](configuration.md) for the accounts file.

## From the tmux lane to the SDK runner

A cousin on this framework runs on the tmux lane until you move it: an
upgrade, a merge or a restart never changes the lane its `cousin.toml` names.
`cousin-migrate` moves one cousin at a time, and every step it takes is
recorded and can be undone. The fleet goes in this order:

1. **A test cousin.** Spawn a throwaway one on the tmux lane
   (`cousin-spawn testa --role "migration test"`), talk to it, then migrate
   it. Nothing is at stake, so this is where a surprise should happen.
2. **One low-stakes cousin.** One whose work can wait a day.
3. **Observe two days.** Leave it on the runner, use it as usual, and run
   `cousin-migrate check` on it each day. Move on only when both days are
   clean.
4. **The rest, one at a time.** One migration, one `check` the next day,
   then the next cousin. Never two in flight.
5. **The engineer cousin last.** The one that works on the framework itself
   goes after the rest have run a week, so the cousin you would call to fix
   a migration is still on the lane you know.

### One cousin

Before you start: after the upgrade that brings `cousin-migrate`, reinstall
the framework into the install's venv (`pip install -e '.[sdk]'` from the
checkout) so the new command is on PATH and the SDK is installed. A
`cousin-supervisor` runs for the install (it starts the runner), the
cousin's account is logged in (`cousin-account status <account>`), and the
cousin is running on the tmux lane: migrating a stopped cousin would start
it, so start it first or leave it for later. The plan checks all of that:

```
cousin-migrate plan wren --account team --validate
#   ok  lane       on the tmux lane
#   ok  running    its tmux session is up
#   ok  record     no migration in progress
#   ok  carry
#         carry   [runtime] model 'claude-opus-5' -> [agent] model
#         carry   [runtime] effort 'high' -> [agent] effort
#         none    [runtime] auth 'claude': --account team
#   ok  account    account=team kind=claude-login loggedIn=True method=claude.ai
#   ok  cli        the runner's CLI: Claude Code 2.1.277 (bundled with claude-agent-sdk 0.2.157)
#   ok  validate   validate: ok (one model turn answered) (model claude-opus-5, effort high, account team); a model the runner's CLI can't run is never written
#   ok  supervisor a cousin-supervisor answers for /srv/fw
#   ok  sdk        claude-agent-sdk is installed
#   ok  import     3 auto-memory file(s) to fold in
#   ok  mcp        the runner loads from .mcp.json: ha; skips cousin (reserved: the runner serves its own `cousin` tools in-process)
#   note The working conversation does not carry: the new session starts from the state digest, the handoff and memory, and is handed the previous transcript path
#   note data/handoff.md is 3.2h old now; the close asks for a new one and waits for it
# steps: close -> handover -> import -> toml -> start -> verify
# wren: ready (run: cousin-migrate apply wren --validate --yes)
```

`plan` writes nothing. Leave out `--account` and the cousin runs on the
host's own login, unless its `[runtime] auth` is `"api_key"`.

**The working conversation does not carry.** The runner starts a new
session: it knows what the state digest, the handoff and memory tell it,
and nothing else of the conversation it was having on the tmux lane. That
conversation stays on disk as Claude Code's transcript of the last tmux
session (`<transcripts_dir>/<session id>.jsonl`, the directory
`config/harness.toml` names), and the new session is told where: its first
message ends with the path, and asks it to have a subagent read the file
from the end and rebuild its active work into STATUS.md, the handoff and
memory. Tell the cousin what matters before you migrate it, and expect to
remind it of the rest.

The runner reads only `[agent]`, so the `carry` lines say what of the tmux
lane's `[runtime]` moves there: `model` and `effort` (or, when `[runtime]`
sets none and `config/agent-cmd` renders the placeholder, the
`config/harness.toml [agent]` default the tmux lane ran on), and for
`auth = "api_key"` an `anthropic-key` account named `<slug>-key`, made at
`apply` from the cousin's own `.secrets/api-key.env` (the key copied to
`.secrets/accounts/<slug>-key`, mode 0600; `config/accounts.toml` gains its
table). A key already in `[agent]`, or `--account`, wins and is listed as
`kept`. An api_key cousin whose key file is missing or malformed makes the
plan say `NO carry`: it never falls back to the host login.

A model the runner's CLI can't run is never written. The runner runs the
CLI bundled with `claude-agent-sdk` (the `cli` line names its version), and
a model newer than that CLI fails every turn with an API 400. So when a
model is carried, `plan` and `apply` say `NO validate` unless you pass
`--validate`: one smallest model turn, on a throwaway client, with the
model, effort and account the runner will run, and the API's own words when
it fails. `apply --validate` runs it again itself, before anything changes.
`cousin-migrate check wren --validate` does the same for a cousin already
on the runner. An effort carried alone is validated the same way, on the
CLI's default model. The turn never uses a key, token or config dir from
your shell: the account is its only credential. For a key account,
`--validate` makes `data/accounts/<slug>-key`, the account's own config
dir with no login in it (no secret is written there; the runner uses the
same dir).

The cousin's `.mcp.json` stays where it is. The runner reads it at its
start and loads every server in it beside its own tools, except the
`cousin` entry spawn wrote for the tmux lane's `cousin-mcp`, which the
runner serves in-process instead. The `mcp` line lists the servers it will
load, by name only, and what it will skip and why (see
[configuration](configuration.md#mcpjson-the-runners-mcp-servers)). It is
never a blocker.

A key file already at `.secrets/accounts/<slug>-key` is used as it is when
it holds the cousin's key, and never removed by a rollback; with another
key there, the plan says `NO carry`. When the plan says ready, and at a moment the cousin is
between tasks:

```
cousin-migrate apply wren --account team --validate --yes
```

It saves the current `cousin.toml` (its exact bytes and mode) in
`data/migration.json`, then:

| step | what happens |
|---|---|
| `close` | a clean stop of the tmux session: the cousin writes its handoff, the transcript is mined, the generation moves on. The close waits (up to 5 minutes) for a handoff written during it, else writes an emergency one; `apply` warns, with the file's age, when the handoff it leaves was not written during the close, and when it is the emergency one |
| `handover` | the path of the last tmux session's transcript, and of the newest other transcript in the same directory, is written to `data/previous-transcript.json` with the time the session ended. A transcript it cannot find is recorded as missing, with why; it never fails the migration |
| `import` | Claude Code's own memory for the cousin is folded into `memory/imported/auto/` (`cousin-memory import-auto --apply`), with a recall baseline first |
| `toml` | `[agent] runner = "sdk"`, `account` when you named one, and what the plan's `carry` lines listed (the key account is made first); nothing else in the file changes. Refused if the tmux session came back meanwhile (a scheduled flip, a console start) |
| `start` | the migration day's boot packet is set aside (the runner starts on its own digest), the supervisor starts the cousin's runner, and the cousin's chat server is started: the supervisor runs none, and other cousins' messages reach the inbox through it |
| `verify` | the runner stays up and holds its lock for 10 seconds, and the chat server answers `/health` for the cousin |

It stops at the first step that fails and says so. At the end it prints
the recorded transcript path(s) (`previous transcript (last): ...`), or why
there are none. The runner's first session starts fresh, on a digest of the
cousin's state, with a closing paragraph that names those paths: the
conversation from before the move, read-only, to be read from the end by a
subagent that extracts only the user and assistant text, never read whole
into the session. That start renames the record to
`data/previous-transcript.json.consumed`, so a later rollover does not
repeat it.

Then check it, the same day and each day after:

1. It answers a chat message, and `cousin-watch wren` shows the turn.
2. Another cousin's message reaches it: `cousin-chat send wren "ping"`
   from a peer (or your shell) lands in its inbox and gets an answer.
3. `cousin-memory import-auto --verify` (with `COUSIN_HOME` set) finds no
   recall that got worse.
4. `cousin-migrate check wren` says `ok`: since the migration, no inbox
   row open for more than an hour, every tool call has a recorded result,
   no recorder hook failed, the runner's model, effort and account agree
   with the cousin's `[runtime]` (a `MISMATCH` line says where not: a
   console model or effort change still writes `[runtime]`), and the chat
   server answers. The supervisor
   runs no chat server: `cousin-chat-watchdog` (its timer) brings a runner
   cousin's back after a reboot or a crash, so keep that timer on.

### Rolling back

```
cousin-migrate rollback wren --yes
```

It undoes the steps `apply` got through, and only those. It stops the
runner and waits until it has let go of its lock, puts the saved
`cousin.toml` back byte for byte, removes what it made of the key account,
even when `apply` stopped half-way through making it: exactly the bytes it
appended to `config/accounts.toml` (the rest of the file comes back byte
for byte), and the secret copy unless another account there still points
at it. When another cousin names the account, all of it is kept and the
step says who. It removes `data/previous-transcript.json` (and the
`.consumed` one) and what the runner kept of its session
(`data/runner-session.json`, a side session's `runner-session-<kind>.json`,
the restart mark `data/runner-restart.json`), so a later migration starts a
fresh session with the handover rather than resuming the old one, has the
supervisor rescan, writes a fresh
boot packet from the cousin's state now, starts the tmux session (unless it
is already up) and releases the supervisor's hold on the runner. If
`apply` failed before `toml`, it changes nothing but the record. A step
that fails is written into `data/migration.json` and reported; fix what it
names and run `rollback` again. It
refuses a second rollback, inbox rows that still wait (nothing on the tmux
lane reads the inbox: let them finish, or pass `--force`, and they stay in
`data/inbox.db`) and an inbox it cannot read. What the migration imported
stays in `memory/imported/auto/`; it does no harm on the tmux lane.
Entries the review gate still holds have no reviewer on the tmux lane:
settle them with `cousin-memory review`. After a rollback, `plan` and
`apply` work again.

By hand, if `cousin-migrate` itself is the problem: `cousin-supervisor stop
wren` and wait until `cousin-supervisor status` shows it `stopped`, put the
old file back (its bytes are `prior_toml_b64` in `data/migration.json`, or
delete the `runner` line from the `[agent]` table: no `runner` is the tmux
lane), delete
`data/pending-boot.json` if one is there (the tmux session would boot on an
old packet otherwise), then `cousin-spawn wren --start`.

## Checking it

Start it, then go down this list:

```
cousin-spawn wren --start
```

1. The chat server is this cousin's:
   `curl -s http://127.0.0.1:<port>/health` answers with `"slug": "wren"`.
2. Memory is found: `COUSIN_HOME=$FRAMEWORK_ROOT/cousins/wren cousin-memory
   search "<something from its notes>"` finds it. With embeddings on, the
   first search rebuilds the index and takes a while.
3. The chat history is there: open the cousin in the console and scroll up.
   Old pictures show.
4. A message from the console reaches the pane, and nothing in
   `data/chat-server.log` says `tmux delivery SKIPPED` or `FAILED`.
5. `cousin-flip wren --dry-run` runs every stage and reports no degraded
   sections you didn't expect.
6. One real flip, `cousin-flip wren --confirm`. With `--confirm` the new
   session posts a line in chat once it's oriented; it should know who it is
   and what it was doing.
7. Its loops show in the console's loops view with sensible next-fire times.

Only then retire the old one. I keep the old home and the archives for a week
or two before deleting anything.

## Remove the subscription bridge from a live install

Some installs put a Claude-subscription bridge beside a hand-installed
opencode: the `opencode-with-claude` plugin and the `@rynfar/meridian` proxy
it starts, which let opencode reach Claude on a Claude login through a local
port (3456). The framework ships nothing of it, and the opencode runner
refuses every sign of it: an opencode cousin runs on a provider key or a
local model. What an install already has stays until you delete it. This is
how, run by the operator, never by the framework.

The commands assume the install keeps its opencode under
`$ROOT/local/opencode`: a `package.json` naming `opencode-ai` and
`opencode-with-claude`, its `package-lock.json`, `node_modules/`, and a
`vendor/` directory with the four tarballs. Stop every cousin that runs
opencode first.

```
ROOT=/path/to/the/framework/root
BRIDGE='opencode-with-claude|meridian|rynfar|claude-max-proxy|CLAUDE_PROXY_(PORT|HOST)|MERIDIAN_|x-meridian|127\.0\.0\.1:3456'
tar -czf "$HOME/opencode-before-bridge-removal.tgz" -C "$ROOT/local" \
    opencode/package.json opencode/package-lock.json opencode/vendor
```

**The packages.**

```
cd "$ROOT/local/opencode"
npm uninstall --offline --ignore-scripts --no-audit --no-fund opencode-with-claude
rm -f vendor/opencode-with-claude-*.tgz vendor/rynfar-meridian-*.tgz \
      vendor/rynfar-meridian-plugin-opencode-scrub-*.tgz
find node_modules -mindepth 1 -maxdepth 1 -type d -empty -delete
npm pkg set description="Pinned opencode."
```

`npm uninstall` takes the plugin out of `package.json` and
`package-lock.json` and prunes it, and everything only it needed (the proxy,
its scrub plugin, the Agent SDK and Claude Code it pulled in), from
`node_modules`; it works offline from the lock file (on a 1.18.31 install it
removed 111 packages). `--ignore-scripts` keeps opencode's own postinstall
from running again. `find` removes the scope directories npm leaves empty.
`vendor/opencode-ai-*.tgz` stays: that is opencode itself. The last line
replaces a `description` that named the bridge.

**Cousin configs.** A cousin that ran opencode by hand may have an
`opencode.json` with `"plugin": ["opencode-with-claude"]` and an `anthropic`
provider whose `baseURL` is `http://127.0.0.1:3456`, and a `cousin.toml`
comment or `NODE_PATH` that points opencode at the bridge in
`local/opencode/node_modules`. List them:

```
grep -rlIiE "$BRIDGE" "$ROOT"/cousins/*/opencode.json "$ROOT"/cousins/*/.opencode \
    "$ROOT"/cousins/*/cousin.toml "$ROOT/config" 2>/dev/null
```

In each `opencode.json` it lists, delete the `plugin` entry and the
`anthropic` provider block, or the whole file for a cousin that moves to the
opencode runner (the runner renders its own config and never reads a
cousin's `opencode.json`). In a `cousin.toml`, delete the bridge's lines.

**The environment and the proxy.**

```
env | grep -E '^(CLAUDE_PROXY_|MERIDIAN_)'
systemctl --user show-environment | grep -E '^(CLAUDE_PROXY_|MERIDIAN_)'
ss -ltnp | grep ':3456 '
```

Remove whichever variables print from where they are set (a shell profile, a
unit's `Environment=`), stop the process listening on 3456, and delete the
proxy's own settings and profiles, `~/.config/meridian`. Claude Code's own
login is not the bridge's; it stays unless you want it gone too.

**The check.** Each of these prints nothing:

```
grep -rlIiE "$BRIDGE" "$ROOT/local/opencode" "$ROOT/config" \
    "$ROOT"/cousins/*/opencode.json "$ROOT"/cousins/*/.opencode \
    "$ROOT"/cousins/*/cousin.toml 2>/dev/null
ls "$ROOT/local/opencode/vendor" | grep -v '^opencode-ai-'
(cd "$ROOT/local/opencode" && npm ls --all 2>/dev/null) \
    | grep -iE 'meridian|rynfar|opencode-with-claude|@anthropic-ai'
```

and opencode still runs: `"$ROOT/local/opencode"/node_modules/.bin/opencode
--version` prints its version. Keep the `-I`: opencode's own binary contains
the word "meridian" in unrelated code, and `-I` skips binary files.

**What it breaks.** Anything that reached Claude through the bridge. A cousin
whose agent command runs opencode with such an `opencode.json` fails at start
(the plugin no longer resolves) or on its first turn (nothing listens on
3456). It was on a Claude subscription through third-party code; to keep it
on opencode, move it to the opencode runner with an opencode account (a
provider key or a local model, see [configuration](configuration.md)), or
back to Claude Code. The framework loses nothing: no framework code, config,
image or doc outside the design notes names the bridge, and
`tests/test_no_bridge.py` keeps it that way.
