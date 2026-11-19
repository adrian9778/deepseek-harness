#!/usr/bin/env bash
# launcher.sh — 长任务启动器通用骨架
# 只能由外部通过 run_in_background 调用，末尾必须有 wait
HERE="$(cd "$(dirname "$0")" && pwd)"
export HERE
# 切换到你项目的真实路径，example：
# SCRIPT="$(cd "$(dirname "$0")" && pwd)/../../scripts/my-long-task.sh"
SCRIPT="$(dirname "$0")/task-demo.sh"
# 后台跑 runner，launcher 所有子进程挂在这个 launcher 下；
# 末尾的 wait 保证本脚本不退出，子进程才不会被回收
bash "$HERE/runner.sh" "$SCRIPT" > "$HERE/log/launcher.log" 2>&1 &
echo "all launched"
wait   # 没有这行，上面的修复全部白搭
