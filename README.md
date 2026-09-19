# cousins-framework

I run a small family of Claude Code agents at home. I call them cousins. Each
one has its own name, voice and job, its own memory, and its own chat, and
they keep all of that when a session ends and a new one starts. This repo is
the framework that makes that work.

A cousin is a Claude Code session in tmux, plus:

- a home directory with its identity (`CLAUDE.md`), memory and notes
- a small chat server, so you (and the other cousins) can talk to it
- memory that carries over from one session to the next
- jobs, loops and heartbeats, so it can work on a schedule

On top of that there's a web console where you see all of them, chat with
them, watch their terminal, and browse their memory.

## What you need

- Linux with Python 3.11 or newer, `tmux`, `git` and systemd
- [Claude Code](https://claude.com/claude-code), logged in once
- Optional: [Ollama](https://ollama.com) with `nomic-embed-text` for
  semantic memory search. Without it, search is keyword only.

## Quick start

```
git clone https://github.com/bomba5/cousins-framework.git
cd cousins-framework
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[mcp]"

# tell the framework how to start Claude Code
printf '%s\n' "$HOME/.local/bin/claude --dangerously-skip-permissions --model {model} --effort {effort} --session-id {session_id}" > config/agent-cmd
cp config/harness.toml.claude-code.example config/harness.toml

# make your first cousin and start it
cousin-spawn wren --name Wren --role "helps me around the house" \
    --voice "Short, plain and honest." --operator ana
cousin-mcp approve wren
cousin-spawn wren --start

# open the console
cousin-console adduser ana
cousin-console --port 8150
```

Then go to `http://localhost:8150`, log in, and say hi to Wren.

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
- [Meetings](docs/meetings.md) - a chat with several cousins at once, in rounds
- [Jobs and loops](docs/jobs-and-loops.md) - background work and schedules
- [MCP tools](docs/mcp.md) - the tools a cousin gets
- [Media](docs/media.md) - images, video and voice, if you want them
- [Remote cousins](docs/remote-cousins.md) - a cousin on another machine,
  like a Pi on your desk
- [Configuration](docs/configuration.md) - every config file and key
- [Commands](docs/commands.md) - every `cousin-*` command
- [Operations](docs/operations.md) - running it day to day, and fixing it
- [Migrating](docs/migrating.md) - moving an existing cousin in
- [Development](docs/development.md) - hacking on the framework

The dense stuff (every API route, the loop model, the boot sequence) is in
[docs/reference](docs/reference/).

## License

Apache 2.0, see [LICENSE](LICENSE).
