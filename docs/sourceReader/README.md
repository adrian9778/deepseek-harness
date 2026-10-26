# DeepSeek Harness 源码阅读指南

> 以代码 HEAD 为准。代码与文档冲突时，以代码为准修正本文档。

---

## 文档层次

| 层级 | 目录 | 目标 | 面向读者 |
|---|---|---|---|
| 第一层 | `01-framework/` | 系统骨架：模块划分、职责、依赖关系、主数据流 | 想了解项目做什么的新手 |
| 第二层 | `02-example/` | 真实全路径：一个最小可运行的成功场景 | 需要动手体验的开发者 |
| 第三层 | `03-detailed/` | 主链路拆解：逐跳调用、参数传递、状态变化 | 需要理解内部流转的开发者 |
| 第四层 | `04-source/` | 源码完整补齐：模块/类型/函数级解析 | 需要改源码的维护者 |

---

## 目录结构

```
docs/sourceReader/
├── README.md                        ← 本文件（总览与导航）
│
├── 01-framework/                    ← 第一层：系统骨架
│   ├── 00-overview.md               项目整体架构总览
│   ├── 01-process-module-map.md     进程/模块划分与职责
│   ├── 02-data-flow.md             主数据流与整体运行流程
│   └── 03-cordis-plugin-system.md  Cordis 插件系统基础
│
├── 02-example/                      ← 第二层：真实全路径
│   └── 00-minimal-example.md       headless 模式执行一条任务（从 argv 到模型响应）
│
├── 03-detailed/                     ← 第三层：详细逐步说明
│   ├── 00-agent-lifecycle.md       Agent 创建→驱动→完成的完整生命周期
│   ├── 01-turn-step-flow.md        一个 Turn 内 Step 的逐步流转
│   ├── 02-session-log.md           Session 日志读写与持久化
│   ├── 03-tool-execution.md        工具执行完整管线
│   └── 04-system-prompt-assembly.md 系统提示词组装过程
│
└── 04-source/                       ← 第四层：源码完整补齐
    ├── 00-agent-loop.md            agent-loop 包逐函数解析
    ├── 01-session.md               session 包逐函数解析
    ├── 02-tools.md                 tools 包逐函数解析
    ├── 03-cli-boot.md              CLI 启动流程源码解析
    └── 04-llm.md                   llm 包逐函数解析
```

---

## 快速导航

### 我想了解这个项目是做什么的
→ 读 [`01-framework/00-overview.md`](01-framework/00-overview.md)

### 我想看请求怎么从入口流到结果
→ 先读 [`01-framework/02-data-flow.md`](01-framework/02-data-flow.md)，再读 [`02-example/00-minimal-example.md`](02-example/00-minimal-example.md)

### 我想理解 Agent 如何运行一轮对话
→ 读 [`03-detailed/00-agent-lifecycle.md`](03-detailed/00-agent-lifecycle.md) + [`03-detailed/01-turn-step-flow.md`](03-detailed/01-turn-step-flow.md)

### 我想改动 Agent 循环逻辑
→ 读 [`04-source/00-agent-loop.md`](04-source/00-agent-loop.md)

### 我想理解 Session 持久化
→ 读 [`03-detailed/02-session-log.md`](03-detailed/02-session-log.md) + [`04-source/01-session.md`](04-source/01-session.md)

### 我想添加一个新工具
→ 读 [`03-detailed/03-tool-execution.md`](03-detailed/03-tool-execution.md) + [`04-source/02-tools.md`](04-source/02-tools.md)

---

## 核心术语速查

| 术语 | 含义 |
|---|---|
| **Cordis** | DeepSeek Harness 使用的插件框架；plugin → service → context → event |
| **Profile** | 一个命名的插件树配置（如 `web`、`headless`、`sdk`） |
| **Bundle** | 可安装的插件组合层；Profile 由若干 Bundle 叠加而成 |
| **Session** | 一次对话的 append-only 事件日志；一切模型历史由此推导 |
| **Turn** | 一轮对话：从用户输入到 Agent 完成（0~N 个 Step） |
| **Step** | 一次模型请求 + 该请求触发的工具调用集合 |
| **Agent** | 一个运行中的对话实体，持有 Session + Inbox + 生命周期 |
| **Tool** | 模型可调用的能力（文件读写、Shell 执行、Web 搜索等） |
| **Seam（能力缝）** | Service Definition / Service Provider / Consumer 三层分离的架构单元 |
| **ctx** | Cordis Context，插件通过它访问其他服务（如 `ctx.sessions`、`ctx.tools`） |
| **Waterfall 事件** | 可被监听器拦截和改写的事件；监听器必须调用 `next()` 才能继续 |
