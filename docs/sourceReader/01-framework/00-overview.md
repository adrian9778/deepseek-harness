# Architecture overview

English | [中文](00-overview.zh.md)

This page answers one question: what is dsh, and what is it made of? After reading it you will know how the system starts, which core parts exist, and what happens to one task from input to output. First contact with the project starts here; for a finer module breakdown, continue with [01-process-module-map.md](01-process-module-map.md).

---

## One-sentence description

DeepSeek Harness (dsh) is a plugin-based Agent runtime framework. You pick a **Profile** with the `dsh` CLI (a "launch menu", such as `web` or `headless`), the framework installs the **Bundle** plugin layers named by that menu, and the result is a complete Agent system: it records **Sessions** (conversation logs), runs the **Agent Loop** (multi-round inference), calls **LLMs**, and executes **Tools**.

---

## The three abstractions

The whole system rests on three concepts. Once you understand them, no later source reading will leave you lost.

### 1. Profile (launch menu)

A Profile decides "which plugins this launch installs". Each Profile is a directory under `$DSH_HOME/profiles/<name>`; its `package.json` declares an ordered `bundles` list in the `dsh.profile` field, and the user can fine-tune it with an own `cordis.patch.yml` overlay.

| Built-in Profile | When to use it |
|---|---|
| `web` | You want a graphical interface: the Agent runs in a browser |
| `headless` | Run one task and exit: fits scripts and automation |
| `sdk` | Use the Agent as a service: JSON-RPC server mode |
| `sdk-minimal` | Same, but trimmed of the full base tree |
| `acp` | ACP (Agent Client Protocol) server for automation clients |

### 2. Bundle (plugin layer)

A Bundle is "one installable layer of plugins"; an npm package declares `"dsh": { "bundle": { "patch": "./cordis.patch.yml" } }` in its `package.json`. A Profile stacks Bundles layer by layer: the bottom layer is always `dsh-base` (model access, tool set, Session persistence, sandbox, and permission policy all live here), and further layers stack on demand.

```
Profile: web
  └─ dsh-web-app       ← 叠加：Web GUI 相关插件
      └─ dsh-base       ← 核心：LLM + 工具 + Session + 沙箱 + 权限
```

### 3. Session (conversation log)

The Session is the core data structure of the whole system: an **append-only event log** that records everything that happened in the conversation — what the user said (`user/message`), what the model replied (`assistant/message`), what tools produced (`tool/result`), what the system inserted (`system/message`).

Key design: the conversation history the model sees is **not stored as a second copy**; it is **computed fresh** from the log on every request (`Session.deriveMessages()`). The log is the single source of truth; everything else is a projection of it.

---

## Module map

```
apps/cli/src/
  bin.ts              ← CLI 入口：解析参数，分派到 profile-boot / plugin / dump-config
  profile-boot.ts     ← Profile 启动：加载 .env，叠加 patch 层，启动 Cordis Loader
  args.ts             ← 命令行参数解析（Commander）
  startup-diagnostics.ts  ← 启动失败诊断报告

packages/
  core/           ← 产品 API 脊柱
    agent-loop/   ← Agent 默认驱动：Turn/Step 状态机
    agent/        ← Agent 注册表 + 句柄 + 事件
    session/      ← Append-only 事件日志 + 内存 Store
    tools/        ← 工具注册表 + 执行管线
    system-prompt/ ← 系统提示词组装
    scope/        ← Agent 作用域注册原语
  llm/            ← LLM 能力族：抽象服务 + 各 Provider 适配器
  shell/          ← Shell 执行能力族（bash/pwsh，local/sandbox）
  sandbox/        ← 进程隔离（bwrap/Landlock/Seatbelt）
  bundle/         ← 可安装 Profile Bundle
  boot/           ← CLI 启动胶水
  team/           ← 子 Agent 树共享任务看板（任务板服务 + 模型任务工具）
  ...             ← 其他能力包（见 packages/README.md）
```

---

## End-to-end flow (text diagram)

One headless task goes from the command line to a result in five steps: parse the command line, prepare the launch environment, install plugins, create the Agent, and loop on inference until done. The diagram marks where each step lands, in which file, and what happens:

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
│   boot(...)               →  启动 Cordis 框架  │
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
│   ├─ ctx.sessions.prepare(sessionId)         │
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
│   ├─ session.deriveMessages()                │
│   │   → 从日志推导模型历史                    │
│   │                                           │
│   STEP START                                 │
│   ├─ ctx.llm.stream(options)                 │
│   │   → 调用 LLM Provider（DeepSeek API）    │
│   │   → 流式返回 token chunks                │
│   │   → 组装成 AssistantMessage              │
│   │                                           │
│   ├─ 解析 Tool Calls                         │
│   ├─ 批量执行 tool calls:                    │
│   │   ctx.tools.execute(batch)               │
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

## Key service keys (ctx keys)

Plugins never import each other; they put capabilities into, and take them from, a shared "service repository" (ctx). Each capability has a key; these are the most used ones on the core path:

| ctx key | Owning package | What it does |
|---|---|---|
| `ctx.sessions` | `core/session` | Create Sessions, read logs, derive model history from the log |
| `ctx.sessionPersistence` | `session/session-persistence` | Put Session logs on disk (per-Session handles) |
| `ctx.sessionProjections` | `session/session-projection` | Fold event streams into handy views (inbox, model selection, ...) |
| `ctx.tools` | `core/tools` | The tool roster + execution pipeline |
| `ctx.llm` | `llm/llm` | Call models (streaming) + manage provider adapters |
| `ctx.agents` | `core/agent` | Register, create, and resume Agents |
| `ctx.agentLoop` | `core/agent-loop` | The default Agent driver (factory registration) |
| `ctx.systemPrompt` | `core/system-prompt` | Assemble the system prompt |
| `ctx.agentDefaultModel` | `core/agent-default-model` | The deployment's default model |
| `ctx.shell` | `shell/shell` | The one entry for running shell commands |
| `ctx.sandboxPolicy` | `sandbox/sandbox-policy` | Sandbox default policy |
| `ctx.jobs` | `jobs/jobs` | Background job management |

---

## Design principles (from AGENTS.md)

Four rules span the whole repository; you will meet them everywhere while reading the source:

- **Seam with three roles**: every capability splits into "interface (Service Definition) / implementation (Service Provider) / consumer (Consumer)"; all three are required
- **Model-visible means logged**: anything that entered a model request must be reconstructable verbatim from the Session log
- **Plugins first**: new behavior mounts as plugins instead of touching the Agent Loop
- **Typed Events**: event types come from TypeScript declaration merging; dispatch has five modes: `emit`/`waterfall`/`parallel`/`serial`/`bail`
