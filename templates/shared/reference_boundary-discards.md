---
name: reference_boundary-discards
description: The cheap step that produces information is the one you skip when you are confident - keep what a boundary throws away
shareable: true
kind: rule
---
A filter, a summary, a status code without its body, a swallowed exception: each throws information away where keeping it was nearly free, and each feels like tidiness when you write it. Confidence is the moment to watch.
- Make a failure print its reason before the experiment, not after the third reproduction.
- Watch a new test fail against the old code; a test that passes on both proves nothing.
- Before filtering a case out, ask what it would have told you. An empty file, a zero count and a silent success are findings.
- When your own fix is questioned, hand the question to someone with nothing invested.
