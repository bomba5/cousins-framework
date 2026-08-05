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
| `tests/*` | written fresh, from behavior | Tests are written from the spec of each module, never ported: ported tests carry ported assumptions. |
| `templates/`, `server/`, `ui/` | (pending) | |
