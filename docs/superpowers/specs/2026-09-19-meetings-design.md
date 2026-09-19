# Meetings - design

Status: approved by the user in chat 2026-09-19 (sections 1 and 2), "build it" at 08:44 UTC.
Target release: 0.6.0.

## Purpose

A meeting is a chat container shared by the user and several running cousins: for
brainstorming, coordinating, or cross-reviewing a change. Teams are the user's console
sidebar groups; the framework keeps no team state.

## Decisions

- Store and orchestration live in a library (`cousin_lib/meetings.py`) over
  `<root>/data/meetings.db`, like the tracker. The console is a view; a console restart
  loses nothing.
- Rounds by default: the user posts, each participant speaks once in the stored order,
  then the floor returns to the user.
- Only running local cousins can be participants. A stopped cousin cannot join; the user
  starts it first. Remote (hive) cousins cannot join in this release.
- No operator name anywhere in code or docs: the human speaker is the console user name.

## Model

`meetings(id, topic, participants JSON [slugs, speaking order], facilitator, state
open|closing|closed, mode round|direct|floor, turn_slug, turn_index, round, turn_started,
turn_delivered, turn_timeout_s default 600, created_by, created_at, closed_at, updated_at)`

`entries(id, meeting_id, speaker, kind user|cousin|system|minutes, text, round, created_at)`

`seen(meeting_id, slug, last_entry_id)`: what each participant has been sent.

## Flow

- `open(topic, participants, created_by, facilitator=None, timeout=600)`: refuses an empty
  list, unknown slugs and duplicates. State open, mode floor (the user's floor).
- `post(meeting, user, text)`: only when the floor is the user's. Text starting `@slug `
  addressed to a participant starts a direct turn to that slug; otherwise a round starts
  at participant 0. Round number increments per user post.
- A turn: `turn_slug` set, `turn_started` now, `turn_delivered` false, then delivery is
  attempted at once. The delivered text is one line block:
  `(Meeting <id> "<topic>" round <n>, your turn): <entries since last_seen, each as
  "<speaker>: <text>">` followed by the how-to line:
  `Answer with: cousin-meeting say <id> "<text>" (or: cousin-meeting pass <id>). One turn; stay on topic; be brief.`
- `say(meeting, slug, text)` / `pass_(meeting, slug)`: refused unless `slug == turn_slug`
  (the refusal names whose turn it is). Stores the entry and advances: next participant
  in a round; floor back to the user after the last participant or after a direct answer.
- `tick(deliver, is_alive, now)` (called by cousin-loops every tick): for every open
  meeting, retries an undelivered turn; skips a speaker whose session is not alive, or
  who has not answered within `turn_timeout_s`, with a system entry "<slug> skipped
  (<reason>)".
- `close(meeting, user)`: with a facilitator, state closing and a minutes turn goes to the
  facilitator with the whole transcript: "write the minutes: decisions, open questions,
  actions; add each action to the tracker; post with cousin-meeting minutes <id>".
  `minutes(meeting, slug, text)` stores a minutes entry and closes. Without a facilitator,
  or when the facilitator times out, the meeting closes directly.
- `skip(meeting, user)`: the user skips the current speaker.

Delivery: tmux injection into the cousin's session (the same injector the loops use).
A failed injection (a menu open) leaves `turn_delivered` false for the next tick.

## Interfaces

- CLI `cousin-meeting`: `list`, `show <id>`, `open`, `post`, `say`, `pass`, `minutes`,
  `skip`, `close`. `say`/`pass`/`minutes` take the speaker from COUSIN_HOME.
- MCP tool `meeting` in the registry (say, pass, minutes, show).
- Console routes: `GET /api/meetings`, `POST /api/meetings`, `GET /api/meetings/<id>`,
  `POST /api/meetings/<id>/post|skip|close`; SSE event `meeting-change` from a poller diff
  (cousins write through the CLI, outside the console).
- Console page "Meetings": list, new-meeting dialog (topic; participants by sidebar group,
  all, or single; stopped and remote cousins disabled; facilitator select; timeout), the
  meeting thread with a turn banner, composer, Skip and Close.

## Teaching the cousins

1. A "Meetings" section in `templates/cousin-CLAUDE.template.md`.
2. `cousin-meeting teach [--apply]`: inserts the same section into existing cousins'
   CLAUDE.md above the cousin-specific marker; a dry run prints the diff.
3. Every turn message carries the how-to line.

## Testing

Library: open validation, round order, out-of-turn refusal, direct turn, pass, skip on
timeout and on a dead session, delivery retry, close with and without facilitator,
minutes. Routes: shapes, auth, 404s. Static: JSX compiles, no em dash. Live canary: a
two-cousin meeting on the running fleet through the console route.
