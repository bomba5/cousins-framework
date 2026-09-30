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
| `cousin-supervisor.service` | `cousin-supervisor run --console-port 8600`: the console (on loopback), the loops daemon and one `cousin-runner` per runner cousin, restarted with backoff and stopped in order | instead of `cousin-console.service` and `cousin-loops.service`, never beside them (see below) |
| `cousin-tool-surface.service` + `cousin-tool-surface.timer` | `cousin-tool-surface --bin {{USER_BIN}}`: rewrites `data/tool-surface.md`, which the boot packet quotes | daily at 06:00 |
| `cousin-sweep.service` + `cousin-sweep.timer` | `cousin-sweep compact --target both`: memory compaction for every cousin | Sundays at 05:30 |

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
systemctl --user enable --now cousin-tool-surface.timer cousin-sweep.timer
loginctl enable-linger "$USER"
```

A cousin comes back after a reboot with the supervisor: every cousin runs
on a runner kind, and `cousin-supervisor` starts each one's runner (unless its
`[agent] auto_start` is false), so enable `cousin-supervisor.service` (below),
or keep the two units and run the supervisor for the runners only. There is
no per-cousin start unit.

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

## One unit instead of two: the supervisor

`cousin-supervisor.service` runs the supervisor the container runs. It starts
the console and the loops daemon itself, so it replaces
`cousin-console.service` and `cousin-loops.service`. Enable one set, never
both: two consoles would serve one root, and the second loops daemon is
refused by the first one's lock, which leaves the supervisor's loops child
in `backoff` (busy, retried against the holder's lock forever, never
counted toward `failing`), not `failing`.

The supervisor's console listens on `127.0.0.1:8600` unless told otherwise,
not where your old console did. First read the old console's address: the
last `ExecStart=` that `systemctl --user cat` prints is the one that runs,
drop-ins included.

```
systemctl --user cat cousin-console.service | grep '^ExecStart='
```

If it has a `--host` or a `--port` (the LAN drop-in from
[install](../docs/install.md#reaching-the-console-from-the-lan), or a port
that hive nodes on other machines call), carry both into a drop-in for the
supervisor before you switch. With `USER_BIN` set as in the install loop
above, and the old console's `--host 0.0.0.0 --port 8087` as the example:

```
mkdir -p ~/.config/systemd/user/cousin-supervisor.service.d
cat > ~/.config/systemd/user/cousin-supervisor.service.d/console.conf <<EOF
[Service]
ExecStart=
ExecStart=$USER_BIN/cousin-supervisor run --console-host 0.0.0.0 --console-port 8087
EOF
systemctl --user daemon-reload
```

The supervisor hands its console only the host and the port. If the old
`ExecStart=` has any other flag (`--secure-cookie`, `--tmux-bin`), keep the
old units for now. Otherwise switch:

```
systemctl --user disable --now cousin-console.service cousin-loops.service
systemctl --user enable --now cousin-supervisor.service
```

and check that the console answers where it did (`curl -fsS
http://127.0.0.1:8087/api/version` in the example). To go back, the old
units and their drop-ins are untouched:

```
systemctl --user disable --now cousin-supervisor.service && systemctl --user enable --now cousin-console.service cousin-loops.service
```

`systemctl --user reload cousin-supervisor.service` rescans `cousins/`: a new
runner cousin gets its runner, and nothing healthy restarts.

Its `KillMode=mixed` stops the supervisor's whole control group: the
runners and their bridges stop with it. To run the supervisor for the
runners only, beside the two old units, use a drop-in (`systemctl --user
edit cousin-supervisor.service`):

```
[Service]
ExecStart=
ExecStart={{USER_BIN}}/cousin-supervisor run --no-console --no-loops
```

Replace `{{USER_BIN}}` there as in the loop above.

## Telegram bridge

There is no unit for it: the bridge starts and stops with its cousin
([telegram](../docs/telegram.md#6-when-it-runs)).

## Install as system units

Same placeholders, plus three changes per unit: add `User=<account>` to
`[Service]`, change `WantedBy=default.target` to `multi-user.target`, and
replace `%h` with that account's home directory (in a system unit `%h` is
root's home). Put them in the system unit directory and use `systemctl`
without `--user`.

## Units 2.0.0 removed

2.0.0 runs no per-cousin chat server: the console, the runner's inbox and
the hive carry chat. It has no legacy tmux lane either, so no unit starts a
cousin's session at boot: the supervisor starts every runner cousin. An
install upgraded from 1.x disables the old units once (`systemctl --user
disable --now cousin-chat-watchdog.timer cousin-chat-server@<slug>.service
cousin-start@<slug>.service`, for each slug that had one) and removes their
files from `~/.config/systemd/user/`.

## Remove

```
systemctl --user disable --now cousin-loops.service cousin-console.service \
    cousin-tool-surface.timer cousin-sweep.timer
rm -rf ~/.config/systemd/user/cousin-*
systemctl --user daemon-reload
systemctl --user reset-failed
```

The full uninstall is in [install](../docs/install.md#uninstall).
