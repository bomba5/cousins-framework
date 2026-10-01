---
name: reference_verify-it-fires
description: Code present is not a feature working - fire it through the entry the operator uses and read the output
shareable: true
kind: rule
---
- "The code is there" is not verification. A bare except or a best-effort fallback turns a hard failure into a silent no-op. Fire the feature and read the output: a real call, a test user, a captured screen.
- The tested entry is not the live one. Commands, launchers and service units wrap the library, and drift hides there. Reproduce a reported defect through the operator's own entry (same URL, same command) before blaming their setup.
- A green suite can run zero tests: the proof a test ran is its name in the output, not the summary line.
- Before declaring something broken, check the probe itself.
