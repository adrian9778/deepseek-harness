# 一个 Turn 内 Step 的逐步流转

[English](01-turn-step-flow.md) | 中文

Turn 和 Step 是 Agent Loop 的心跳节律：Turn 是"一轮对话"，Step 是"问一次模型"。这一页先给两个定义和一张全状态机图，再拆开讲四个关键决策点，最后把两类事件列成表。读完你就能准确说出"任意时刻 Agent 正在哪个状态、下一步会去哪"。

## Turn 与 Step 的定义

- **Turn（对话轮）**：从用户提交输入到 Agent 完成响应的完整过程。包含 0 到 N 个 Step。
- **Step（模型步骤）**：一次 LLM 请求 + 该请求触发的一组工具调用。

```
Turn
  ├─ Step 1: LLM 调用 → 返回 tool_call_1, tool_call_2 → 执行工具 → 结果写入 Session
  ├─ Step 2: LLM 调用（含工具结果）→ 返回 tool_call_3 → 执行工具 → 结果写入 Session
  └─ Step 3: LLM 调用（含工具结果）→ 返回纯文本 → Turn 结束
```

---

## 一个 Turn 的完整状态机

整张图从上往下读：Turn 开始 → 取输入 → 询问是否放行 → Step 循环（备料、问模型、跑工具）→ Turn 结束。菱形判断只有两处：pre-step 放不放行、模型还要不要工具。

```
┌─────────────────────────────────────────────────────────────────┐
│                        TURN START                              │
│  session.append('turn/start', { turn: N })                     │
│  dispatch.emit('agent/status', { status: 'running' })          │
└──────────────────────────┬──────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│                    CLAIM INPUT                                   │
│  - 第一次进入 Turn：claim next-turn + next-step 消息            │
│  - Step 之间：只 claim next-step 消息                            │
│  - 消息从 Inbox 中移除 → agent/inbox/claimed 事件               │
└──────────────────────────┬──────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│                  AGENT/PRE-STEP (Waterfall)                     │
│  dispatch.waterfall('agent/pre-step', { turn, step, signal })   │
│                                                                  │
│  监听器可做：                                                     │
│  - { kind: 'reject' } — 拒绝此 Step，Turn 直接关闭              │
│  - { kind: 'enter', messages, startsRequestSeries? }            │
│      — 改写进入的 messages                                       │
│      — startsRequestSeries: true 表示开始新的请求系列            │
│                                                                  │
│  如果 reject 或首次 enter 后 messages 为空 → 关闭 Turn          │
└──────────────────────────┬──────────────────────────────────────┘
                           │ enter
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│                      STEP LOOP                                   │
│                                                                  │
│  ┌─ STEP START ─┐                                               │
│  │ session.append('step/start', { turn, step })                 │
│  └──────┬───────┘                                               │
│         │                                                       │
│         ▼                                                       │
│  ┌─ PROMPT ASSEMBLY ─┐                                          │
│  │ 1. ctx.tools.resolve({ agent, modelSelection, signal })      │
│  │    → 解析当前可见工具集                                       │
│  │ 2. ctx.systemPrompt.assemble({ modelSelection, toolSet })    │
│  │    → 拼接系统提示词章节                                       │
│  │    → 注入当前工具 Schema                                     │
│  │ 3. agent/pre-step waterfall 默认决策                         │
│  │    → renderContextSections + runtimeContext.project          │
│  └──────┬────────┘                                              │
│         │                                                       │
│         ▼                                                       │
│  ┌─ REQUEST PREPARATION ─┐                                      │
│  │ 1. prepareRequest(turn, step, selection, signal)             │
│  │    → 从 session.requestHeader() 读取上次配置                 │
│  │    → agent/request waterfall（配置不能改 provider/model）    │
│  │    → ctx.llm.prepareCall(config, signal)                     │
│  │ 2. buildRequest(...)                                         │
│  │    → canonicalHeader + request/header 事件                   │
│  │    → 工具增减写 developer/message                            │
│  │    → session.deriveMessages() deep-freeze 后发送             │
│  └──────┬────────┘                                              │
│         │                                                       │
│         ▼                                                       │
│  ┌─ STREAM MODEL ─┐                                             │
│  │ for await (chunk of preparedCall.stream(request)) {          │
│  │   dispatch.emit('agent/assistant-stream', chunk)             │
│  │ }                                                            │
│  │                                                              │
│  │ Stream 结束时：                                              │
│  │  - 成功：AssistantStreamAttempt.settle → assistant/message    │
│  │  - 失败/取消：settle 为 assistant/attempt（不进入模型历史）   │
│  └──────┬─────────┘                                              │
│         │                                                       │
│         ▼                                                       │
│  ┌─ PARSE TOOL CALLS ─┐                                         │
│  │ 从 stream 结果中提取 tool_call 列表                           │
│  │ 如果没有 → 退出 Step 循环                                    │
│  └──────┬─────────┘                                              │
│         │ 有 tool calls                                          │
│         ▼                                                       │
│  ┌─ EXECUTE TOOL CALLS ─┐                                       │
│  │ ctx.tools.execute({ calls, maxParallelToolCalls })           │
│  │   ├─ 按执行模式分组（parallel / exclusive）                   │
│  │   ├─ 连续 parallel 组进有界并发池                              │
│  │   ├─ exclusive 调用作为屏障串行                               │
│  │   └─ 每个 call：                                             │
│  │       pre-execute → guard → execute → post-execute → result  │
│  └──────┬─────────┘                                              │
│         │                                                       │
│         ▼                                                       │
│  ┌─ STEP END ─┐                                                  │
│  │ session.append('step/end', { turn, step })                   │
│  └──────┬───────┘                                                │
│         │                                                       │
│         ▼                                                       │
│  ┌─ CHECK FOR MORE ─┐                                           │
│  │ turnEnds 且 next-step 无 pending？→ 退出                      │
│  │ 否则 → 回到 STEP START（target = 'next-step'）                │
│  └──────────────────┘                                            │
│                                                                  │
└──────────────────────────┬──────────────────────────────────────┘
                           │ 无更多 tool calls
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│                      TURN END                                   │
│  dispatch.serial('agent/turn-stopping', { turn, signal })       │
│  session.append('turn/end', { turn: N, reason: { kind: ... } }) │
│  phase 回到 idle 或继续下一个 Turn                               │
└─────────────────────────────────────────────────────────────────┘
```

