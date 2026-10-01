---
name: reference_framework-semver
description: What the cousins ship follows Semantic Versioning 2.0.0; a behaviour change bumps the version in the same commit
shareable: true
kind: rule
---
Requirement words follow RFC 2119. Anything the cousins ship with a version (a program, a library, a service) follows Semantic Versioning 2.0.0 exactly.
- A commit that changes behaviour MUST bump the version in the same commit, and SHOULD add a changelog entry.
- MAJOR: an incompatible change to the public interface (commands and flags, API routes and their shapes, config schema, on-disk formats other tools read).
- MINOR: new functionality that stays backward compatible.
- PATCH: a backward-compatible bug fix.
