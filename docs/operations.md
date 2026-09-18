# Operations

Running an install day to day: what each service does, where the logs are,
backups, the weekly sweep, upgrades, and what to check when something looks
wrong. It assumes you've done [install](install.md).

## What runs

Five things run under your systemd user manager. The templates and how to
install them are in [the units](../systemd/README.md).

- **`cousin-loops.service`** is the scheduler. Every 30 seconds it ticks:
  heartbeats, each cousin's `[[loops]]`, one-shot schedules, timed flip
  requests, the daily `flip_at` flips and the transcript-size guard. It's the
  only thing that fires recurring work, so don't add a cron job that also
  fires a loop or a flip.
- **`cousin-console.service`** is the web console on port 8600. It owns
  nothing but browser sessions and `config/console-users.json`; everything it
  shows it reads from the other stores on each request. Restarting it costs
  every open browser a login and nothing else.
- **`cousin-chat-watchdog.timer`** runs every 10 minutes and makes sure every
  running cousin's chat server answers (more below).
- **`cousin-tool-surface.timer`** runs daily at 06:00 and rewrites
  `data/tool-surface.md`.
- **`cousin-sweep.timer`** runs Sundays at 05:30 and compacts every cousin's
  memory.

Outside systemd, each running cousin is two processes: its agent in a tmux
session, and its chat server (`cousin-chat-server --home <home>`), started
detached by `cousin-spawn --start`, a flip, the console, or the watchdog.
Both the loops and console units use `KillMode=process`, so restarting them
doesn't take the chat servers they started down with them.

Check everything at once:

```
systemctl --user list-units 'cousin-*'
systemctl --user list-timers 'cousin-*'
cousin-loops status
cousin-chat-watchdog --dry-run
tmux ls
```

## Logs

| what | where |
|---|---|
| loops daemon | `journalctl --user -u cousin-loops.service` |
| console | `journalctl --user -u cousin-console.service` |
| watchdog | `journalctl --user -u cousin-chat-watchdog.service` |
| sweep | `journalctl --user -u cousin-sweep.service` |
| tool surface | `journalctl --user -u cousin-tool-surface.service` |
| a cousin's chat server | `cousins/<slug>/data/chat-server.log` |
| background jobs | `data/job-logs/` under the root, or `cousin-job tail <id>` |
| what a loop fired, and when | `data/loops-fires.jsonl` under the root |

The chat server writes every failed or skipped delivery into its log as
`[chat-server] tmux delivery FAILED` or `tmux delivery SKIPPED`. That's the
first thing to grep when a cousin didn't see a message.

## The chat-server watchdog

A chat server started by spawn or a flip has nobody watching it. The
watchdog is that somebody. Each run it looks at every cousin and does one of
four things:

- **skip**: no tmux session (a stopped cousin needs no chat) or no chat port
- **ok**: `/health` answers with the cousin's slug
- **alert**: the port is taken but `/health` doesn't answer with this slug.
  It logs an alert, exits 1 and touches nothing, because the thing on the
  port might be a squatter or a stuck server, and killing blind is worse.
- **spawn**: the port is free, so it starts `cousin-chat-server` detached,
  logging to `<home>/data/chat-server.log`, and waits up to 5 seconds for it
  to answer

```
cousin-chat-watchdog --dry-run
#   [chat-watchdog] wren: ok
#   [chat-watchdog] kestrel: spawn (would spawn cousin-chat-server --home ...)
```

A lock in `data/chat-watchdog.lock` makes an overlapping run exit quietly. If
your agents live on a non-default tmux socket, add
`Environment=COUSIN_TMUX_SOCKET=<path>` to the watchdog service.

If you'd rather have systemd own a cousin's chat server, use
`cousin-chat-server@<slug>.service` instead and leave the watchdog timer off.
Never both: see [the units](../systemd/README.md#chat-server-pick-one-owner).

## The daily flip

A flip ends a cousin's session and starts a fresh one with a boot packet
built from its memory. There's no unit for it; the loops daemon does it. Set
`flip_at = "HH:MM"` under `[lifecycle]` in a cousin's `cousin.toml` and it
flips once a day at or after that time. The daemon flips at most one cousin
per tick, but give them times a few minutes apart anyway. If the daemon was
down at flip time, the flip happens once on the next tick after it comes
back, not once per missed day. Worker cousins are skipped.

```
cousin-loops requests          # pending timed flips
cousin-flip wren --dry-run     # what a flip would do
cousin-flip wren --confirm     # flip now; --confirm has it post one line in chat once it's back
```

Never run a flip from inside the cousin itself. More in
[cousins](cousins.md).

## Backups

```
cousin-backup --home cousins/wren --dest /srv/backups/cousins
```

That writes `<dest>/wren/<YYYY-MM-DD>/`. Every SQLite database under the
cousin's `data/` is copied with `VACUUM INTO` (a plain file copy of a
database another process has open can come out torn), plus `memory/`,
`MEMORY.md`, `STATUS.md` and `CLAUDE.md`. Search indexes are skipped; they're
rebuilt on the next search. A second run on the same day overwrites that
day's snapshot.

