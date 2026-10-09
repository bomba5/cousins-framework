#!/bin/sh
# Harness hook: run before the agent harness compacts the conversation.
# Dumps the state a compacted session needs to re-orient into
# <home>/data/pre-compact-checkpoint.md, then prints a one-line JSON
# system message naming it (the shape harness hooks read from stdout).
#
# The home is the first argument when given (the per-cousin harness
# settings write it into the hook command, so the hook does not depend
# on the agent's environment), else COUSIN_HOME. COUSIN_SLUG is
# optional and defaults to the home's directory name. Writes only under <home>/data/. Without a home
# it says so and exits 0: a hook must never break the harness.

HOME_DIR="${1:-${COUSIN_HOME:-}}"
if [ -z "$HOME_DIR" ] || [ ! -d "$HOME_DIR" ]; then
  echo '{"systemMessage":"Context compaction imminent - COUSIN_HOME is not set, checkpoint skipped."}'
  exit 0
fi

SLUG="${COUSIN_SLUG:-$(basename "$HOME_DIR")}"
DATA="$HOME_DIR/data"
CHECKPOINT="$DATA/pre-compact-checkpoint.md"
TS=$(date '+%Y-%m-%dT%H:%M:%S%z')
mkdir -p "$DATA"

decisions_tail() {
  # Last five decisions, one bullet each; the raw lines if python3 is
  # not on PATH.
  tail -n 5 "$DATA/decisions.jsonl" 2>/dev/null | {
    if command -v python3 >/dev/null 2>&1; then
      python3 -c '
import json, sys
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        e = json.loads(line)
    except ValueError:
        continue
    print("- [%s] %s (why: %s)" % (e.get("topic", "?"),
                                   e.get("decision", ""),
                                   e.get("reasoning", "")))
'
    else
      sed 's/^/- /'
    fi
  }
}

{
  echo "# Pre-compaction checkpoint - $SLUG - $TS"
  echo
  echo "## Current activity"
  if [ -f "$DATA/last-activity.txt" ]; then
    cat "$DATA/last-activity.txt"
  else
    echo "Unknown."
  fi
  echo
  echo "## Recent decisions"
  if [ -f "$DATA/decisions.jsonl" ]; then
    decisions_tail
  else
    echo "None recorded."
  fi
  echo
  echo "## Files on disk"
  for f in CLAUDE.md MEMORY.md STATUS.md PROFILE.md; do
    if [ -f "$HOME_DIR/$f" ]; then
      echo "- $f: $(wc -l < "$HOME_DIR/$f" | tr -d ' ') lines"
    fi
  done
} > "$CHECKPOINT"

echo "{\"systemMessage\":\"Context compaction imminent. After compaction read data/pre-compact-checkpoint.md, then cousin-memory search <topic>.\"}"
