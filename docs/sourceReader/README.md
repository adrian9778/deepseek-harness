# DeepSeek Harness source reading guide

English | [中文](README.zh.md)

> Code at HEAD is authoritative. When code and this guide disagree, fix the guide against the code.

---

## Document layers

Read in order; each layer gets closer to the source than the one before:

| Layer | Directory | What you read | Who it is for |
|---|---|---|---|
| 1 | `01-framework/` | System skeleton: which modules exist, what each owns, how data flows | First contact: you want to know what the project is |
| 2 | `02-example/` | Follow one real task from start to finish | You want to run it hands-on and see the effect |
| 3 | `03-detailed/` | The main path step by step: who calls whom, what is passed, how state changes | Developers who need the internal flow |
| 4 | `04-source/` | Per-module source reference: types, services, functions | Maintainers who change the source |

---

## Directory layout

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

## Quick navigation

### I want to know what this project does
→ read [`01-framework/00-overview.md`](01-framework/00-overview.md)

### I want to see how a request flows from entry to result
→ read [`01-framework/02-data-flow.md`](01-framework/02-data-flow.md) first, then [`02-example/00-minimal-example.md`](02-example/00-minimal-example.md)

### I want to understand how an Agent runs one conversation round
→ read [`03-detailed/00-agent-lifecycle.md`](03-detailed/00-agent-lifecycle.md) + [`03-detailed/01-turn-step-flow.md`](03-detailed/01-turn-step-flow.md)

### I want to change the Agent loop logic
→ read [`04-source/00-agent-loop.md`](04-source/00-agent-loop.md)

### I want to understand Session persistence
→ read [`03-detailed/02-session-log.md`](03-detailed/02-session-log.md) + [`04-source/01-session.md`](04-source/01-session.md)

### I want to add a new tool
→ read [`03-detailed/03-tool-execution.md`](03-detailed/03-tool-execution.md) + [`04-source/02-tools.md`](04-source/02-tools.md)

---

## Core terminology

Look up an unfamiliar word here first; each word gets a fuller explanation in the document behind it.

| Term | Plain-language explanation |
|---|---|
| **Cordis** | The plugin framework vendored under `vendor/`; everything is assembled as plugin → service → context → event |
| **Profile** | A launch "menu": pick `web` for an interface, `headless` to run one task and exit; a directory under `$DSH_HOME/profiles/<name>` whose manifest lists ordered bundles |
| **Bundle** | One installable layer of plugins (`dsh.bundle.patch`); a Profile stacks several Bundles in order |
| **Session** | One conversation's append-only event log; all history the model sees is computed from it |
| **Turn** | One conversation round: from received input to the round's end reason |
| **Step** | One model request plus the batch of tool calls it demands |
| **Agent** | One running conversation entity: Session + Inbox + lifecycle |
| **Tool** | A capability the model can call (file I/O, shell commands, web search, ...) |
| **Seam** | An architecture unit split into interface / implementation / consumer, with none depending on another |
| **ctx** | Cordis's service repository; plugins reach each other's capabilities through keys like `ctx.sessions`, `ctx.tools` |
| **Waterfall event** | An event a listener may intercept and rewrite; a listener must call `next()` to hand processing to the next one |
