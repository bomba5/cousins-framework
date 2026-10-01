---
name: reference_state-hygiene
description: What makes STATUS and the handoff usable by the next generation, and untested versus confirmed
shareable: true
kind: rule
---
- At boot, check what is actually running (jobs, schedules, processes, dirty worktrees) before acting on the handoff: the handoff is what the last generation believed.
- STATUS open loops: one bullet per loop, each with the exact next command. "Continue the work" is not actionable.
- Untested is not confirmed. A prediction the run never reached is "UNTESTED", in STATUS and to the operator.
- Deliberate uncommitted edits are state: list each dirty file, why, and whose decision it waits on.
- What the operator said goes in at operator level with a citation; your own reasoning stays at conclusion level.
