# Provenance

Every file in this repository answers three questions: where did it come
from, what changed, and why does it belong in a public framework. Entries
are added as files land.

This ledger is the one place where the project's history legitimately
lives, and it ships publicly, so it carries its own content rule:

- Origin references stay abstract. "The source framework" means the
  private codebase this project is extracted from; nothing here names it
  further - not its people, its machines, or its incidents.
- Entries describe the mechanism of change: what was kept, what was
  replaced, and why the file ships. Not anecdote.
- If explaining an entry seems to require household detail, the
  explanation is wrong, not the rule.

| file | origin | what changed / why it ships |
|---|---|---|
| `cousin_lib/gate/scanner.py` | written fresh | The contamination gate; exists before anything it guards. |
| `cousin_lib/gate/cli.py` | written fresh | CLI for gate and triage modes. |
| `cousin_lib/config.py` | written fresh | The configuration seam. Every hardcoded root, port, and personal default in the source framework becomes a lookup here; operator is optional by design. |
| `cousin_lib/reply.py` | inspected rewrite of the source framework's reply module | Kept: the refuse-without-context rule and JSON body transport (both hard-won). Changed: cousins-directory default and defaulted operator name replaced by configuration (a missing operator is an error, not a fallback human); slug/port sniffing replaced by `COUSIN_HOME`; media attachment flags deferred with the media subsystem. |
| `cousin_lib/outbound_filter.py` | rewrite from the mechanism of the source framework's outbound guard | Kept: the two-tier model (base terms always, per-surface additions), the internal-destination exemption, the explicit override. Changed: all vocabulary, protected slugs, and trusted peers moved to configuration - the framework ships no banned words and trusts nobody by default; a hardcoded trusted-peer slug in the original's control flow became config; matching gained word boundaries. |
| `cousin_lib/chat.py` | inspected rewrite of the source framework's peer-chat module | Kept: the bidirectional peer-visibility gate with its fail-open registry read, self-send refusal, filter-before-wire ordering. Changed: target resolution is filesystem-only (a service registry required a service to be running); root and identity come from configuration; media attachments and CLI trace wiring deferred with their subsystems. |
| `docs/chat-server-spec.md` | authored from a full behavioral inventory of the source framework's chat server | The rewrite contract. Keeps the mechanisms that earned their place (per-request connection close, WAL bounds, injection serialization and verified submit, slug-bound reply path, fail-open static guard, extend-only allowlist); corrects contract inconsistencies rather than porting them (validation, defaults, id space, archive semantics); states its limits; lists conscious exclusions. |
| `tests/*` | written fresh, from behavior | Tests are written from the spec of each module, never ported: ported tests carry ported assumptions. |
| `cousin_lib/server/storage.py` | written fresh against `docs/chat-server-spec.md` | Chat message + reaction storage. Full schema at creation (no migration dance, single id space), WAL with a small autocheckpoint, tap-bumps-never-toggles reactions, mode-aware history paging with counted `has_more`, per-thread archive with keep-N. The spec left `has_more`'s direction implicit; here it answers "more in the direction you are paging", counted before the LIMIT truncates. |
| `cousin_lib/server/app.py` | written fresh against `docs/chat-server-spec.md` | The HTTP surface: guard-first on every route, per-request store opened and explicitly closed (the close is load-bearing under large payloads), slug-bound reply path, 400-on-malformed-JSON, presence marker touched after delivery composes its text, inbound data: images decoded to the chat inbox with extension normalization, allowlisted static serving with a resolve-then-contain traversal guard. Terminal delivery and the network guard are injected seams; their implementations land as their own modules. Departure from the source: attachments are not persisted in the database (the columns ship with the media subsystem); an inbound image exists only as the decoded inbox file, and a failed decode is flagged in the delivery line rather than recoverable from storage. |
| `cousin_lib/server/injection.py` | rewrite from the mechanism of the source framework's terminal delivery | Kept, because each was earned against a real failure: process-wide injection serialization (interleaved keystrokes merge messages), the length-scaled settle between paste and Enter (a premature Enter strands the paste in the input box), capture-pane verification with exactly one Enter retry (alnum normalization makes box wrap and border glyphs irrelevant), loud-but-never-raised failure logging (the HTTP response has already returned). Changed: tmux binary and socket come from configuration/PATH instead of hardcoded host paths; composition is a pure function carrying the reserved recall seam (a failing suffix provider never costs the delivery); single-line construction is enforced at composition. |
| `templates/`, `server/`, `ui/` | (pending) | |
