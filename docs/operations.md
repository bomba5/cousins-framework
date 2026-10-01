# Operations

Running an install day to day: what each service does, where the logs are,
backups, the weekly sweep, upgrades, and what to check when something looks
wrong. It assumes you've done [install](install.md). The first section is the
Docker install; the rest is a bare host, where the same pieces run as systemd
units.

## The container

**What runs.** One container, `framework`, whose main process is
`cousin-supervisor` (the entrypoint prepares the volume, then hands over to
it). The [supervisor](glossary.md#supervisor) starts the console, the loops daemon, one
`cousin-runner` per [runner](glossary.md#runner) [cousin](glossary.md#cousin) and, for a cousin with `[telegram]` enabled,
its Telegram bridge (`telegram:<slug>`) and each enabled [plugin](plugins.md)'s
service (`plugin:<name>`), restarts a child that exits (backing
off up to 60 seconds), and stops them in order when the container stops: the
bridges, then the runners, each given 35 seconds to finish its [turn](glossary.md#turn), then the
plugin services, then the loops daemon, then the console. compose waits 45 seconds before it kills anything
(`stop_grace_period`). A `tmux`-kind cousin needs a bare host: the image has
no tmux. See each child:

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
a reboot. `stop` answers once the
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
`down -v` deletes it. The `embeddings` service keeps the Ollama model on a
second volume, `cousins-framework_embeddings-models`; nothing on it needs a
backup (the service pulls the model again if it is gone).

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
off; a message that arrived while it was down is waiting in its [inbox](glossary.md#inbox). A
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
anywhere else, is yours: see [Claude logins and Anthropic's terms](terms-risk.md).

**Cousins on opencode.** A cousin with `[agent] runner = "opencode"` runs its
turns through `opencode serve` on another provider's API key or a local
OpenAI-compatible model ([runners](reference/runners.md)).
The default image carries the opencode binary, so the plain
`docker compose up -d --build` runs it; only the slim image
(`compose.slim.yml`, for a Claude-only install) has none, and an opencode
cousin on it is refused. An image choice is an override of the framework
service, not a profile (a second service on the same volume would be a
second supervisor, which the root's lock refuses). `compose.opencode.yml`
is no longer needed; a setup that passes it gets the same default image.

```
docker compose up -d --build
docker compose exec -T framework cousin-account login keyed --provider openai < openai.key
```

The account is a `kind = "opencode"` one in `config/accounts.toml`, and the
cousin names its model (`[agent] model = "<provider>/<model>"`, required).
An API key goes in from stdin or a key file, never through chat; log in
while the cousin is stopped (the login takes no lock against a running
`opencode serve`). Everything opencode writes, its `auth.json` included,
stays in the account's data dir on the volume. On a bare host the same
cousin needs only the `opencode` binary on `PATH` (or `[agent] opencode_bin`).

This [lane](glossary.md#lane) never carries a Claude subscription: a Claude account on it, an
Anthropic OAuth login in its `auth.json`, or any config or environment that
names the Claude-subscription bridge refuses the start (exit 2), and a live
install that still carries the bridge removes it with the runbook in
[migrating](migrating.md). What the lane does not do yet (transcript
mining and memory proposals, image attachments as parts,
`--check-auth --validate`, `apply_patch` under an `Edit`/`Write` deny, side
sessions) is listed under [known gaps](reference/runners.md#known-gaps).

**The same supervisor on a bare host.** `systemd/cousin-supervisor.service`
runs `cousin-supervisor run` as one user unit: the console, the loops daemon
and one runner per cousin, `tmux`-kind cousins included. The units directory
also has `cousin-console.service` and `cousin-loops.service`, the same two
daemons as separate units; leave them disabled, never enabled beside the
supervisor. `cousin-loops run` holds `run/loops.lock`, so a second loops
daemon (a second clock) exits 5 (busy): whichever started second never
ticks. When that is the supervisor's loops child, it waits in `backoff`
(never `failing`) and becomes the clock once the other daemon stops, so no
clock ticks until then. The supervisor's console listens on
`127.0.0.1:8600` unless its unit carries `--console-host` and
`--console-port` (the LAN drop-in in
[install](install.md#reaching-the-console-from-the-lan)).
`systemctl --user reload cousin-supervisor.service` is the rescan. Its stop
takes the unit's whole control group with it: the runners and their bridges
stop with it. Moving an older install from the two separate units to the
supervisor is in
[the units](../systemd/README.md#one-unit-instead-of-two-the-supervisor).

One thing to know, in the container as on a bare host: the loops daemon
delivers at least once, so a stop that lands in the middle of its tick can
deliver that tick's heartbeat or loop again after the start.

## What runs

Three units run under your systemd user manager. The templates and how to
install them are in [the units](../systemd/README.md).

- **`cousin-supervisor.service`** runs the [supervisor](#the-container),
  and the supervisor runs the rest as its children:
  - **the loops daemon** (`loops`) is the scheduler. Every 30 seconds it
    ticks: heartbeats, each cousin's `[[loops]]`, one-shot schedules, timed
    [flip](glossary.md#flip) requests and the daily `flip_at` flips. It's
    the only thing that fires recurring work, so don't add a cron job that
    also fires a loop or a flip.
  - **the console** (`console`) is the web console on port 8600. It owns
    nothing but browser sessions and `config/console-users.json`; everything
    it shows it reads from the other stores on each request. Restarting it
    costs every open browser a login and nothing else.
  - **one `cousin-runner` per cousin** (`runner:<slug>`), and a cousin's
    Telegram bridge and each enabled plugin's service when there are any.
- **`cousin-tool-surface.timer`** runs daily at 06:00 and rewrites
  `data/tool-surface.md`.
- **`cousin-sweep.timer`** runs Sundays at 05:30 and compacts every cousin's
  memory.

No cousin runs a chat server of its own: the console answers chat itself
([chat](chat.md#where-a-message-goes)). The runners are not children of the
console or the loops daemon, so restarting either of those
(`cousin-supervisor stop --name console` then `start --name console`, or
`--name loops`) leaves the cousins running.

Check everything at once:

```
systemctl --user list-units 'cousin-*'
systemctl --user list-timers 'cousin-*'
cousin-loops status
cousin-supervisor status
```

## Logs

| what | where |
|---|---|
| loops daemon | `journalctl --user -u cousin-supervisor.service`, the lines starting with `loops` |
| console | `journalctl --user -u cousin-supervisor.service`, the lines starting with `console` |
| sweep | `journalctl --user -u cousin-sweep.service` |
| tool surface | `journalctl --user -u cousin-tool-surface.service` |
| the supervisor and its children (runners, bridges) | `journalctl --user -u cousin-supervisor.service`, each line starting with the child's name (`runner:wren`) |
| what a cousin's runner did | its [stream](glossary.md#stream) in `cousins/<slug>/data/stream/`, or `cousin-watch <slug>` |
| a cousin's Telegram bridge | `cousins/<slug>/data/telegram.log` |
| background jobs | `data/job-logs/` under the root, or `cousin-job tail <id>` |
| what a loop fired, and when | `data/loops-fires.jsonl` under the root |

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
back, not once per missed day. A cousin whose session started after the
day's flip time (one spawned or started since) is not flipped that day:
its session is younger than the flip point. [Worker](glossary.md#worker) cousins are skipped.

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
`MEMORY.md`, `STATUS.md` and `CLAUDE.md`. The runner's event [stream](glossary.md#stream),
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

To restore, stop the cousin and copy the files back into its home, then
start it again. The console and the loops daemon open a cousin's databases
when they need them and pick up the restored files.

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
. .venv/bin/activate && pip install -e ".[mcp,sdk]"
cousin-tool-surface
systemctl --user daemon-reload
systemctl --user restart cousin-supervisor.service
```

If `systemd/` changed, re-render the units before the `daemon-reload` (see
[the units](../systemd/README.md)). Restarting the supervisor restarts the
console, the loops daemon and every running cousin, each resuming its
session. To restart one cousin's runner on the new code instead, use the
console's restart button, or `cousin-supervisor stop <slug>` then
`start <slug>`; the steps are in [install](install.md#update).

An install upgraded from 1.x disables the units 1.x had for its per-cousin
chat servers and session starts once, since 2.0.0 runs neither:

```
systemctl --user disable --now cousin-chat-watchdog.timer \
    cousin-chat-server@<slug>.service cousin-start@<slug>.service
```

with one `cousin-chat-server@<slug>.service` and one
`cousin-start@<slug>.service` for each slug that had one, and removes their
files ([the units](../systemd/README.md#units-200-removed)).

`cousin-version` prints the version and commit of the checkout; the
console's top bar shows the one the console process is running.

## After a reboot

The units come back by themselves (with linger on). `cousin-supervisor`
starts every cousin with it, each resuming its session, except a cousin with
`[agent] auto_start = false` or one held down by a stop (`<home>/run/held`):
those stay stopped until you start them from the console or with
`cousin-supervisor start <slug>`. See
[systemd/README.md](../systemd/README.md).

## Troubleshooting

Work from the outside in and stop at the first thing that's wrong. Nothing
here loses memory: a dead component is a process to restart, not data to
recover.

**Nothing recurring happens (no heartbeats, loops or flips)**
- Check: `cousin-loops status`. "loops daemon has never run" or "loops daemon
  down (last tick Ns ago)" means nothing fires for anyone.
- Fix: `cousin-supervisor status` (the `loops` child) and the supervisor's
  journal (the `loops | ` lines) say why. After a fix, a loop that was due fires once on the next tick.

**A loop "never fires"**
- Check: the loops daemon's journal. A loop only counts as fired once its
  text was delivered, so a loop that never fires is usually a delivery that
  keeps failing, and every failure is logged there. A cousin.toml that
  doesn't parse, or a loop with two schedule forms or an empty prompt, is
  also logged by name.
- Fix: fix the delivery (a cousin with no `[agent] runner` gets none, see
  [chat](chat.md#where-a-message-goes)) or the loop entry. For a worker
  cousin, check that `config/worker-cmd` exists.
- To test a fix without waiting for the schedule: `cousin-loops fire
  <slug> <loop>` queues a request the daemon picks up on its next tick
  and fires that one loop right away, named as it is in `[[loops]]`, or
  `context-heartbeat` for the heartbeat itself. `cousin-loops requests`
  shows it pending, then `done` or `failed` (with why: an unknown loop
  name, or the delivery that failed).

**A message gets no answer**
- Check: `cousin-supervisor status`. The cousin's `runner:<slug>` should be
  running; a `failing` one shows its reason, and a stopped cousin waits for
  `start <slug>`. A runner whose account needs a login says so in the
  console and waits.
- Check: `cousin-watch <slug>` shows the runner's stream: whether a turn
  took the message, and what it did with it.
- Check: a cousin with no `[agent] runner` is refused by name, and nothing
  is delivered to it ([chat](chat.md#where-a-message-goes)).

**The console shows 0 cousins**
- Check: the console's startup line in `journalctl --user -u
  cousin-supervisor.service` (`console | cousin-console: serving <root>`)
  names the root it serves. It has to be the directory whose `cousins/` holds
  your homes. A wrong `{{ROOT}}` in the unit or a wrong `--root` gives an
  empty fleet.
- Check: a card marked hidden disappears unless "show hidden" is on in
  Settings.
- Check: one `cousin.toml` that doesn't parse breaks the whole list (the API
  answers 500 with the parse error). Find it:
  `for f in cousins/*/cousin.toml; do python3 -c 'import sys,tomllib; tomllib.load(open(sys.argv[1],"rb"))' "$f" || echo "$f"; done`
- Fix: correct the root and restart the supervisor, or fix the file.

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
- Check: the button exits with code 75 and relies on whatever runs the
  console to start it again. The supervisor restarts it at once. If you run
  the console by hand, or as its own unit without a restart policy, nothing
  brings it back.
- Fix: `cousin-supervisor start --name console`, and run the console under
  the supervisor.

**A cousin keeps getting restarted**
- Check: `cousin-loops requests` and the loops journal. A flip ends the
  session and starts a new one. The usual causes: `flip_at` in its
  `cousin.toml` (or the install's `default_flip_at`), or timed flip requests.
- Check: `cousin-supervisor status`. A runner that keeps exiting is
  restarted with backoff, and the reason is on its `runner:<slug> | ` lines
  in the supervisor's journal.
- Fix: move `flip_at`, or fix what the runner names.

**Port already in use**
- The console: the supervisor's `console` child fails at start with
  "Address already in use". Change `--console-port` in the supervisor's
  drop-in, or stop whatever holds 8600.

**A start fails right away**
- `cousin-spawn <slug> --start` and the console name the cause: no
  supervisor running (start it, then start the cousin again; the home is
  kept), a cousin with no `[agent] runner` or one that is not a runner kind
  (refused by name, exit 2), or a runner started by hand that holds the
  cousin's lock (stop it first).
- A runner that starts and exits shows as `failing` in `cousin-supervisor
  status`, with its reason. A configuration error (exit 2) is `failing` at
  once: an unknown account or one on the wrong lane, a secret file open to
  group or others, a `policy.toml` that doesn't parse, an `opencode` cousin
  with no `[agent] model`. An `sdk` cousin on a checkout installed without
  the `sdk` extra says "claude-agent-sdk is not installed"; reinstall with
  `pip install -e ".[mcp,sdk]"`.
- An `opencode` cousin whose binary isn't found fails its start with
  `opencode start: ...`. Under systemd, PATH is the unit's, not your login
  shell's; put the binary's absolute path in `[agent] opencode_bin`.
- An account that isn't logged in is not a start failure: the runner says
  "login required" in the console and waits. See [cousins](cousins.md) and
  [install](install.md#4-claude-code).

**The cousin booted degraded**
- Check: the boot packet header lists `DEGRADED layers`. A missing tool
  surface means `cousin-tool-surface` hasn't run. The other layers
  (self-portrait, calibration, active state) each say how to fix them in
  their own section. See [lifecycle](reference/lifecycle.md).

**Search is keyword only**
- Check: `config/embedding.toml` exists and the service answers. When it's
  configured but unreachable, search says so under the results. A timeout
  shorter than one chunk's embedding time looks the same: raise `timeout_s`.
- In Docker: `docker compose ps embeddings` is `healthy` once the model is on
  its volume; `docker compose logs embeddings` shows the pull and, with no
  network, its retries every minute.

**A reply was blocked**
- `cousin-reply` or `cousin-chat send` exits 3 and names the word:
  `config/outbound-filter.json` matched. Nothing was sent.
