# cousins-framework

A framework for persistent, co-located AI agents ("cousins"): durable
identity across sessions, layered memory, lifecycle machinery, and
inter-agent chat.

**Status: v1 feature-complete, pre-release.** Extracted from a private
framework by rewriting it in the open, file by file, behind a
contamination gate that has run on every commit since the first one.
Every subsystem below is implemented, tested, and specified; what
remains before a tagged release is broader real-world exercise.

## What is in it

Each subsystem is a `cousin_lib` module with its own CLI and a
contract under `docs/`. Nothing here needs a third-party dependency;
serving the tool surface over MCP is the one optional extra.

| subsystem | CLI | contract |
|---|---|---|
| create a cousin from the one template | `cousin-spawn` | `docs/spawn-and-template-spec.md` |
| per-cousin chat server + inter-cousin chat | `cousin-chat-server`, `cousin-chat`, `cousin-reply` | `docs/chat-server-spec.md` |
| durable memory, decisions, keyword+semantic search | `cousin-memory` | `docs/memory-tiers.md` |
| reviewed shared-memory tier | `cousin-shared` | `docs/memory-tiers.md` |
| session boundary: boot packets and flip | `cousin-flip` | `docs/lifecycle-spec.md` |
| the reviewed identity layer | `cousin-self-portrait` | `docs/lifecycle-spec.md` |
| identity surgery: reincarnate a role, transplant memory or body | `cousin-reincarnate`, `cousin-transplant` | `docs/lifecycle-surgery.md` |
| recurring work: heartbeats, loops, timed flips | `cousin-loops`, `cousin-schedule`, `cousin-cycle` | `docs/loops-spec.md` |
| job + sub-agent tracking | `cousin-job` | - |
| in-flight work tracker, framework-wide (domain, state, tags, owner) | `cousin-tracker` | `docs/tracker-spec.md` |
| media generation (image/voice/video), configurable provider | `cousin-image`, `cousin-voice`, `cousin-video` | `docs/media-spec.md` |
| Telegram bridge, per-cousin configurable | `cousin-telegram` | `docs/telegram-spec.md` |
| hive: cross-machine cousins over an authed bus, and the copy-over node archive | `cousin-hive`, `cousin-spawn-node` | `docs/hive-spec.md`, `docs/deploying-a-node.md` |
| the web console (a view, never a source of truth) | `cousin-console` (`cousin-ui` is its retired alias) | `docs/ui-spec.md` |
| the console's API contract: routes, live streams, auth, what was dropped | `cousin-console` | `docs/console-spec.md` |
| the contamination gate | `cousin-gate` | `docs/gate.md` |
| unattended operation: units, the fleet sweep, the tool-surface manifest | `cousin-sweep`, `cousin-tool-surface`, `systemd/` | `docs/operations.md` |
| the same CLIs as tools over MCP stdio, provisioned at spawn | `cousin-mcp` | `docs/mcp-spec.md` |

Two cross-cutting contracts shape the rest: `docs/operator-interface.md`
(the operator is a subsystem, and every surface works without one) and
the rule the whole design serves - the same one the web console states
in one line: a component that dies loses nothing that was not already
in a store some other component owns. Every optional install seam is
listed in `docs/configuration.md`.

To install on a new machine, follow `docs/install.md` top to bottom.
For a narrated walk through every feature with worked examples, see
`docs/guide.md`. To run it unattended (systemd unit templates, the
daily flip, backups, the weekly sweep, what to check when a cousin goes
quiet), see `docs/operations.md`.

The framework is being brought to parity with the private one it was
extracted from, phase by phase; the plan and its results log are in
`docs/port-plan.md`; moving a cousin over by hand is `docs/migration-runbook.md`.

## Quickstart: a cousin with memory, from a cold clone

Prerequisites: Python >= 3.11, `python3-venv`, `tmux` and `git`. On
stock Ubuntu 24.04 (which refuses `pip install` outside a venv):

```
sudo apt-get update && sudo apt-get install -y python3-venv tmux git
```

The repository may be private: cloning it needs GitHub access (a
deploy key or a token).

