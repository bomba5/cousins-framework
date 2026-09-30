<p align="center">
  <img src="cousin_lib/console_static/icon-512.png" alt="cousins-framework" width="96">
</p>

# cousins-framework

I run a small family of Claude Code agents at home. I call them [cousins](docs/glossary.md#cousin). Each
one has its own name, voice and job, its own memory, and its own chat, and
they keep all of that when a session ends and a new one starts. This repo is
the framework that makes that work.

A cousin is a Claude Code session in tmux, plus:

- a home directory with its identity (`CLAUDE.md`), memory and notes
- a chat store, so you (through the console) and the other cousins can talk to it
- memory that carries over from one session to the next
- jobs, loops and heartbeats, so it can work on a schedule

On top of that there's a web console where you see all of them, chat with
them, watch their terminal, and browse their memory.

## What you need

- Linux with Python 3.11 or newer, `tmux`, `git` and systemd
- [Claude Code](https://claude.com/claude-code), installed and logged in
  before you start. The quick start below does not install it.
- Optional: [Ollama](https://ollama.com) with `nomic-embed-text` for
  semantic memory search. Without it, search is keyword only.

## What it costs, and what it does unattended

Read this before the quick start, because it starts as soon as you finish it.

A cousin is a live Claude Code session. It is woken on a schedule, not only
when you talk to it: a heartbeat every hour by default, and a [flip](docs/glossary.md#flip) once a day
that ends its session and starts a new one. Every wake is a [turn](docs/glossary.md#turn) against your
Claude account, and it keeps happening while you sleep. The console's tokens
page shows what your cousins are actually using; `cousin-loops flips` shows
when each one flips. Both are adjustable, and a cousin can be told never to
flip, but the defaults are on.

The agent also runs with `--dangerously-skip-permissions`, which is what makes
it able to work unattended and means it can do anything your account can do on
that machine. That is the trade this framework asks you to make. If you are not
comfortable with an autonomous agent holding a shell on your box, continuously,
at your expense, this is not for you.

## Quick start

```
sudo apt-get install -y python3-venv tmux git    # Debian/Ubuntu
git clone https://github.com/bomba5/cousins-framework.git ~/cousins-framework
cd ~/cousins-framework
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[mcp]"

# tell the framework how to start Claude Code
printf '%s\n' "$(command -v claude) --dangerously-skip-permissions --model {model} --effort {effort} --session-id {session_id}" > config/agent-cmd
cp config/harness.toml.claude-code.example config/harness.toml

# make your first cousin and start it
cousin-spawn wren --name Wren --role "helps me around the house" \
    --voice "Short, plain and honest." --operator ana
cousin-mcp approve wren
cousin-tool-surface
cousin-spawn wren --start

# open the console
cousin-console adduser ana
cousin-console --port 8600
```

Then go to `http://localhost:8600`, log in, and say hi to Wren.

That's the short version. The full one, with systemd units, the LAN setup
and how to remove it all again, is in [install](docs/install.md).

## Where to go next

- [Install](docs/install.md) - the full setup, updates, uninstall
- [The console](docs/console.md) - every page of the web UI
- [Cousins](docs/cousins.md) - making them, starting them, how a cousin
  survives a new session
- [Memory](docs/memory.md) - what a cousin remembers and how
- [Chat](docs/chat.md) - talking to cousins, and cousins talking to each
  other
- [Telegram](docs/telegram.md) - a cousin's chat on your phone: the bridge and its setup
- [Meetings](docs/meetings.md) - a chat with several cousins at once, in rounds
- [Jobs and loops](docs/jobs-and-loops.md) - background work and schedules
- [MCP tools](docs/mcp.md) - the tools a cousin gets
- [Media](docs/media.md) - images, video and voice, if you want them
- [Remote cousins](docs/remote-cousins.md) - a cousin on another machine,
  like a Pi on your desk
- [Runners](docs/reference/runners.md) - the agent loops a cousin can run on
  (the Claude Agent SDK, a tmux pane, opencode), how to pick one, and the
  contract table
- [Configuration](docs/configuration.md) - every config file and key
- [Commands](docs/commands.md) - every `cousin-*` command
- [Operations](docs/operations.md) - running it day to day, and fixing it
- [Migrating](docs/migrating.md) - moving an existing cousin in
- [Development](docs/development.md) - hacking on the framework

The dense stuff (every API route, the loop model, the boot sequence) is in
[docs/reference](docs/reference/).

## License

Apache 2.0, see [LICENSE](LICENSE).
