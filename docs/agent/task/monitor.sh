#!/usr/bin/env bash
# monitor.sh — 持续观察长任务进度
# 用法: bash monitor.sh
# 按 Ctrl+C 结束监控
set -u
PID_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG="$PID_DIR/log/monitor.log"
PROGRESS=/tmp/task-progress
echo "=== monitor started at $(date +%Y-%m-%dT%H:%M:%S%z) ===" > "$LOG"
TOTAL=10
trap 'break;' INT
while true; do
  if [ -f "$PROGRESS" ]; then
    CUR=$(cat "$PROGRESS")
  else
    CUR=0
  fi
  if [ "$CUR" -ge "$TOTAL" ] 2>/dev/null; then
    echo "[$(date +%Y-%m-%dT%H:%M:%S%z)] TASK COMPLETE (progress $CUR/$TOTAL)" >> "$LOG"
    break
  fi
  echo "[$(date +%Y-%m-%dT%H:%M:%S%z)] progress $CUR/$TOTAL" >> "$LOG"
  sleep 5
done
echo "=== monitor ended at $(date +%Y-%m-%dT%H:%M:%S%z) ===" >> "$LOG"
