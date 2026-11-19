#!/usr/bin/env bash
# 示例长任务：模拟数据处理，中间有阶段性输出，10% 概率失败
set -u
TOTAL=10
for i in $(seq 1 $TOTAL); do
  echo "[$(date +%Y-%m-%dT%H:%M:%S%z)] progress $i/$TOTAL"
  echo "$i" > /tmp/task-progress
  sleep 3
done
echo "[$(date +%Y-%m-%dT%H:%M:%S%z)] DONE ALL $i/$TOTAL"
echo "$TOTAL" > /tmp/task-progress
exit 0
