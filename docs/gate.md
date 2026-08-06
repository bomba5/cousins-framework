# The contamination gate

The gate fails the build on content that must never be public. It has two
modes built on one engine:

- **gate** (default): zero tolerance. Any hit of any kind in any position
  fails. A term in a docstring fails exactly like a term in an `if`.
- **triage**: classifies instead of forbidding. Emits one JSON record per
  hit with a parse-level position (comment, docstring, code-string, code,
  prose, binary), for auditing a private tree before extraction.

All examples in this document use fictional terms. The documentation of a
tool that removes names must not itself ship a name.

## What it catches without any denylist

- Private address literals (RFC1918 and the CGNAT /10 block)
- Absolute `/home/<user>` paths
- Secret-shaped strings (JWT structure)
- Binary files outside a small image/font allowlist: what the gate cannot
  read, it fails as an opaque carrier rather than waves through

## The denylist

Real terms (names, hosts, vocabulary) are supplied at run time:

```
cousin-gate --root . --denylist "$COUSIN_GATE_DENYLIST"
```

The file format is one term per line; `#` comments and blanks are skipped.

Two properties are enforced by the loader, not by convention:

1. The path must not sit inside any git work tree. A denylist that gets
   committed publishes an index of exactly what it exists to hide.
2. The path must not resolve (through symlinks) into a managed store. On
   declaratively managed hosts the realistic failure is not a leak but a
   tidy-up: someone adopts the file into system configuration because
   managing it "like the others" looks like hygiene, and the system
   configuration lives in a repository.

Both raise `DenylistLocationError`. Create the file by hand, somewhere that
is neither version-controlled nor declaratively managed.

## What a green gate does not prove

The gate proves the tree is sterile of the terms and shapes it knows. It
does not prove a file is finished. Comment quality, naming, and whether a
docstring states the mechanism rather than someone's history are editorial
judgements no scanner makes; their receipt is review. Read a green gate as
"nothing known-private is here" - never as "this file is done".

## Matching discipline

Name terms match case-insensitively on word boundaries, never as
substrings: a term `rea` must not match `React`. Loose matching is doubly
harmful - it inflates counts and it buries real hits in noise, so precision
failures become recall failures at review time. Every hit carries file,
line, column, and the source line, so matches stay auditable by a human.

## Position classification

For Python sources every hit is classified by where it sits: comments and
docstrings are mechanical positions (prose a rename clears); string
literals in executable code, and code itself, are structural positions (the
term participates in behavior). Unknown is unsafe: anything the classifier
cannot place is structural. Triage aggregates per file - one structural hit
outweighs any number of mechanical ones.

## Fixtures are publishable content

The gate's scope includes test files and fixture data, and this is not
theoretical: during this repository's own construction the gate caught
a real personal name inside a freshly written test fixture, before it
could enter history. A reader's instinct is that tests are not
"publishable content"; the instinct is wrong - tests ship with the
tree.

Consequence: example people and cousins in tests come from a small
fixed cast (Wren, Testa, Sam, Priya, Toki, Mallory), all invented for
this repository. Use the cast; never invent a name mid-edit, because
inventing one under pressure is exactly when a real one surfaces.
