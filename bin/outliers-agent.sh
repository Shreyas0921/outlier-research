#!/usr/bin/env bash
# Run one unattended Claude agent for an /outliers task in tmux, one outlier run at a time (for an always-on box;
# needs tmux, flock and the claude CLI).
# usage: outliers-agent "Run /outliers Sociology, review the notes, report"
# Log: ~/outlier-agent-logs/<name>.log ; <name>.log.done appears when it finishes.
# It waits up to an hour for /tmp/outliers.lock and holds it for the whole agent run (OUTLIERS_LOCK_HELD=1).
set -euo pipefail
PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[ $# -ge 1 ] || { echo "usage: outliers-agent \"<task>\""; exit 1; }
name="outl-agent-$(date +%Y%m%d-%H%M%S)"
dir="$HOME/outlier-agent-logs"; mkdir -p "$dir"
printf '%s\n' "$*" > "$dir/$name.task"
tmux new -d -s "$name" "cd '$PROJECT' && if flock -w 3600 /tmp/outliers.lock env OUTLIERS_LOCK_HELD=1 claude -p \"\$(cat '$dir/$name.task')\" --dangerously-skip-permissions > '$dir/$name.log' 2>&1; then :; else echo \"exit \$? (waited 1 h for /tmp/outliers.lock, or claude failed)\" >> '$dir/$name.log'; fi; touch '$dir/$name.log.done'"
echo "started $name"
echo "log: $dir/$name.log"
