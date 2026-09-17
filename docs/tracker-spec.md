# Tracker specification: framework-wide in-flight work

The tracker is one list, for the whole framework, of the work that is
in flight: what is open, who holds it, what it is blocked on, what
closed. Jobs (`cousin-job`) are processes that are running now; the
tracker is the operator's ledger of threads, and a thread may have no
process behind it for weeks.

**The store is the library's.** `cousin_lib/tracker.py` owns
`<root>/data/tracker.db`; the CLI and the web console are two callers of
the same functions. Nothing about the tracker needs a service running:
`cousin-tracker add` on a machine with no console works, and the console
later shows what it wrote. The source framework had this backwards
(the console owned the database and the CLI was an HTTP client of it),
which meant the tracker died with the console. That is the one design
change here and everything below follows from it.

## The item

```json
{
  "id": 3,
  "title": "migrate the chat archive",
  "domain": "infra",
  "state": "blocked",
  "tags": ["q4", "disk"],
  "owner": "testa",
  "notes": "waiting on the disk swap",
  "created_at": "2026-09-17T10:00:00+00:00",
  "updated_at": "2026-09-17T12:30:00+00:00"
}
```

| field | type | rule |
|---|---|---|
| `id` | integer | assigned by the store; **never recycled** (AUTOINCREMENT): a deleted id is not handed out again |
| `title` | string | required, stripped, non-blank |
| `domain` | string | free text, `""` when unset; a grouping key (`infra`, `income`, `framework`, ...) |
| `state` | string | one of `open`, `active`, `blocked`, `done`, `dropped`; default `open` |
| `tags` | list of strings | stored as a JSON list; stripped, deduplicated, order kept |
| `owner` | string | defaults to the calling cousin's slug (from `COUSIN_HOME`), `""` when there is no cousin identity, or whatever the caller names |
| `notes` | string | free text, `""` when unset |
| `created_at` | string | UTC ISO 8601 with offset, seconds precision, set once |
| `updated_at` | string | as above; set on every mutation, equal to `created_at` at creation |

There is no `summary`, `links` or `last_action`: those three source
fields were free text with no consumer, and `notes` holds what an
operator writes. `done` and `dropped` are the closed states; the other
three are open. Any state may move to any other; the tracker records
what the operator says, it does not police a workflow.

## The store

- Path: `<root>/data/tracker.db`, where the root is `--root` on the
  CLI, the `root=` keyword in the library, else `FRAMEWORK_ROOT`. No
  root is a loud error naming both channels, exit 2 on the CLI.
- Created on first use; WAL journal; three indexes (`state`, `domain`,
  `owner`).
- **Concurrency rule:** one connection per call, closed in `finally`;
  every mutation runs under `BEGIN IMMEDIATE` so the write lock is
  taken before the read that precedes the write, and the busy timeout
  is 10 s. Eight cousins adding at once produce every row and no
  "database is locked". Reads never take the lock.
- Ordering of `list`: open states first, then closed; within each half
  the most recently updated first, ties by higher id first. The console
  shows the list in this order and does not re-sort.

## The library (what the console calls)

```python
from cousin_lib.tracker import (
    STATES, TrackerError, ItemNotFound,
    add, update, set_state, list_items, show, delete, db_path,
)

add(title, *, domain="", state="open", tags=(), owner=None, notes="",
    root=None) -> item
update(item_id, *, title=None, domain=None, state=None, tags=None,
       owner=None, notes=None, root=None) -> item
set_state(item_id, state, *, root=None) -> item
list_items(domain=None, state=None, tag=None, *, owner=None,
           root=None) -> [item, ...]
show(item_id, *, root=None) -> item | None
delete(item_id, *, root=None) -> bool
db_path(root=None) -> pathlib.Path
```

- `update` touches only the keywords that are not `None`; `tags`
  replaces the whole list. Naming nothing is a `TrackerError`.
- `list_items` filters are ANDed; `tag` matches an item that carries
  that tag exactly.
- Errors: `TrackerError` (a `ValueError`) for a blank title, an unknown
  state, an empty update; `ItemNotFound` (a `TrackerError` and a
  `KeyError`) for `update`/`set_state` on a missing id. `show` returns
  `None` and `delete` returns `False` for a missing id instead of
  raising. A console maps `ItemNotFound` to 404 and any other
  `TrackerError` to 400.

## The CLI

```
cousin-tracker add "<title>" [--domain D] [--state S] [--tag T ...]
                   [--owner SLUG] [--notes TEXT]
cousin-tracker update <id> [--title T] [--domain D] [--state S]
                      [--tag T ...] [--add-tag T ...] [--owner SLUG]
                      [--notes TEXT]
cousin-tracker state <id> open|active|blocked|done|dropped
cousin-tracker list [--domain D] [--state S] [--tag T] [--owner SLUG]
cousin-tracker show <id>
cousin-tracker delete <id>
```

Every verb takes `--json` and `--root <framework root>`. `update --tag`
replaces the tag list; `--add-tag` appends to it (to the replaced list
when both are given). Exit codes: 0 done; 1 the id names no item; 2 a
refused call (bad state, blank title, empty update, no root).

Plain output: `add`/`update` print `#<id> <title> [<state>] ...`,
`state` prints `#<id> -> <state>`, `list` prints a table or
`(no tracker items)`, `show` prints every field, `delete` prints
`deleted #<id>`.

## The JSON shapes (CLI `--json` and the console are the same)

One envelope per shape, so a script reading the CLI and a page reading
the console parse the same thing.

**List** (`cousin-tracker list --json`; console `GET /api/tracker`
with optional `domain`, `state`, `tag`, `owner` query filters):

```json
{"items": [ <item>, <item> ]}
```

**Item** (`add`, `update`, `state`, `show` with `--json`; console
`GET /api/tracker/<id>` and the response to every mutation):

```json
{"item": <item>}
```

**Delete** (`delete --json`; console delete of `/api/tracker/<id>`):

```json
{"ok": true, "deleted": 3}
```

**Mutation payloads the console accepts** (JSON bodies; every key
optional except where stated; unknown keys are ignored):

| route | body | library call |
|---|---|---|
| create | `{"title": "...", "domain": "", "state": "open", "tags": [], "owner": "", "notes": ""}` - `title` required | `add(**body)` |
| update `<id>` | any of `{"title", "domain", "state", "tags", "owner", "notes"}`; absent means untouched | `update(id, **body)` |
| set state `<id>` | `{"state": "active"}` | `set_state(id, body["state"])` |
| delete `<id>` | none | `delete(id)` |

**Errors** from the console: `{"error": "<message>"}` with 400 for a
`TrackerError` (bad state, blank title, empty update), 404 for
`ItemNotFound` or a `show`/`delete` of a missing id. The message is
the exception's text, which names the allowed states.

## Stated limits

- No history: the tracker holds the current row, not the sequence of
  states it went through. A cousin that wants the trail logs a decision
  (`cousin-memory decide`) when it moves an item.
- No ownership check: any caller may change any item. The tracker is
  one operator's ledger, not a multi-tenant queue.
- No cross-machine sync: a hive node's tracker is its own file; the
  queen does not carry it.
