# Install on a new machine

The one ordered procedure from a stock machine to a logged-in cousin
answering chat, then its reverse. Written against Ubuntu 24.04; any
Linux with Python 3.11 or newer, tmux, git and a systemd user manager
works the same way with its own package names. Every step is a command
you can paste; the example cousin is `testa`.

What you end up with: the checkout (it is also the framework root),
a venv inside it, one cousin under `cousins/testa`, the agent running
in a tmux session named `testa`, its chat server on a local port, and
the user units (loops daemon, console, three timers) running whether
or not you are logged in.

## Prerequisites

| need | why | Ubuntu 24.04 |
|---|---|---|
| Python >= 3.11 | the framework | ships 3.12 |
| `python3-venv` | Ubuntu refuses `pip install` into the system Python (PEP 668); the framework lives in a venv | `apt-get install python3-venv` |
| `tmux` | every cousin's agent runs in a tmux session; `--start`, flips, chat delivery and the console's pane view all use it | `apt-get install tmux` |
| `git` | the clone, and the gate's self-test | `apt-get install git` |
| `curl` | the agent and Ollama installers | usually present |
| access to the repository | it may be private: the clone needs a GitHub deploy key or a token | - |

## 1. System packages

```
sudo apt-get update
sudo apt-get install -y python3-venv tmux git curl
```

`apt-get update` first: on a machine that has not refreshed its
package lists, `python3.12-venv` can 404 on a stale `.deb`.

## 2. Clone and install into a venv

```
git clone <repo-url> ~/cousins-framework     # private repo: deploy key or token
cd ~/cousins-framework
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[mcp]"
```

`[mcp]` is the one optional extra (the MCP SDK and a few dozen
dependency packages); it
is what lets the agent use the cousin's tools over MCP, which spawn
wires up for every cousin, so install it unless you know you will not
use it. The package is not on PyPI: always install from the checkout
(`-e .` or `-e ".[mcp]"`).

Every later shell starts in the checkout with the venv on PATH and the
root named (the steps below use paths relative to the checkout):

```
cd ~/cousins-framework && . .venv/bin/activate && export FRAMEWORK_ROOT=$PWD
```

(or put the last two in your shell profile). To point the memory and job
tools at one cousin from a shell, also `export COUSIN_HOME=$PWD/cousins/<slug>`;
without it they stop with "no cousin context". The units below carry them
themselves. Commands you type inside the checkout find the root on their
own, and a cousin's tools find it from the cousin's home, but naming it
keeps every command in agreement.

## 3. Run the suite

```
python3 -m unittest discover -s tests
```

About 1390 tests in about six minutes on a small VM; the summary line
must read `OK` (a few `skipped` are fine). Run it now, before any cousin
exists, so a failure is the framework's and not your install's.

## 4. The agent: Claude Code

The framework starts whatever `config/agent-cmd` names; it writes
Claude Code's project files for every cousin (`.claude/settings.json`,
`.mcp.json`), so Claude Code is the agent this procedure installs.

```
curl -fsSL https://claude.ai/install.sh | bash     # installs ~/.local/bin/claude
~/.local/bin/claude                                 # log in once, then /exit
```

Log in interactively once (or run `claude auth login`) BEFORE any
cousin starts. A cousin started before that sits at the login menu in
its tmux session; the console shows it as running with a "needs
attention" line (with the preset below), and anything sent to it is
typed into the menu.

The agent command, with an absolute path so it resolves under the
units' PATH as well as yours:

```
printf '%s\n' "$HOME/.local/bin/claude --dangerously-skip-permissions --model {model} --effort {effort} --session-id {session_id}" > config/agent-cmd
```

`--dangerously-skip-permissions` lets the agent run every tool without
asking, which is what an unattended cousin needs, and it means exactly
what it says: the cousin can do anything your account can. Leave it
out to answer permission prompts yourself (in the console's pane view
or `tmux attach -t testa`); Claude Code may also ask you once to
confirm the mode. `{model}`, `{effort}` and `{session_id}` are filled
per start (see `docs/configuration.md`).

The Claude Code preset for `config/harness.toml`:

```
cp config/harness.toml.claude-code.example config/harness.toml
```

Without this file the console's token view says `config/harness.toml
absent`, and these are all off: token counts, transcript mining at
flip, the transcript-size guard that flips a cousin before its
transcript grows unbounded, the harness auto-memory search collection,
`cousin-mcp approve`, the `{model}`/`{effort}` defaults, and the
"needs attention" flag for a pane parked on the login menu.

## 5. Optional: semantic search with Ollama

Search is keyword-only until `config/embedding.toml` exists. For
meaning-based search with a local Ollama:

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

Disk: the installer takes about 2.4 GB even on a CPU-only machine (it
ships its GPU runtimes regardless), plus about 260 MB for the model.
`timeout_s = 120` is deliberate: on a CPU without AVX one 560-character
chunk took 32 seconds to embed, and a timeout shorter than one chunk
makes every search fall back to keyword (it says so) after waiting the
full timeout. On a machine with a GPU or a modern CPU, 30 is plenty.

## 6. Create and start the cousin

```
export FRAMEWORK_ROOT="$PWD"
cousin-spawn testa --root "$PWD" --name Testa --role "test cousin" \
    --voice "Plain and helpful." --operator "$USER"
