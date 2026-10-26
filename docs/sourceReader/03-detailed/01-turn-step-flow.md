# 一个 Turn 内 Step 的逐步流转

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
│  dispatch.waterfall('agent/pre-step', { messages })            │
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
│  │ 1. systemPrompt.assemble(agentCtx)                           │
│  │    → 拼接系统提示词章节                                       │
│  │    → 注入当前工具 Schema                                     │
│  │ 2. session.deriveMessages()                                  │
│  │    → 从日志推导历史                                         │
│  │ 3. ctx.llm.prepareCall(config, signal, configure?)           │
│  │    → 验证 adapter，应用 defaults，deep-freeze                 │
│  └──────┬────────┘                                              │
│         │                                                       │
│         ▼                                                       │
│  ┌─ AGENT/REQUEST (Waterfall) ─┐                                │
│  │ dispatch.waterfall('agent/request', preparedCall)            │
│  │ 监听器可改写调用配置                                         │
│  └──────┬───────────────────────┘                                │
│         │                                                       │
│         ▼                                                       │
│  ┌─ STREAM MODEL ─┐                                             │
│  │ for await (chunk of ctx.llm.stream(preparedCall)) {          │
│  │   dispatch.emit('agent/assistant-stream', chunk)             │
│  │ }                                                            │
│  │                                                              │
│  │ Stream 结束时：                                              │
│  │  - 成功：assembleAssistantStream() → AssistantMessage        │
│  │  - 失败/取消：AssistantAttempt（不进入模型历史）             │
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
│  │ executeToolCalls(calls, ...)                                 │
│  │   ├─ 分离 exclusive vs concurrent                            │
│  │   ├─ exclusive：串行屏障                                      │
│  │   ├─ concurrent：bounded pool（maxParallelToolCalls）         │
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
│  │ 有新 tool calls？→ 回到 STEP START                            │
│  │ 无 → 退出 Step 循环                                          │
│  └──────────────────┘                                            │
│                                                                  │
└──────────────────────────┬──────────────────────────────────────┘
                           │ 无更多 tool calls
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│                      TURN END                                   │
│  session.append('turn/end', { turn: N, reason: 'completed' })   │
│  dispatch.serial('agent/turn-stopping', ...)                    │
│  dispatch.emit('agent/status', { status: 'idle' })              │
└─────────────────────────────────────────────────────────────────┘
```

---

## 关键决策点详解

### 1. agent/pre-step 决策

**源码位置**：`packages/core/agent-loop/src/agent.ts` +pre-step-decision

```ts
type PreStepDecision =
  | { kind: 'reject' }
  | {
      kind: 'enter'
      messages: UserMessage[]
      startsRequestSeries?: true
      assembly: PromptAssembly
    }
```

- **reject**：Turn 关闭，不产生 Step。触发 `turn/end` with reason `rejected`
- **enter**：进入 Step。`startsRequestSeries: true` 表示这是一个新的模型请求系列（如模型配置发生变化）

### 2. 请求系列（Request Series）

一个 Turn 内可能包含多个 Step，这些 Step 分为不同的 **request series**：

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

**源码位置**：`packages/core/agent-loop/src/tool-calls.ts` +1-100

```ts
// 将工具调用分为两组
const exclusive = calls.filter(c => !c.isConcurrencySafe)
const concurrent = calls.filter(c => c.isConcurrencySafe)

// exclusive：每个单独等待
for (const call of exclusive) {
  await executeOne(call)
}

// concurrent：有界并发池
await executeWithPool(concurrent, { maxParallel: 10 })
```

### 4. 取消对 Step 的影响

```
cancel() 在 Step 执行中：
  ├─ LLM stream: AbortSignal 触发 → stream 中止
  │   → 已累积的文本以 interrupted: true 写入 assistant/message
  │
  ├─ 工具执行: exec.signal 触发
  │   → 工具 execute() 函数应 cooperative 检查 signal
  │   → 未完成的结果不写入 Session
  │
  └─ Step 中止后：
      → 不写入 step/end
      → 下一个 Turn 从当前 Step 的 assistant 内容继续
```

---

## 事件类型总结

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

| 事件类型 | 分发模式 | 用途 |
|---|---|---|
| `agent/pre-step` | waterfall | 拦截/改写 Step 输入 |
| `agent/request` | waterfall | 拦截/改写模型请求 |
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

| 概念 | 源码文件 | 关键部分 |
|---|---|---|
| Turn 状态机 | `packages/core/agent-loop/src/agent.ts` | +298-399 |
| Step 循环 | `packages/core/agent-loop/src/agent.ts` | +400-560 |
| Pre-step 决策 | `packages/core/agent-loop/src/agent.ts` | +preStep() |
| Tool 并行执行 | `packages/core/agent-loop/src/tool-calls.ts` | +60-260 |
| Stream 处理 | `packages/core/agent-loop/src/assistant-stream.ts` | +18-140 |
| Inbox 管理 | `packages/core/agent-loop/src/inbox.ts` | +74-244 |
| Request header | `packages/core/session/src/request-header.ts` | +1-69 |
