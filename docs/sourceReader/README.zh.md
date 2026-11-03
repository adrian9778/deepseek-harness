# DeepSeek Harness 源码阅读指南

[English](README.md) | 中文

> 以代码 HEAD 为准。代码与本文档冲突时，以代码为准修正本文档。

---

## 文档层次

按顺序阅读，每一层都比上一层更接近源码：

| 层级 | 目录 | 读什么 | 适合谁 |
|---|---|---|---|
| 第一层 | `01-framework/` | 系统骨架：有哪些模块、各管什么、数据怎么流 | 第一次接触项目，想知道“它是什么” |
| 第二层 | `02-example/` | 跟着一条真实任务从头走到尾 | 想亲手跑一遍、看效果 |
| 第三层 | `03-detailed/` | 主链路一步一步拆开：谁调谁、传了什么、状态怎么变 | 要理解内部流转的开发者 |
| 第四层 | `04-source/` | 逐模块的源码参考：类型、服务、函数 | 要改源码的维护者 |

---

## 目录结构

```
docs/sourceReader/
├── README.md                        ← 本文件（总览与导航）
│
├── 01-framework/                    ← 第一层：系统骨架
│   ├── 00-overview.md               架构总览
│   ├── 01-process-module-map.md     进程/模块划分与职责
│   ├── 02-data-flow.md              主数据流端到端
│   └── 03-cordis-plugin-system.md   Cordis 插件系统基础
│
├── 02-example/                      ← 第二层：一条真实全路径
│   └── 00-minimal-example.md       一条 headless 任务：从 argv 到模型响应
│
├── 03-detailed/                     ← 第三层：详细逐步说明
│   ├── 00-agent-lifecycle.md       Agent 创建 → 驱动 → 完成生命周期
│   ├── 01-turn-step-flow.md        一个 Turn 内的 Step 流转
│   ├── 02-session-log.md           Session 日志读写与持久化
│   ├── 03-tool-execution.md        工具执行管线
│   └── 04-system-prompt-assembly.md 系统提示词组装
│
└── 04-source/                       ← 第四层：源码参考
    ├── 00-agent-loop.md            agent-loop 包参考
    ├── 01-session.md               session 包参考
    ├── 02-tools.md                 tools 包参考
    ├── 03-cli-boot.md              CLI 启动链路参考
    └── 04-llm.md                   llm 包参考
```

---

## 快速导航

### 我想了解这个项目是做什么的
→ 读 [`01-framework/00-overview.md`](01-framework/00-overview.zh.md)

### 我想看请求怎么从入口流到结果
→ 先读 [`01-framework/02-data-flow.md`](01-framework/02-data-flow.zh.md)，再读 [`02-example/00-minimal-example.md`](02-example/00-minimal-example.zh.md)

### 我想理解 Agent 如何运行一轮对话
→ 读 [`03-detailed/00-agent-lifecycle.md`](03-detailed/00-agent-lifecycle.zh.md) + [`03-detailed/01-turn-step-flow.md`](03-detailed/01-turn-step-flow.zh.md)

### 我想改动 Agent 循环逻辑
→ 读 [`04-source/00-agent-loop.md`](04-source/00-agent-loop.zh.md)

### 我想理解 Session 持久化
→ 读 [`03-detailed/02-session-log.md`](03-detailed/02-session-log.zh.md) + [`04-source/01-session.md`](04-source/01-session.zh.md)

### 我想添加一个新工具
→ 读 [`03-detailed/03-tool-execution.md`](03-detailed/03-tool-execution.zh.md) + [`04-source/02-tools.md`](04-source/02-tools.zh.md)

---

## 核心术语速查

遇到不认识的词先查这张表；每个词在后面对应的文档里还有更详细的解释。

| 术语 | 通俗解释 |
|---|---|
| **Cordis** | 被 vendored 到 `vendor/` 的插件框架；一切功能都以“插件 → 服务 → 上下文 → 事件”的方式组装 |
| **Profile** | 一次启动的“套餐”：选 `web` 就带界面，选 `headless` 就跑完即退；对应 `$DSH_HOME/profiles/<name>` 目录，清单声明有序的 bundle |
| **Bundle** | 可安装的一层插件包（`dsh.bundle.patch`）；Profile 把多个 Bundle 按顺序叠起来 |
| **Session** | 一次对话的 append-only 事件日志；模型看到的全部历史都从这份日志现算出来 |
| **Turn** | 一轮对话：从收到输入到本轮结束原因 |
| **Step** | 问一次模型 + 模型要求执行的一批工具调用 |
| **Agent** | 一个正在运行的对话实体：Session + Inbox + 生命周期 |
| **Tool** | 模型能调用的能力（读写文件、跑命令、搜网页……） |
| **Seam（能力缝）** | “接口 / 实现 / 使用者”三层分离的架构单元，三方互不依赖 |
| **ctx** | Cordis 的服务仓库；插件通过 `ctx.sessions`、`ctx.tools` 这样的键互相取用能力 |
| **Waterfall 事件** | 可以被监听器拦截、改写的事件；监听器必须调用 `next()` 才会把处理权交给下一个 |
