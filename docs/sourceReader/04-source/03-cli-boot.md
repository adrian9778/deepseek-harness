# CLI 启动流程源码解析

## 模块概览

**源码目录**：`apps/cli/src/`

| 文件 | 职责 |
|---|---|
| `bin.ts` | CLI 主入口：参数分派 |
| `args.ts` | Commander.js 参数解析 |
| `profile-boot.ts` | Profile 启动：加载配置、叠加 patch、启动 Cordis |
| `plugin.ts` | `dsh plugin` 子命令实现 |
| `dump-config.ts` | `--dump-config` 实现 |
| `dump-config-schema.ts` | `--dump-config-schema` 实现 |
| `startup-diagnostics.ts` | 启动失败诊断报告 |
| `process-shutdown.ts` | 优雅关机处理 |

---

## bin.ts — 主入口

### `runCli(options)`（+1-55）

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

**关键设计**：CLI 自身只解析 launcher 拥有的参数（`--profile`、`--patch`、`--dump-*`），所有其他参数透传给被启动的 Profile 应用。

---

## args.ts — 参数解析

### `parseDshArgs(argv, version, manageDesktopProfile)`（+80-250）

**返回类型**：`DshInvocation`（联合类型）

```ts
type DshInvocation =
  | ProfileInvocation    // { mode: 'profile', profile, args, patches, fromDefaultProfile? }
  | DumpConfigInvocation // { mode: 'dump-config', profile, defaultOnly, patches, fromDefaultProfile? }
  | DumpConfigSchemaInvocation
  | PluginInvocation     // { mode: 'plugin', profile, args }
```

**解析逻辑**（Commander.js）：

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

### `runProfile(options)`（profile-boot.ts +271）

```ts
export async function runProfile(options: RunProfileOptions): Promise<{ ctx: Context; shutdown: ProcessShutdown }>
```

**完整启动流程**：

```
1. loadLayeredEnv('dsh')
   → 读取 $DSH_HOME/.env
   → 读取项目根目录 .env
   → 合并到 process.env
   → 返回 LaunchEnvironmentSnapshot

2. resolveProfileDir(profile, home)
   → $DSH_HOME/profiles/<profile>/

3. readProfilePatches(profileDir)
   → 读取 package.json.dsh.profile.bundles → Bundle 列表
   → 读取 cordis.patch.yml → 用户覆盖层
   → 读取 cordis.yml（如果有）→ 基线配置

4. loadOverlayPatches(patches)
   → 解析 --patch 指定的额外 overlay 文件

5. createAppReady()
   → 创建 readiness 信号（boot 完成后再提交）

6. initProfile(tree, { patches, overlayPatches, ... })
   → 组装最终插件树
   → 空根配置 [] + 所有 patch 叠加

7. boot(composedTree, { onReady, environment, ... })
   → 启动 Cordis Loader
   → 按依赖顺序 apply 每个 Plugin
   → Loader 就绪后 commitAppReady()
   → 触发 ctx.cmdlineArgs 注入 inner args

8. 等待应用完成（取决于具体 Profile）
   - headless: 等 Agent 完成 → 打印结果 → 退出
   - web: 启动 HTTP 服务器 → 保持运行
   - sdk: 启动 JSON-RPC server → 保持运行
```

### `initializeProfileFromDefault(name, fromDefaultProfile, home)`（+100-200）

**用途**：从 shipped template 重建 custom profile。

**步骤**：
1. 验证 template 存在于 `PROFILE_TEMPLATES`
2. 在 `.<name>.staging-*` 目录中创建新 profile
3. 原子 rename 到目标位置
4. 失败时恢复原目录

---

## 启动失败处理

### `reportStartupFailure(error, { home, version, profile })`（`startup-diagnostics.ts`）

**逻辑**：
1. 检测常见失败模式（端口占用、权限、缺少依赖）
2. 生成结构化错误报告
3. 写入 `$DSH_HOME/logs/startup-failure.log`
4. 打印到 stderr
5. `process.exit(1)`

---

## 进程关机

### `createProcessShutdown(ctx)`（`process-shutdown.ts`）

**注册信号处理**：
- `SIGINT` / `SIGTERM` → 触发 `ctx.dispose()`
- 等待所有 Agent 完成（`whenIdle()`）
- 超时后强制退出

**设计原则**：关机是 graceful 的——不硬 kill，让 Agent Loop 正常完成当前 Step 后再退出。
