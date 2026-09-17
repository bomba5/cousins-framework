# Memory tiers: private by default, shared by review

**Status: implemented.** The shared tier ships as `cousin_lib/shared_tier.py`
(`cousin-shared list/read/diff/propose/promote/reject`) against this page;
the page remains the contract the module is tested against.

## The model

A cousin's memory is **private by default**. Everything under
`<home>/memory/` and `<home>/notes/` belongs to that cousin alone:
other cousins do not read it, tools do not index it fleet-wide, and
nothing promotes out of it silently.

Above the private tier sits a **shared tier** with exactly one entry
path:

- `shared/<file>.md` - canonical, readable by every cousin.
- `shared/proposed/<slug>__<file>.md` - pending proposals.
- An append-only audit log of every write, with source attribution.

## The promotion rule

Nothing reaches the canonical tier except by **reviewed promotion**:

1. A cousin proposes: its candidate lands in `proposed/`, never in
   canonical, with the proposer's slug in the filename.
2. A designated reviewer - the operator, or a cousin the operator
   appoints - reads the diff against canonical and promotes or
   rejects.
3. Overwriting an existing proposal requires an explicit force flag;
   there are no silent overwrites anywhere in the tier.

The rule an implementation must preserve: **the proposing side and
the promoting side are never the same principal by default.** That
separation is the boundary; everything else is mechanism.

## Why this is doctrine now, code later

Boundaries are cheap to design in and expensive to retrofit. A fleet
built with every cousin writing to one shared directory has working
code, real data, and no seam to add review to - retrofitting the tier
then means migrating live data and re-teaching every cousin. Building
against this page from day one costs nothing: keep cousin memory in
cousin homes, and treat anything fleet-visible as needing a promotion
path, even while that path is manual.

## What v1 ships toward this

- Private-tier layout and tooling in full (`memory/`, `notes/`,
  `cousin-memory`).
- The outbound content filter's per-surface model - the same
  private-by-default posture at the message boundary.
- The self-portrait review gate - the same propose/review/commit shape
  applied to identity, which is the promotion rule in miniature.

- The shared tier itself: `cousin-shared` with reviewer-checked
  promotion (`config/shared-reviewers.json`) and an audit log.
