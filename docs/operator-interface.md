# The operator interface

The operator is a subsystem, not an assumption. A zero-operator
install is not a degraded configuration; it is a framework with one
subsystem absent - and every surface must work in that state, with
the absence explicit wherever it changes behavior: a required
argument, a named degradation, a refusal with remediation. Never a
silently defaulted human being.

**The executable form of this page is `tests/test_null_operator.py`.**
A documented seam with no test is a claim; the suite runs the whole
v1 surface against an install with no operator anywhere.

## The five operator roles and their seams

1. **Truth.** The top tier of the memory truth hierarchy is
   operator-stated fact. Null profile: nothing occupies that tier;
   cousin conclusions stay labeled as cousin conclusions and never
   get promoted to operator-stated by default.
2. **Calibration.** The boot packet carries an operator-calibration
   layer. Null profile: the layer is absent and the packet NAMES the
   degradation in its header - a cousin booting uncalibrated should
   know it, and a reader of the packet should see it.
3. **Crash recovery.** A stale flip marker is reported, never
   auto-recovered: the operator IS the recovery path, by decision
   rather than accident. Null profile: the report still lands; a
   crashed cousin in an operatorless install stays down until a human
   chooses otherwise, which is the honest behavior.
4. **Identity review.** The self-portrait's synthesize/commit gate
   expects a reviewer. Null profile: candidates accumulate
   uncommitted; the boot packet keeps naming the degraded portrait
   layer; nothing auto-commits an identity nobody reviewed. The same
   shape governs the shared tier: with no reviewers configured,
   promotion refuses with remediation instead of inventing an
   approver.
5. **Trust.** What it means to run an agent - binary, flags,
   permission posture - is the operator's call, expressed as host
   configuration (`config/agent-cmd`). Null profile: nothing runs; a
   framework does not choose a trust model on anyone's behalf.

## The recurring rule

Wherever the operator's absence changes behavior, the change must be
loud at the point of use. The chat surface requires an explicit
recipient because there is no default person; the scheduler and
memory tools never needed a person at all. Absence is accommodated
only where absence is a no-op.