---

## 关键决策点详解

状态机图里有四个值得停下来细看的位置，逐个拆开：

### 1. agent/pre-step 决策

**源码位置**：`packages/core/agent-loop/src/agent.ts` preStep

```ts
type PreparedStep =
  | { kind: 'reject' }
  | {
      kind: 'enter'
      messages: UserMessage[]
      startsRequestSeries?: true
      assembly: PromptAssembly
      selection: Readonly<ModelSelection>
      toolSet: ResolvedToolSet
    }
```

- **reject**：Turn 关闭，不产生 Step。触发 `turn/end` with reason `{ kind: 'blocked' }`
- **enter**：进入 Step。`startsRequestSeries: true` 表示这是一个新的模型请求系列（如模型配置发生变化）

### 2. 请求系列（Request Series）

一个 Turn 内可能包含多个 Step，这些 Step 分为不同的 **request series**（请求系列）。可以把它理解为"同一套模型配置下连续的几问"：模型配置变了、或者历史被替换了，就开一个新系列，请求头也重写一次：

```
Turn N
  ├─ Series 1 (startsSeries: true)
  │   ├─ Step 1a: header.reason = 'initial'
  │   ├─ Step 1b: header.reason = undefined (继承 1a)
  │   └─ Step 1c: header.reason = undefined (继承 1a)
  ├─ Series 2 (startsSeries: true, envelope changed)
  │   ├─ Step 2a: header.reason = 'change'
  │   └─ Step 2b: header.reason = undefined
  └─ Series 3 (started by surface replacement)
      └─ Step 3a: header.reason = 'series'
```

Header 只在新系列开始时写一次，后续 Step 继承最新 header。

### 3. 工具并行执行

一批工具调用怎么排顺序？规则是：能并行的进并发池，必须独占的当路障，但提交给日志的顺序永远和模型要求的顺序一致：

**源码位置**：`packages/core/tools/src/batch.ts` executeToolBatch

```ts
// 每次调用前按 isConcurrencySafe(args) 重新分类
// 连续 parallel 调用为一组，进有界并发池（maxParallelToolCalls）
// exclusive 调用单独一组，作为屏障
// 提交严格按模型顺序（commitReady）
// abort 时为未启动的调用补合成 skip 结果
```

