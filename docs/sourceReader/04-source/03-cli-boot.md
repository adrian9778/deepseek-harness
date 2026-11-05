# CLI launch path reference

English | [中文](03-cli-boot.zh.md)

敲下 `dsh` 之后发生什么？这一篇拆开 CLI 的启动链路：入口怎么分派、参数怎么解析、Profile 怎么加载、失败怎么报告、进程怎么优雅退出。与第二层的全路径示例互补：那篇看全景，这篇看实现。源码位置按"符号 + 偏移"标注（`+0` = 符号定义行，`+N` = 符号体内第 N 行，闭区间）。

## Module overview

九个文件按职责划分：入口（`bin.ts` 直接运行、`installed-bin.ts` 供已安装载体）、参数、启动、子命令、诊断、关机：

**Source directory**: `apps/cli/src/`

| 文件 | 职责 | 行数 |
|---|---|---|
| `bin.ts` | CLI 主入口：参数分派 | ~78 |
| `args.ts` | Commander 参数解析 | ~216 |
| `profile-boot.ts` | Profile 启动：加载配置、叠加 patch、启动 Cordis | ~353 |
| `plugin.ts` | `dsh plugin` 子命令实现 | ~108 |
| `installed-bin.ts` | 已安装 CLI 入口：转调 `runCli`，Windows 下先解析 8.3 短路径别名 | ~28 |
| `dump-config.ts` | `--dump-config` 实现 | ~76 |
| `dump-config-schema.ts` | `--dump-config-schema` 实现 | ~48 |
| `startup-diagnostics.ts` | 启动失败诊断报告 | ~68 |
| `process-shutdown.ts` | 优雅关机控制器 | ~77 |

---

## bin.ts — the main entry

### `runCli(options)` (bin.ts · runCli)

入口只做一件事：解析参数，然后按模式分派给四个子模块（动态 import，保证冷启动只加载用到的部分）：

```ts
export async function runCli(options: RunCliOptions = {}): Promise<void>
```

**执行流程**（`runCli +0~+48`）：

```
1. getDshRuntimeVersion() → version，并从 options 拆出 manageDesktopProfile
2. parseDshArgs(process.argv.slice(2), version, manageDesktopProfile) → DshInvocation
3. switch (invocation.mode):
   case 'profile':
     import('./profile-boot.ts')
     runProfile({ environment: loadLayeredEnv('dsh'), profile, fromDefaultProfile, patchFiles, args, ...profileOptions })
     catch StartupError → reportStartupFailure(error, { home, version, profile }) → process.exit(1)
   case 'plugin':
     import('./plugin.ts')
     process.exit(await runPlugin(profile, args, packageManager))
   case 'dump-config':
     import('./dump-config.ts')
     runDumpConfig(profile, defaultOnly, patches, fromDefaultProfile)
   case 'dump-config-schema':
     import('./dump-config-schema.ts')
     await runDumpConfigSchema(profile, patches, fromDefaultProfile)
   default:
     invocation satisfies never → 抛 unhandled invocation mode
```

**关键设计**：CLI 自身只解析 launcher 拥有的参数（`--profile`、`--patch`、`--dump-*`），所有其他参数透传给被启动的 Profile 应用。这样各 Profile 可以定义自己的子命令而不互相干扰。

**环境快照**：`loadLayeredEnv('dsh')` 在 profile 分支里取一次快照，随 `RunProfileOptions.environment` 传入 `runProfile`；插件看到的是这份不可变快照，而不是 `process.env` 的后续变化。

**自执行**：`import.meta.main` 为真时（`bin.ts` 作为脚本直接运行）调用 `runCli()`；磁盘启动器与嵌入式运行时走 `installed-bin.ts`，它在自己的模块树里 import 同一入口。

---

## args.ts — argument parsing

### `parseDshArgs(argv, version, manageDesktopProfile)` (args.ts · parseDshArgs)

解析规则一句话：先吃掉自己认识的标志，剩下的全给 Profile：

**返回类型**：`DshInvocation`（联合类型）

```ts
type DshInvocation =
  | ProfileInvocation          // { mode: 'profile', profile, args, patches, fromDefaultProfile? }
  | DumpConfigInvocation       // { mode: 'dump-config', profile, defaultOnly, patches, fromDefaultProfile? }
  | DumpConfigSchemaInvocation // { mode: 'dump-config-schema', profile, patches, fromDefaultProfile? }
  | PluginInvocation           // { mode: 'plugin', profile, args }
```

**解析逻辑**（Commander，`parseDshArgs +0~+70`）：

```
Launcher 自己的标志（先消费）：
  --profile <name>          或简写 <name>（首个非选项 token 不是 plugin 时补成 --profile <name>）
  --patch <path>            可重复；非 variadic（否则会吞掉 inner args）
  --dump-config
  --dump-default-config
  --dump-config-schema
  --from-default-profile <name>
  -V / --version

第一个不被识别的 token → 开始 inner args（原样交给被启动的 app）
```

