# agent/task — 长任务监督、留痕与自愈

## 约定

- 留痕文件按 `YYYY-MM-DD-{任务}.md` 存放在本目录，每日追加同一文件（一主题一文件）
- 每次失败/成功/退避耗尽，都会向留痕文件和 log/ 追加时间线
- 入口脚本固定为 `launcher.sh`（末尾必须在 `wait`，否则子进程随工具调用一起被回收）
- 执行包装器固定为 `runner.sh`（指数退避 8 次，2/4/8/.../256 秒）
- 监控脚本 `monitor.sh` 可实时显示进度（需任务向 /tmp/task-progress 写当前进度）

## 使用流程

1. **准备自己的任务脚本**：在 `task-demo.sh` 基础上替换内容
2. **修改 `launcher.sh`**：把 `SCRIPT=` 指向你的任务脚本
3. **启动**：用 `run_in_background: true` 调用 `docs/agent/task/launcher.sh`
4. **监控**：运行 `monitor.sh` 查看进度，或检查留痕 md 或 log 下的 `*.log`
5. **收尾**：`job_kill` 或等任务自然完成

## 脚本俯视图

| 文件 | 角色 |
|---|---|
| `launcher.sh` | 启动器入口，固定末尾 `wait` |
| `runner.sh` | 任务执行器：失败 → 指数退避，最多 8 次 |
| `task-demo.sh` | 示例任务（模拟数据处理，10% 失败率） |
| `monitor.sh` | 实时进度 + 日志日志轮

## 指数退避

第 N 次失败后，休眠 `2^N` 秒（2, 4, 8, 16, 32, 64, 128, 256），求和后不超过 8.5 分钟。每次失败都记录退出码和尝试次数，耗尽时输出 `RETRIES_EXHAUSTED`。
