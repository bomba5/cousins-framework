#!/bin/sh
# Harness hook: run when a session stops. Writes
# <home>/data/session-checkpoint.md so the next session can recover
# context quickly, then prints a one-line JSON system message naming it.
#
# The home is the first argument when given (the per-cousin harness
# settings write it into the hook command), else COUSIN_HOME.
# COUSIN_SLUG is optional and defaults to the home's directory name. Writes only under <home>/data/. Without a home
# it says so and exits 0: a hook must never break the harness.

HOME_DIR="${1:-${COUSIN_HOME:-}}"
if [ -z "$HOME_DIR" ] || [ ! -d "$HOME_DIR" ]; then
  echo '{"systemMessage":"Session ending - COUSIN_HOME is not set, checkpoint skipped."}'
  exit 0
fi

SLUG="${COUSIN_SLUG:-$(basename "$HOME_DIR")}"
DATA="$HOME_DIR/data"
CHECKPOINT="$DATA/session-checkpoint.md"
TS=$(date '+%Y-%m-%dT%H:%M:%S%z')
mkdir -p "$DATA"

decisions_tail() {
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
  echo "# Session checkpoint - $SLUG - $TS"
  echo
  echo "## What was happening"
  if [ -f "$DATA/last-activity.txt" ]; then
    cat "$DATA/last-activity.txt"
  else
    echo "No activity recorded."
  fi
  echo
  echo "## Open work (from STATUS.md)"
  if [ -f "$HOME_DIR/STATUS.md" ]; then
    # Open and in-progress checkboxes only; closed items are history.
    grep -E '^- \[[ ~]\]' "$HOME_DIR/STATUS.md" 2>/dev/null | head -n 20
  else
    echo "No STATUS.md."
  fi
  echo
  echo "## Last decisions"
  if [ -f "$DATA/decisions.jsonl" ]; then
    decisions_tail
  else
    echo "None recorded."
  fi
} > "$CHECKPOINT"

echo "{\"systemMessage\":\"Session ending. Checkpoint at data/session-checkpoint.md; the next session reads it first.\"}"
