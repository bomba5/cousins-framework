# Plain language

*Optional: nothing on this page is needed to run a cousin.*

A check for text people read: docs, skills, notes and the replies a
[cousin](glossary.md#cousin) writes. It finds em dashes, sentences that run
too long, words you banned, and words your glossary says not to use. It also
points at verbs that may be passive. It runs in three places:

- `cousin-lint`, on files or on stdin ([commands](commands.md#plain-language)).
- The console's Language page: paste text, check it, see the fixed text, and
  edit the settings ([console](console.md#language)).
- `cousin-glossary`, which finds the words a repository's code and docs
  really use, to build the glossary from ([below](#a-glossary-from-the-code)).
- A cousin's `reply` and `send` tools, when its `cousin.toml` asks for it
  ([below](#the-reply-modes)).

## What it checks

The check reads Markdown or plain text. It skips fenced code blocks (in a
list item or a quote too), indented code blocks (four spaces or a tab, after
a blank line and outside a list), inline code spans, URLs, link targets,
HTML comments and the separator row of a table. A table cell is checked as
text of its own. A YAML front matter block at the top of a file is skipped;
a chat message has none, so the `reply` and `send` tools check a message
that opens with `---` whole.

| rule | finds | fix |
|---|---|---|
| `em-dash` | an em dash (U+2014), and an en dash (U+2013) with a space on each side | ` - ` |
| `sentence-length` | a sentence with more words than its limit | none |
| `banned-word` | a word or phrase from the banned list, in any case, as whole words | none |
| `glossary` | a word that a glossary entry lists under "Do not use" | none |
| `passive` | `is`, `are`, `was`, `were`, `be`, `been` or `being`, then a word ending in -ed or -en (one word may sit between) | none |

Two limits apply to `sentence-length`. The `step` limit (20 words) is for a
numbered list item, and for a bulleted item that starts with a verb such as
"run", "open" or "check". The `description` limit (25 words) is for every
other sentence. Words are counted after the Markdown markup is gone: a code
span or a URL counts as one word, a link counts as its text. A heading is not
a sentence. A sentence ends at `.`, `!` or `?` before a space. After "e.g",
"i.e", "etc", "vs" or "no" it ends only when the next word starts with a
capital letter (a heuristic).

An em dash at the start of a line is a note with no fix: a hyphen there
would make the line a list item, so it needs rewriting by hand.

The `passive` rule is a heuristic, like the verb list behind the `step`
limit and the abbreviation rule above. It knows a few words that end in -ed or -en
and are not verbs ("often", "open", "token", "need"), but it will still be
wrong at times. So its findings are notes: they show, and they never make
`cousin-lint` exit 1. Only the em dash has a fix the check can apply on its
own; a long sentence or a banned word needs a person.

## Settings

The install-wide settings are in `config/plain-language.toml`. Without the
file, the check uses the defaults. `config/plain-language.toml.example` shows
every key:

```toml
reply_default = "off"         # a cousin's reply mode when its cousin.toml sets none
glossary = "docs/glossary.md" # optional, relative to the install root

[limits]
step = 20
description = 25

[rules]                       # each rule can be switched off
em_dash = true
sentence_length = true
banned_word = true
glossary = true
passive = true

[[banned]]
word = "leverage"
use = "use"                   # optional: named in the finding
```

The console's Language page, under settings, reads and writes this file. The
server checks every value before it writes, and a refused value writes
nothing. A save writes the whole file again from the values, so comments in
it are lost.

## Glossaries

The glossary rule reads a glossary file in one of two formats. A file can
hold both; a table row wins over a heading of the same term.

1. A Markdown table whose header has the columns `Term`, `Meaning` and
   `Do not use`, in any case and in any order (other columns are ignored).
   The "Do not use" cell is a comma list; `-` means none.

   ```
   | Term | Meaning | Do not use |
   |---|---|---|
   | reply | an answer on the chat surface | response, answer message |
   ```

2. The format of these docs' own [glossary](glossary.md): a `### term`
   heading, then a paragraph. A line in the paragraph that starts
   `Do not use:` holds the comma list.

   ```
   ### reply

   an answer on the chat surface.
   Do not use: response, answer message
   ```

A word from the "Do not use" list is a finding, except where it is part of
the preferred term itself. `cousin-lint --glossary FILE` and the console's
glossary field use a file for one check; the `glossary` key sets the default.

## A glossary from the code

A glossary helps only when its terms are the words the code really uses.
`cousin-glossary` reads a repository and tells you which words those are. It
only reads; `derive --out` is the one command that writes a file.

- `cousin-glossary terms PATH [--top N] [--glossary FILE]` lists the most used
  terms, counted apart in code and in docs. Code is the identifiers of a code
  file, split at snake_case, camelCase and kebab-case. Docs are its comments
  and strings, and every Markdown and text file. Terms in the glossary are
  marked. The top of the list holds the domain words that need an entry.
- `cousin-glossary groups PATH --glossary FILE` takes each entry as a synonym
  group: the term and its "Do not use" words. It counts each member in code
  and in docs and names the dominant one: the most code uses, with docs to
  break a tie. An entry whose term is not the dominant one is flagged, for
  example `code says "conversation" 41 times, the glossary prefers "thread" (3)`.
  `--group "a,b,c"` (repeatable, the preferred word first) adds a group of
  its own. Exit 1 when an entry is flagged.
- `cousin-glossary derive PATH --glossary FILE --out FILE` writes a glossary
  in the table format, with a `Uses` column. Each term is the dominant
  member, the others go under "Do not use", and the meanings are kept (an
  empty one is written `TODO`). It never overwrites a file without `--force`.

Every command takes `--json`. Every word is counted in lower case, and a
plural counts as its singular ("entries" as "entry"; a heuristic). Words under
3 letters and common stopwords are left out of the term list. The walk skips
`.git`, virtual environments, `node_modules`, build output, binary files and
files over 1 MB (and anything that is not a regular file), and honours
`.gitignore` where `git ls-files` works; a directory git ignores is walked.
`git` runs there with the repository's fsmonitor hook and the system config
off, so a scanned repository runs nothing. The term list, the splitting and
the plural rule are heuristics.

The Language page's glossary tab runs the same three. Its path must lie in the
install, a cousin's home or the framework checkout (when the console runs
from one); the server refuses any other, and any path through a `.secrets`
directory. A scan never reads a `.secrets` directory either. Derive shows the text to copy there, and the console writes no file.

## The reply modes

A cousin's `cousin.toml` can turn the check on for its own replies:

```toml
[plain_language]
reply = "note"
```

| mode | what `reply` and `send` do |
|---|---|
| `off` | nothing (the default) |
| `note` | send the text as written, and add the findings to the tool's result, so the cousin sees them |
| `fix` | fix the em dashes before the text goes, and add the other findings to the result |

The check never blocks a reply or a message. If it fails, the text goes as
written. A text over 64000 characters goes unchecked, and the note says so.
The mode applies to the in-process tools of the SDK and opencode
[runners](glossary.md#runner), to the pane's reply on the tmux kind, to
the `send` tool served by `cousin-mcp`, and to `cousin-reply` (which prints
the findings after its own line). `cousin-chat send` run by hand is not
checked. Without a mode in `cousin.toml`, the
install's `reply_default` applies. The mode is read at each reply, so a change
needs no restart. The inspector's cousin settings and the Language page both
write it.

## The command

```
cousin-lint docs/*.md                      # path:line:col rule message ("(note) ..." for a note)
cousin-lint --only em-dash,banned-word README.md
cousin-lint --glossary docs/glossary.md --json notes/today.md
printf 'done \342\200\224 at last.\n' | cousin-lint --fix   # an em dash in, ' - ' out
cousin-lint --fix --write docs/install.md  # fix the file in place
```

`--fix` prints the text with the fixable findings fixed and lists the rest on
stderr, with the lines and columns of the fixed text. It never writes a file
unless `--write` is given too, and keeps the file's line endings (CRLF stays
CRLF). Exit 0: no
findings (notes do not count). Exit 1: findings. Exit 2: a usage error, a bad
config or a glossary that cannot be read.
