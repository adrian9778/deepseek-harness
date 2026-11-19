#!/usr/bin/env bash
# runner.sh — 任务执行包装器：重试（指数退避 8 次）+ 留痕 + 健康监控
# 固定写到本文件所在目录的 log/ 和 YYYY-MM-DD-{任务}.md（与业务脚本在哪无关）
# 用法: bash runner.sh <任务脚本路径>
set -u

SCRIPT="$1"
if [ ! -f "$SCRIPT" ]; then
  echo "error: script not found: $SCRIPT"
  exit 2
fi
TASK_NAME="$(basename "$SCRIPT" .sh)"
DATE="$(date +%Y-%m-%d)"
HERE="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$HERE/log"
mkdir -p "$LOG_DIR"
# 留痕文件放在 runner.sh 同目录（docs/agent/task/YYYY-MM-DD-{任务}.md）
HISTORY="$HERE"

retry=0; max=8; base=2
run() { bash "$SCRIPT"; return $?; }
while true; do
  run
  rc=$?
  ts="$(date +%Y-%m-%dT%H:%M:%S%z)"
  if [ "$rc" -eq 0 ]; then
    echo "[$ts] OK (attempt $((retry+1)))" >> "$LOG_DIR/$TASK_NAME.log"
    echo "[$ts] TASK_SUCCESS attempt=$((retry+1))" >> "$HISTORY/$DATE-$TASK_NAME.md"
    break
  fi
  echo "[$ts] FAIL rc=$rc (attempt $((retry+1))/8)" >> "$LOG_DIR/$TASK_NAME.log"
  echo "[$ts] TASK_FAILURE rc=$rc" >> "$HISTORY/$DATE-$TASK_NAME.md"
  if [ "$retry" -ge "$max" ]; then
    echo "[$ts] RETRIES_EXHAUSTED (8 attempts, final rc=$rc)" >> "$HISTORY/$DATE-$TASK_NAME.md"
    break
  fi
  sleep "$((base ** retry))"
  retry=$((retry+1))
done
