<p align="center">
  <img src="cousin_lib/console_static/icon-512.png" alt="cousins-framework" width="96">
</p>

# cousins-framework

## Why "cousins"

In Sicily families are big. Not "two kids and a dog" big. Big. And in a big
family there is always a relative who is strangely good at one thing: the
uncle who can hear what's wrong with an engine, the aunt who knows which
doctor to see, the [cousin](docs/glossary.md#cousin) who fixes your laptop
and judges you for what's on it.

So when you tell a Sicilian you have a problem, you rarely get advice. You
get a phone number. "You have that issue? You should ask my cousin."

And "cousin" is a generous word. The friend you grew up with is a cousin.
The guy who helped you move flats fifteen years ago is a cousin. Nobody
checks the family tree.

That is the whole idea of this project. Instead of one AI assistant
pretending to know everything, I run a small family of them at home. Each
[cousin](docs/glossary.md#cousin) has a name, a job and a voice of its own:
one is the generalist I talk to every day, one knows PCBs and datasheets,
one cooks. Each keeps its own memory, so it is the same cousin tomorrow as
it was today. And when a question belongs to someone else, it does what any
good cousin does: it asks the one who knows.

This repo is the framework underneath them: the memory, the chat, the
schedules, the web console, and the plumbing that lets a cousin survive the
end of its session and wake up as itself.

## What a cousin is

A cousin is an agent session (the Claude Agent SDK, or opencode on other
models), plus:

- a home directory with its identity (`CLAUDE.md`), memory and notes
- a chat store, so you (through the console) and the other cousins can talk to it
- memory that carries over from one session to the next
- jobs, loops and heartbeats, so it can work on a schedule
- **plugins**: add tools, a background service and a console page to an install without touching the framework ([plugins](docs/plugins.md))

On top of that there's a web console where you see all of them, chat with
them, watch them work, and browse their memory.

## What you need

- Docker with the compose plugin, and git. Or, for a bare host: Linux with
  Python 3.11 or newer, git and systemd.
- A model to run on. A Claude account is optional: on opencode's free models
  a cousin needs no key and no account at all. For Claude, an Anthropic API
  key or a Claude login ([Claude Code](https://claude.com/claude-code),
  logged in, on a bare host).
- Optional: [Ollama](https://ollama.com) with `nomic-embed-text`, for
  semantic memory search. Docker runs a bundled one beside the framework and
  pulls both on the first start (a 3.8 GB image and a 274 MB model); to use
  your own Ollama or none, `compose.own-ollama.yml`
  ([install](docs/install.md#your-own-ollama-or-none)). Without it, search is
  keyword only.

## What it costs, and what it does unattended

Read this before the quick start, because it starts as soon as you finish it.

A cousin is a live agent session. It is woken on a schedule, not only
when you talk to it: a heartbeat every hour by default, and a [flip](docs/glossary.md#flip) once a day
that ends its session and starts a new one. Every wake is a [turn](docs/glossary.md#turn) against the
account it runs on, and it keeps happening while you sleep. The console's tokens
page shows what your cousins are actually using; `cousin-loops flips` shows
when each one flips. Both are adjustable, and a cousin can be told never to
flip, but the defaults are on.

The agent also runs every tool without asking, which is what makes it able
to work unattended and means it can do anything its user can do on that
machine (in the container, on Docker). That is the trade this framework asks
you to make. If you are not comfortable with an autonomous agent holding a
shell on your box, continuously, this is not for you.

## Claude logins and Anthropic's terms

A cousin can run on a Claude subscription login, but Anthropic's terms do not
clearly allow it: they tell products built on the Agent SDK to use API keys,
and Anthropic may enforce that without notice. If you run cousins on your
subscription, the risk to that account is yours. This project is not
affiliated with or endorsed by Anthropic. An API key or opencode avoids the
question; [Claude logins and Anthropic's terms](docs/terms-risk.md) has the
details.

## Quick start: Docker, no key, no Claude account

```
git clone https://github.com/bomba5/cousins-framework.git
cd cousins-framework
docker compose up -d --build                     # opencode, and semantic search
docker compose exec framework cousin-console adduser ana

# an opencode account on its free models: no key
docker compose exec -T framework sh -c 'cat >> config/accounts.toml' <<'EOF'
[accounts.zen]
kind = "opencode"
providers = ["opencode"]
EOF

# make your first cousin on it and start it
docker compose exec -T framework cousin-spawn wren --name Wren \
    --role "helps me around the house" --voice "Short, plain and honest." \
    --operator ana --runner opencode --account zen \
    --model opencode/big-pickle --start
```

Then go to `http://127.0.0.1:8600`, log in as `ana`, and say hi to Wren.
Most free models let their vendor train on what you send; the
[install](docs/install.md#a-first-cousin-on-opencodes-free-model) page says
more, and covers a Claude key or login too.

## Quick start: bare host, Claude Code

With Claude Code installed and logged in (`claude auth login`):

```
sudo apt-get install -y python3-venv git    # Debian/Ubuntu
git clone https://github.com/bomba5/cousins-framework.git ~/cousins-framework
cd ~/cousins-framework
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[mcp,sdk]"
cp config/harness.toml.claude-code.example config/harness.toml
cousin-console adduser ana

# the supervisor: the console on 127.0.0.1:8600, the loops daemon and every
# cousin; keep it running (a second terminal, or systemd as in install)
cousin-supervisor run &

# make your first cousin and start it
cousin-spawn wren --name Wren --role "helps me around the house" \
    --voice "Short, plain and honest." --operator ana
cousin-mcp approve wren
cousin-tool-surface
cousin-spawn wren --start
```

Then go to `http://127.0.0.1:8600`, log in, and say hi to Wren.

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
- [Plugins](docs/plugins.md) - tools, a service and a console tab the framework runs but does not ship
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
