---
name: reference_requirement-levels-rfc2119
description: Requirement wording in docs, rules and specs follows RFC 2119 (MUST, SHOULD, MAY)
shareable: true
kind: rule
---
Wherever a requirement is written (docs, rules, specs, runbooks, reviews), its level follows RFC 2119:
- MUST, REQUIRED, SHALL: absolute. MUST NOT, SHALL NOT: absolutely forbidden.
- SHOULD, RECOMMENDED (and SHOULD NOT): there may be valid reasons to deviate, once the implications are understood and weighed.
- MAY, OPTIONAL: truly optional.

Only the capitalised forms carry the meaning (RFC 8174): write them in capitals where a requirement level is meant, and plain words everywhere else.
