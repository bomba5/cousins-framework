# Install

This takes you from a plain Linux box to a logged-in console with a first
cousin answering chat, and back again. I wrote it against Ubuntu 24.04; any
Linux with the same pieces works with its own package names. The example
cousin is `wren` and the console user is `ana`.

When you're done you have: the checkout (which is also the framework root),
a venv inside it, one cousin under `cousins/wren`, its Claude Code session in
a tmux session called `wren`, its chat server on a local port, and a handful
of systemd user units that keep running whether or not you're logged in.

## What you need

- **Python 3.11 or newer.** The framework is Python and uses `tomllib`, which
  arrived in 3.11. Ubuntu 24.04 ships 3.12.
- **python3-venv.** Ubuntu won't let pip install into the system Python, so
  the framework lives in a venv.
- **tmux.** Every cousin's agent runs in a tmux session. Starting, flipping,
  delivering chat messages and the console's terminal view all go through
  it.
- **git.** For the clone. The console also reads the commit it's running
  from it.
- **A systemd user manager.** The loops daemon, the console and the timers
  run as user units. You can skip systemd and run the commands by hand, but
  then nothing recurring happens while you're away.
- **Claude Code.** The agent. The framework starts whatever command
  `config/agent-cmd` holds, but it writes Claude Code's project files for
  every cousin (`.claude/settings.json`, `.mcp.json`) and ships a Claude Code
  preset, so that's what this page installs.
- **curl.** For the Claude Code and Ollama installers. Usually already there.
- **The `mcp` Python package** (optional extra, pulled in by
  `pip install -e ".[mcp]"`). It's what lets a cousin use its tools over MCP.
  Spawn wires MCP up for every cousin, so install it unless you know you
  won't use it. Everything else in the framework is standard library.
- **Ollama with `nomic-embed-text`** (optional). Gives memory search a
  semantic leg. Without it, search is keyword only.
- **A browser with internet access** for the console. The page loads React,
  Babel, marked, mermaid and xterm from unpkg and jsdelivr. The backend
  fetches nothing.

## 1. System packages

```
sudo apt-get update
sudo apt-get install -y python3-venv tmux git curl
```

Run `apt-get update` first. On a box that hasn't refreshed its package
lists, `python3.12-venv` can 404 on a stale `.deb`.

## 2. Clone and install

```
git clone https://github.com/bomba5/cousins-framework.git ~/cousins-framework
cd ~/cousins-framework
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[mcp]"
```

The package isn't on PyPI. Always install from the checkout.

For every shell after this one:

```
cd ~/cousins-framework && . .venv/bin/activate && export FRAMEWORK_ROOT=$PWD
```

Put the last two in your shell profile if you like. Commands you type inside
the checkout find the root on their own, but exporting it keeps every command
in agreement. For the memory and job tools from a plain shell, also
`export COUSIN_HOME=$PWD/cousins/<slug>`, otherwise they stop with "no cousin
context". The systemd units set all of this themselves.

## 3. Run the tests

```
python3 -m unittest discover -s tests
```

It takes a few minutes (about ten on a 2-core VM). The summary has to say
`OK` (a few skips are fine); a line or two of test output can print after
it, so look for `OK` near the end rather than on the very last line.
Do it now, before any cousin exists, so a failure is the framework's and not
your install's.

## 4. Claude Code

```
curl -fsSL https://claude.ai/install.sh | bash     # puts claude in ~/.local/bin
~/.local/bin/claude                                 # log in once, then /exit
```

Log in once (interactively, or with `claude auth login`) before any cousin
starts. A cousin started before that sits on Claude Code's first-run screens
(the theme picker, then the login menu) in its tmux session. With the preset below, the console marks it "needs attention" and
the framework types nothing into that pane: chat messages, heartbeats,
scheduled prompts and a flip's boot text are all skipped with a
`tmux delivery SKIPPED` line in the log. The chat message is stored, but the
cousin never sees it. Without the preset there's nothing to recognise the
menu by, and all of that gets typed into it, where it can pick options.

Now tell the framework how to start the agent. Use the absolute path, so it
resolves under systemd's PATH as well as yours:

```
printf '%s\n' "$HOME/.local/bin/claude --dangerously-skip-permissions --model {model} --effort {effort} --session-id {session_id}" > config/agent-cmd
cp config/harness.toml.claude-code.example config/harness.toml
```

