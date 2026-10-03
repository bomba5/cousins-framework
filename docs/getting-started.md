# Getting started

You finished one of the README's quick starts and said hi to Wren. This
page is the next hour: what is running now, how you talk to Wren, what it
remembers, what happens when its session ends, and how you keep it healthy,
upgrade it and back it up. Every section is short and links the page that
owns the details. The last section names everything you can leave off.

The commands here run in two places, depending on your quick start:

- **Bare host**: in the checkout, with its venv active and `FRAMEWORK_ROOT`
  set to the checkout. Some commands find the install from the directory
  they run in, but not all of them do, so set it once per shell:

  ```
  cd ~/cousins-framework && . .venv/bin/activate && export FRAMEWORK_ROOT=$PWD
  ```

- **Docker**: put `docker compose exec framework` in front, from the
  checkout you ran `docker compose up` in. Inside the container the install
  is `/data`, and the commands run there.

So `cousin-health` is `docker compose exec framework cousin-health` on
Docker. The examples use the quick start's names: a
[cousin](glossary.md#cousin) called Wren (slug `wren`) and a console user
called ana.

## What you have now

One process runs everything: the
[supervisor](glossary.md#supervisor), `cousin-supervisor`. In Docker it is
the container's main process. On a bare host it is the `cousin-supervisor
run &` you started, and it stops when that terminal closes; to have it start
with the machine and come back after a reboot, install the systemd units
([install, step 6](install.md#6-the-systemd-units)). The supervisor keeps
three kinds of children up and restarts one that crashes:

- **the console** (`console`): the web UI on `http://127.0.0.1:8600`.
- **the loops daemon** (`loops`): the clock. Every hour it sends Wren a
  heartbeat (a short prompt with whatever changed in its identity, status and
  memory files, answered in one line), once a day it runs Wren's
  [flip](glossary.md#flip), and it fires anything scheduled.
- **one [runner](glossary.md#runner) per cousin** (`runner:wren`): the
  process that holds Wren's model session and answers its messages.

See them, with their state and how often each was restarted:

```
cousin-supervisor status
```

Everything Wren is lives in its home directory: `cousins/wren/` in the
checkout on a bare host, `/data/cousins/wren/` in the container (on the
`cousins-framework_framework-data` volume). Its identity is `CLAUDE.md`, its
settings `cousin.toml`, its open work `STATUS.md`, its memory `memory/` and
`MEMORY.md`, and its chat history and runtime state are under `data/`. The
full list is in [cousins](cousins.md#what-a-cousin-is). `CLAUDE.md` is yours
to edit; a new session reads it.

Wren costs something while you are away: every heartbeat and every flip is a
[turn](glossary.md#turn) on the account it runs on. The README's
[cost section](../README.md#what-it-costs-and-what-it-does-unattended) says
what wakes it and how to turn each one down.

## Talking to it

Open the console, log in as ana and click Wren in the sidebar. What you type
goes into Wren's [inbox](glossary.md#inbox), and its runner answers it in a
turn: one run of the model on Wren's session, from your message to its
answer. Your conversation with Wren is one
[thread](glossary.md#thread), `operator:ana`; a heartbeat or a schedule has a
thread of its own and does not show up in yours.

Wren answers with its `reply` tool, which writes the answer into its chat
history, where the console shows it. Text the model writes without that tool
is its working-out, not a message to you: watch it in the console's
[reasoning pane](console.md#the-reasoning-pane-a-runner-cousin), or from a shell with `cousin-watch wren --follow`. A message you send
while Wren is busy is not lost: it joins the turn already running or waits in
the inbox for the next one. How a message travels, and how cousins talk to
each other, is in [chat](chat.md).

## What it remembers, and how to see it

Wren's memory is in its home, and Wren writes it itself. You tell it what to
keep in chat ("remember that the espresso machine needs descaling every 200
shots") and it records that with its memory tool. Each entry carries a truth
level: `operator` for something you said (with where you said it),
`conclusion` for what Wren worked out, `hypothesis` for a guess, and a few
more. That way neither of you mistakes a guess for a fact.

To see it, open the console's Memory page and Wren's tab: the entries by
truth level, the layers and the files
([console](console.md#memory)). From a shell:

```
cousin-memory --home cousins/wren search "espresso"
```

Search for something you told it; a cousin you just made has little to find.

`--home` names whose memory to read; without it (or `COUSIN_HOME`) the
command refuses rather than guess. Start with
[memory's short version](memory.md#the-short-version); the
[truth levels](memory.md#truth-levels) have a table.

## What happens at a new session

A model session cannot grow forever: its context fills. So Wren lives in
generations. When its context is 80% full (`[agent] rollover_at_percent`)
the runner ends the session itself (a [rollover](glossary.md#rollover)), and
once a day, at 04:00 unless you change it, the loops daemon does the same
(the daily flip). That is 04:00 on the clock of the machine the supervisor
runs on; in the container, UTC.
Either way Wren first writes a handoff, and the next session starts from a
boot packet built from disk: its identity, the open loops in `STATUS.md`,
the handoff and its memory. Nothing it wrote down is lost; what it did not
write down is. A restart is different: it resumes the same session, so the
conversation carries on.

```
cousin-loops flips            # when each cousin flips, and why that time
cousin-flip wren --dry-run    # what a flip now would do; drop --dry-run to flip
```

`flip_at` under `[lifecycle]` in Wren's `cousin.toml` moves its daily flip
(`"never"` turns it off). The whole story is in
[cousins](cousins.md#generations-and-the-flip).

## Keeping it healthy

```
cousin-health
#   -> FAIL  runner:wren  backoff since 2026-10-03T06:12:09+00:00  exited (code 1)
#      31 ok, 1 failing
```

`cousin-health` lists what has been failing and since when, then counts the
rest; `--all` lists those too. It exits 1 when anything fails, so a monitor
can call it. The console shows the same as a red count in its top bar, and
its System page lists every child of the supervisor with its state, a start
and a stop ([console](console.md#system)). `cousin-doctor` checks the
install for things to fix by hand, such as a cousin home other users can
read, and prints the command that fixes each; it changes nothing.

When something is wrong, read the logs first. On Docker:
`docker compose logs -f framework`. On a bare host: the terminal the
supervisor runs in, or `journalctl --user -u cousin-supervisor.service`
once it runs under systemd. Each line starts with the child that wrote it
(`runner:wren`). [Operations](operations.md#troubleshooting) goes from the
outside in.

## Upgrading

On a bare host, `cousin-upgrade` does the whole upgrade from the release
tags in your checkout. Fetch them first, or the plan only knows the releases
you already had:

```
git fetch --tags
cousin-upgrade --dry-run             # the plan: what changes, what restarts; writes nothing
cousin-upgrade --apply-homes --yes   # bring each cousin's tool list (its home's mcp-registry.toml) to the release
cousin-upgrade --switch              # move the code, reinstall, restart in order
cousin-tool-surface                  # refresh the command list (next section)
cousin-shared templates              # your law and house rules against the new release
```

The dry run reads the newest release tag and prints the changelog up to it,
whether the dependencies changed, each cousin's changes and the restarts in
order. Read it before the next two. `--apply-homes` and `--switch` ask
before they change anything (`--yes` answers for you). `--switch` refuses
while tracked files in the checkout have changes of yours (your
`config/` and `cousins/` are not tracked and never count), and refuses a dependency change unless you add
`--deps`; it restarts the loops daemon, the console and each runner one at a
time and stops at the first that does not come back on the new release,
printing how to roll back. The supervisor itself keeps its old code until
you restart it, which CHANGELOG.md says when to do (the `--dry-run` plan prints that release's entries): under systemd,
`systemctl --user restart cousin-supervisor.service`; started by hand, stop
it and run `cousin-supervisor run &` again. Restarting it restarts every
cousin, and each resumes its session.

After a switch the checkout sits on the release tag, not on a branch, so a
plain `git pull` no longer applies: keep upgrading with the steps above.

An upgrade never touches the [law](house-rules.md#the-framework-law)
(`config/law.md`, the rules every cousin reads first) or the house rules the
install was seeded with, because you may have edited them.
`cousin-shared templates` lists each one that differs from what the new
release ships (`--full` prints the diff); merge what you want by hand
([house rules](house-rules.md#how-they-arrive)).

In Docker, `cousin-upgrade` cannot plan: the image carries no git history.
The upgrade is `git pull`, `docker compose build` and
`docker compose up -d` in the checkout; the volume keeps every cousin, and
each one resumes its session. Then run `cousin-shared templates` as above.
Both ways, step by step, are in [operations](operations.md#upgrades).

## Backing up

```
cousin-backup --home cousins/wren --dest ~/backups/cousins
```

That writes `~/backups/cousins/wren/<date>/`: every database copied safely
while Wren runs, its memory, `MEMORY.md`, `STATUS.md` and `CLAUDE.md`. It
does not copy `cousin.toml` or `notes/`, so copy the rest of the home too;
[operations](operations.md#backups) has the lines for all of it and how
to restore. In Docker, back up inside the container and copy it out:

```
docker compose exec framework cousin-backup --home cousins/wren --dest /data/backups
docker compose cp framework:/data/backups ./backups
```

The console's System page has a backup tab that does the same for the
cousins you pick. Nothing runs a backup on a schedule: where your copies go
is your call, so put the command in a timer or a cron job of your own.

## The two quick-start steps, explained

The bare-host quick start ran two commands between making Wren and starting
it. The Docker one skipped them: its Wren runs on opencode, with no Claude
Code to approve anything, and the command list below can be written at any
time.

**`cousin-mcp approve wren`.** Claude Code does not start the tool servers a
project's `.mcp.json` names until someone has trusted the folder and approved
each server, so a repository you clone cannot run programs just by being
opened. `cousin-spawn` wrote such a `.mcp.json` in Wren's home: it starts
`cousin-mcp`, which serves Wren's tools (memory, chat, jobs, schedules).
Wren works unattended, so nobody would be there to answer that prompt.
`approve` records the answer ahead of time, in the file
`config/harness.toml` names as `settings_file` (`~/.claude.json` with the
quick start's preset): the home is trusted and its `cousin` server enabled.
That file exists once Claude Code has run once, which is why the quick start
asks for a logged-in Claude Code. On the default runner kind, `sdk`, the
runner builds Wren's tools in its own process and reads no settings file;
the approval is what a Claude Code session started in the home needs (a
cousin on the `tmux` kind, or you running `claude` there), and doing it now
means nothing stops at a prompt later. More in [mcp](mcp.md#how-its-wired).

**`cousin-tool-surface`.** It writes `data/tool-surface.md` under the
install: one line per `cousin-*` command with the first line of its
`--help`. Wren's `CLAUDE.md` tells it to read that list instead of running
every command's `--help` to find out what it can do, which costs time and
tokens. The
systemd units refresh it daily; without them, nothing does, so run it again
after every upgrade (the upgrade steps above include it). In Docker you can
run it the same way. See
[operations](operations.md#the-tool-surface-manifest).

## What is optional

Everything above is the core: one cousin, run, talked to, kept and upgraded.
Wren needs none of what follows; each is off until you turn it on or reach
for it, and each page says so at the top.

- **Telegram**: Wren's chat on your phone, through `cousin-telegram`
  ([telegram](telegram.md)).
- **Meetings**: a chat with several cousins at once, in rounds,
  `cousin-meeting` ([meetings](meetings.md)).
- **Media**: images, voice and video through a provider you configure,
  `cousin-image`, `cousin-voice`, `cousin-video` ([media](media.md)).
- **Remote cousins**: a cousin on another machine, `cousin-hive` and
  `cousin-spawn-node` ([remote cousins](remote-cousins.md)).
- **Plugins**: tools, a service and a console page the framework runs but
  does not ship ([plugins](plugins.md)).
- **Dreaming**: a background pass that tidies a cousin's memory, off by
  default ([memory](memory.md#dreaming)).
- **Semantic search**: memory search by meaning, through an embedding
  service; Docker runs one for you ([memory](memory.md#semantic-search)).
- **The [shared tier](glossary.md#shared-tier)**: files every cousin reads, reviewed before they land
  ([memory](memory.md#the-shared-tier)).
- **More cousins**: `cousin-spawn` again, with another slug; they can then
  message each other with `cousin-chat` ([cousins](cousins.md#spawning-one),
  [chat](chat.md#cousin-to-cousin)).
- **Jobs and loops beyond the defaults**: recurring prompts of your own,
  one-shot schedules (`cousin-schedule`), background jobs (`cousin-job`)
  and the tracker (`cousin-tracker`) ([jobs and loops](jobs-and-loops.md)).
- **Other runner kinds**: the `tmux` pane and opencode beside the default
  `sdk`, and moving a cousin between them with `cousin-migrate`
  ([runners](reference/runners.md)).
- **Migrating**: bringing in a cousin you already have, chat history
  included, with `cousin-migrate` and `cousin-chat-import`
  ([migrating](migrating.md)).

Every command, with its class (core, optional, cousin, internal or
developer), is in [commands](commands.md#which-commands-you-need).
