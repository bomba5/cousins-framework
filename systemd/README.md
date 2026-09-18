# systemd unit templates

The files here are templates, not units. Anything that depends on your
install is a placeholder, and you make real units by replacing the
placeholders with `sed`. Nothing in this directory knows where your checkout
is or where pip put the `cousin-*` commands (a test refuses absolute paths
here).

## The placeholders

- `{{ROOT}}` - the framework root: the directory holding `cousins/`,
  `config/` and `templates/`. Normally the checkout.
- `{{USER_BIN}}` - the directory with the installed `cousin-*` commands. For
  the venv install in [install](../docs/install.md) that's `<checkout>/.venv/bin`.
  If they're on your PATH already: `dirname "$(command -v cousin-loops)"`.
- `{{SYSTEM_PATH}}` - the PATH the units see after `{{USER_BIN}}` and
  `%h/.local/bin`, so `tmux` and your agent command resolve. Take it from
  the user manager: `systemctl --user show-environment | sed -n 's/^PATH=//p'`.

## The units

| unit | runs | when |
|---|---|---|
| `cousin-loops.service` | `cousin-loops run --interval 30`: heartbeats, loops, one-shot schedules, timed flips, the daily `flip_at` | always |
| `cousin-console.service` | `cousin-console --port 8600`: the web console, on loopback | always |
| `cousin-chat-watchdog.service` + `cousin-chat-watchdog.timer` | `cousin-chat-watchdog`: starts a missing chat server for any running cousin, logs an alert for a sick one, never kills | every 10 minutes |
| `cousin-tool-surface.service` + `cousin-tool-surface.timer` | `cousin-tool-surface --bin {{USER_BIN}}`: rewrites `data/tool-surface.md`, which the boot packet quotes | daily at 06:00 |
| `cousin-sweep.service` + `cousin-sweep.timer` | `cousin-sweep compact --target both`: memory compaction for every cousin | Sundays at 05:30 |
| `cousin-chat-server@.service` | `cousin-chat-server --home {{ROOT}}/cousins/<slug>` for the slug after the `@` | always, one per cousin, only if you want systemd to own chat servers (see below) |

A `.timer` starts the `.service` with the same name. Enable the timer, not
the service.

Every service runs from the root and sets three things:

- `FRAMEWORK_ROOT={{ROOT}}`, so every command agrees on the install.
- `PATH={{USER_BIN}}:%h/.local/bin:{{SYSTEM_PATH}}`. `%h` is systemd's
  shorthand for your home directory. The Claude Code installer puts `claude`
  in `~/.local/bin`, which the user manager's PATH doesn't include; without
  it, a flip started by the loops daemon can't find the agent.
- `PYTHONUNBUFFERED=1`, so status lines reach the journal when they're
  printed, not when a buffer fills.

The console and loops units also set `KillMode=process`. Both start chat
servers (the console's start button, a flip), and without it a plain
`systemctl --user restart` would kill every chat server they started.

## Install as user units

From the checkout, with the venv install:

```
ROOT="$PWD"
USER_BIN="$PWD/.venv/bin"
SYSTEM_PATH="$(systemctl --user show-environment | sed -n 's/^PATH=//p')"
mkdir -p ~/.config/systemd/user
for unit in systemd/*.service systemd/*.timer; do
  sed -e "s|{{ROOT}}|$ROOT|g" \
      -e "s|{{USER_BIN}}|$USER_BIN|g" \
      -e "s|{{SYSTEM_PATH}}|$SYSTEM_PATH|g" \
      "$unit" > ~/.config/systemd/user/"$(basename "$unit")"
done
! grep -l '{{' ~/.config/systemd/user/cousin-*
systemctl --user daemon-reload
systemctl --user enable --now cousin-loops.service cousin-console.service
systemctl --user enable --now cousin-chat-watchdog.timer cousin-tool-surface.timer cousin-sweep.timer
loginctl enable-linger "$USER"
```

The `grep` line prints nothing when every placeholder was replaced. If it
prints a file name, that unit still has a `{{...}}` in it and will fail at
start with a path that doesn't exist, which looks like a broken install
rather than a missed step.

`loginctl enable-linger` keeps user units running while you're logged out.
Run it once per account (with `sudo` if it's refused).

To change a unit later, use a drop-in (`systemctl --user edit <unit>`)
rather than editing the rendered file: re-running the loop above overwrites
the file but leaves drop-ins alone. That's how you put the console on the
LAN, see [install](../docs/install.md#reaching-the-console-from-the-lan).

## Install as system units

Same placeholders, plus three changes per unit: add `User=<account>` to
`[Service]`, change `WantedBy=default.target` to `multi-user.target`, and
replace `%h` with that account's home directory (in a system unit `%h` is
root's home). Put them in the system unit directory and use `systemctl`
without `--user`.

## Chat server: pick one owner

`cousin-spawn --start`, a flip and the console's start button all start a
cousin's chat server themselves, detached. The watchdog timer then brings it
back if it dies. That's the normal setup, and it's why the block above does
not enable `cousin-chat-server@.service`.

Use `cousin-chat-server@<slug>.service` only if you want systemd to own that
chat server instead:

```
kill "$(cat cousins/<slug>/data/chat-server.pid)"      # stop the spawn-started one
systemctl --user enable --now cousin-chat-server@<slug>.service
systemctl --user disable --now cousin-chat-watchdog.timer
```

Never both. Two servers on one port means the second can't bind, and with
`Restart=always` it retries every five seconds forever while the first one
looks like it's fine. Spawn and the console check the port first and reuse a
server that's already answering. A flip always launches one, and with the
unit's server on the port it fails to bind and exits, leaving a line in
`chat-server.log`; that's harmless. The watchdog is the one that has to be
off.

If your agents run on a non-default tmux socket, add
`Environment=COUSIN_TMUX_SOCKET=<path>` to `cousin-chat-watchdog.service` and
`cousin-chat-server@.service` (a drop-in is fine).

## Remove

```
systemctl --user disable --now cousin-loops.service cousin-console.service \
    cousin-chat-watchdog.timer cousin-tool-surface.timer cousin-sweep.timer
rm -rf ~/.config/systemd/user/cousin-*
systemctl --user daemon-reload
systemctl --user reset-failed
```

The full uninstall is in [install](../docs/install.md#uninstall).
