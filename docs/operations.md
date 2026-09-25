# Operations

Running an install day to day: what each service does, where the logs are,
backups, the weekly sweep, upgrades, and what to check when something looks
wrong. It assumes you've done [install](install.md). The first section is the
Docker install; the rest is a bare host, where the same pieces run as systemd
units.

## The container

**What runs.** One container, `framework`, whose main process is
`cousin-supervisor` (the entrypoint prepares the volume, then hands over to
it). The supervisor starts the console, the loops daemon, one
`cousin-runner` per runner cousin and, for a cousin with `[telegram]` enabled,
its Telegram bridge (`telegram:<slug>`), restarts a child that exits (backing
off up to 60 seconds), and stops them in order when the container stops: the
bridges, then the runners, each given 35 seconds to finish its turn, then the
loops daemon, then the console. compose waits 45 seconds before it kills anything
(`stop_grace_period`). Every cousin in the container is a runner cousin; tmux
cousins need a bare host. See each child:

```
docker compose exec framework cousin-supervisor status
docker compose exec framework cousin-supervisor stop wren     # held down until start
docker compose exec framework cousin-supervisor start wren    # lifts the hold
docker compose exec framework cousin-supervisor start --name loops
docker compose exec framework cousin-supervisor reload        # rescan cousins/
```

