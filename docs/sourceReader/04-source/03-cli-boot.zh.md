# CLI 启动流程源码解析

[English](03-cli-boot.md) | 中文

敲下 `dsh` 之后发生什么？这一篇拆开 CLI 的启动链路：入口怎么分派、参数怎么解析、Profile 怎么加载、失败怎么报告、进程怎么优雅退出。与第二层的全路径示例互补：那篇看全景，这篇看实现。

## 模块概览

八个文件按职责划分：入口、参数、启动、子命令、诊断、关机：

**源码目录**：`apps/cli/src/`

| 文件 | 职责 |
|---|---|
| `bin.ts` | CLI 主入口：参数分派 |
| `args.ts` | Commander 参数解析 |
| `profile-boot.ts` | Profile 启动：加载配置、叠加 patch、启动 Cordis |
| `plugin.ts` | `dsh plugin` 子命令实现 |
| `dump-config.ts` | `--dump-config` 实现 |
| `dump-config-schema.ts` | `--dump-config-schema` 实现 |
| `startup-diagnostics.ts` | 启动失败诊断报告 |
| `process-shutdown.ts` | 优雅关机处理 |

---

## bin.ts — 主入口

### `runCli(options)`（+26）

入口只做一件事：解析参数，然后按模式分派给四个子模块（动态 import，保证冷启动只加载用到的部分）：

```ts
export async function runCli(options: RunCliOptions = {}): Promise<void>
```

**执行流程**：

```
1. getDshRuntimeVersion() → version string
2. parseDshArgs(argv.slice(2), version, manageDesktopProfile)
   → DshInvocation（profile / plugin / dump-config / dump-config-schema）
3. switch(invocation.mode):
   case 'profile':
     import('./profile-boot.ts')
     runProfile({ environment, profile, fromDefaultProfile, patchFiles, args, ... })
     catch StartupError → reportStartupFailure() → process.exit(1)
   case 'plugin':
     import('./plugin.ts')
     process.exit(await runPlugin(profile, args, packageManager))
   case 'dump-config':
     import('./dump-config.ts')
     runDumpConfig(...)
   case 'dump-config-schema':
     import('./dump-config-schema.ts')
     await runDumpConfigSchema(...)
```

**关键设计**：CLI 自身只解析 launcher 拥有的参数（`--profile`、`--patch`、`--dump-*`），所有其他参数透传给被启动的 Profile 应用。这样各 Profile 可以定义自己的子命令而不互相干扰。

---

## args.ts — 参数解析

### `parseDshArgs(argv, version, manageDesktopProfile)`（+146）

解析规则一句话：先吃掉自己认识的标志，剩下的全给 Profile：

**返回类型**：`DshInvocation`（联合类型）

```ts
type DshInvocation =
  | ProfileInvocation    // { mode: 'profile', profile, args, patches, fromDefaultProfile? }
  | DumpConfigInvocation // { mode: 'dump-config', profile, defaultOnly, patches, fromDefaultProfile? }
  | DumpConfigSchemaInvocation
  | PluginInvocation     // { mode: 'plugin', profile, args }
```

**解析逻辑**（Commander）：

```
Launcher 自己的标志（先消费）：
  --profile <name>      或简写 <name>
  --patch <file>        可重复
  --dump-config
  --dump-default-config
  --dump-config-schema
  --from-default-profile <template>
  -h / --help
  -v / --version

第一个不被识别的 token → 开始 inner args
```

**特殊规则**：
- `rejectElectronProfile(program, profile)`：禁止直接从 CLI 启动 `desktop` profile
- `--patch` 不可用 variadic（否则会吞掉 inner args）
- Inner args 原样传递，由注入的 app plugin 解析

---

## profile-boot.ts — Profile 启动

启动的五步舞：环境变量 → Profile 配置 → 关机处理 → Cordis 装插件 → 等应用完成。

### `runProfile(options)`（profile-boot.ts +271）

```ts
export async function runProfile(options: RunProfileOptions): Promise<{ ctx: Context; shutdown: ProcessShutdown }>
```

**完整启动流程**：

```
1. loadLayeredEnv('dsh')
   → 读取 $DSH_HOME/.env
   → 读取项目根目录 .env
   → 合并到 process.env（不覆盖已有值）
   → 返回 LaunchEnvironmentSnapshot

2. composeProfile(profile, patchFiles, fromDefaultProfile, resolvedProfile)
   → resolveProfileDir → $DSH_HOME/profiles/<profile>/
   → createRuntimeResolution
   → loadOverlayPatches

3. createProcessShutdown(dispose)
   → SIGTERM → interrupt(0)；SIGINT → interrupt(130)
   → 优雅 dispose 超时后强制退出

4. boot(NAME, rootConfig, readProfilePatches(...), prepare)
   → new Context() + ctx.plugin(Loader)
   → mountRootInclude：cordis:include 条目树 + patch 层
   → prepare：profileContext、launch environment、PluginPackages、provideCmdline
   → loader.await() 按依赖顺序挂载全部条目

5. 等待应用完成（取决于具体 Profile）
   - headless: 等 Agent 完成 → 打印结果 → 退出
   - web: 启动 HTTP 服务器 → 保持运行
   - sdk: 启动 JSON-RPC server → 保持运行
```

### `initializeProfileFromDefault(name, fromDefaultProfile, home)`（+108）

从 shipped 模板重建自定义 Profile。用 staging 目录 + 原子 rename，失败不污染现有目录：

**用途**：从 shipped template 重建 custom profile。

**步骤**：
1. 验证 template 存在于 `PROFILE_TEMPLATES`
2. 在 `.<name>.staging-*` 目录中创建新 profile
3. 原子 rename 到目标位置
4. 失败时恢复原目录

---

## 启动失败处理

失败不是无声的：写结构化报告到日志目录，stderr 给出路径，退出码 1。

### `reportStartupFailure(error, context, write?)`（`startup-diagnostics.ts` +34）

**逻辑**：
1. 生成结构化错误报告（时间、版本、Node/平台、profile、error，含凭据警告）
2. 写入 `$DSH_HOME/logs/startup-<timestamp>-<uuid>.log`
3. 打印错误与诊断路径到 stderr
4. 调用方 `process.exit(1)`

---

## 进程关机

Ctrl+C 之后发生什么：优雅收尾而不是硬杀。

### `createProcessShutdown(ctx)`（`process-shutdown.ts` +22）

**注册信号处理**：
- `SIGINT` / `SIGTERM` → 触发 `interrupt()` → 优雅 dispose
- 超时（`PROCESS_SHUTDOWN_TIMEOUT_MS` = 5s）后强制退出

**设计原则**：关机是 graceful 的——不硬 kill，让应用树正常完成当前工作后再退出。
