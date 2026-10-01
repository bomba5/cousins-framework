# Migrating a cousin

How to move a [cousin](glossary.md#cousin) you already have into an install: its home, its memory,
its chat history, and, if you ran cousins on other machines under an older
framework, the hive's tokens and memory. It assumes the target install is
done and working ([install](install.md)).

There are two cases, and they go differently (moving a cousin already
here with no [runner](glossary.md#runner) kind onto one is its own section,
[below](#a-cousin-with-no-runner)):

- **The cousin already runs on this framework**, on another machine or in
  another root. The home has the right shape. You copy it across.
- **The cousin comes from an older framework** whose home looks different.
  You spawn a fresh cousin here and carry the memory and chat into it.

Either way, nothing on the source side is touched until the very end. Keep
the old home around until you're happy with the new one.

## Before you start

Stop the cousin on the source, after it has written its handoff (a [flip](glossary.md#flip) or
its session-end routine does that). On a 2.x source, that is the console's
stop button or:

```
cousin-supervisor stop wren
```

A source from before 2.0.0 (1.x, or the older framework) ran a cousin in two
halves, a tmux session and a per-cousin chat server. Stop both:

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
store (`data/chat.db`) with its pictures under `chat/`, loops and hooks.
The account a cousin runs on is the install's, not the home's: make sure
`config/accounts.toml` here has the account its `[agent] account` names,
logged in.

Then fix what points at the old place:

```
cousin-spawn wren --repair-settings     # rewrite .claude/settings.json and .mcp.json with the new paths
cousin-mcp approve wren                 # trust the new path in ~/.claude.json
```

- **A home from 1.x.** Its `cousin.toml` can still carry keys 2.0.0 no
  longer reads (`[chat] port` among them; no chat server runs, so a port
  collides with nothing) and may have no `[agent] runner`. See
  [a cousin with no runner](#a-cousin-with-no-runner) and
  [removed keys](#removed-keys-after-the-upgrade-to-200).
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

With the new cousin stopped (`cousin-supervisor stop wren`):

```
cousin-chat-import wren --old-home <old home>
```

`--old-home` is the old home or an unpacked archive of it; it needs
`data/chat.db` and `chat/`. The command refuses while the cousin's runner
is running (exit 2), since the import rewrites its store. What it does:

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

Existing cousins keep working with no change: a cousin that names no account runs on `host`, the host's default login in `~/.claude`, shared by every such cousin, exactly as before; a cousin with `api_key_file` runs on an implicit key account. One change is not silent: an `api_key_file` is now read as strictly as an account's secret file, so a key file at mode 0644, or in a directory open to group or others, now REFUSES to start (exit 2, the message names the file and the `chmod`): `chmod 600` the file and `chmod 700` its directory. Move a cousin to its own login with an `[accounts.<name>]` entry, the login command, and `account = "<name>"` in its `cousin.toml`; a token or key account keeps its secret in `.secrets/accounts/<name>` unless `secret_file` says otherwise.

Moving a cousin from `host` to a named login account moves where the CLI
keeps its local transcripts (they live under the account's config dir), so
the first resume after the move is `resume_failed`, then a fresh session
with the digest: expected, and it costs a conversation, never state. In the
container, the host's `~/.claude` is not visible: `host` there is the
image user's own `~/.claude` (under `HOME=/data/home`, on the volume), empty
until `docker compose exec framework cousin-account login host`; a named
account, whose config dir is on the volume too, is the usual choice.

See [configuration](configuration.md) for the accounts file.

## A cousin with no runner

2.0.0 has no legacy tmux [lane](glossary.md#lane): every cousin names a runner kind in
`cousin.toml [agent] runner` (`sdk`, `tmux`, `opencode` or `fake`). A cousin
without one is refused by name everywhere it would run, start, receive a
message or flip, with one line:

```
wren has no [agent] runner: 2.0.0 has no legacy tmux lane. Move it on the
last 1.x release with cousin-migrate apply wren, or convert it by hand
(docs/migrating.md, "A cousin with no runner")
```

A [worker](glossary.md#worker) (`[cousin] type = "worker"`) is not refused this way: it has no
session to deliver to, and its line says so.

**The easy way** is to move every cousin before the upgrade, on the last 1.x
release: `cousin-migrate apply <slug> --validate --yes`, one at a time, then
upgrade. That path closes the session cleanly, carries `[runtime] model` and
`effort` into `[agent]`, [folds](glossary.md#fold) the auto-memory in and hands the new session
the old transcript's path.

**By hand**, on 2.0.0:

1. Stop the old pane if it still runs: `tmux kill-session -t <slug>` on the
   socket it used.
2. Fold its auto-memory in: `cousin-memory import-auto <slug>`.
3. In `cousin.toml`, add an `[agent]` table with `runner = "sdk"` (or another
   kind) and `account = "<name>"` from `config/accounts.toml`, and move
   `model` and `effort` from `[runtime]` into it.
4. `cousin-supervisor reload`: the [supervisor](glossary.md#supervisor) starts it. The new session
   starts from the state digest, the handoff and memory; the old
   conversation does not carry.
5. `cousin-migrate tidy <slug> --yes` removes the keys 2.0.0 no longer reads
   ([below](#removed-keys-after-the-upgrade-to-200)).

## Checking it

Start it, then go down this list:

```
cousin-spawn wren --start
```

1. `cousin-supervisor status` lists `runner:wren` as `running`, and neither
   its `refused` nor its `config` block names wren.
2. Memory is found: `COUSIN_HOME=$FRAMEWORK_ROOT/cousins/wren cousin-memory
   search "<something from its notes>"` finds it. With embeddings on, the
   first search rebuilds the index and takes a while.
3. The chat history is there: open the cousin in the console and scroll up.
   Old pictures show.
4. A message from the console gets an answer, and the reasoning pane shows
   the [turn](glossary.md#turn).
5. Its loops show in the console's loops view with sensible next-fire times.

Only then retire the old one. I keep the old home and the archives for a week
or two before deleting anything.

## Switching between runner kinds

`cousin-migrate --to sdk|tmux` switches a cousin between the `sdk` kind and
the `tmux` kind, live and reversibly, keeping the same session where both
kinds can resume it. `plan`, `apply` and `rollback` need `--to` in 2.0.0; a
cousin with no runner is refused ([above](#a-cousin-with-no-runner)).

```
cousin-migrate plan sam --to sdk
#   sam: tmux -> sdk, steps close, toml, start, verify
#     ok  kind       tmux -> sdk
#     ok  account    team (claude-login)
#     ok  session    session <id> continues
#     ok  supervisor the supervisor runs
#   ready
cousin-migrate apply sam --to sdk --yes
#     ok  close      the tmux runner stopped at idle; session <id> kept
#     ok  toml       [agent] runner = 'sdk', the kind's settings removed
#     ok  cursor     the mining cursor at the end of the sdk kind's record
#     ok  notice     inbox row <n>, the first turn after the switch
#     ok  start      the sdk runner resumes <id>
#     ok  verify     the SDK's session_init names <id>
#   switched
```

`plan` checks, and writes nothing: `kind` (the cousin is on the runner
lane, in the other kind, and `--to` names one of `sdk` or `tmux`),
`account` (a working account; see below for the `tmux` kind's own rule),
`session` (`data/runner-session.json` names a session to hand over; a
cousin that has never taken a turn on the runner lane has none, and a
session mid-rollover, marked `"fresh"` in that file, needs one turn
first), `supervisor` (a `cousin-supervisor` answers, since it stops and
starts the runner), and, for `--to tmux`, `trust` (never a gate; see
below). A cousin whose kind already matches `--to`, or whose runner is
`opencode` or `fake`, cannot switch: those are not the kinds the switch
moves between.

**Switching to the `tmux` kind** starts the host's interactive Claude
Code CLI in a tmux pane on the framework's own tmux socket
(`<root>/run/tmux.sock`, session `tmux-<slug>`, separate from the tmux
sessions 1.x used). Its account has to be a subscription login
(`claude-login`): a `claude-token` or `anthropic-key` account is refused
at `plan`, because the tmux kind has no way yet to show that CLI a
login-free config dir with no menu to click through. The CLI may show
the trust dialog once for this home (the cursor can start on "No,
exit"); `apply` waits at that dialog rather than failing on it, up to
`TRUST_WAIT_S` (600 s) instead of the usual verify timeout, printing
where to accept it: `tmux -S <root>/run/tmux.sock attach -t
tmux-<slug>`, or the console's pane view. If the account's config dir
already has this home recorded as trusted, `plan` says the pane should
not ask. When the account's own config also names MCP servers
(`.claude.json`'s `mcpServers`), `plan` warns that they load in the pane
and not in the `sdk` kind; nothing stops the switch over it.

**Either direction**, `apply`'s `close` stops the current kind's runner
at idle, keeping the session id; `toml` writes `[agent] runner =
"<kind>"` and nothing else, and writes or removes that kind's harness
settings; then it sets the mining cursor to the end of the new kind's
own record and queues one system message ahead of everything else
waiting, so the cousin's first turn on the new kind is told it moved,
and that instructions written for the old kind no longer apply; `start`
asks the supervisor to run the new kind; `verify` waits for the new
kind's own sign of life (a turn starting, for `tmux`; the SDK's
`session_init`, for `sdk`) for up to `VERIFY_S` (90 s), or, for `tmux`,
until the trust dialog is cleared. A `verify` that times out fails the
apply (rollback or look at the pane first) but does not undo `close` or
`toml`; if the operator accepts the trust dialog after `verify` gave up
and the cousin takes the queued notice anyway, the switch did complete,
late, and `cousin-migrate check` reports it as `switched` with that
noted.

```
cousin-migrate rollback sam --to tmux --yes
#     ok  close      the sdk runner stopped
#     ok  notice     the switch's notice, never taken, dropped
#     ok  restore    cousin.toml as it was, byte for byte; the tmux kind's settings
#     ok  cursor     the mining cursor at the end of the tmux kind's record
#     ok  start      the tmux runner resumes <id>
#   rolled_back
```

`rollback --to <kind>` undoes exactly what `apply` did, and only that:
`cousin.toml` restored byte for byte, the kind's settings written back
for the restored kind, the queued notice dropped if nobody took it, a
stale `tmux` trust flag cleared, the mining cursor set to the end of the
restored kind's record, and the runner started again on the same
session. `--to` has to name the kind the switch came from; it refuses a
second rollback.

`cousin-migrate check <slug>` shows a switch either way, once one is
recorded: `kind switch: tmux -> sdk, switched`, or `..., failed (...)`
for one that hasn't completed.

## Removed keys after the upgrade to 2.0.0

2.0.0 no longer reads the legacy tmux lane's keys (`[chat] port` and
`tmux_session`, `[runtime]`, the pane patterns in `config/harness.toml`,
`home_chat_url`, `config/agent-cmd`; the full list is in
[configuration](configuration.md#removed-in-200)). Every cousin spawned
on 1.x carries some of them. They do nothing and stop nothing: the runner
names them at start, `cousin-supervisor status` lists them under `config`,
and the console's card shows them, until you take them out.

Once every cousin runs on a runner kind, and after disabling the units only
the old lane used (`cousin-chat-server@<slug>.service` restarts a stopped
server by itself), look first, then remove:

```
cousin-migrate tidy --all          # lists each key and its line, writes nothing
cousin-migrate tidy --all --yes    # removes them
cousin-migrate tidy --all          # nothing to tidy
```

Each edited file keeps its prior bytes beside it
(`<home>/data/cousin.toml.pre-2.0.0`, `config/harness.toml.pre-2.0.0`,
`config/hive.toml.pre-2.0.0`, `config/agent-cmd.pre-2.0.0`), and only the
removed lines go: your comments and the other keys stay as they were. A 1.x
chat server still running for a cousin is stopped, and its
`data/chat-server.pid` removed. `tidy <slug>` does one cousin only. A cousin
with no `[agent] runner` is refused, with the way out; `tidy` does not
convert it. To go back to 1.x, the `.pre-2.0.0` copies are the files as they
were.

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