### 4. 取消对 Step 的影响

取消不打断"已经发生的"，只叫停"还没发生的"：已流出的文本保留，未完成的调用收到合成收尾，Turn 以 aborted 落幕：

```
cancel() 在 Step 执行中：
  ├─ LLM stream: AbortSignal 触发 → stream 中止
  │   → 已累积的文本以 interrupted: true 写入 assistant/message
  │     （没有任何文本时降级为 assistant/attempt，不进入模型历史）
  │
  ├─ 工具执行: exec.signal 触发
  │   → 工具 execute() 函数应 cooperative 检查 signal
  │   → 真实执行结果不写入；Step 收尾时由 ToolCallRecovery 补记合成
  │     error 结果（未启动 TOOL_NOT_STARTED / 已启动结果未知
  │     TOOL_OUTCOME_UNKNOWN），provider 不会看到悬空的工具调用
  │
  └─ Step 中止后（turn() 的 finally 照常执行）：
      → step/end 照常写入；turn/end 以 reason { kind: 'aborted' } 收尾
      → 只有进程崩溃留下的悬空 turn，才由 interruptedTurnClosers
        在 resume 时补齐关闭事件（cause 'interrupted'）
```

---

## 事件类型总结

一个 Turn 走完，留下了哪些"案底"？分两类：持久化的 Session 事件（写入日志、重启后还在）和 Live 事件（只在本次进程内广播，用于 UI 和插件协作）。

### Durable Session 事件（持久化）

| 事件类型 | 写入时机 | 模型可见？ |
|---|---|---|
| `turn/start` | Turn 开始 | 否 |
| `turn/end` | Turn 结束 | 否 |
| `step/start` | Step 开始 | 否 |
| `step/end` | Step 结束 | 否 |
| `user/message` | 用户输入被采纳 | 是 |
| `system/message` | 系统提示词写入 | 是 |
| `assistant/message` | 模型响应成功 | 是 |
| `assistant/attempt` | 模型响应失败/取消 | 否 |
| `tool/result` | 工具执行完成 | 是（作为 tool 角色消息） |
| `developer/message` | 系统注入的修改消息 | 是 |
| `request/header` | 请求头快照 | 否 |
| `request/context` | 请求上下文变化 | 否 |

### Live Agent 事件（非持久化）

这一类事件不进日志，只在本进程内广播，是插件挂接 Agent 行为的主要入口：

| 事件类型 | 分发模式 | 用途 |
|---|---|---|
| `agent/pre-step` | waterfall | 拦截/改写 Step 输入 |
| `agent/request` | waterfall | 拦截/改写模型请求 |
| `agent/request-error` | waterfall | 请求失败后裁决重试或放弃 |
| `agent/assistant-stream` | emit | UI 增量渲染 |
| `agent/status` | emit | UI 状态更新 |
| `agent/created` | serial | 初始化后通知 |
| `agent/disposed` | emit | 销毁通知 |
| `agent/turn-stopping` | serial | Turn 结束前拦截 |
| `agent/inbox/inserted` | emit | Inbox 变更通知 |
| `agent/inbox/claimed` | emit | 消息被取出通知 |
| `agent/inbox/discarded` | emit | 消息被取消通知 |

---

## 关键源码定位

想直接跳进源码时用这张表，按概念直达文件与符号：

| 概念 | 源码文件 | 关键部分 |
|---|---|---|
| Turn 状态机 | `packages/core/agent-loop/src/agent.ts` | `ReactLoopAgent.turn` |
| Step 循环 | `packages/core/agent-loop/src/agent.ts` | `ReactLoopAgent.step` |
| Pre-step 决策 | `packages/core/agent-loop/src/agent.ts` | `ReactLoopAgent.preStep` |
| Tool 批量执行 | `packages/core/tools/src/batch.ts` | `executeToolBatch` |
| Stream 处理 | `packages/core/agent-loop/src/assistant-stream.ts` | `AssistantStreamAttempt` |
| Inbox 管理 | `packages/core/agent-loop/src/inbox.ts` | `ReactLoopInbox` |
| Request header | `packages/core/session/src/request-header.ts` | `canonicalHeader / foldRequestHeader` |
