# Configuration

Everything operator- or install-specific is configuration, read from
`<framework root>/config/`. Every seam is OPTIONAL: absent, the
framework runs with that capability off, and the absence is a no-op or
a stated degradation, never a silent default that becomes somebody's
value. This page lists every file the code reads; a test asserts the
list stays complete, so a new seam cannot ship undocumented.

The `config/` directory does not exist in a fresh checkout - create it
and add only the files you need.

| file | read by | absent means |
|---|---|---|
| `config/agent-cmd` | `cousin-spawn --start`, `cousin-flip` | no agent starts; spawn/flip that need it error with this path named |
| `config/worker-cmd` | `cousin-loops` (worker cousins) | a worker loop stays due and names this path; nothing fires |
| `config/law.md` | boot packet assembly | no framework-law layer; a missing law file is an install matter, never a per-cousin degradation |
| `config/net-allowlist.json` | the network guard (chat server, UI) | loopback + RFC1918 only; the file only ever ADDS CIDRs |
| `config/outbound-filter.json` | the outbound content filter | no extra protected terms; the framework ships no vocabulary of its own |
| `config/embedding.toml` | `cousin-memory search` | keyword search only, silently - nothing was promised |
| `config/shared-reviewers.json` | `cousin-shared` promotion | promotion refuses with remediation - never a defaulted approver |

## Formats

- `agent-cmd`, `worker-cmd`: one command line. `agent-cmd` may carry a
  `{session_id}` placeholder (flip mints and substitutes it);
  `worker-cmd` carries `{prompt}` and `{home}`. The binary and its
  trust posture are yours - the framework hardcodes neither.
- `law.md`: markdown; the framework law every cousin boots with.
- `net-allowlist.json`: `{"allow": ["203.0.113.0/24", ...]}`. Extends
  the defaults; can never remove loopback.
- `outbound-filter.json`: the filter's per-surface additions;
  see the outbound filter module for the shape.
- `embedding.toml`: `url`, `model`, `timeout_s`. The endpoint takes
  `{"model", "prompt"}` and returns `{"embedding": [...]}`; front any
  service with that contract. A configured-but-unreachable service
  degrades to keyword AND says so - it never quietly pretends.
- `shared-reviewers.json`: `{"reviewers": ["name-or-slug", ...]}`. A
  reviewer may never be the proposer; that boundary is enforced at the
  promote site regardless of what this file says.

## The rule these share

No configuration key ships without a consumer, and no consumer reads a
key this page does not list. A dead config key is worse than dead
code: a user sets it and believes something changed. The test suite
holds both halves - the code never reads an undocumented `config/`
file, and the specs never promise a key nothing reads.
