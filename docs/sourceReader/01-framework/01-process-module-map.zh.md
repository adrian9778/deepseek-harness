# 进程/模块划分与职责

[English](01-process-module-map.md) | 中文

## 顶层进程结构

dsh 只有一个进程入口：`dsh` CLI（`apps/cli/src/bin.ts`）。你敲的每个命令都进同一个入口，不同的 Profile 决定这个进程接下来扮演什么角色：

```
dsh (Node.js 进程)
  │
  ├── profile: web       → 启动 Web 服务器 + GUI host
  ├── profile: headless  → 一次性任务执行后退出
  ├── profile: sdk       → 启动 JSON-RPC 服务器
  ├── profile: sdk-minimal → 精简 SDK 组合包
  ├── profile: acp       → 启动自动化专用 ACP 服务器
  └── profile: plugin    → 插件管理命令（不是应用启动）
```

所有 Profile 共享同一份 CLI 源码，差异仅在于选哪组 Bundle 层来启动 Cordis。

---

## 包分组与职责（packages/ 分组全览）

分组细节见 [packages/README.zh.md](../../../packages/README.zh.md)；下表按组列出核心链路上的包，每行说明"这个包是干嘛的"。

### 核心脊柱组（core/）

一句话：这组包就是"一次对话"本身的骨架——日志、工具、提示词、Agent，全在这里。

| 包 | 职责 | 对外服务键 |
|---|---|---|
| `scope` | Agent 级作用域注册原语，无 ctx key | （无，纯库） |
| `session` | Append-only 事件日志 + 内存 Store | `ctx.sessions` |
| `system-prompt` | 从有序章节 + 工具 Schema 组装系统提示词 | `ctx.systemPrompt` |
| `tools` | 工具注册表 + 安全执行管线 | `ctx.tools` |
| `agent` | Agent 注册表 + 句柄 + 事件 | `ctx.agents` |
| `agent-default-model` | 部署默认模型选择 | `ctx.agentDefaultModel` |
| `agent-loop` | 默认 Agent 驱动：Turn/Step 状态机 | `ctx.agentLoop` |
| `agent-tool-presentation` | Agent 工具呈现模式选择器 | （无，preset 用） |

### LLM 能力组（llm/）

一句话：这组包负责"和模型说话"——抽象出统一的服务，再把各家 Provider（DeepSeek、Pi AI…）适配上来。

| 包 | 职责 |
|---|---|
| `llm` | 抽象 LLM 服务：流式调用、Provider 适配器注册、Call 配置 |
| `llm-deepseek` | DeepSeek API Provider 适配器 |
| `llm-deepseek-api-key` | API Key 认证 Provider |
| `llm-deepseek-account` | 账户登录认证 Provider |
| `llm-pi-ai` | Pi AI 多 Provider 适配器 |
| `llm-retry` | 请求失败重试包装器 |
| `token-meter` | Token 用量计量 |

### Shell 能力组（shell/）

一句话：这组包负责"跑命令"——从模型发出的 `bash`/`pwsh` 工具调用，到真实进程的启动、隔离与回收。

| 包 | 职责 |
|---|---|
| `shell` | Executor 契约：前台命令、后台进程、请求/规格词汇 |
| `bash-local` | POSIX 上跑 `bash -c` 进程 |
| `bash-sandbox` | 经沙箱隔离的 Bash 执行 |
| `pwsh-local` | Windows 上跑 `pwsh -Command` |
| `pwsh-sandbox` | 经沙箱隔离的 PowerShell 执行 |
| `tool-bash` / `tool-pwsh` | 模型看到的 `bash`/`pwsh` 工具 |
| `tool-bash-persistent` / `tool-pwsh-persistent` | 持久 Shell 会话工具 |

### 执行与隔离组

一句话：这组包是"安全带"——凡是会触碰真实世界的能力（进程、终端、远程、沙箱）都在这里收口。

| 包 | 职责 |
|---|---|
| `subprocess` | 子进程管理能力缝 |
| `sandbox` | 进程隔离：bwrap（Linux）/ Seatbelt（macOS）/ Landlock |
| `ssh` | POSIX 远程连接：paired filesystem + subprocess + sandbox provider |
| `terminal` | 持久 PTY 终端服务 |
| `ptc-runtime` | PTC（可编程工具调用）执行能力 |

### 业务逻辑组

一句话：这组包实现 Agent 的"高级行为"——记目标、做计划、压缩上下文、委派子 Agent、跑工作流。

| 包 | 职责 |
|---|---|
| `goal` | 同 Session 目标持久化与生命周期 |
| `schedule` | 宿主管理的定时跟进 |
| `feedback` | 人类反馈捕获与命令 |
| `deliverables` | 对话交付物：显式文件交付 + 工作区变更记录 |
| `compaction` | 上下文压缩：折叠旧事件，保留摘要 |
| `subagent` | 子 Agent 委托：能力缝 + 模型委托工具 |
| `workflow` | 工作流执行：PTC 进程引擎 + 模型 `workflow` 工具 |
| `plan` | 计划协作状态 |
| `todo` | 模型 `todo_write` 工具 |
| `preset` | 每个 Session 的 Agent 组成（从 preset cordis.yml） |
| `guard` | 循环卫生守卫：重复调用提醒 + 工具超时 |

