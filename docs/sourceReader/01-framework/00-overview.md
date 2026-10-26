# 项目整体架构总览

## 一句话描述

DeepSeek Harness（简称 dsh）是一个基于插件架构的 Agent 运行时框架。用户通过 `dsh` 命令行启动一个 **Profile**（如 `web`、`headless`、`sdk`），框架加载一组 **Bundle** 作为插件层，组装出完整的 Agent 系统：负责管理 **Session**（对话日志）、驱动 **Agent Loop**（多轮推理循环）、调用 **LLM 模型**、执行 **Tools**（工具）。

---

## 三大抽象概念

### 1. Profile（配置文件集合）

Profile 是启动时的完整插件树描述。每个 Profile 以 `package.json` 中的 `dsh.profile.bundles` 字段声明自身依赖的 Bundle 列表，外加用户自定义的 `cordis.patch.yml` 覆盖层。

| 内置 Profile | 用途 |
|---|---|
| `web` | Web GUI 模式，浏览器里运行 Agent |
| `headless` | 无头模式，一条任务执行完即退出 |
| `sdk` | JSON-RPC SDK 服务器模式 |
| `sdk-minimal` | 精简 SDK，不含完整 base 树 |
| `acp` | 自动化专用 ACP（Agent Client Protocol）服务器 |

### 2. Bundle（插件层）

Bundle 是可插拔的功能层。`dsh-base` 是所有 base-backed Profile 的共享核心层，提供：模型接入、工具集、Session 持久化、沙箱、权限策略。上层 Bundle（如 `dsh-web-app`、`dsh-headless`）在 base 之上叠加特定功能。

```
Profile: web
  └─ dsh-web-app       ← 叠加：Web GUI 相关插件
      └─ dsh-base       ← 核心：LLM + 工具 + Session + 沙箱 + 权限
```

### 3. Session（对话日志）

Session 是整个系统的核心数据结构。它是一个 **append-only 事件日志**，记录所有模型可见的事实：`user/message`、`assistant/message`、`tool/result`、`system/message` 等。模型看到的对话历史不是单独存储的，而是从日志中**实时推导**（`deriveMessages()`）。

---

## 模块地图

```
apps/cli/src/
  bin.ts              ← CLI 入口：解析参数，分派到 profile-boot / plugin / dump-config
  profile-boot.ts     ← Profile 启动：加载 .env，叠加 patch 层，启动 Cordis Loader
  args.ts             ← 命令行参数解析（Commander.js）
  startup-diagnostics.ts  ← 启动失败诊断报告

packages/
  core/           ← 产品 API 脊柱
    agent-loop/   ← Agent 默认驱动：Turn/Step 状态机
    agent/        ← Agent 句柄接口 + 注册表
    session/      ← Append-only 事件日志 + 内存存储
    tools/        ← 工具注册表 + 执行管线
    system-prompt/ ← 系统提示词组装
    scope/        ← Agent 作用域注册原语
  llm/            ← LLM 能力族：抽象服务 + 各 Provider 适配器
  shell/          ← Shell 执行能力族（bash/pwsh，local/sandbox）
  sandbox/        ← 进程隔离（bwrap/Landlock/Seatbelt）
  bundle/         ← 可安装 Profile Bundle
  boot/           ← CLI 启动胶水
  ...             ← 其他能力包（见 packages/README.md）
```

---

## 整体运行流程（文本图）

