# Operations

How to run the framework unattended: install from a cold clone, the
service units, the daily flip, backups, the fleet sweep, the
tool-surface manifest, what to check when a cousin goes quiet, and
the web console with its first user and first login.
`docs/guide.md` narrates the features; this page is for the machine
that runs them overnight.

Every value that belongs to one install (the checkout, the user, the
wrapper directory) is a placeholder here and in `systemd/`. Nothing on
this page names a real host.

## 1. Install from a cold clone

The complete procedure for a new machine, prerequisites to uninstall,
is `docs/install.md`; follow it rather than this summary on a fresh
box. The shape, on Ubuntu 24.04 (Python >= 3.11; the repository may be
private, so the clone needs a deploy key or token):

```
sudo apt-get update && sudo apt-get install -y python3-venv tmux git
git clone <this repo> cousin-framework && cd cousin-framework
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[mcp]"              # from the checkout; not on PyPI
python3 -m unittest discover -s tests   # before any cousin exists
export FRAMEWORK_ROOT="$PWD"         # the checkout is the root
printf '%s\n' "$HOME/.local/bin/claude --dangerously-skip-permissions --model {model} --effort {effort} --session-id {session_id}" > config/agent-cmd
cp config/harness.toml.claude-code.example config/harness.toml
cousin-spawn testa --root "$PWD" --name Testa --role "test cousin" \
    --voice "Plain and helpful."
cousin-tool-surface                  # so the first boot is not degraded
cousin-spawn testa --start
```

The agent line assumes Claude Code, installed and logged in once
(`docs/install.md` step 4); any agent works, with an absolute path to
its executable. `cousin-spawn <slug> --start` starts an existing
cousin and checks tmux and the agent executable before touching
anything; it is a no-op on a cousin that is already running.

`config/` holds every install seam and is gitignored except for
`*.example` files; `docs/configuration.md` lists each file and what its
absence means. The root's `data/` directory (loop state, the request
store, job logs, the tool-surface manifest) is runtime state and is
gitignored too.

## 2. The units

`systemd/` ships templates with three placeholders (`{{ROOT}}`,
`{{USER_BIN}}`, `{{SYSTEM_PATH}}`); `systemd/README.md` gives the
`sed` loop that turns them into user units and the one-line check that
no placeholder survived. What each one owns:

| unit | owns |
|---|---|
| `cousin-loops.service` | the scheduler: heartbeats, `[[loops]]`, timed-flip requests, and the per-cousin daily `flip_at` |
| `cousin-sweep.timer` | the weekly compaction sweep (Sunday 05:30) |
| `cousin-tool-surface.timer` | the daily manifest refresh (06:00) |
| `cousin-chat-server@<slug>.service` | one cousin's chat server, ONLY when systemd rather than spawn/flip should own it: not for a cousin started with `--start`, and not together with the watchdog timer |
| `cousin-chat-watchdog.timer` | the chat-server watchdog (every 10 minutes) for spawn/flip-owned servers |
| `cousin-console.service` | the web console on loopback port 8600: a view over every store above, owning only browser sessions and the users file (section 8) |

Every service sets `FRAMEWORK_ROOT`, a `PATH` that finds the wrappers
first and then `%h/.local/bin` (where the Claude Code installer puts
`claude`, so a flip the loops daemon runs can start the agent), and
`PYTHONUNBUFFERED=1` so status lines reach the journal as they are
printed. A unit that carries none of this fails in ways that read like
a broken install (a command "not found" from inside a working
checkout). Enable lingering (`loginctl enable-linger "$USER"`) or the
user units stop when you log out.

The loops daemon is the one owner of recurring work. Do not add a cron
entry that also fires a loop or a flip: two owners means the "fired"
state each one commits is a lie to the other.

The chat server started by `cousin-spawn --start` or `cousin-flip` has
no supervisor of its own; `cousin-chat-watchdog` is that supervisor,
one ensure pass per timer fire. For every cousin in the registry it
decides one of four things: no tmux session or no `chat.port`, skip;
`/health` answers with the cousin's slug, ok; the port is occupied but
health does not answer for this slug, alert (a line in the journal,
exit 1, and nothing touched, because the occupant may be a squatter or
a wedged server and a watchdog must never kill blind); the port is
free, spawn `cousin-chat-server --home <home>` detached with its output
appended to `<home>/data/chat-server.log`, then wait up to 5 seconds
for `/health`. `cousin-chat-watchdog --dry-run` prints the decision per
cousin and spawns nothing. An flock on `<root>/data/chat-watchdog.lock`
makes an overlapping fire exit 0 with "another pass is running". Do
not enable the timer for cousins the `cousin-chat-server@` units own:
systemd restarts those itself, and two owners of one port is the
failure the README warns about.