A runner cousin starts with the container unless its `cousin.toml` says
`[agent] auto_start = false` or a stop holds it. Its Telegram bridge, when
`[telegram]` is enabled and complete, starts after it and is stopped and held
with it; a config the bridge would refuse is one `supervisor:` line with the
reason and no child, and `reload` picks up a change. A stop of a runner cousin
(`cousin-supervisor stop` or the console's stop button) writes
`cousins/<slug>/run/held`, with the time and who asked, and the hold lasts
until `start`: across `docker compose restart`, `down` and `up`, an upgrade or
a reboot, as a stopped tmux cousin stays stopped. `stop` answers once the
runner is down (it finishes its turn first, up to 35 seconds); `stop
--no-wait` answers at once. The console's stop button does not wait either:
it answers 202 `stopping` and the fleet row shows when the runner is down;
its restart button answers 202 too and starts the runner again once it is
down. A child that exits five times inside a minute is `failing` and left
down; `status` shows the reason, and `start <slug>` (or `start --name
console`, `start --name loops`) tries again once you've fixed it. A runner
that exits 5 found another runner holding its cousin's lock and is restarted
with backoff; a runner's configuration error (exit 2) is `failing` at once.
A runner whose account is not logged in does not exit: it says so in the
console and waits, and resumes once the credentials change. The console's
own restart button brings the console back at once.

**Logs.** Everything goes to the container's output:

```
docker compose logs -f framework
```

Each line starts with who wrote it: `console | `, `loops | `,
`runner:wren | `, `telegram:wren | `, `supervisor: ` for the supervisor itself and `entrypoint: `
for the start-up steps. `supervisor: runner:wren failing: ...` is the line
to look for when a cousin stays down. The files the bare host keeps (the loops fire
log, job logs) are in the same places under `/data`.

**Volumes.** The `framework-data` volume, mounted at `/data`, is the framework
root and holds everything: `config/`, `cousins/`, `data/`, `shared/`, the
installed secrets in `.secrets/`, the supervisor's socket in `run/`, and
`home/`. The container runs as uid 10001 with `HOME=/data/home`; the agent
CLI writes its own session transcripts there, a cache the framework never
reads (a cousin's transcript is its `data/sessions.db`). `templates/` is a
link into the image, so an upgrade brings new templates, and the
`config/*.example` files are refreshed from the image on every start. compose
pins the project name, so the volume is `cousins-framework_framework-data`
whatever you called the checkout. `docker compose down` keeps it;
`down -v` deletes it.

**Backup.** `cousin-backup` works inside the container, then copy the
snapshot out:

```
docker compose exec framework cousin-backup --home cousins/wren --dest /data/backups
docker compose cp framework:/data/backups ./backups
```

What it copies, in what order, and why a restored runner answers a row at
least once (never zero times, possibly twice) is in [Backups](#backups).

For everything at once, snapshot the volume with the stack stopped, using
the image's own `tar`:

```
docker compose down
docker run --rm -u 0 --entrypoint tar -v cousins-framework_framework-data:/data:ro \
    cousins-framework:local czf - -C /data . > framework-data.tgz
docker compose up -d
```

To restore into an empty volume, the same with `xzf - -C /data` and the
archive on stdin (`-i`, and the volume mounted read-write), with the stack
stopped. `tar` run as root keeps the owners (uid 10001) and modes, the
private `.secrets/` included, so keep the archive as private as the keys in
it.

**Upgrade.**

```
git pull
docker compose build
docker compose up -d
```

`up` recreates the container on the new image; the volume is untouched.
Each runner resumes its session from the volume (`data/runner-session.json`
and `data/sessions.db`), so a cousin picks up its conversation where it left
off; a message that arrived while it was down is waiting in its inbox. A
cousin you stopped stays stopped until you start it. The
console's top bar shows the version it runs, as does
`docker compose exec framework cousin-version`.

**Extending the image.** pip is removed from the image (the venv's and the
base image's own), and it has no compilers and few tools. For more, build on it and keep its user:

```
FROM cousins-framework:local
USER root
RUN apt-get update && apt-get install -y --no-install-recommends jq \
 && rm -rf /var/lib/apt/lists/*
USER 10001:10001
```

```
docker build -t cousins-framework:local .
docker build -t cousins-framework:mine -f Dockerfile.mine .
```

and run yours from `compose.override.yml`:

```
services:
  framework:
    image: cousins-framework:mine
    build: !reset null
```

Keep `USER 10001:10001` last: the volume's files belong to that uid, and a
cousin running as root can write anything in it. After a `git pull`, rebuild
both images.

**Auth.** Two lanes, the same as on a bare host.

- The API key: a compose secret, `secrets/anthropic_api_key`, turned on by
  `cp compose.api-key.yml compose.override.yml`
  ([install](install.md#install-with-docker)). The entrypoint installs it as
  the `api-key` account's key on every start, so a rotated key needs
  `docker compose restart`, not a rebuild.
- A Claude login: a `claude-login` account in `config/accounts.toml`, logged
  in with `docker compose exec framework cousin-account login <name>`; its
  credentials stay on the volume. Mounting a host's own credential directory
  instead is possible (the commented `volumes:` line in `compose.yml`), but
  not a good idea: two processes refreshing one login race on its token file.

The terms risk of running cousins on a subscription login, in a container or
anywhere else, is yours.

**Cousins on opencode.** A cousin with `[agent] runner = "opencode"` runs its
turns through `opencode serve` on another provider's API key or a local
OpenAI-compatible model ([runners](reference/runners.md)). The default image
has no opencode binary; run the framework service on the image's opencode
variant with the override file, not a profile (a second service on the same
volume would be a second supervisor, which the root's lock refuses):

```
docker compose -f compose.yml -f compose.opencode.yml up -d --build
docker compose exec -T framework cousin-account login keyed --provider openai < openai.key
```

The account is a `kind = "opencode"` one in `config/accounts.toml`, and the
cousin names its model (`[agent] model = "<provider>/<model>"`, required).
An API key goes in from stdin or a key file, never through chat; log in
while the cousin is stopped (the login takes no lock against a running
`opencode serve`). Everything opencode writes, its `auth.json` included,
stays in the account's data dir on the volume. On a bare host the same
cousin needs only the `opencode` binary on `PATH` (or `[agent] opencode_bin`).

This lane never carries a Claude subscription: a Claude account on it, an
Anthropic OAuth login in its `auth.json`, or any config or environment that
names the Claude-subscription bridge refuses the start (exit 2), and a live
install that still carries the bridge removes it with the runbook in
[migrating](migrating.md). What the lane does not do yet (usage records,
transcript mining and memory proposals, image attachments as parts,
`--check-auth --validate`, `apply_patch` under an `Edit`/`Write` deny, side
sessions) is listed under [known gaps](reference/runners.md#known-gaps).

**The same supervisor on a bare host.** `systemd/cousin-supervisor.service`
runs `cousin-supervisor run` as one user unit in place of
`cousin-console.service` and `cousin-loops.service`, never beside them.
`cousin-loops run` holds `run/loops.lock`, so a second loops daemon (a second
clock) exits 5 (busy): whichever started second never ticks. When that is the
supervisor's loops child, it waits in `backoff` (never `failing`) and becomes
the clock once the old daemon stops, so no clock ticks until then. And the
supervisor's console listens on `127.0.0.1:8600` unless its unit carries the
old console's `--host` and `--port`, which the migration in the units README
does. `systemctl --user reload cousin-supervisor.service` is the rescan. Its
stop takes the unit's whole control group with it, so while you still have
tmux cousins keep the two old units and run the supervisor beside them for
the runner cousins only (`--no-console --no-loops`). The steps are in
[the units](../systemd/README.md#one-unit-instead-of-two-the-supervisor).

A few things the container does not do yet: a hive node's `[tell-home]` has
no chat server to post to there ([remote cousins](remote-cousins.md)), and
the loops daemon, like on a bare host, delivers at least once, so a stop that
lands in the middle of its tick can deliver that tick's heartbeat or loop
again after the start.

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

- **skip**: the cousin is not running (a stopped cousin needs no chat) or
  has no chat port. Running means its tmux session is up, or, for a runner
  cousin, that its runner holds its lock: the supervisor runs no chat
  server, so this is what brings a runner cousin's back after a reboot or a
  crash
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
built from its memory. There's no unit for it; the loops daemon does it.

**Every cousin flips, configured or not.** The time is its own
`flip_at = "HH:MM"` under `[lifecycle]` in `cousin.toml`, else the install's
`default_flip_at` in `config/harness.toml`, else 04:00. `flip_at = "never"`
opts a cousin out, and `cousin-loops flips` prints each cousin's time and
where it came from. The daemon flips at most one cousin per tick, so a shared
time queues rather than collides. If the daemon was
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
database another process has open can come out torn), the runner's
`inbox.db`, `sessions.db` and `usage.db` included, plus `memory/`,
`MEMORY.md`, `STATUS.md` and `CLAUDE.md`. The runner's event stream,
`data/stream/*.jsonl`, is copied too, each file cut in the copy to its last
complete line (the runner may be mid-line when the snapshot runs), and so
are the runner's small state files, as plain copies: the sessions it resumes
(`data/runner-session*.json`), its generation count (`data/generation.txt`)
and the cursors and window of its per-turn mining and proposals
(`data/extract-cursor.json`, `data/propose-cursor.json`,
`data/proposals.json`). Without them a restored cousin starts a fresh
session, resets its generation and mines turns again. Search indexes are
skipped; they're rebuilt on the next search. A second run on the same day
overwrites that day's snapshot.

The order is fixed: `inbox.db` first, then the other databases, then the
streams, then the state files. A turn commits its reply to `chat.db`
before it marks its row `done` in `inbox.db`, so with the inbox copied first
a row that is `done` in the snapshot has its reply in the snapshot too. The
other way round, a reply written while `chat.db` was being copied could be
missing while its row reads `done`, and nothing would answer it again.

A snapshot taken while the runner is mid-turn restores cleanly: the row it
was answering is `claimed` in the copy, and the runner's start puts every
claim back in the queue (it holds the home's lock, so no other runner owns
one), then answers it. That is at-least-once, not once: if the turn had
already replied when the snapshot ran, the restored runner answers that row
a second time. No message is lost.

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
#   wrote <root>/data/tool-surface.md (43 tools)
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

The units come back by themselves (with linger on). A cousin comes back only
if you enabled its start unit:

```
systemctl --user enable cousin-start@wren.service
```

That runs `cousin-spawn wren --start --resume` once at boot, which picks up
the cousin's last session where it can and opens a new one where it can't.
Cousins without the unit stay stopped until you start them from the console
or with `cousin-spawn <slug> --start`. Either way the watchdog then keeps
the chat server up. See [systemd/README.md](../systemd/README.md).

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
- To test a fix without waiting for the schedule: `cousin-loops fire
  <slug> <loop>` queues a request the daemon picks up on its next tick
  and fires that one loop right away, named as it is in `[[loops]]`, or
  `context-heartbeat` for the heartbeat itself. `cousin-loops requests`
  shows it pending, then `done` or `failed` (with why: an unknown loop
  name, or the delivery that failed).

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
- Check: the button exits with code 75 and relies on systemd to start it
  again. That works with the shipped unit (`Restart=on-failure`). If you run
  the console by hand or under a unit without a restart policy, nothing
  brings it back.
- Fix: `systemctl --user start cousin-console`, and use the shipped unit.

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