What it doesn't copy: `cousin.toml`, `notes/`, the other files in `data/`
(`decisions.jsonl`, `corrections.jsonl`, `handoff.md` and friends), and
anything else in the home. It also only does one home, not the root's own
`data/` (jobs, schedules, the tracker, loop requests) or `shared/` (the
shared memory tier and the hive database). For a full copy, stop the cousin
and tar its home. What I do is the snapshot for the databases, then a plain
copy of the rest:

```
for home in "$FRAMEWORK_ROOT"/cousins/*/; do
  cousin-backup --home "$home" --dest /srv/backups/cousins
done
rsync -a --exclude '*.db' --exclude '*.db-wal' --exclude '*.db-shm' \
    "$FRAMEWORK_ROOT"/cousins/ /srv/backups/cousins-files/
```

There's no backup unit on purpose: where snapshots go (rsync, restic, a git
remote) is your call. The root's `data/*.db` and `shared/hive/` are worth
copying the same way with `sqlite3 <db> "VACUUM INTO '<dest>'"`.

To restore, stop the cousin and copy the files back into its home. The chat
server and the loops daemon open their databases lazily and pick up the
restored files.

## The sweep

```
cousin-sweep compact --target both
```

Runs `cousin-memory compact` for every cousin, one after the other. `index`
retires old pointers from `MEMORY.md` until it fits its size budget (it
never deletes the memory itself), `raw` folds daily raw files older than the
hot window into monthly gzip archives plus a digest (lossless). One cousin's
failure doesn't stop the rest; the exit code is 1 if any failed, so the unit
shows as failed in the journal.

On a new install, run it by hand with `--target index` first and read the
per-cousin lines before you enable the timer. See [memory](memory.md).

## The tool-surface manifest

```
cousin-tool-surface
#   wrote <root>/data/tool-surface.md (37 tools)
```

Writes `<root>/data/tool-surface.md`: one line per `cousin-*` command with
the first line of its `--help`. The boot packet quotes it so a cousin knows
what it can run. Without the file, every boot is marked degraded. The timer
refreshes it daily; run it by hand after an upgrade that adds commands.

## Upgrades

```
cd ~/cousins-framework && git pull
. .venv/bin/activate && pip install -e ".[mcp]"
cousin-tool-surface
systemctl --user restart cousin-loops.service cousin-console.service
```

If `systemd/` changed, re-render the units first (see
[the units](../systemd/README.md)). Chat servers keep running the old code
until they're restarted; a cousin picks up everything on its next flip. To
restart one chat server by hand, kill it and let the watchdog bring it back,
or run the watchdog now:

```
kill "$(cat cousins/wren/data/chat-server.pid)"
cousin-chat-watchdog
```

`cousin-version` prints the version and commit of the checkout; the
console's top bar shows the one the console process is running.

## After a reboot

The units come back by themselves (with linger on). The cousins don't:
nothing restarts an agent session on its own. Start each one from the
console or with `cousin-spawn <slug> --start`. The watchdog then keeps its
chat server up.

## Troubleshooting

Work from the outside in and stop at the first thing that's wrong. Nothing
here loses memory: a dead component is a process to restart, not data to
recover.

**Nothing recurring happens (no heartbeats, loops or flips)**
- Check: `cousin-loops status`. "loops daemon has never run" or "loops daemon
  down (last tick Ns ago)" means nothing fires for anyone.
- Fix: `systemctl --user status cousin-loops.service` and its journal say
  why. After a fix, a loop that was due fires once on the next tick.

**A loop "never fires"**
- Check: the loops daemon's journal. A loop only counts as fired once its
  text was delivered, so a loop that never fires is usually a delivery that
  keeps failing, and every failure is logged there. A cousin.toml that
  doesn't parse, or a loop with two schedule forms or an empty prompt, is
  also logged by name.
- Fix: fix the delivery (usually the tmux session, see below) or the loop
  entry. For a worker cousin, check that `config/worker-cmd` exists.

**A cousin looks stopped, but its tmux session is up**
- Check: `tmux ls` shows the session, but the console card says stopped. The
  console and the session are on different tmux sockets, or the session's
  name isn't `[chat] tmux_session` from `cousin.toml`.
- Fix: pass `--tmux-socket <path>` in the console unit's `ExecStart`, and
  `COUSIN_TMUX_SOCKET` for the chat server and watchdog. Or rename the
  session to match.

**A cousin looks running, but it's dead**
- Check: `curl -s http://127.0.0.1:<port>/health`. It should answer with this
  cousin's slug. Something else on the port (another cousin, or an unrelated
  service) makes a plain port check read "running". The watchdog reports it
  as ALERT and leaves it alone.