## 3. The daily flip

There is no daily-flip unit. The daemon carries the driver: set
`flip_at = "HH:MM"` under `[lifecycle]` in a cousin's `cousin.toml` and
the daemon flips that cousin once per day at or after that time, at
most one cousin per tick, so boot packets never assemble at the same
moment. Stagger the times across cousins yourself (a few minutes apart
is enough). A missed time (the daemon was down) fires on the next tick
after it comes back, once, not once per missed day. Worker-type
cousins are skipped.

`cousin-loops requests` lists pending timed flips; `cousin-loops status`
says whether the daemon has ticked recently. A manual flip is
`cousin-flip --confirm <slug>`, never from inside the cousin itself.

## 4. Backups

`cousin-backup --home <home> --dest <dir>` snapshots one cousin: every
database under its `data/` through `VACUUM INTO` (a file copy of a
database another process holds open can be torn), plus `memory/`,
`MEMORY.md`, `STATUS.md`, `CLAUDE.md`, `cousin.toml`, staged then
renamed so a failed run leaves no half-written snapshot. Rebuildable
search indexes are skipped. It lands at `<dest>/<slug>/<date>/`.

There is no fleet backup unit because what to do with a directory of
snapshots is yours to decide (rsync, restic, a git remote). A loop over
the registry is one line:

```
for home in "$FRAMEWORK_ROOT"/cousins/*/; do
  cousin-backup --home "$home" --dest /path/to/snapshots
done
```

Restore is a copy back into the home while the cousin is stopped; the
chat server and the loops daemon open their databases lazily and pick
the restored files up on their next access.

## 5. The sweep

`cousin-sweep compact --target both` runs `cousin-memory compact` for
every cousin in registry order: `index` retires old reachable pointers
from `MEMORY.md` until it fits its byte budget (hygiene, never
deletion), `raw` folds daily raw files older than the hot window into
monthly gzip archives plus a digest (lossless). One cousin's failure
never stops the rest; the exit code is 1 if any failed, which marks the
unit failed so the journal carries it:

```
journalctl --user -u cousin-sweep.service -n 50
```

Run it by hand with `--target index` first on a new fleet and read the
per-cousin lines before enabling the timer.

## 6. The tool surface

`cousin-tool-surface` writes `<root>/data/tool-surface.md`: one line
per console script with the first line of its `--help`, the script
list taken from the installed package's entry points (from
`pyproject.toml` when running from an uninstalled checkout). The boot
packet quotes it as section "Tool Surface", bounded to 1500
characters, and marks the boot degraded when the file is absent, so run
it once at install and let the daily timer keep it current after every
upgrade. The unit passes `--bin {{USER_BIN}}` so the manifest describes
the wrappers actually on PATH.

## 7. When a cousin is silent

Work from the outside in; stop at the first thing that is wrong.

1. **Is the daemon ticking?** `cousin-loops status`. "never run" or
   "down" means nothing recurring fires for anyone; `systemctl --user
   status cousin-loops.service` and its journal say why.
2. **Is the chat server up, and is it THIS cousin's?** A port answers
   `GET /health` with its slug. A port that answers with a
   different slug (or an unrelated service) reads as "running" to a
   port check while the cousin is dead; kill the squatter, then start
   the right server. Its log is `<home>/data/chat-server.log`.
   `cousin-chat-watchdog --dry-run` gives this answer for the whole
   fleet in one line per cousin (ok, spawn, alert, skip).
3. **Is the agent session alive?** `tmux ls` (or your agent's own
   listing) for the cousin's session. No session (after a reboot
   there is none): `cousin-spawn <slug> --start` starts it with the
   chat server; `cousin-flip --confirm <slug>` instead starts a fresh
   generation with a boot packet. Either stops and names the cause
   when `config/agent-cmd` is missing or tmux or the agent executable
   does not resolve. A session that is alive but parked on the agent's
   login menu shows "needs attention" on its console card when
   `config/harness.toml` lists `attention_patterns` (the Claude Code
   preset does); log the agent in once (`docs/install.md` step 4).
