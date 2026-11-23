# Decisions

*Optional: nothing on this page is needed to run a cousin.*

A [cousin](glossary.md#cousin) often reaches a choice that is yours to make:
which option to take, whether to go ahead, what to keep. Asked only in chat,
such a question scrolls away. The decision register keeps it until you
answer: one list for the whole install, in the console's **Decisions** page.

## How a decision goes

1. The cousin asks with its `decision` tool: one question per call, with
   its options, the option it recommends and why. Then it ends its
   [turn](glossary.md#turn).
2. The question waits in the register. The console's fleet shows the cousin
   as **needs you** while it has an open decision and is not working on
   something else, with "N decisions open" on its dot.
3. You answer on the **Decisions** page: click an option (the recommended
   one is marked), add a note, or write an answer of your own.
4. The answer is recorded first. Then the cousin gets it twice:
   - an operator-level memory in its home, topic `decision #<id>: <the
     question>`, with the question, your answer and the recommendation it
     asked with, cited `decision register #<id>, answered by <you> at
     <time> in the console`;
   - a turn on its operator [thread](glossary.md#thread) (the `system` thread
     when it has no operator), from you, reading
     `Decision #<id> answered: <question> -> <answer>`.

If the turn cannot be delivered (the cousin has no
[runner](glossary.md#runner) right now, say), the answer is kept and marked
undelivered; the Decisions page shows "turn not delivered" (or "memory not
written") beside it. The console retries what is owed every few seconds,
and **retry now** on the page and `cousin-decision redeliver` retry at
once. Each memory and each turn is claimed by one process before it is
written, so a retry that overlaps an answer never writes it twice, and a
claim left by a process that died is taken over after five minutes.

## Groups

Questions that belong together share a `group`. Their answers come back as
one turn, listing every answer of the group in id order, once every
decision of that cousin and group is answered or withdrawn and its newest
question is at least 60 seconds old. The wait is there because a cousin
asks one question per call: without it, you could answer the first
question before the cousin had asked the second. Each memory is
still written when its own decision is answered. A decision with no group
comes back at once.

## Skill stop points

A skill can name the places where it stops for your choices
(`metadata.stops` in its SKILL.md, [skills](reference/skills.md#stop-points)).
At each one the cousin asks with `skill` and `stop` set. The group is then
`<skill>:<stop>@<run>`: the run this cousin has under way at that stop (a
question still open or an answer not yet sent), else a new one named for
the ask time to the minute. The run is not the asking turn's id, because
the terminal [lane](glossary.md#lane) has none; both lanes use this rule, and a stop the skill
reaches again in a later run is a new group. The stop's answers come back
as one turn that starts `Decisions answered at skill <skill>, stop <stop>`. The
Decisions page titles the group "skill <skill>, stop <stop>", and the fleet's
tooltip says "waiting on you at <skill>:<stop>".

## The tool

| Command | What it does |
|---|---|
| `ask` | `question`, optional `options` (each `"key: text"`, a bare text gets the next letter), `recommendation` (an option key), `why`, `group`, `skill`, `stop` |
| `list` | the cousin's own decisions, open ones by default (`status`, `group`) |
| `withdraw` | takes one of its own open questions back (`id`) |

There is no `answer` command: answering is your act. `cousin-decision
answer` exists for your own terminal and refuses to run inside a cousin's
environment (`COUSIN_HOME` set), and `ask` and `withdraw` take the cousin
from `COUSIN_HOME`. These checks read the environment, the same way
`cousin-skill import` does: they guard against a mistake, not against a
cousin that sets out to get around them. They are not a security boundary. The framework contract tells every cousin
that serves the tool to put a choice that is yours into it, not only into
its chat text, and then to end the turn.

## In the terminal

```
cousin-decision list --status all
cousin-decision show 3
cousin-decision answer 3 --option b --answer "and keep the old one a week"
cousin-decision redeliver
```

The store is `data/decisions.db` in the install, with a history of every
change beside it. The routes are `GET /api/decisions` and
`POST /api/decisions/{id}/answer` ([console API](reference/console-api.md)).
