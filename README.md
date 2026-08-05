# cousin-framework

A framework for persistent, co-located AI agents ("cousins"): durable
identity across sessions, layered memory, lifecycle machinery, and
inter-agent chat.

**Status: pre-release extraction in progress.** This repository is being
built by rewriting a private framework in the open, file by file, behind a
contamination gate. The gate came first on purpose: it has run on every
commit since the first one.

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