```
用户输入: dsh headless "帮我分析一下代码"
         │
         ▼
┌──────────────────────────────────────────────┐
│ apps/cli/src/bin.ts                          │
│   parseDshArgs(argv)  →  ProfileInvocation   │
│   runProfile({profile: 'headless', args: [...]}) │
└──────────────────┬───────────────────────────┘
                   │
                   ▼
┌──────────────────────────────────────────────┐
│ apps/cli/src/profile-boot.ts                 │
│   loadLayeredEnv('dsh')  →  加载 .env 文件   │
│   resolveProfileDir()     →  找到 Profile 目录 │
│   readProfilePatches()    →  读取 Bundle 配置  │
│   boot(cordisTree)        →  启动 Cordis 框架  │
└──────────────────┬───────────────────────────┘
                   │
                   ▼
┌──────────────────────────────────────────────┐
│ Cordis Loader 启动                            │
│   1. 加载 dsh-base Bundle（LLM、工具、Session…） │
│   2. 加载 headless Bundle（一次性 Runner）     │
│   3. 应用 cordis.patch.yml 覆盖层            │
│   4. 所有 Plugin.apply(ctx) 依次执行          │
└──────────────────┬───────────────────────────┘
                   │
                   ▼
┌──────────────────────────────────────────────┐
│ ctx.agents.create()  ← 创建 Agent            │
│   ├─ ctx.sessions.create(sessionId)         │
│   └─ ReactLoopAgent(session, options)        │
└──────────────────┬───────────────────────────┘
                   │
                   ▼
┌──────────────────────────────────────────────┐
│ Agent Loop 开始驱动 Turn                     │
│                                              │
│   TURN START                                 │
│   ├─ 从 Inbox 取出用户消息                    │
│   ├─ ctx.systemPrompt.assemble()             │
│   │   → 拼接系统提示词 + 工具 Schema          │
│   ├─ ctx.sessions.deriveMessages()           │
│   │   → 从日志推导模型历史                    │
│   │                                           │
│   STEP START                                 │
│   ├─ ctx.llm.stream(options)                 │
│   │   → 调用 LLM Provider（DeepSeek API）    │
│   │   → 流式返回 token chunks                │
│   │   → 组装成 AssistantMessage              │
│   │                                           │
│   ├─ 解析 Tool Calls                         │
│   ├─ FOR EACH tool call:                     │
│   │   ctx.tools.execute(toolName, args)      │
│   │   ├─ tools/pre-execute (waterfall)       │
│   │   ├─ 守卫检查（权限策略）                  │
│   │   ├─ tools/execute (waterfall)           │
│   │   ├─ 执行 execute() 函数体               │
│   │   ├─ tools/post-execute (waterfall)      │
│   │   └─ 记录 tool/result 到 Session 日志    │
│   │                                           │
│   STEP END                                   │
│   │ 模型无更多 tool calls → 退出 Step 循环   │
│                                              │
│   TURN END                                 │
└──────────────────────────────────────────────┘
                   │
                   ▼
           输出结果，进程退出
```

---

## 关键服务键（ctx 键）

| ctx 键 | 所属包 | 职责 |
|---|---|---|
| `ctx.sessions` | `core/session` | Session 创建/查询/派生历史 |
| `ctx.tools` | `core/tools` | 工具注册表 + 执行管线 |
| `ctx.llm` | `llm/llm` | LLM 流式调用 + Provider 适配器管理 |
| `ctx.agents` | `core/agent` | Agent 创建/注册/查询 |
| `ctx.agentLoop` | `core/agent-loop` | Agent 默认驱动（工厂注册） |
| `ctx.systemPrompt` | `core/system-prompt` | 系统提示词组装 |
| `ctx.shell` | `shell/shell` | Shell 执行契约 |
| `ctx.sandbox` | `sandbox/` | 进程隔离后端 |
| `ctx.jobs` | `jobs/` | 后台任务管理 |

---

## 设计原则（来自 AGENTS.md）

- **Seam 三角色**：每个能力缝 = Service Definition + Service Provider + Consumer，三者缺一不可
- **模型可见即已记录**：任何进入模型请求的数据必须能从 Session 日志中重构
- **插件优先**：新功能通过挂载插件实现，而非修改 Agent Loop
- **Typed Events**：事件通过 TypeScript 声明合并定义，分 `emit`/`waterfall`/`parallel`/`serial`/`bail` 五种分发模式
