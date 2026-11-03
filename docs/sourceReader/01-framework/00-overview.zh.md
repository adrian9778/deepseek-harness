# 项目整体架构总览

[English](00-overview.md) | 中文

这一页回答一个问题：dsh 这个项目到底是什么、由哪几块组成。读完你会知道它怎么启动、有哪些核心部件、一条任务从输入到输出经历了什么。第一次接触项目从这里开始；想看更细的模块划分，接着读 [01-process-module-map.zh.md](01-process-module-map.zh.md)。

---

## 一句话描述

DeepSeek Harness（简称 dsh）是一个基于插件架构的 Agent 运行时框架。你用 `dsh` 命令行选一个 **Profile**（可以理解为"启动套餐"，比如 `web`、`headless`），框架按套餐装好一组 **Bundle** 插件层，拼出一个完整的 Agent 系统：记 **Session**（对话日志）、跑 **Agent Loop**（多轮推理循环）、调 **LLM**（大模型）、执行 **Tools**（工具）。

---

## 三大抽象概念

整座系统建立在三个概念上。理解了它们，后面的源码阅读都不会迷路。

### 1. Profile（启动套餐）

Profile 决定"这次启动要装哪些插件"。每个 Profile 对应 `$DSH_HOME/profiles/<name>` 下的一个目录，目录内 `package.json` 的 `dsh.profile` 字段声明有序 `bundles` 列表，另外用户还可以用自己的 `cordis.patch.yml` 覆盖层做微调。

| 内置 Profile | 什么时候用 |
|---|---|
| `web` | 想要图形界面：浏览器里跑 Agent |
| `headless` | 跑完一条任务就退出：适合脚本和自动化 |
| `sdk` | 把 Agent 当服务用：JSON-RPC 服务器模式 |
| `sdk-minimal` | 同上，但裁掉了完整的 base 树 |
| `acp` | 给自动化客户端用的 ACP（Agent Client Protocol）服务器 |

### 2. Bundle（插件层）

Bundle 是"一层可以安装的插件包"，npm 包在 `package.json` 中声明 `"dsh": { "bundle": { "patch": "./cordis.patch.yml" } }`。Profile 把 Bundle 一层层叠起来：最底下永远是 `dsh-base`（模型接入、工具集、Session 持久化、沙箱、权限策略都在这层），上面再按需叠加。

```
Profile: web
  └─ dsh-web-app       ← 叠加：Web GUI 相关插件
      └─ dsh-base       ← 核心：LLM + 工具 + Session + 沙箱 + 权限
```

### 3. Session（对话日志）

Session 是整个系统的核心数据结构：一份**只追加、不修改**的事件日志（append-only），把对话里发生过的每件事都记下来——用户说了什么（`user/message`）、模型回了什么（`assistant/message`）、工具跑出了什么（`tool/result`）、系统插了什么话（`system/message`）。

关键设计：模型看到的对话历史**不单独存一份**，而是每次请求时从这份日志**现算**出来（`Session.deriveMessages()`）。日志是唯一事实来源，别的都是它的投影。

---

## 模块地图

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
  ...             ← 其他能力包（见 packages/README.md）
```

---

## 整体运行流程（文本图）

一条 headless 任务从命令行到出结果，一共五步：解析命令行 → 准备启动环境 → 装插件 → 建 Agent → 循环推理直到做完。下面的图把每一步落在哪个文件、发生了什么都标出来了：

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

## 关键服务键（ctx 键）

插件之间不互相 import，而是往一个公共的"服务仓库"（ctx）里放能力、取能力。每个能力有一个键，下面是核心链路上最常用的几个：

| ctx 键 | 所属包 | 管什么 |
|---|---|---|
| `ctx.sessions` | `core/session` | 建 Session、查日志、从日志推导模型历史 |
| `ctx.sessionPersistence` | `session/session-persistence` | 把 Session 日志落到磁盘（按 Session 句柄读写） |
| `ctx.sessionProjections` | `session/session-projection` | 把事件流折叠成好用的视图（inbox、模型选择等） |
| `ctx.tools` | `core/tools` | 工具的花名册 + 执行管线 |
| `ctx.llm` | `llm/llm` | 调大模型（流式） + 管理各家 Provider 适配器 |
| `ctx.agents` | `core/agent` | Agent 的注册、创建、恢复 |
| `ctx.agentLoop` | `core/agent-loop` | Agent 默认驱动（工厂注册） |
| `ctx.systemPrompt` | `core/system-prompt` | 拼系统提示词 |
| `ctx.agentDefaultModel` | `core/agent-default-model` | 部署方指定的默认模型 |
| `ctx.shell` | `shell/shell` | 跑 shell 命令的统一入口 |
| `ctx.sandboxPolicy` | `sandbox/sandbox-policy` | 沙箱默认策略 |
| `ctx.jobs` | `jobs/jobs` | 后台任务管理 |

---

## 设计原则（来自 AGENTS.md）

四条贯穿全仓库的规矩，读源码时随时会碰到：

- **Seam 三角色**：每个能力都拆成"接口（Service Definition）/ 实现（Service Provider）/ 使用者（Consumer）"三方，缺一不可
- **模型可见即已记录**：凡是进过模型请求的数据，必须能从 Session 日志里原样重建
- **插件优先**：加新功能靠挂插件，不动 Agent Loop 本身
- **Typed Events**：事件类型用 TypeScript 声明合并定义，分发分五种：`emit`/`waterfall`/`parallel`/`serial`/`bail`