```
git clone <this repo> cousins-framework && cd cousins-framework
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[mcp]"                     # not on PyPI: install from the checkout
python3 -m unittest discover -s tests       # before any cousin exists; ends in OK

# Create a cousin. The root is the checkout itself (it holds templates/);
# pass it absolute. Inside the checkout it is also the default.
cousin-spawn testa --root "$PWD" --name Testa --role "test cousin" \
    --voice "Plain and helpful."
#   -> created testa at <checkout>/cousins/testa (chat port 8090)

# Give it a memory, then find it again.
export COUSIN_HOME=$PWD/cousins/testa
echo "The espresso machine descales every 200 shots." \
    > cousins/testa/memory/upkeep.md
cousin-memory search "espresso descale"
#   -> 1. [memory] .../memory/upkeep.md  "The [espresso] machine..."
```

Later shells need the venv again (`. .venv/bin/activate`, or
`.venv/bin` on PATH). That is the core loop: spawn from the template
(the single identity source - an unfilled voice is a spawn failure,
not a TODO), write memory where the tools index it, search finds what
you wrote.

**The full install** - the agent, its login, semantic search, the
user units, the console and its first user, a cousin answering chat,
and the uninstall - is one ordered procedure in `docs/install.md`.
The short version of each piece:

- **Live agent.** The framework starts whatever `config/agent-cmd`
  names and writes Claude Code's project files for each cousin. Install
  Claude Code (`curl -fsSL https://claude.ai/install.sh | bash`), log
  in once interactively (`~/.local/bin/claude`, or `claude auth
  login`) before any cousin starts, then:

  ```
  printf '%s\n' "$HOME/.local/bin/claude --dangerously-skip-permissions --model {model} --effort {effort} --session-id {session_id}" > config/agent-cmd
  cp config/harness.toml.claude-code.example config/harness.toml
  cousin-mcp approve testa                 # trust the home, enable its MCP server
  cousin-spawn testa --start               # starts an existing cousin
  ```

  The permission flag lets an unattended cousin run every tool without
  asking; leave it out to answer prompts yourself. `harness.toml`
  turns on token counts, transcript mining at flip, the
  transcript-size guard, `cousin-mcp approve`, the `{model}`/`{effort}`
  defaults and the console's "needs attention" flag for a pane stuck at
  the login menu; without it the console's token view says
  `config/harness.toml absent`. `--start` checks tmux and the agent
  executable before it creates or starts anything.
- **Semantic search.** Search is keyword-first and needs nothing
  installed. For the semantic leg, point `config/embedding.toml` at
  an HTTP embedding service (`url`, `model`, `timeout_s`; the endpoint
  takes `{"model", "prompt"}` and returns `{"embedding": [...]}`). With
  a local Ollama: `curl -fsSL https://ollama.com/install.sh | sh`,
  `ollama pull nomic-embed-text`, and copy `config/embedding.toml.example`
  with `url = "http://localhost:11434/api/embeddings"` and
  `timeout_s = 120`. Budget about 2.4 GB of disk for Ollama even
  CPU-only; the long timeout is for CPUs without AVX, where one chunk
  took 32 s. An unreachable service degrades search to keyword and
  SAYS SO - it never quietly pretends.
- **Console.** `cousin-console --port 8600` serves the fleet view on
  loopback; it renders what the framework persists and never becomes a
  source of truth. Create a login first with `cousin-console adduser
  <name>` (from inside the checkout, or with `--root <checkout>`). For
  LAN access run it with `--host 0.0.0.0` (plain HTTP: only on a
  trusted LAN, TLS in front otherwise); or keep loopback and tunnel:
  `ssh -L 8600:127.0.0.1:8600 <user>@<machine>`.
- **Unattended.** `systemd/` ships user-unit templates (loops daemon,
  console, sweep, tool surface, chat watchdog); `docs/install.md`
  step 7 installs them, with `loginctl enable-linger "$USER"`.

## The gate

`cousin-gate` scans a tree for content that must never be public: denylisted
terms (word-boundary, position-classified), private address literals,
absolute home paths, secret-shaped strings, and opaque binaries. The
denylist itself never lives in a repository - see `docs/gate.md`.

```
python3 -m unittest discover -s tests   # the suite includes the self-gate
cousin-gate --root . --denylist /path/outside/any/tree
```

`cousin-gate --git-visible` scans only what git would publish, so a
checkout that also hosts a live install (gitignored `cousins/`,
`config/`) can be gated in place; the suite's self-gate does exactly
that.

Zero third-party dependencies; Python 3.11+. The one optional extra,
`pip install -e ".[mcp]"` from the checkout, is needed only to serve
MCP.

## License

Apache-2.0. Authored by Jhonata Poma-Hansen.
