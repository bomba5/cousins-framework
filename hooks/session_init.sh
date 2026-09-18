#!/bin/sh
# Harness hook: run when a session starts. Prints a small who/where/what
# banner to stdout so the session begins knowing its slug, its home, and
# which identity files and checkpoints exist on disk. It reminds; it does
# not cat the files (the harness delivers them itself).
#
# The home is the first argument when given (the per-cousin harness
# settings write it into the hook command), else COUSIN_HOME.
# COUSIN_SLUG is optional; the slug defaults to the home's directory
# name. Writes nothing. Without a home it prints the
# banner without file details and exits 0.

HOME_DIR="${1:-${COUSIN_HOME:-}}"
SLUG="${COUSIN_SLUG:-}"
if [ -z "$SLUG" ] && [ -n "$HOME_DIR" ]; then
  SLUG=$(basename "$HOME_DIR")
fi

echo "=== COUSIN SESSION START ==="
echo "slug: ${SLUG:-<unknown>}"
echo "home: ${HOME_DIR:-<unknown>}"
echo "time: $(date '+%Y-%m-%dT%H:%M:%S%z')"
if [ -n "$HOME_DIR" ] && [ -d "$HOME_DIR" ]; then
  echo
  echo "--- identity on disk ---"
  for f in CLAUDE.md MEMORY.md STATUS.md PROFILE.md; do
    if [ -f "$HOME_DIR/$f" ]; then
      lines=$(wc -l < "$HOME_DIR/$f" 2>/dev/null | tr -d ' ')
      echo "  $f (${lines:-?} lines)"
    fi
  done
  for f in session-checkpoint.md pre-compact-checkpoint.md handoff.md; do
    if [ -f "$HOME_DIR/data/$f" ]; then
      echo "  data/$f (read it first)"
    fi
  done
  echo
  echo "Tools: cousin-memory search|decide|activity, cousin-session start|end"
fi
echo "=== END ==="
