# House rules

A fresh install comes with a handful of working rules every
[cousin](glossary.md#cousin) follows: how to reason, how to verify, how to
leave state for the next session. They are ordinary `kind: rule` files in the
[shared tier](glossary.md#shared-tier), so each one is quoted in full in every
cousin's system prompt ([what the boot packet reads](memory.md#what-the-boot-packet-reads)).
They are yours: edit them, delete them, add your own.

## What ships

| file | what it says |
|---|---|
| `reference_first-principles.md` | Decompose the problem, read the system, reason from constraints, then act; no trial and error on a problem you have not decomposed. |
| `reference_requirement-levels-rfc2119.md` | Requirement words (MUST, SHOULD, MAY) follow RFC 2119, in capitals only where a requirement is meant. |
| `reference_framework-semver.md` | Anything the cousins ship with a version follows Semantic Versioning 2.0.0; a behaviour change bumps the version in the same commit. |
| `reference_verify-it-fires.md` | Code present is not a feature working: fire it through the entry the operator uses and read the output. |
| `reference_boundary-discards.md` | Keep what a filter, a summary or a swallowed exception would throw away, above all when you are confident. |
| `reference_state-hygiene.md` | STATUS and the handoff carry one loop per bullet with its next command; untested is not confirmed. |
| `reference_process-hygiene.md` | Stop processes by pid, stop their children too, and never start a tracked job that cannot end. |

Why they exist: an agent left to itself guesses before it reads, calls code
"done" because it is written, and hands its successor a vague summary. These
rules are the habits that, in practice, make cousins worth leaving
unattended. They are short on purpose: all seven together take about 4300 of
the 6000 characters the boot packet gives the shared tier, which leaves room
for rules of your own.

The source files are in the checkout under `templates/shared/`.

## The Framework Law

Beside the house rules ships the Framework Law, the contract every cousin
boots with: who it is when a session ends, how to treat memory by its truth
level, what to leave behind before a session ends, and what never to
improvise. It is `templates/law.md` in the checkout and `config/law.md` in
an install, written by the [supervisor](glossary.md#supervisor) on its first start under the same
rule as the house rules (once, never over an existing file, gone for good
if you delete it). See [law.md](configuration.md#lawmd).

## How they arrive

When the supervisor starts (the container's
command, a bare host's unit), it copies every `templates/shared/*.md` into
`<root>/shared/` before any cousin boots. The supervisor is the one start
every install has, and no cousin runs without it. Each copy is a `seed` row
in `shared/audit.jsonl`, and a file name with a seed row is never seeded
again:

- an existing `shared/<file>.md` is never overwritten (its row says `kept`);
- a rule you delete stays deleted, at every later start;
- a rule a later release adds arrives at the first start after the upgrade.

An upgrade never changes a seeded file, the law included: it may hold your
edits. To see whether the version you upgraded to ships different text, run
`cousin-shared templates` (`--full` for the diffs, from your file to the
shipped one) and copy over what you want by hand. It reads only, and a file
you deleted shows as `missing in install`.

Seeded rules skip the propose and promote review: they come with the install,
like a config default, and are canonical from the start. Nothing is queued
for a reviewer and the review rule never sees them.

## Edit, remove, add

- **Edit** `shared/<file>.md` in place. The next session of each cousin reads
  the new text. Keep the frontmatter: without `kind: rule` the file is only an
  index line, not a rule.
- **Remove** a rule by deleting `shared/<file>.md`. It does not come back.
- **Add** your own through the review: a cousin proposes it with
  `cousin-shared propose`, a reviewer promotes it
  ([the shared tier](memory.md#the-shared-tier)). Put `kind: rule` in its
  frontmatter for it to be quoted in full; a rule costs prompt on every
  session of every cousin, so keep it short.

## The Scrum example

`templates/shared/examples/reference_scrum-team.md` is an example and is not
seeded: the operator as Product Owner, a cousin as Scrum Master, the
[tracker](glossary.md#tracker) as the backlog, no sprints. To use it, copy it
into the shared tier from the framework root, then name your Scrum Master in it:

```
cp templates/shared/examples/reference_scrum-team.md shared/
```

Every cousin follows it from its next session. Delete the file to stop.
