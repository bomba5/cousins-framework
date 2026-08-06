# cousin-framework

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
contract under `docs/`. Nothing here needs a third-party dependency.

| subsystem | CLI | contract |
|---|---|---|
| create a cousin from the one template | `cousin-spawn` | `docs/spawn-and-template-spec.md` |
| per-cousin chat server + inter-cousin chat | `cousin-chat-server`, `cousin-chat`, `cousin-reply` | `docs/chat-server-spec.md` |
| durable memory, decisions, keyword+semantic search | `cousin-memory` | `docs/memory-tiers.md` |
| reviewed shared-memory tier | `cousin-shared` | `docs/memory-tiers.md` |
| session boundary: boot packets and flip | `cousin-flip` | `docs/lifecycle-spec.md` |
| the reviewed identity layer | `cousin-self-portrait` | `docs/lifecycle-spec.md` |
| recurring work: heartbeats, loops, timed flips | `cousin-loops`, `cousin-schedule`, `cousin-cycle` | `docs/loops-spec.md` |
| job + sub-agent tracking | `cousin-job` | - |
| the web console (a view, never a source of truth) | `cousin-ui` | `docs/ui-spec.md` |
| the contamination gate | `cousin-gate` | `docs/gate.md` |

Two cross-cutting contracts shape the rest: `docs/operator-interface.md`
(the operator is a subsystem, and every surface works without one) and
the rule the whole design serves - the same one the web console states
in one line: a component that dies loses nothing that was not already
in a store some other component owns. Every optional install seam is
listed in `docs/configuration.md`.

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
memory where the tools index it, search finds what you wrote.

Search is keyword-first and needs nothing installed. To add the
optional semantic leg, point `config/embedding.toml` at any HTTP
embedding service (`url`, `model`, `timeout_s`; the endpoint takes
`{"model", "prompt"}` and returns `{"embedding": [...]}`). As one
non-normative example, a local Ollama exposes that contract at
`http://localhost:11434/api/embeddings` with a model such as
`nomic-embed-text`. If the configured service is unreachable, search
degrades to keyword and SAYS SO - it never quietly pretends. To run
the cousin as a live agent, put the command line that starts your
agent in `config/agent-cmd` and pass `--start`; its chat server then
serves the ports in `cousins/*/cousin.toml`. To watch the fleet in a
browser, run `cousin-ui --port 8600` - it renders what the framework
persists and never becomes a source of truth of its own.

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
