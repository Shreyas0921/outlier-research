#!/usr/bin/env bash
# Keep finding outliers until a goal number of new, kept notes is reached.
# Built for an always-on Linux box (needs tmux, flock, timeout and the claude CLI); on macOS: brew install tmux flock coreutils
# and put coreutils' gnubin on PATH so `timeout` exists.
#
# usage: outliers-hunt <goal> [--budget CREDITS (default 20 x goal)] [--name ID] [--batch-minutes 45] [--max-batches 40]
#                      [--model claude-sonnet-5-5] [--effort low|medium|high] (default: Claude Code's own)
#        outliers-hunt --resume <id> [same options]
#
# Starts itself in tmux (session hunt-<id>) and returns. Each batch is a fresh Claude agent that follows the
# /outliers skill's "Hunt mode" with OUTLIER_HUNT=<id>, so the tool counts its notes and credits. The loop stops
# when the goal is reached, the budget is spent, 3 batches in a row add nothing, --max-batches is hit, or
# someone creates state/hunts/<id>.stop. Logs: ~/outlier-agent-logs/hunt-<id>/. Progress:
#   .venv/bin/python run.py hunt status <id>   (from this project folder)
# Report: reports/hunt-<id>.md in this project folder.
set -euo pipefail
PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$PROJECT/.venv/bin/python"
SELF="$PROJECT/bin/outliers-hunt.sh"

goal="" budget="" name="" resume="" mins=45 max_batches=40 inside="" model="" effort=""
while [ $# -gt 0 ]; do
  case "$1" in
    --budget) budget="$2"; shift 2 ;;
    --name) name="$2"; shift 2 ;;
    --resume) resume="$2"; shift 2 ;;
    --batch-minutes) mins="$2"; shift 2 ;;
    --max-batches) max_batches="$2"; shift 2 ;;
    --model) model="$2"; shift 2 ;;
    --effort) effort="$2"; shift 2 ;;
    --inside) inside=1; shift ;;
    -h|--help) sed -n '2,14p' "$SELF"; exit 0 ;;
    *) goal="$1"; shift ;;
  esac
done
cd "$PROJECT"

if [ -z "$inside" ]; then
  if [ -n "$resume" ]; then id="$resume"; "$PY" run.py hunt status "$id" >/dev/null
  else
    [ -n "$goal" ] || { echo "usage: outliers-hunt <goal> [--budget N] ..."; exit 1; }
    budget="${budget:-$((goal * 20))}"  # default ~20 credits per outlier
    args=(hunt start --goal "$goal" --budget "$budget"); [ -n "$name" ] && args+=(--name "$name")
    id=$("$PY" run.py "${args[@]}")
  fi
  mkdir -p "$HOME/outlier-agent-logs/hunt-$id"
  tmux new -d -s "hunt-$id" "bash '$SELF' --inside --resume '$id' --batch-minutes '$mins' --max-batches '$max_batches' --model '$model' --effort '$effort' > '$HOME/outlier-agent-logs/hunt-$id/loop.log' 2>&1"
  echo "hunt $id started (tmux session hunt-$id)"
  echo "status: cd $PROJECT && .venv/bin/python run.py hunt status $id"
  echo "stop:   touch $PROJECT/state/hunts/$id.stop"
  exit 0
fi

id="$resume"
logs="$HOME/outlier-agent-logs/hunt-$id"; mkdir -p "$logs"
report="$PROJECT/reports/hunt-$id.md"
field() { "$PY" run.py hunt status "$id" --json | python3 -c "import json,sys; v=json.load(sys.stdin)['$1']; print(', '.join(v) if isinstance(v, list) else v)"; }
stall=0
agent_opts=()
[ -n "$model" ] && agent_opts+=(--model "$model")
[ -n "$effort" ] && agent_opts+=(--effort "$effort")
reason=""
echo "$(date -u +%FT%TZ) hunt $id loop started (model ${model:-default}, effort ${effort:-default})"
while true; do
  if [ "$(field done)" = "True" ]; then reason="$(field done_reasons)"; break; fi
  batch=$("$PY" run.py hunt batch "$id")
  if [ "$batch" -gt "$max_batches" ]; then reason="hit --max-batches $max_batches"; break; fi
  before=$(field kept)
  prompt="You are batch $batch of outlier hunt $id. Goal: $(field goal) new outliers; $before kept so far; credits $(field credits_used) of $(field budget) used. It is $(date -u +%H:%M) UTC now. Follow the 'Hunt mode' section of the /outliers skill exactly; your report is $report. Work for about $mins minutes (check the time with date), then update the plan file and the report and stop. OUTLIER_HUNT and OUTLIERS_LOCK_HELD are already set for you."
  echo "$(date -u +%FT%TZ) batch $batch starting (kept $before)"
  set +e
  flock -w 3600 -E 99 /tmp/outliers.lock env OUTLIER_HUNT="$id" OUTLIERS_LOCK_HELD=1 \
    timeout "$((mins + 30))m" claude -p "$prompt" "${agent_opts[@]}" --dangerously-skip-permissions > "$logs/batch-$batch.log" 2>&1
  code=$?
  set -e
  if [ "$code" -eq 99 ]; then
    echo "$(date -u +%FT%TZ) batch $batch never started: another outlier run held the lock for an hour; waiting again"
    continue  # not a stall: nothing ran
  fi
  after=$(field kept)
  echo "$(date -u +%FT%TZ) batch $batch ended (exit $code, kept $before -> $after)"
  if [ "$after" -le "$before" ]; then stall=$((stall + 1)); else stall=0; fi
  if [ "$stall" -ge 3 ]; then reason="stalled: 3 batches in a row kept nothing new"; break; fi
done
summary=$("$PY" run.py hunt status "$id")
echo "$(date -u +%FT%TZ) hunt $id finished: $reason"
mkdir -p "$(dirname "$report")"
printf '\n## Hunt finished (%s)\n%s\n\n```\n%s\n```\n' "$(date -u '+%Y-%m-%d %H:%M UTC')" "Reason: $reason" "$summary" >> "$report"
