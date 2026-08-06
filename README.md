# cousin-framework

A framework for persistent, co-located AI agents ("cousins"): durable
identity across sessions, layered memory, lifecycle machinery, and
inter-agent chat.

**Status: pre-release extraction in progress.** This repository is being
built by rewriting a private framework in the open, file by file, behind a
contamination gate. The gate came first on purpose: it has run on every
commit since the first one.

## Quickstart: a cousin with memory, from a cold clone

```
git clone <this repo> cousin-framework && cd cousin-framework
pip install -e .

# Create a cousin. --root is the checkout itself (it holds templates/).
cousin-spawn testa --root . --name Testa --role "test cousin" \
    --voice "Plain and helpful."
#   -> created testa at ./cousins/testa (chat port 8090)

# Give it a memory, then find it again.
export COUSIN_HOME=$PWD/cousins/testa
echo "The espresso machine descales every 200 shots." \
    > cousins/testa/memory/upkeep.md
cousin-memory search "espresso descale"
#   -> 1. [memory] .../memory/upkeep.md  "The [espresso] machine..."
```

That is the core loop: spawn from the template (the single identity
source - an unfilled voice is a spawn failure, not a TODO), write
memory where the tools index it, search finds what you wrote. To run
the cousin as a live agent, put the command line that starts your
agent in `config/agent-cmd` and pass `--start`; its chat server then
serves the ports in `cousins/*/cousin.toml`. The docs directory
carries the contracts: `spawn-and-template-spec.md`,
`chat-server-spec.md`, `lifecycle-spec.md`, `memory-tiers.md`.

## The gate

`cousin-gate` scans a tree for content that must never be public: denylisted
terms (word-boundary, position-classified), private address literals,
absolute home paths, secret-shaped strings, and opaque binaries. The
denylist itself never lives in a repository - see `docs/gate.md`.

```
python3 -m unittest discover -s tests   # the suite includes the self-gate
cousin-gate --root . --denylist /path/outside/any/tree
```

Zero third-party dependencies; Python 3.11+.

## License

Apache-2.0. Authored by Jhonata Poma-Hansen.