4. **Did delivery fail rather than the loop?** Firing state commits
   only after delivery, so a loop that "never fired" is usually a
   delivery that keeps failing; the daemon prints each error to its
   stderr (the unit's journal). Fix the target, and the loop fires on
   the next tick, once.
5. **Did it boot degraded?** The boot packet header lists `DEGRADED
   layers`. A missing self-portrait, calibration, active state or
   tool surface each has a one-command fix named in its section.
6. **Is STATUS stale?** The packet warns when decisions were logged
   after STATUS.md's last edit; the cousin anchors on STATUS, so
   reconcile it (`cousin-sync-state` afterwards) before blaming
   memory.
7. **Did the outbound filter block the reply?** Exit 3 from
   `cousin-reply` or `cousin-chat` is a protected term in the text;
   the message was not sent and the CLI said which rule.

What the framework promises across all of this: a component that dies
loses nothing that was not already in a store some other component
owns. A silent cousin is a process to restart, not memory to recover.

## 8. The console: unit, first user, first login

`cousin-console.service` runs `cousin-console --port 8600` from the
root, on loopback. It is the same unit shape as the others (root,
`FRAMEWORK_ROOT`, the wrappers first on `PATH`, `Restart=on-failure`)
and it may be restarted at any time: the console owns nothing but
in-memory browser sessions and `config/console-users.json`, so a
restart costs every open tab a login and nothing else
(`docs/ui-spec.md`). Enable it with the rest:

```
systemctl --user enable --now cousin-console.service
journalctl --user -u cousin-console.service -n 20
#   -> cousin-console: serving <root> on 127.0.0.1:8600 (auth not configured: cousin-console adduser <name>)
```

That parenthesis is the first thing to act on. Out of the box the
console is open to every address the network guard admits (loopback
and the RFC1918 private ranges; `config/net-allowlist.json` adds
more),
and it says so on its account panel. Before the console is reachable
from anything but the machine it runs on, create the first user; the
password is read from a prompt, never from argv, so it lands in no
shell history or process listing:

```
cousin-console --root "$FRAMEWORK_ROOT" adduser ana   # --root optional inside the checkout
#   password for ana: ********
#   again: ********
#   -> cousin-console: user ana set in <root>/config/console-users.json
```

The file is written atomically with mode 0600 (PBKDF2-HMAC-SHA256, a
random salt per user); the same command with an existing name resets
that user's password. No restart is needed: the console reads the
file on every request, and from the moment it holds one user every
`/api/*` route but login and `me` answers 401 without a session. There
is no loopback or trusted-LAN bypass to fall back on, by design.

First login: open `http://127.0.0.1:8600/` on the machine, or tunnel
from another one (`ssh -L 8600:127.0.0.1:8600 <user>@<machine>`, then
the same URL). For direct LAN access put `--host 0.0.0.0` in the
unit's `ExecStart`; that is plain HTTP, so only on a trusted LAN, and
behind TLS with `--secure-cookie` (the cookie is then marked Secure)
anywhere else. The page loads without a session; the login
form is part of it. Sign in with the user just created; the account
panel then lists the configured users and offers a password change and
logout. A cousin created later by `cousin-spawn` appears on the next
request, and a cousin whose chat server is down shows `chat: down` on
its card rather than vanishing: the console asks each store every
time and caches nothing anything else trusts.

What to check when the console misbehaves, outside in:

1. **403 on every route** is the network guard: the client's address
   is not loopback and not in `config/net-allowlist.json`.
2. **401 on every route but the page** is the users file: it exists
   and holds a user, and this browser has no session (a console
   restart drops every session; log in again).
3. **A card says `stopped` for a cousin whose tmux session is up**
   means the console and the session use different tmux sockets; pass
   `--tmux-socket` in the unit's `ExecStart`, the same seam the chat
   server reads as `COUSIN_TMUX_SOCKET`.
4. **The page loads but stays blank** is the one runtime network fetch
   the browser makes (React, Babel, marked, mermaid and xterm from a
   CDN, named in `index.html`): vendor those files under the static
   directory and edit the tags if the browser cannot reach them; the
   backend fetches nothing.

The end-to-end walk of exactly this session - one cousin spawned in a
temp root, its real chat server, a fake tmux, one user, login, fleet,
chat through the proxy, the pane and its stream, jobs, loops, memory,
the tracker, the static bundle - is `tests/console/test_console_e2e.py`,
in process and on loopback, so the flow above is run on every test run
rather than remembered.
