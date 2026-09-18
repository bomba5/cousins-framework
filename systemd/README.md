# systemd unit templates

These are templates, not units. Every install-specific value is a
placeholder, and a unit is produced by substituting all of them. Nothing
here knows where your checkout lives, which user runs it, or where pip
put the `cousin-*` wrappers; those are install facts and a test refuses
any absolute path in this directory.

| placeholder | meaning | typical value |
|---|---|---|
| `{{ROOT}}` | the framework root: the checkout that holds `cousins/`, `config/`, `templates/` | the directory you cloned into |
| `{{USER_BIN}}` | the directory holding the installed `cousin-*` console scripts | `<checkout>/.venv/bin` for the documented venv install; `dirname "$(command -v cousin-loops)"` once it is on PATH |
| `{{SYSTEM_PATH}}` | the PATH each unit sees after `{{USER_BIN}}` and `%h/.local/bin`, so `tmux` and your agent command resolve | `systemctl --user show-environment \| sed -n 's/^PATH=//p'` |

## Units

| unit | what it runs | cadence |
|---|---|---|
| `cousin-loops.service` | `cousin-loops run`: the one owner of heartbeats, `[[loops]]`, timed flips and the per-cousin daily `flip_at` | always on |
| `cousin-sweep.service` + `cousin-sweep.timer` | `cousin-sweep compact --target both` over every cousin home | weekly, Sunday 05:30 |
| `cousin-tool-surface.service` + `cousin-tool-surface.timer` | `cousin-tool-surface --bin {{USER_BIN}}`: rewrites `data/tool-surface.md`, which the boot packet quotes | daily, 06:00 |
| `cousin-chat-server@.service` | `cousin-chat-server --home {{ROOT}}/cousins/<slug>` for the instance name after `@` | always on, one instance per cousin |
| `cousin-chat-watchdog.service` + `cousin-chat-watchdog.timer` | `cousin-chat-watchdog`: one ensure pass over every cousin; spawns a missing chat server, alerts on a sick one, never kills | every 10 minutes |
| `cousin-console.service` | `cousin-console --port 8600`: the web console on loopback, a projection of the stores the other units own; a restart costs every browser its login and nothing else | always on |

Every service carries the environment the framework needs
(`FRAMEWORK_ROOT`; a `PATH` of the wrappers, then `%h/.local/bin`,
then the system PATH; and `PYTHONUNBUFFERED=1`) and runs from the root
as its working directory. `%h` is systemd's home-directory specifier:
the Claude Code installer puts `claude` in `~/.local/bin`, which the
user manager's PATH does not carry, and without it a flip started by
the loops daemon could not find the agent. Unbuffered output is what
makes a status line such as the console's "serving" line reach the
journal when it is printed rather than when a buffer fills. A `.timer` activates the
`.service` of the same name; enable the timer, not the service.

## Substitute and install (user units)

```
ROOT="$PWD"                                   # the checkout
USER_BIN="$PWD/.venv/bin"                     # the documented venv install
SYSTEM_PATH="$(systemctl --user show-environment | sed -n 's/^PATH=//p')"
mkdir -p ~/.config/systemd/user
for unit in systemd/*.service systemd/*.timer; do
  sed -e "s|{{ROOT}}|$ROOT|g" \
      -e "s|{{USER_BIN}}|$USER_BIN|g" \
      -e "s|{{SYSTEM_PATH}}|$SYSTEM_PATH|g" \
      "$unit" > ~/.config/systemd/user/"$(basename "$unit")"
done
grep -l '{{' ~/.config/systemd/user/cousin-* && echo "unsubstituted placeholder" 
systemctl --user daemon-reload
systemctl --user enable --now cousin-loops.service
systemctl --user enable --now cousin-sweep.timer cousin-tool-surface.timer
systemctl --user enable --now cousin-chat-watchdog.timer          # supervises spawn/flip-owned servers
systemctl --user enable --now cousin-console.service             # the web console
```

That block is the documented path: `cousin-spawn <slug> --start` (and
every flip) starts the cousin's chat server, and the watchdog timer
restarts it if it dies. It deliberately does NOT enable
`cousin-chat-server@<slug>.service`: for a cousin whose server spawn
already started, that unit fails to bind the port (EADDRINUSE) and
restarts every five seconds. The unit is the alternative owner; see
"Chat server: pick one owner" below.

The `grep` line is the check that every placeholder was replaced; a
unit with `{{` left in it fails at start with a path that does not
exist, which reads like a broken install rather than a missed step.

For the units to run while you are logged out, enable lingering for
the account once: `loginctl enable-linger "$USER"`. For system units
instead of user units, add a `User=` line to each `[Service]`, change
`WantedBy=default.target` to `multi-user.target`, replace `%h` with
that user's home directory (in a system unit `%h` is root's home), and
install under the system unit directory; the placeholders are the
same.

## Chat server: pick one owner

Both `cousin-console.service` and `cousin-loops.service` carry `KillMode=process`: a chat server started through the console, or respawned by a flip, lives in that unit's cgroup, and without it a plain `systemctl --user restart` of the console silently killed every chat server it had started.

`cousin-spawn --start` and `cousin-flip` start a cousin's chat server
themselves, detached. Use `cousin-chat-server@<slug>.service` only when
you want systemd to own that lifetime instead: then stop the
spawn-started server first (`kill "$(cat <home>/data/chat-server.pid)"`),
enable the unit, and leave the watchdog timer disabled. Never both,
since two servers on one port make the second one fail (a
bind-and-restart loop every five seconds) and the first one look like
the survivor. See `docs/operations.md`.

`cousin-chat-watchdog.timer` is the supervisor for the spawn/flip-owned
case: every ten minutes it spawns a server for any running cousin whose
port is free and logs an alert (never a kill) when the port is occupied
but `/health` does not answer with the cousin's slug. With the
`cousin-chat-server@` units, systemd already restarts the server, so
leave the watchdog timer disabled. If your agent sessions live on a
non-default tmux socket, add `Environment=COUSIN_TMUX_SOCKET=<path>` to
the watchdog service, the same seam the chat server reads.
