# Configuration

Everything operator- or install-specific is configuration, read from
`<framework root>/config/`. Every seam is OPTIONAL: absent, the
framework runs with that capability off, and the absence is a no-op or
a stated degradation, never a silent default that becomes somebody's
value. This page lists every file the code reads; a test asserts the
list stays complete, so a new seam cannot ship undocumented.

The `config/` directory does not exist in a fresh checkout - create it
and add only the files you need.

**The framework root** (which contains `cousins/`, `config/`, and
`templates/`) is named the same way by every entry point that needs
it: an explicit `--root` flag wins, else the `FRAMEWORK_ROOT`
environment variable, else a loud error naming both. `cousin-spawn`
and `cousin-ui` take it identically; a flag is discoverable from
`--help`, the env var suits a service unit.

| file | read by | absent means |
|---|---|---|
| `config/agent-cmd` | `cousin-spawn --start`, `cousin-flip` | no agent starts; spawn/flip that need it error with this path named |
| `config/worker-cmd` | `cousin-loops` (worker cousins) | a worker loop stays due and names this path; nothing fires |
| `config/law.md` | boot packet assembly | no framework-law layer; a missing law file is an install matter, never a per-cousin degradation |
| `config/net-allowlist.json` | the network guard (chat server, UI) | loopback + RFC1918 only; the file only ever ADDS CIDRs |
| `config/outbound-filter.json` | the outbound content filter | no extra protected terms; the framework ships no vocabulary of its own |
| `config/embedding.toml` | `cousin-memory search`, proactive recall in the chat server | keyword search only, silently - nothing was promised; proactive recall keeps every keyword hit and uses the default `[recall]` thresholds |
| `config/harness.toml` | transcript mining at flip, the harness auto-memory search collection | both off, said once at flip |
| `config/shared-reviewers.json` | `cousin-shared` promotion | promotion refuses with remediation - never a defaulted approver |
| `config/media.toml` | `cousin-image`/`cousin-voice`/`cousin-video` | media generation is off; the CLIs refuse naming this file, nothing leaves the box |

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
  Optional search keys: `chunk_chars` (default 2000) and
  `chunk_overlap` (default 200) split long files into overlapping
  chunks before embedding. Optional `[recall]` table for proactive
  recall in the chat server: `min_chars` (default 24, shorter
  operator messages are not searched), `min_score` (default 0.45,
  the semantic similarity a hit needs to be mentioned; keyword-only
  hits under a configured seam are never mentioned, and without the
  seam every keyword hit is), `top` (default 3, the most hits one
  message may surface). Thresholds live here, not in code; a
  per-cousin opt-out is `cousin.toml [memory] proactive_recall =
  false` (see `docs/chat-server-spec.md`).
- `harness.toml`: `transcripts_dir`, `auto_memory_dir`, each a path
  template with `{home}` (the cousin home) and `{home_encoded}` (the
  harness's project-dir encoding of it: every `/` becomes `-`, so
  `/a/b` is `-a-b`). `transcripts_dir` is where the harness writes a
  session's transcript; `auto_memory_dir` is the harness's own memory
  directory for that cousin. A key left out is None, never a guessed
  location. Unparsable is loud: the file promised something.
- `shared-reviewers.json`: `{"reviewers": ["name-or-slug", ...]}`. A
  reviewer may never be the proposer; that boundary is enforced at the
  promote site regardless of what this file says.
- `media.toml`: per-kind sections `[image]` / `[voice]` / `[video]`,
  each `url`, `model`, optional `key_file`, `timeout_s`. See
  `docs/media-spec.md`. A configured provider or an inert refusal -
  never a silent reroute to a vendor you did not choose.

## The rule these share

No configuration key ships without a consumer, and no consumer reads a
key this page does not list. A dead config key is worse than dead
code: a user sets it and believes something changed. The test suite
holds both halves - the code never reads an undocumented `config/`
file, and the specs never promise a key nothing reads.