### 可观测与存储组

一句话：这组包负责"找得回来、存得住"——检索历史 Session、存非会话数据、算 token 用量。

| 包 | 职责 |
|---|---|
| `session-query` | Session 检索：逻辑语料库、有界读取、谱系、语义过滤 |
| `storage` | 非 Session 存储中心 + 后端 |
| `spill` | 工具结果溢出存储策略 |
| `attachment` | 二进制附件：内容寻址存储 |
| `session-telemetry-otel` | OTel 遥测上报通道 |
| `identity` | 匿名身份管理 |

### 扩展与交互组

一句话：这组包负责"人和外界的接口"——审批弹窗、外部工具（MCP）、Skill、凭证与设置。

| 包 | 职责 |
|---|---|
| `extensions` | Agent 运行时自修改：插件检查与热插拔 |
| `mcp` | 外部 Model Context Protocol 服务器 |
| `skill` | Skill 能力：提供者注册表 + 模型目录/加载 |
| `interaction` | 人机协作：审批/交互缝、权限预设、ask-user 工具 |
| `credentials` | 凭证引用 + 凭证记录 + 授权流程 |
| `settings` | 用户设置文件提供者 |

### Web 能力组

一句话：这组包让 Agent 能"看网页、控电脑、开浏览器"。

| 包 | 职责 |
|---|---|
| `web` | Web 搜索/Fetch 能力缝 + 模型工具 |
| `computer-use` | 桌面 Provider 独占注册 |
| `browser-use` | 浏览器 Provider 独占注册 |

### 基础设施组

一句话：这组包是"水电煤"——启动、RPC、文件系统、LSP、后台任务、SDK 服务器等通用底座。

| 包 | 职责 |
|---|---|
| `boot` | CLI 启动胶水：Profile 加载、patch 叠加、命令分派 |
| `api` | 远程 BFF 组装 + Typert RPC 网关 |
| `typert` | 类型图生成 + 运行时注册表 |
| `fs` | 文件系统能力缝 + 模型文件工具 |
| `lsp` | LSP 能力缝 + 通用 stdio Provider |
| `jobs` | 通用后台任务运行时 |
| `bundle` | Profile Bundle 定义 |
| `sdk` | 进程外 SDK：JSON-RPC 协议 + TS Client/Server |
| `acp` | 自动化专用 Agent Client Protocol 服务器 |
| `client` | Web GUI 浏览器端：shell、wire、object services |
| `host` | Web GUI Host 服务：目录选择、应用启动、插件清单 |
| `context` | 模型可见的请求上下文（工作区指令、时间上下文） |
| `workspace` | 工作区实体 |
| `document` | Host Office-to-PDF 转换 |
| `experimental` | 评估中的能力，无产品支持承诺 |
| `test-support` | 测试基础设施（testkits、replay、Loader smokes） |
| `util` | 零依赖底层工具（Branded 类型、路径辅助、超时等） |

---

## 模块间依赖关系（核心链路）

谁依赖谁、依赖方向朝哪，这张图一图看尽（从入口往下，越靠下越底层）：

```
apps/cli (入口)
  └─ boot/app-boot (启动)
       └─ bundle/base (核心 Bundle)
            ├─ llm/llm              ← LLM 服务
            ├─ core/session         ← 日志存储
            ├─ core/tools           ← 工具注册表
            ├─ core/agent           ← Agent 注册表
            ├─ core/agent-loop      ← Agent 驱动
            ├─ core/system-prompt   ← 提示词组装
            ├─ shell/bash-local     ← Shell 执行
            ├─ sandbox/             ← 进程隔离
            └─ ...
```

**关键依赖方向**（读懂这四条，就能判断改动会波及谁）：
- `agent-loop` → `session`、`tools`、`llm`、`system-prompt`（消费这些服务）
- `agent` → 独立于 `agent-loop`（允许替换驱动）
- `tools` → 不依赖 `session`，但执行结果由 agent-loop 写入 session
- `session` → 不依赖其他核心包（自包含事件源）

---

## 输入输出总结

每个核心组件"吃什么、吐什么"，对接排障时按图索骥：

| 组件 | 输入 | 输出 |
|---|---|---|
| CLI (`bin.ts`) | `argv[]` | 启动 Profile 或退出码 |
| Profile Boot | Profile 名称 + 环境变量 | Cordis Context（含所有服务） |
| Agent Loop | `AgentOptions`（provider/model） | Session 日志更新 + 模型输出 |
| LLM Stream | `GenerateOptions` | `AsyncIterable<StreamChunk>` |
| Tool Execution | `ToolBatchInput`（calls + 上限） | `ToolBatchResult` |
| Session | `SessionEvent` 追加 | 推导出的 `Message[]` |