**特殊规则**：
- `rejectElectronProfile(program, profile)`：`desktop` profile 由 Electron 应用独占，launcher 拒绝直接启动；`plugin` 子命令仅在 `manageDesktopProfile` 为真时放行
- `-h / --help` 不是 launcher 先消费的标志：只有裸 `dsh -h`（没有 profile 可交给 app）时打印 launcher 自己的 help，其余情况透传给 app
- 三个 dump 标志互斥；dump 是 boot-free 的，不接受 app 参数；`--dump-default-config` 不许叠加 `--patch`
- `plugin` 子命令要求 `--profile`，且至少给一个要转发给 pnpm 的参数
- help、version 与参数错误在 `parseDshArgs` 内部直接退出（`exitOverride` 后 `process.exit`），不会返回 `DshInvocation`
- Inner args 原样传递，由注入的 app plugin 解析

---

## profile-boot.ts — Profile launch

启动的五步：代理安装 → Profile 组合 → 关机与 fail-loud → Cordis 装插件 → 交还进程生命周期。

### `runProfile(options)` (profile-boot.ts · runProfile)

```ts
export async function runProfile(options: RunProfileOptions): Promise<{ ctx: Context; shutdown: ProcessShutdown }>
```

**完整启动流程**（`runProfile +0~+82`）：

```
1. installProxyFromEnvironment(options.environment, ...)
   → 在第一个插件挂载前装好 Node fetch 的代理（fetch 自己不读代理环境变量）
   → 从启动快照（bin.ts 里的 loadLayeredEnv('dsh')）解析，而不是 process.env

2. composeProfile(profile, patchFiles, fromDefaultProfile, resolvedProfile)
   → prepareProfile → resolveProfileDir → $DSH_HOME/profiles/<profile>/
   → 每次重写空根配置 cordis.yml（组合结果全部来自 patch 层）
   → createRuntimeResolution
   → loadOverlayPatches（--patch 覆盖层，按 argv 顺序）

3. createProcessShutdown(dispose) + 注册信号 + installFailLoud
   → SIGTERM → interrupt(0)；SIGINT → interrupt(130)
   → dispose 依次释放应用树与代理，失败聚合成 AggregateError
   → installFailLoud 兜住未捕获异常/拒绝，并先释放应用树
   → 启动窗口内信号同样生效，不等 boot() 结算

4. boot(NAME, rootConfig, readProfilePatches(...), prepare)
   → new Context() + ctx.plugin(Loader)
   → mountRootInclude：cordis:include 条目树 + patch 层
   → prepare：profileContext、launch environment、PluginPackages、provideCmdline（args / exit / ready）
   → loader.await() 按依赖顺序挂载全部条目

5. 提交 appReady 并返回 { ctx, shutdown }
   → 仅在未收到信号、fiber 仍 ACTIVE、loader 已就位时提交 ready
   → 进程生命周期交给已挂载插件（或一次性 runner）
   → 启动失败：先 dispose 再抛原错误；清理也失败则抛 AggregateError
```

### `initializeProfileFromDefault(name, fromDefaultProfile, home)` (profile-boot.ts · initializeProfileFromDefault)

从 shipped 模板重建自定义 Profile。用 staging 目录 + 原子 rename，失败不污染现有目录：

**用途**：从 shipped template 重建 custom profile。

**步骤**：
1. 验证 template 存在于 `PROFILE_TEMPLATES`，否则列出可选模板报错
2. 拒绝与 shipped profile 同名（大小写不敏感比较）
3. 在 `.<name>.staging-*` 目录中按 `template.bundles` 建好新 profile
4. 先把旧目录 rename 到 `previous/`，再把新目录 rename 到目标位置
5. 失败时恢复旧目录；目标已被并发重建时判定对方胜出；恢复也失败抛 `AggregateError` 并保留 staging 路径

---

## Startup failure handling

失败不是无声的：写结构化报告到日志目录，stderr 给出路径，退出码 1。

### `reportStartupFailure(error, context, write?)` (startup-diagnostics.ts · reportStartupFailure)

**逻辑**（`reportStartupFailure +0~+34`）：
1. 生成结构化报告：凭据警告 + timestamp / dshVersion / nodeVersion / platform / arch / profile / error（`inspect` 全展开，不截断数组与字符串）
2. 先把 `error.message` 写到 stderr，再把报告写入 `$DSH_HOME/logs/startup-<ISO 时间戳，冒号换横线>-<uuid>.log`（目录 0700、文件 0600、`wx` 独占创建）
3. 写入失败时把完整报告打到 stderr 而不是声称已保存路径；成功则打印完整诊断路径
4. 调用方 `process.exit(1)`（`bin.ts` 的 `StartupError` 分支）

---

## Process shutdown

Ctrl+C 之后发生什么：优雅收尾而不是硬杀。

### `createProcessShutdown(dispose, forceExit?, complete?, timeoutMs?)` (process-shutdown.ts · createProcessShutdown)

`createProcessShutdown` 只造关机控制器，不注册信号；信号由 `runProfile` 注册（SIGTERM → `interrupt(0)`、SIGINT → `interrupt(130)`）。

**两种收尾**（`createProcessShutdown +0~+55`）：
- `shutdown(code)`：优雅 dispose 后记自然完成码（`process.exitCode`），不强制退出
- `interrupt(code)`：优雅 dispose 后强制退出；关机已在进行时再收到 interrupt 立即强制退出（升级）

**兜底**：`PROCESS_SHUTDOWN_TIMEOUT_MS`（5s）超时强制退出；dispose 抛错也强制退出。

**设计原则**：关机是 graceful 的——不硬 kill，让应用树正常完成当前工作后再退出。
