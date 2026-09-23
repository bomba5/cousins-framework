# Migrating a cousin

How to move a cousin you already have into an install: its home, its memory,
its chat history, and, if you ran cousins on other machines under an older
framework, the hive's tokens and memory. It assumes the target install is
done and working ([install](install.md)).

There are two cases, and they go differently:

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