`--dangerously-skip-permissions` lets the cousin run every tool without
asking, which is what an unattended cousin needs. It means what it says: the
cousin can do anything your account can. Leave it out if you'd rather answer
permission prompts yourself in the console's terminal view or with
`tmux attach -t wren`. `{model}`, `{effort}` and `{session_id}` are filled in
on every start; see [configuration](configuration.md#agent-cmd).

To try the framework without a Claude login (a test VM, a demo), give it a
stand-in agent that reads its terminal, so what the framework types lands
somewhere you can check:

```
printf '%s\n' 'bash -lc "cat > $HOME/agent-input.txt"' > config/agent-cmd
```

A chat message then shows up in `~/agent-input.txt` as `(Chat ana): ...`.
Don't use something like `sleep infinity`: it never reads the terminal, the
boot text fills the input buffer and later messages go nowhere.

Without `config/harness.toml`, a lot quietly stays off: token counts in the
console, transcript mining at flip, the transcript-size guard, the harness
memory search collection, `cousin-mcp approve`, the `{model}`/`{effort}`
defaults and the "needs attention" flag. Copy the preset.

## 5. Optional: semantic search with Ollama

Search is keyword only until `config/embedding.toml` exists. With a local
Ollama:

```
curl -fsSL https://ollama.com/install.sh | sh
until ollama list >/dev/null 2>&1; do sleep 1; done   # the service needs a moment
ollama pull nomic-embed-text
cat > config/embedding.toml <<'EOF'
url = "http://localhost:11434/api/embeddings"
model = "nomic-embed-text"
timeout_s = 120
EOF
```

The Ollama installer takes about 2.4 GB of disk even on a CPU-only box, plus
about 260 MB for the model. The 120 second timeout is on purpose: on an old
CPU without AVX one chunk took 32 seconds to embed, and a timeout shorter than
one chunk makes every search wait it out and then fall back to keyword. With
a GPU or a modern CPU, 30 is plenty.

## 6. Make the first cousin

```
cousin-spawn wren --name Wren --role "helps me around the house" \
    --voice "Short, plain and honest." --operator ana
cousin-mcp approve wren
cousin-tool-surface
cousin-spawn wren --start
```

- `cousin-spawn` creates `cousins/wren/` with its `cousin.toml`, `CLAUDE.md`
  from the template, `STATUS.md`, `MEMORY.md`, the MCP registry and
  `.mcp.json`, and picks a free chat port from 8090 up. `--role` and
  `--voice` are required. `--operator` is the name you'll chat as; the
  cousin's `send` tool can reach that name. Leave it out for a cousin with no
  operator.
- `cousin-mcp approve` marks the home as trusted in `~/.claude.json` and
  enables the cousin's `cousin` MCP server, so Claude Code doesn't stop on
  its trust prompt. The file exists once Claude Code has run once.
- `cousin-tool-surface` writes `data/tool-surface.md`, which the boot packet
  quotes. Without it the first boot is marked degraded. The daily timer keeps
  it fresh after this.
- `cousin-spawn wren --start` starts the tmux session and the chat server.
  On a cousin that's already running it does nothing. Before it touches
  anything it checks that tmux and the agent command resolve, and stops with
  the reason if either doesn't.

More on all of this in [cousins](cousins.md).

## 7. The systemd units

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
! grep -l '{{' ~/.config/systemd/user/cousin-*   # prints nothing when every placeholder was replaced
systemctl --user daemon-reload
cousin-console adduser ana
systemctl --user enable --now cousin-loops.service cousin-console.service
systemctl --user enable --now cousin-sweep.timer cousin-tool-surface.timer cousin-chat-watchdog.timer
loginctl enable-linger "$USER"
```

Add the user before the console starts. Until the first user exists the
console has no login at all: anyone the network guard lets in can use it. By
default it listens on loopback only, so in practice that's anyone on this
machine. `cousin-console adduser` doesn't need the console running. It asks
for the password twice (at least 8 characters) and never takes it as an
argument. For a scripted install, pipe it: `printf '%s\n%s\n' "$PW" "$PW" |
cousin-console adduser ana` (Python warns that it can't hide the input; the
password is set). Login is enforced from the moment the users file exists, no
restart needed.

`loginctl enable-linger` keeps your user units running after you log out. If
it's refused, run it with `sudo`.

Don't enable `cousin-chat-server@wren.service`. `--start` already started
Wren's chat server and the watchdog timer looks after it. A second server on
the same port fails to bind and restarts every five seconds. The template
unit is for when you want systemd to own the chat server instead; see
[the units](../systemd/README.md).

## 8. Open the console

```
journalctl --user -u cousin-console.service -n 5
#   cousin-console: serving <root> on 127.0.0.1:8600
```

If the line ends with `(auth not configured: cousin-console adduser <name>)`,
the console started before the user existed. Login is enforced anyway; the
suffix only goes away on the next restart. On a machine that had the console
before, `-n 5` can also show lines from earlier runs: look at the newest.

Open `http://127.0.0.1:8600/` on the machine itself, or tunnel from another
one:

```
ssh -L 8600:127.0.0.1:8600 ana@192.0.2.10     # then open http://127.0.0.1:8600/
```

Log in, open Wren and send a message. The card should say running with no
"needs attention" line, and the reply shows up in the thread.

## Reaching the console from the LAN

Give the unit a drop-in. Re-running step 7 overwrites the unit files but
leaves drop-ins alone.

```
mkdir -p ~/.config/systemd/user/cousin-console.service.d
cat > ~/.config/systemd/user/cousin-console.service.d/lan.conf <<'EOF'
[Service]
ExecStart=
ExecStart=%h/cousins-framework/.venv/bin/cousin-console --host 0.0.0.0 --port 8600
EOF
systemctl --user daemon-reload && systemctl --user restart cousin-console
```

This is plain HTTP, so passwords and chat cross the network in the clear.
Create the user first. The network guard only lets in loopback and the
private ranges (10/8, 172.16/12, 192.168/16); `config/net-allowlist.json`
adds more. For anything beyond a LAN you trust, put TLS in front and add
`--secure-cookie` to that `ExecStart`.

Cousins on other machines need the console reachable too, since their nodes
call it. That's off until `config/hive.toml` turns it on; see
[remote cousins](remote-cousins.md).

## After a reboot

The units come back on their own (that's what linger is for). The cousins'
tmux sessions don't: nothing restarts an agent by itself. Start each one,
from the console's start button or:

```
cousin-spawn wren --start
```

## Update

```
cd ~/cousins-framework
git pull
. .venv/bin/activate
pip install -e ".[mcp]"          # picks up new commands; harmless otherwise
cousin-tool-surface               # the timer would do it by 06:00, this is now
systemctl --user daemon-reload
systemctl --user restart cousin-loops.service cousin-console.service
```

The `pip install` matters when the update adds a new `cousin-*` command:
an editable install only creates wrappers for the commands it knew about.
If `systemd/` changed in the pull, re-run the `sed` loop from step 7 before
the `daemon-reload`.

Restarting the console and the loops daemon doesn't touch the cousins or
their chat servers (both units use `KillMode=process`). Running cousins keep
the old code in their chat servers until they're restarted or flipped. A
flip picks up everything new; see [cousins](cousins.md). The console's top
bar shows the version and commit the console process is running, so a pull
without a restart is visible there.

If you move the checkout to another path, the cousins' Claude Code settings
still point at the old one. Fix each with `cousin-spawn <slug>
--repair-settings`, then `cousin-mcp approve <slug>` again.

## Uninstall

The reverse, in order. Back up first if you might want the cousins again:
`cousins/` holds all their memory and nothing else has a copy (see
[operations](operations.md#backups)).

```
systemctl --user disable --now cousin-loops.service cousin-console.service \
    cousin-sweep.timer cousin-tool-surface.timer cousin-chat-watchdog.timer
rm -rf ~/.config/systemd/user/cousin-*        # -r: the LAN drop-in is a directory
rm -f ~/.local/share/systemd/timers/stamp-cousin-*   # the timers' last-run stamps
systemctl --user daemon-reload
systemctl --user reset-failed

# every cousin: the tmux session and the chat server
for home in ~/cousins-framework/cousins/*/; do
  slug=$(basename "$home")
  tmux kill-session -t "$slug" 2>/dev/null
  [ -f "$home/data/chat-server.pid" ] && kill "$(cat "$home/data/chat-server.pid")"
done

rm -rf ~/cousins-framework     # checkout, venv, config, every cousin home
```

If a cousin's tmux session has a different name, it's `[chat] tmux_session`
in its `cousin.toml`. Turn linger off only if nothing else of yours needs it:
`loginctl disable-linger "$USER"`.

Claude Code and Ollama are separate products with their own uninstall.
Keep in mind that `~/.claude` and `~/.claude.json` hold Claude Code's login
and every project's transcripts, the cousins' included; `cousin-mcp approve`
also wrote one entry per cousin home into `~/.claude.json`. The Ollama installer
adds a system service, a user and a group:

```
sudo systemctl disable --now ollama
sudo rm -f /etc/systemd/system/ollama.service && sudo systemctl daemon-reload
sudo rm -rf /usr/local/bin/ollama /usr/local/lib/ollama /usr/share/ollama
sudo userdel ollama        # "group ollama not removed": the installer added you to it
getent group ollama >/dev/null && sudo groupdel ollama
```

The apt packages are ordinary system packages; remove them if nothing else
uses them.