- Fix: find the process on the port (`ss -ltnp | grep :<port>`), stop it,
  then run `cousin-chat-watchdog` to start the right server.

**Chat messages don't reach the pane**
- Check: `grep 'tmux delivery' cousins/<slug>/data/chat-server.log`.
  - `SKIPPED ... the pane shows "Select login method"` (or another attention
    pattern): the agent is waiting on a person. The console card also says
    "needs attention". Log Claude Code in once (`claude` in a shell, or
    `tmux attach -t <slug>`), or answer the trust prompt, which
    `cousin-mcp approve <slug>` prevents.
  - `FAILED ... can't find session`: the tmux session is gone or has another
    name. Start the cousin, or fix `[chat] tmux_session`.
  - Nothing at all: the message never reached this chat server. Check the
    server answers `/health`, and that it wasn't started with
    `--no-terminal-delivery`.
- Also: with no `config/harness.toml`, nothing is skipped and text is typed
  into whatever the pane shows, menus included. Copy the Claude Code preset.

**The console shows 0 cousins**
- Check: the startup line in `journalctl --user -u cousin-console.service`
  names the root it serves. It has to be the directory whose `cousins/` holds
  your homes. A wrong `{{ROOT}}` in the unit or a wrong `--root` gives an
  empty fleet.
- Check: a card marked hidden disappears unless "show hidden" is on in
  Settings.
- Check: one `cousin.toml` that doesn't parse breaks the whole list (the API
  answers 500 with the parse error). Find it:
  `for f in cousins/*/cousin.toml; do python3 -c 'import sys,tomllib; tomllib.load(open(sys.argv[1],"rb"))' "$f" || echo "$f"; done`
- Fix: correct the root and restart the console, or fix the file.

**The console: 403, 401 or 503 on every request**
- 403: your address isn't loopback or a private range, and isn't in
  `config/net-allowlist.json`.
- 401: login required and this browser has no session. A console restart
  drops every session; log in again.
- 503 and `CLOSED` in the startup line: `config/console-users.json` exists
  but can't be used. Restore it, or delete it and `cousin-console adduser`
  again.

**The console page loads but stays blank**
- Check: the browser's console. The page loads React, Babel, marked, mermaid
  and xterm from unpkg and jsdelivr; a browser that can't reach them gets a
  blank page.
- Fix: give the browser internet access, or download those files next to
  `cousin_lib/console_static/index.html` and point its script tags at them.

**The console's restart button stops the console**
- Check: the button exits the console process cleanly and relies on systemd
  to start it again. The shipped unit has `Restart=on-failure`, which doesn't
  restart after a clean exit.
- Fix: `systemctl --user start cousin-console`, or add a drop-in with
  `Restart=always` if you want the button to work.

**A cousin keeps getting restarted**
- Check: `cousin-loops requests` and the loops journal. A flip ends the
  session and starts a new one. The usual causes: `flip_at` in its
  `cousin.toml`, or `flip_when_transcript_mb` in `config/harness.toml`
  (a long session crosses the size and gets flipped).
- Check: `systemctl --user list-units 'cousin-chat-server@*'`. If a
  `cousin-chat-server@<slug>` unit is enabled for a cousin whose chat server
  spawn also starts, the unit loses the port and restarts every 5 seconds.
- Fix: raise or remove the threshold, move `flip_at`, or disable the extra
  unit.

**Port already in use**
- A chat server: `chat-server.log` says `cannot bind chat port`. Something
  else holds `[chat] port`. Stop it, or give the cousin another port in
  `cousin.toml` and restart it. Spawn picks ports from 8090 up that no other
  `cousin.toml` claims and nothing listens on, but it can't know about a
  service that starts later. One such line right after a flip is normal: a
  flip always launches a chat server, and when the old one is still
  answering, the new one exits.
- The console: the unit fails at start with "Address already in use". Change
  `--port` in a drop-in, or stop whatever holds 8600.

**A start fails right away**
- `cousin-spawn <slug> --start` and the console name the cause: no
  `config/agent-cmd`, tmux not on PATH, or the agent command's first word not
  found. Under systemd, PATH is the unit's, not your login shell's; write the
  agent's absolute path into `config/agent-cmd`.
- `auth:` errors come from the `api_key` mode's checks (no key file, a login
  left in the isolated directory). See [cousins](cousins.md).

**The cousin booted degraded**
- Check: the boot packet header lists `DEGRADED layers`. A missing tool
  surface means `cousin-tool-surface` hasn't run. The other layers
  (self-portrait, calibration, active state) each say how to fix them in
  their own section. See [lifecycle](reference/lifecycle.md).

**Search is keyword only**
- Check: `config/embedding.toml` exists and the service answers. When it's
  configured but unreachable, search says so under the results. A timeout
  shorter than one chunk's embedding time looks the same: raise `timeout_s`.

**A reply was blocked**
- `cousin-reply` or `cousin-chat send` exits 3 and names the word:
  `config/outbound-filter.json` matched. Nothing was sent.