cousin-mcp approve testa                  # trust the home, enable its MCP server
cousin-tool-surface                       # so the first boot is not degraded
cousin-spawn testa --start
```

`--operator` is the name the cousin's `send` tool may reach (use the
name you will chat as); leave it out for a cousin with no operator.
`cousin-mcp approve` records in `~/.claude.json` that the cousin's
home is trusted and its `cousin` MCP server enabled (the Claude Code
installer creates that file; no login is needed for this step). `cousin-spawn <slug>
--start` starts an existing cousin (tmux session plus chat server) and
is a no-op when it is already running. Before creating or starting
anything, spawn checks that tmux and the agent command's executable
resolve, and stops with the reason if either does not.

Inside the checkout, the root defaults to the working directory for
every command you type, so `--root "$PWD"` is optional there; the
exported `FRAMEWORK_ROOT` covers other directories.

## 7. The user units

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
! grep -l '{{' ~/.config/systemd/user/cousin-*   # success prints nothing; a file name means a placeholder was left
systemctl --user daemon-reload
systemctl --user enable --now cousin-loops.service cousin-console.service
systemctl --user enable --now cousin-sweep.timer cousin-tool-surface.timer cousin-chat-watchdog.timer
loginctl enable-linger "$USER"            # units keep running after you log out
cousin-console adduser "$USER"            # now, not later: prompts for a password
```

The console has no authentication from its first start until the
first `cousin-console adduser`: in that window anyone the network
guard admits (loopback and the private ranges; the default bind is
loopback, so in practice anyone on this machine) can use it without
signing in. Its journal line says so with a suffix,
`(auth not configured: cousin-console adduser <name>)`. That is why
the adduser line above follows the `enable` line directly: run it
immediately. Auth is enforced as soon as the users file exists, with
no restart; the suffix stays in the journal line printed at start
until the next restart.

Do not enable `cousin-chat-server@testa.service` here: `--start`
already started that chat server and the watchdog timer supervises it;
a second server on the same port fails to bind and restarts every
five seconds. The unit is the alternative for when you want systemd,
not spawn, to own the server (`systemd/README.md`). The units' PATH
includes `~/.local/bin` (as `%h/.local/bin`), where Claude Code lives.
If `loginctl enable-linger` is refused, run it with `sudo`.

## 8. Console and first chat

```
journalctl --user -u cousin-console.service -n 5
#   -> cousin-console: serving <root> on 127.0.0.1:8600
#      (with the "auth not configured" suffix if it started before adduser)
```

The console binds loopback. Open `http://127.0.0.1:8600/` on the
machine itself, or from another one through a tunnel:

```
ssh -L 8600:127.0.0.1:8600 <user>@<machine>     # then open http://127.0.0.1:8600/
```

For direct LAN access, give the unit a drop-in (re-running step 7 does
not overwrite a drop-in; it would overwrite an edited unit file):

```
mkdir -p ~/.config/systemd/user/cousin-console.service.d
cat > ~/.config/systemd/user/cousin-console.service.d/lan.conf <<'EOF'
[Service]
ExecStart=
ExecStart=%h/cousins-framework/.venv/bin/cousin-console --host 0.0.0.0 --port 8600
EOF
systemctl --user daemon-reload && systemctl --user restart cousin-console
```
 Caution:
that is plain HTTP, so passwords and chat cross the network in clear,
and the network guard admits loopback and the private ranges
(`config/net-allowlist.json` adds more); create the user first, and
put TLS in front (`--secure-cookie`) for anything beyond a trusted
LAN.

Sign in, open `testa`, send a message in its chat. The card should
say running with no "needs attention" line, and the reply arrives in
the thread.

## After a reboot

The units come back on their own (linger). The tmux sessions do not:
nothing restarts an agent until its next flip. Start each cousin
once, and the watchdog then brings its chat server back within ten
minutes:

```
cousin-spawn testa --start
```

## Uninstall

In reverse. Stop the units and the cousin, remove the unit files and
the checkout; the agent and Ollama are separate products with their
own uninstall.

```
systemctl --user disable --now cousin-loops.service cousin-console.service \
    cousin-sweep.timer cousin-tool-surface.timer cousin-chat-watchdog.timer
rm -rf ~/.config/systemd/user/cousin-*   # -r: the LAN drop-in is a directory
rm -f ~/.local/share/systemd/timers/stamp-cousin-*   # the timers' last-run stamps
systemctl --user daemon-reload
systemctl --user reset-failed
tmux kill-session -t testa                # one per cousin
kill "$(cat ~/cousins-framework/cousins/testa/data/chat-server.pid)"
rm -rf ~/cousins-framework                 # the checkout, venv, config, every cousin home
```

`cousins/` holds every cousin's memory and nothing else has a copy:
archive it first if you may want it back (`cousin-backup`, or a tar of
the directory). Turn linger off only if nothing else of yours needs
it: `loginctl disable-linger "$USER"`.

Claude Code: `rm -rf ~/.local/bin/claude ~/.local/share/claude
~/.cache/claude ~/.local/state/claude` (the binary, its versions, the
installer's staging directory and its lock directory), and `~/.claude`
plus `~/.claude.json` if you do not use it otherwise (they hold its
login and every project's transcripts, the cousins' included).

Ollama (its installer creates a system service, a user and a group):

```
sudo systemctl disable --now ollama
sudo rm -f /etc/systemd/system/ollama.service && sudo systemctl daemon-reload
sudo rm -rf /usr/local/bin/ollama /usr/local/lib/ollama /usr/share/ollama
sudo gpasswd -d "$USER" ollama   # the installer adds you to its group
sudo userdel ollama
getent group ollama >/dev/null && sudo groupdel ollama   # userdel usually removed it already
```

The apt packages are ordinary system packages; if nothing else uses
them, remove them together with the dependencies they pulled in (names
for Ubuntu 24.04):

```
sudo apt-get remove -y tmux libevent-core-2.1-7t64 \
    python3-venv python3.12-venv python3-pip-whl python3-setuptools-whl
```
