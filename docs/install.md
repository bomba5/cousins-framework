# Install

There are two ways to install, and this page covers both, and how to take
each back out. [Install with Docker](#install-with-docker) runs the whole
framework in one container: the console, the scheduler and every [cousin](glossary.md#cousin), with
all state on one volume. It needs git and Docker and nothing else.
[Install on a bare host](#install-on-a-bare-host) is a checkout with a venv
and systemd user units. Either way every cousin runs on a [runner](glossary.md#runner)
(`cousin-runner`) that `cousin-supervisor` starts: the Claude Agent SDK by
default, or opencode. The example cousin is `wren` and the console user
is `ana`.

A Claude account is optional. A cousin on opencode's free models needs no
key and no account of any kind; the Docker section starts with that one.

## Install with Docker

You need git and Docker with the compose plugin. I tested with Docker 29 and
compose 5 on Linux.

The default image is 162 MB compressed and about 400 MB on disk. The first
`up` builds it from the checkout: it pulls the `python:3.13-slim` base and
downloads the Agent SDK, whose bundled Claude Code CLI is most of the size.
Nothing else is pulled unless you turn on a profile or the opencode variant
(below). The build's inputs are pinned: the base image by its multi-arch
digest (`python:3.13-slim@sha256:...`, so an arm64 host builds the same
release), every Python package by exact version and sha256
(`docker/requirements.txt`, installed with `--require-hashes`), and the
opencode binary by its sha256. No stage installs a system package, so
nothing is left to float; two builds of one checkout install the same
packages, though the image's own digest differs (timestamps). The
optional `embeddings` service's `ollama/ollama` image is pinned by tag
only. Refreshing the pins: [development](development.md#the-images-pins).

```
git clone https://github.com/bomba5/cousins-framework.git
cd cousins-framework
```

**Pick one lane before the first start.** A cousin needs a way to reach a
model:

- **opencode, with no key and no Claude account.** The opencode variant of
  the image and an `opencode` account. Its free models (OpenCode Zen's, named
  `opencode/<model>`) need no key. The same account kind also takes the key
  of a provider you hold, or a local OpenAI-compatible endpoint (see
  [accounts.toml](configuration.md#accountstoml)). The worked example is
  [A first cousin on opencode's free model](#a-first-cousin-on-opencodes-free-model).
- **An Anthropic API key**, metered:
  [a cousin on a Claude key or login](#a-cousin-on-a-claude-key-or-login).
- **A Claude login**, the same section. The terms risk of running cousins on
  a subscription login, in a container or anywhere else, is yours.

**Every compose command uses the same files.** compose reads `compose.yml`
and, when it exists, `compose.override.yml` by itself, but only when you
give no `-f`. Once you start with `-f` files, give the same `-f` files to
every later command (`up`, `down`, `exec`, `logs`): a plain
`docker compose up -d` after a `-f compose.opencode.yml` start recreates the
container on the default image, which has no opencode binary, and every
opencode cousin then fails to start and keeps failing. The simplest way
out is to put your one override in `compose.override.yml` and never use
`-f` at all, which is what the next section does.

### A first cousin on opencode's free model

From the checkout, with nothing else set up:

```
cp compose.opencode.yml compose.override.yml
docker compose up -d --build
docker compose logs framework
```

The first start prints a checklist of what to edit, and a warning that the
console is open because no user exists yet. Add one now; it asks for the
password twice (at least 8 characters):

```
docker compose exec framework cousin-console adduser ana
```

From a script, with no terminal, pipe the password in instead (`-T`: no
terminal in the container):

```
printf '%s\n%s\n' "$PW" "$PW" | docker compose exec -T framework cousin-console adduser ana
```

Declare an `opencode` account on Zen, with no key (the container's
working directory is the framework root, `/data`, so this is
`/data/config/accounts.toml`):

```
docker compose exec -T framework sh -c 'cat >> config/accounts.toml' <<'EOF'

[accounts.zen]
kind = "opencode"
providers = ["opencode"]
EOF
```

Make the cousin on it and start it:

```
docker compose exec -T framework cousin-spawn wren --name Wren \
    --role "helps me around the house" --voice "Short, plain and honest." \
    --operator ana --runner opencode --account zen \
    --model opencode/big-pickle --start
#   created wren at /data/cousins/wren
#   started wren
docker compose exec -T framework cousin-supervisor status
#   a runner:wren row, running
```

Open `http://127.0.0.1:8600/`, log in as `ana`, open Wren and send it a
message. The reply comes in the [thread](glossary.md#thread).

- `--runner opencode` is required: `compose.yml` sets
  `COUSIN_DEFAULT_RUNNER=sdk` for a cousin that names no runner.
- `--model` is required on opencode, as `<provider>/<model>`; there is no
  default. `opencode/big-pickle` was one of Zen's free models on 2026-10-01;
  Zen's current list is in its documentation (opencode.ai/docs/zen). Most
  free models let the vendor use what the cousin sends to train models;
  [accounts.toml](configuration.md#accountstoml) says which ones keep
  nothing. `--effort` is refused on opencode, which reads no effort.
- One opencode account serves one cousin: the runner holds the account
  while it runs, and a second cousin on the same account is refused at its
  start. Give each opencode cousin its own account (`[accounts.zen2]`, the
  same three lines).
- The console's spawn dialog does the same: pick the `opencode` kind, the
  `zen` account and type the model.
- Nothing here costs money, but the cousin still wakes on a schedule (a
  heartbeat every hour, a [flip](glossary.md#flip) once a day), like every cousin.

### A cousin on a Claude key or login

For the key lane, put the key in a file beside `compose.yml` and turn on the
key override, once, before the first start:

```
mkdir -p secrets && chmod 700 secrets
(umask 022; printf '%s' "<your key>" > secrets/anthropic_api_key)
cp compose.api-key.yml compose.override.yml
```

compose reads `compose.override.yml` by itself. The key file is mode 644
inside a 700 directory because the container's user (uid 10001) must read
it; on every start the entrypoint copies it to a private file on the volume
and declares an `api-key` account, and new cousins use it. A rotated key is
picked up at the next start. Both `secrets/` and `compose.override.yml` are
in `.gitignore`. To have the opencode variant as well, there are now two
overrides: name all three files on every command,
`docker compose -f compose.yml -f compose.api-key.yml -f compose.opencode.yml ...`.

For the login lane, skip the key and log in inside the container after the
first start (below).

**Start it.**

```
docker compose up -d
docker compose logs framework
```

The first start prints a checklist of what to edit, and a warning that the
console is open because no user exists yet. Add one now (it asks for the
password twice; the piped form above works here too):

```
docker compose exec framework cousin-console adduser ana
```

For the login lane, declare a login account and log it in; the sign-in URL is
printed in your terminal and you paste back the code:

```
docker compose exec -T framework sh -c 'cat >> config/accounts.toml' <<'EOF'
[accounts.mine]
kind = "claude-login"
EOF
docker compose exec framework cousin-account login mine
```

Then make it the default account for new cousins, in `compose.override.yml`
(create the file if you don't have one), and apply it with
`docker compose up -d`:

```
services:
  framework:
    environment:
      COUSIN_DEFAULT_ACCOUNT: mine
```

Its credentials stay on the volume, under `data/accounts/mine`.

**Spawn a cousin.** Open `http://127.0.0.1:8600/`, log in, and spawn one from
the console. The spawn dialog has a kind and an account field, and the
container's environment only preselects them: `compose.yml` sets
`COUSIN_DEFAULT_RUNNER=sdk`, so the kind starts on `sdk`, and
`COUSIN_DEFAULT_ACCOUNT` (the key override, or the line above) is the
account a blank account field gets. "create cousin" creates it and starts
it through the [supervisor](glossary.md#supervisor). Send it a message.

A cousin on `host` (no account named, and no `COUSIN_DEFAULT_ACCOUNT`) has
no login in the container: give it the key or the login account. Or log
`host` itself in, from the directory of `compose.yml`, which is what its
"login required" line says to run:

```
docker compose exec framework cousin-account login host
```

`host` in the container is the image user's own `~/.claude`, on the volume
(`HOME=/data/home`), so the login stays across `down` and `up` and every
cousin on `host` uses it. The line names no host: the image sets
`COUSIN_IN_CONTAINER=1`, and the framework leaves out the hostname, which
in the container is its id.

### Running the container

The console's port is published on loopback only. From another machine, use
an SSH tunnel (`ssh -L 8600:127.0.0.1:8600 <host>`), or publish it on the LAN
once a user exists, in `compose.override.yml`:

```
services:
  framework:
    ports: !override
      - "8600:8600"
```

That is plain HTTP; the notes in
[Reaching the console from the LAN](#reaching-the-console-from-the-lan) apply.

**Semantic search** is the `embeddings` profile: `docker compose --profile
embeddings up -d`, then the steps in the comment in `compose.yml` (pull the
model once, write `config/embedding.toml`). It pulls the Ollama image, several
GB.

**The opencode variant** is the image with the opencode binary added, for
cousins on the opencode runner. It is the Dockerfile's `--target opencode`:
the default image plus opencode 1.18.31, one self-contained binary at
`/opt/opencode/bin/opencode` (on `PATH`, and in `COUSIN_OPENCODE_BIN`), with
no node, no bun and no npm. The build downloads the pinned package from the
npm registry and checks its sha256; only x86-64 is pinned. The image is
223 MB compressed and about 580 MB on disk. The default image never carries
it. Run the framework service on it with the override file:

```
docker compose -f compose.yml -f compose.opencode.yml up -d --build
```

and the same two `-f` files on every later command. Add
`-f compose.api-key.yml` before the last file to keep the key lane, or copy
`compose.opencode.yml` to `compose.override.yml` when it is your only
override (then no `-f` at all). SDK cousins run on it unchanged.

Stop and start with `docker compose down` and `docker compose up -d` (with
your `-f` files, if you use any): every cousin, message and session is on
the `framework-data` volume and survives, and a cousin you stopped stays
stopped until you start it. `docker compose down -v` deletes the volume,
and with it everything. Running it day to day, backups and upgrades are in
[operations](operations.md#the-container).

## Install on a bare host

This takes you from a plain Linux box to a logged-in console with a first
cousin answering chat, and back again. I wrote it against Ubuntu 24.04; any
Linux with the same pieces works with its own package names.

When you're done you have: the checkout (which is also the framework root),
a venv inside it, one cousin under `cousins/wren` on the default `sdk` runner
kind, and a handful of systemd user units that keep running whether or not
you're logged in: `cousin-supervisor`, which runs the console, the loops
daemon and every cousin's runner, and two timers.

### What you need

- **Python 3.11 or newer.** The framework is Python and uses `tomllib`, which
  arrived in 3.11. Ubuntu 24.04 ships 3.12.
- **python3-venv.** Ubuntu won't let pip install into the system Python, so
  the framework lives in a venv.
- **tmux**, only for the `tmux` runner kind, which drives an interactive
  Claude Code in a tmux pane. The default `sdk` kind and `opencode` use no
  tmux.
- **git.** For the clone. The console also reads the commit it's running
  from it.
- **A systemd user manager.** The supervisor and the timers run as user
  units. You can skip systemd and run the commands by hand, but
  then nothing recurring happens while you're away.
- **Claude Code**, to log in. The default runner kind, `sdk`, runs the
  Claude Agent SDK (the `sdk` extra below), which brings its own Claude Code
  CLI. A cousin on the host's login (`host`, the default account) uses the
  login Claude Code keeps in `~/.claude`, so this page installs Claude Code
  to log in once. A cousin on opencode needs no Claude Code (end of step 4).
- **curl.** For the Claude Code and Ollama installers. Usually already there.
- **The `sdk` and `mcp` Python extras**, pulled in by
  `pip install -e ".[mcp,sdk]"`. `sdk` is the Claude Agent SDK the default
  runner kind runs on; without it a cousin on `sdk` fails to start with
  "claude-agent-sdk is not installed". `mcp` is what lets a cousin use its
  tools over MCP. Spawn wires MCP up for every cousin, so install it unless
  you know you won't use it. Everything else in the framework is standard library.
- **Ollama with `nomic-embed-text`** (optional). Gives memory search a
  semantic leg. Without it, search is keyword only.
- **A browser with internet access** for the console. The page loads React,
  Babel, marked, mermaid and xterm from unpkg and jsdelivr. The backend
  fetches nothing.

### 1. System packages

```
sudo apt-get update
sudo apt-get install -y python3-venv tmux git curl
```

Run `apt-get update` first. On a box that hasn't refreshed its package
lists, `python3.12-venv` can 404 on a stale `.deb`.

### 2. Clone and install

```
git clone https://github.com/bomba5/cousins-framework.git ~/cousins-framework
cd ~/cousins-framework
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[mcp,sdk]"
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

### 3. Run the tests

```
python3 -m unittest discover -s tests
```

It takes a few minutes (about ten on a 2-core VM). The summary has to say
`OK` (a few skips are fine); a line or two of test output can print after
it, so look for `OK` near the end rather than on the very last line.
Do it now, before any cousin exists, so a failure is the framework's and not
your install's.

### 4. Claude Code

```
curl -fsSL https://claude.ai/install.sh | bash     # puts claude in ~/.local/bin
~/.local/bin/claude                                 # log in once, then /exit
```

Log in once (interactively, or with `claude auth login`) before a cousin on
`host` starts. A cousin that starts with no login runs no [turn](glossary.md#turn): its runner
says "login required" with the command to run (in the console and
`cousin-chat list`) and waits, as described under Re-login below.

A runner cousin can run on an account of its own instead (see
[configuration](configuration.md#accountstoml)). The first login per account
kind, always started from a shell on the host:

- a cousin on `host`: `claude auth login` once, as the host user (the step
  above);
- a named login account: `cousin-account login <name> --via <any cousin you
  chat with>`; the sign-in URL arrives in that cousin's chat, you sign in on
  any device and reply there with the code;
- a token account: `cousin-account token <name> --via <any cousin you chat
  with>`, the same way;
- a key account: write the key to its secret file yourself, mode 0600, in a
  0700 directory.

`cousin-runner --home cousins/<slug> --check-auth` says whether a cousin's
account is logged in (exit 0, or 4 when it is not), with no model call; an
install script can gate on it. Add `--validate` for one smallest model [turn](glossary.md#turn).

Re-login: when a login expires or is revoked, a key or token stops working, or
an account's billing stops it, the runner says so in the console,
`cousin-chat list` and Telegram, and waits. Run the action it names. It
resumes on its own once the credentials change, or delete
`cousins/<slug>/data/login-required.json` to make it try now (the way out of a
billing stop, where no credential changes).

Then copy the Claude Code preset:

```
cp config/harness.toml.claude-code.example config/harness.toml
```

Without `config/harness.toml`, a lot quietly stays off: transcript mining at
[flip](glossary.md#flip), the harness memory collection in search, the console's token
counts, `cousin-mcp approve`, and the spawn dialog's model and effort
preselection ([configuration](configuration.md#harnesstoml)). There is no
agent command to write: 2.0.0 removed `config/agent-cmd`, and every runner
kind starts its own agent.

A cousin runs every tool without asking (an `sdk` cousin's session runs in
`bypassPermissions`, the `tmux` kind's CLI with
`--dangerously-skip-permissions`), which is what an unattended cousin needs. It means
what it says: the cousin can do anything your user can. What it may not do
is its `policy.toml` ([configuration](configuration.md#policytoml)).

To run a cousin with no Claude login at all, put it on opencode's free
models: the `opencode` binary on `PATH` (or its absolute path in
`COUSIN_OPENCODE_BIN`), the `zen` account from
[the Docker example](#a-first-cousin-on-opencodes-free-model) in
`config/accounts.toml`, and `--runner opencode --account zen --model
opencode/big-pickle` on the `cousin-spawn` line in step 7. This page does
not install opencode.

### 5. Optional: semantic search with Ollama

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

### 6. The systemd units

The supervisor has to run before any cousin can start: `cousin-spawn
--start` and the console's start button both ask it, and with none running
the start fails.

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
systemctl --user enable --now cousin-supervisor.service
systemctl --user enable --now cousin-sweep.timer cousin-tool-surface.timer
loginctl enable-linger "$USER"
```

`cousin-supervisor.service` runs the console (on `127.0.0.1:8600`), the
loops daemon and one runner per cousin. The loop also renders
`cousin-console.service` and `cousin-loops.service`, the same two daemons
as separate units: leave them disabled, never enabled beside the
supervisor (two consoles would serve one root; see
[systemd/README.md](../systemd/README.md)).

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

### 7. Make the first cousin

Before you do: from here on this can cost money and runs unattended. A
cousin is woken on a schedule, not only when you talk to it. The defaults
are a heartbeat every hour and a flip once a day, and every wake is a
[turn](glossary.md#turn) against the account it runs on. The console's tokens page shows what they are
using, `cousin-loops flips` shows when each one flips, and
`context_beat_seconds` and `flip_at` in a cousin's `cousin.toml` change both
(`flip_at = "never"` opts a cousin out of the daily flip entirely). Start with
one cousin until you have seen a day of it.

```
cousin-spawn wren --name Wren --role "helps me around the house" \
    --voice "Short, plain and honest." --operator ana
cousin-mcp approve wren
cousin-tool-surface
cousin-spawn wren --start
```

- `cousin-spawn` creates `cousins/wren/` with its `cousin.toml`, `CLAUDE.md`
  from the template, `STATUS.md`, `MEMORY.md`, the MCP registry and
  `.mcp.json`. `--role` and `--voice` are required. `--operator` is the
  name you'll chat as; the cousin's `send` tool can reach that name. Leave it
  out for a cousin with no operator. With no `--runner` and no `--account`
  it is an `sdk` cousin on `host`.
- `cousin-mcp approve` marks the home as trusted in `~/.claude.json` and
  enables the cousin's `cousin` MCP server, so Claude Code doesn't stop on
  its trust prompt. The file exists once Claude Code has run once.
- `cousin-tool-surface` writes `data/tool-surface.md`, which the boot packet
  quotes. Without it the first boot is marked degraded. The daily timer keeps
  it fresh after this.
- `cousin-spawn wren --start` asks the running supervisor to start Wren's
  runner. On a cousin that's already running it does nothing. With no
  supervisor running it fails (exit 1) and the home is kept: start the
  supervisor (step 6) and run it again.

More on all of this in [cousins](cousins.md).

### 8. Open the console

```
journalctl --user -u cousin-supervisor.service -n 20
#   console | cousin-console: serving <root> on 127.0.0.1:8600
```

The supervisor prefixes each child's lines with its name (`console`,
`loops`, `runner:wren`). If the console's line ends with
`(auth not configured: cousin-console adduser <name>)`, the console started
before the user existed. Login is enforced anyway; the suffix only goes away
on the next restart. On a machine that had the console before, the journal
can also show lines from earlier runs: look at the newest.

Open `http://127.0.0.1:8600/` on the machine itself, or tunnel from another
one:

```
ssh -L 8600:127.0.0.1:8600 ana@192.0.2.10     # then open http://127.0.0.1:8600/
```

Log in, open Wren and send a message. The card should say running, and the
reply shows up in the [thread](glossary.md#thread).

### Reaching the console from the LAN

Give the supervisor's unit a drop-in. Re-running step 6 overwrites the unit
files but leaves drop-ins alone.

```
mkdir -p ~/.config/systemd/user/cousin-supervisor.service.d
cat > ~/.config/systemd/user/cousin-supervisor.service.d/lan.conf <<'EOF'
[Service]
ExecStart=
ExecStart=%h/cousins-framework/.venv/bin/cousin-supervisor run --console-host 0.0.0.0 --console-port 8600
EOF
systemctl --user daemon-reload && systemctl --user restart cousin-supervisor
```

Restarting the supervisor restarts every running cousin too; each resumes
its session.

This is plain HTTP, so passwords and chat cross the network in the clear.
Create the user first. The network guard only lets in loopback and the
private ranges (10/8, 172.16/12, 192.168/16); `config/net-allowlist.json`
adds more. For anything beyond a LAN you trust, put TLS in front. The
supervisor hands its console only the host and the port, so a console with
`--secure-cookie` runs as its own unit, `cousin-console.service`, beside a
supervisor started with `--no-console` (see
[systemd/README.md](../systemd/README.md)).

Cousins on other machines need the console reachable too, since their nodes
call it. That's off until `config/hive.toml` turns it on; see
[remote cousins](remote-cousins.md).

### After a reboot

The units come back on their own (that's what linger is for), and the
supervisor starts every cousin with it, each resuming its session, except
one you stopped (it stays stopped until you start it) or one with
`[agent] auto_start = false`. Start those from the console's start button
or:

```
cousin-spawn wren --start
```

### Update

```
cd ~/cousins-framework
git pull
. .venv/bin/activate
pip install -e ".[mcp,sdk]"      # picks up new commands; harmless otherwise
cousin-tool-surface               # the timer would do it by 06:00, this is now
systemctl --user daemon-reload
systemctl --user restart cousin-supervisor.service
```

The `pip install` matters when the update adds a new `cousin-*` command:
an editable install only creates wrappers for the commands it knew about.
If `systemd/` changed in the pull, re-run the `sed` loop from step 6 before
the `daemon-reload`.

Restarting the supervisor restarts the console, the loops daemon and every
running cousin, each resuming its session. To restart only the console or
the loops daemon, ask the supervisor: `cousin-supervisor stop --name console
&& cousin-supervisor start --name console` (or `--name loops`); the cousins
keep running. Running cousins keep the old code until they're restarted or
flipped. A flip
picks up everything new; see [cousins](cousins.md). The console's top bar
shows the version and commit the console process is running, so a pull
without a restart is visible there. (In the container the commit is blank:
the image carries no `.git`.)

An install upgraded from 1.x still has the old chat server units. 2.0.0 runs
no chat server, so disable them once, for each slug that had one, and delete
their files from `~/.config/systemd/user/`:

```
systemctl --user disable --now cousin-chat-watchdog.timer cousin-chat-server@<slug>.service
```

If you move the checkout to another path, the cousins' Claude Code settings
still point at the old one. Fix each with `cousin-spawn <slug>
--repair-settings`, then `cousin-mcp approve <slug>` again.

### Uninstall

The reverse, in order. Back up first if you might want the cousins again:
`cousins/` holds all their memory and nothing else has a copy (see
[operations](operations.md#backups)).

```
systemctl --user disable --now cousin-supervisor.service \
    cousin-loops.service cousin-console.service \
    cousin-sweep.timer cousin-tool-surface.timer
rm -rf ~/.config/systemd/user/cousin-*        # -r: the LAN drop-in is a directory
rm -f ~/.local/share/systemd/timers/stamp-cousin-*   # the timers' last-run stamps
systemctl --user daemon-reload
systemctl --user reset-failed

# the tmux kind's panes, if any cousin ran on it (its own tmux socket)
tmux -S ~/cousins-framework/run/tmux.sock kill-server 2>/dev/null

rm -rf ~/cousins-framework     # checkout, venv, config, every cousin home
```

Turn linger off only if nothing else of yours needs it:
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
