# agent-loop 包源码逐函数解析

## 模块概览

**源码目录**：`packages/core/agent-loop/src/`

| 文件 | 职责 | 行数 |
|---|---|---|
| `index.ts` | Plugin 入口、AgentLoop Service、工厂注册、配置 | ~929 |
| `agent.ts` | ReactLoopAgent 驱动：Turn/Step 状态机、取消 | ~705 |
| `inbox.ts` | ReactLoopInbox：durable Inbox 实现 + 投影 | ~244 |
| `tool-calls.ts` | 工具调度：独占屏障 + 有界并行池 | ~290 |
| `assistant-stream.ts` | AssistantStreamAttempt：流式累积 + 沉降 | ~140 |
| `runtime-context.ts` | RuntimeContextProjection + SystemPromptProjection | ~164 |
| `constants.ts` | DEFAULT_MAX_PARALLEL_TOOL_CALLS = 10 | ~6 |

---

## index.ts — Plugin 入口

### `AgentLoop` Service（+1-150）

```ts
export class AgentLoop implements Service {
  static override inject = ['agents', 'sessionProjections']
```

**职责**：注册 `AgentFactory` 到 `ctx.agents`，提供声明式 Agent 启动。

**核心方法**：

| 方法 | 行范围 | 职责 |
|---|---|---|
| `apply(ctx)` | +100-200 | 注册 AgentLoopFactory，注册 turnBoundary 投影 |
| `createAgent(options)` | +200-350 | 创建 Session → ReactLoopAgent → 注册 → 返回 handle |
| `resumeAgent(options)` | +350-500 | 从持久化恢复 Session → 创建 Agent → 重放历史 |

### `turnBoundaryProjectionDefinition`（+50-95）

```ts
export const turnBoundaryProjectionDefinition = {
  key: 'turnBoundary',
  stateVersion: 2,
  stateSchema: turnBoundaryProjectionSchema,
  init: () => ({ openTurnStartSeq: null, lastStepStartSeq: null, lastStepBoundary: null, lastTurn: 0 }),
  apply: (state, event) => {
    switch (event.type) {
      case 'turn/start': return { ...state, openTurnStartSeq: event.seq, lastTurn: event.data.turn }
      case 'turn/end': return { ...state, openTurnStartSeq: null }
      case 'step/start': return { ...state, lastStepStartSeq: event.seq }
      case 'step/end': return { ...state, lastStepBoundary: { kind: 'end', seq: event.seq } }
    }
  },
}
```

**职责**：将 Turn/Step 边界事件折叠为可查询的投影状态。Host 端通过 `ctx.sessionProjections.stateOf(session, 'turnBoundary')` 读取当前处于哪一轮/哪一步。

---

## agent.ts — ReactLoopAgent 驱动

### 构造函数（agent.ts +98-120）

```ts
constructor(
  private loopCtx: Context,
  public readonly id: SessionId,
  public readonly options: AgentOptions,
  public readonly session: Session,
)
```

**前置条件**：`loopCtx` 必须已包含 `sessions`、`systemPrompt`、`llm`、`tools`、`agentLoop` 服务。

**初始化步骤**：
1. 保存 `session.surface.contentGeneration` 到 `requestSurfaceGeneration`
2. 创建 `dispatch = agentEvents(loopCtx, this)` — 融合的分发器
3. 创建 `scope = createScope(loopCtx, this)` — Agent 作用域
4. 创建 `inbox = new ReactLoopInbox(...)` — 持久化收件箱
5. 创建 `runtimeContext` 和 `systemPrompt` 投影
6. 初始 phase = `{ kind: 'idle', lastTurn }`

### `send()` / `followup()` / `steer()` / `inject()`（agent.ts +130-170）

| 方法 | target | wakeup | 用途 |
|---|---|---|---|
| `followup(msg)` | `next-turn` | `true` | 用户发起新一轮对话 |
| `steer(msg)` | `next-step` | `true` | 在当前 Turn 内引导下一步 |
| `inject(msg)` | `next-step` | `false` | 注入模型上下文但不唤醒 |

**关键逻辑**（`send` +133-143）：
```ts
const wakingAfterAbort = wakeup && this.phase.kind !== 'idle' && this.phase.abort.signal.aborted
const resolvedTarget = wakingAfterAbort ? 'next-turn' : target
this.inbox.splice(resolvedTarget, Infinity, 0, [message])
if (wakeup) this.wakeDriver(wakingAfterAbort)
```
如果 Agent 处于 aborted 状态且需要 wakeup，消息被重新分类到 `next-turn`（下一轮），避免在已中止的活动上浪费唤醒。

### `cancel()`（agent.ts +148-155）

```ts
cancel(cause: AgentCancelCause, options: CancelOptions = {}): void {
  if (!options.keepInbox) {
    this.inbox.clear()
    if (this.phase.kind !== 'idle') this.phase.wakeRequested = false
  }
  if (this.phase.kind !== 'idle') this.phase.abort.abort(cause)
}
```

**后置条件**：
- `keepInbox=false`（默认）：清除所有 pending 消息 + 中止当前活动
- `keepInbox=true`：只中止活动，保留 pending 消息供后续使用
- 如果 phase 已是 `idle`，cancel 是 no-op（幂等）

### `turn()` — 主 Turn 循环（agent.ts +340-440）

```ts
private async turn(): Promise<boolean>
```

**返回值**：`true` 表示还有更多 Turn 待处理（inbox 中有 pending 消息），`false` 表示进入 idle。

**流程**：
1. `session.append('turn/start', { turn })` — 打开 Turn
2. `while (true)` 循环：
   a. `preStep(target, position)` — 获取准入决策
   b. 如果 reject → `turnEnds = { kind: 'blocked' }`，退出
   c. `session.append('step/start', { turn, step })` — 打开 Step
   d. `this.step(decision)` — 执行一个 Step
   e. `session.append('step/end', { turn, step })` — 关闭 Step
   f. 检查 `turnEnds` 和 `inbox.nextStep.length`：
      - 有结束原因 + 无 next-step 消息 → `dispatch.serial('agent/turn-stopping', ...)` → break
      - 否则 `target = 'next-step'`，继续循环
3. `finally: session.append('turn/end', { turn, reason })` — 确保 Turn 始终关闭

**max-tokens 粘性**：如果任何 Step 返回 `max-tokens`，后续 completed Step 不会降级此结果。

### `step()` — 单 Step 执行（agent.ts +440-620）

```ts
private async step(decision: Extract<PreparedStep, { kind: 'enter' }>): Promise<StepEndReason | null>
```

**内部 while(true) 循环（支持重试）**：

1. `prepareRequest(turn, step, signal)` — 解析 provider/model，触发 `agent/request` waterfall
2. `systemPrompt.project(renderedPrompt, { inHistory, startsSeries })` — 确定 system message 写入策略
3. 写入 `system/message` 事件（按 commits 数组逐个追加）
4. 写入 `user/message` 事件（首次尝试时写入所有 admitted messages）
5. 构建 `request`（deep-freeze 的 GenerateOptions）
6. 创建 `AssistantStreamAttempt` 对象
7. `for await (const chunk of stream)` — 流式处理
   - 每个 chunk 调用 `live.push(chunk)`
   - 流结束时根据 `finish.kind` 决定：
     - `'error' | 'aborted'` → `assistant/attempt` + `agent/request-error` waterfall（决定是否重试）
     - `'completed'` → `assistant/message` + 提取 toolCalls

8. 如果有 toolCalls → `executeToolCalls(...)` → 返回 `null`（继续循环）
9. 如果无 toolCalls → 返回 `{ kind: 'completed' }` 或 `{ kind: 'max-tokens' }`

### `prepareRequest()`（agent.ts +620-680）

```ts
private async prepareRequest(turn: number, step: number, signal: AbortSignal)
  : Promise<{ config: LlmCallConfig; preparedCall?: PreparedLlmCall }>
```

**逻辑**：
1. 从 `session.requestHeader()` 读取上次请求配置
2. 构建 `seedConfig`：
   - 首次：使用 `options.provider/model/reasoningEffort/maxTokens`
   - 后续：从 header 中提取 adapter 未覆盖的值
3. 触发 `agent/request` waterfall，允许监听器改写配置
4. 调用 `ctx.llm.prepareCall(proposedConfig, signal)` 绑定 adapter

**取消安全**：任何异步阶段（waterfall、prepareCall）在抛出前会先 `signal.throwIfAborted()`。

### `buildRequest()`（agent.ts +680-705）

```ts
private buildRequest(config, preparedCall, tools, position, startsRequestSeries, signal)
  : GenerateOptions
```

**职责**：
1. 计算 `header`（canonicalHeader）
2. 比较新旧 header：
   - 首次 → 写 `request/header` with reason `'initial'`
   - 变化 → 写 `request/header` with reason `'change'`
   - 仅系列开始 → 写 `request/header` with reason `'series'`
3. 检测工具增减 → 写 `developer/message` with `tool-registry` source
4. `deepFreeze(boundaryMessages)` — 冻结所有历史消息
5. 返回 frozen `GenerateOptions`

---

## inbox.ts — ReactLoopInbox

### 投影定义（inbox.ts +21-70）

```ts
export const inboxProjectionDefinition = {
  key: 'inbox',
  stateSchema: inboxProjectionSchema,  // { 'next-turn': UserMessage[], 'next-step': UserMessage[] }
  init: () => ({ 'next-turn': [], 'next-step': [] }),
  apply: (state, event) => { /* 处理 agent/inbox/spliced */ },
}
```

**折叠逻辑**：对每个 `agent/inbox/spliced` 事件，验证 splice 坐标合法、无重复 MessageId，然后应用 toSpliced。

### ReactLoopInbox 类（inbox.ts +74-244）

| 方法 | 行范围 | 职责 |
|---|---|---|
| `constructor` | +55-65 | 保存 projections/session/dispatch 引用 |
| `get nextTurn/nextStep/hasPending` | +70-85 | 读取投影状态 |
| `clear()` | +90-95 | 清空两个队列 |
| `claim(target, turn)` | +100-115 | 取出消息并触发 claimed 事件 |
| `append/prepend/replace/remove` | +120-170 | 基本操作，委托给 splice |
| `splice(target, start, deleteCount, inserted)` | +180-210 | 核心操作：写持久化事件 + 触发通知 |

**`splice()` 内部流程**（+185-230）：
1. 规范化坐标（处理负数、越界）
2. 验证插入消息无重复 id
3. 调用 `session.append('agent/inbox/spliced', splice)` 持久化
4. 对删除的消息触发 `agent/inbox/discarded`
5. 对插入的消息触发 `agent/inbox/inserted`
6. 返回被删除的消息数组

---

## tool-calls.ts — 工具调度

### `executeToolCalls()`（tool-calls.ts +60-120）

```ts
export async function executeToolCalls(
  ctx: Context, turn: number, step: number,
  toolCalls: ToolCallBlock[], signal: AbortSignal,
  acceptContext: (context: UserMessage) => void,
): Promise<{ concluded: boolean }>
```

**调度策略**：
1. 将 toolCalls 转换为 `PlannedCall[]`（含解析后的 args）
2. 按顺序分组：
   - 连续 parallel 工具 → 放入同一组
   - 遇到 exclusive 工具 → 单独一组（作为屏障）
3. 对每组调用 `runGroup()`
4. 如果组内 abort → 为剩余 call 写入 synthetic error result

### `runGroup()`（tool-calls.ts +125-260）

**有界并发池实现**：

```
while (nextToStart < group.length) {
  1. 检查 signal.aborted → 如果有，退出
  2. 判断 mode：parallel → 填充 pool；exclusive → 单任务
  3. startCall(index)：
     a. appendToolCall(session, ...) → 记录 tool/call 事件
     b. ctx.tools[TOOL_RUNTIME_SCHEDULER].prepare(exec) → pre-execute + guard
     c. 根据 prepare 结果：
        - 'dispatch' → 提交到 inFlight Map
        - 'post-result' / 'final-result' → 直接填充 slot
  4. commitReady()：按模型顺序提交已完成的 slot
  5. 如果 signal.aborted → aborted = true
}
```

**Abort 处理**：
- 已启动的 call 等待完成（drain）
- 未启动的 call 写入 synthetic error result（`TOOL_ABORTED_BEFORE_DISPATCH`）
- `acceptContext()` 收集已启动 call 的 additionalContexts

---

## assistant-stream.ts — 流式累积

### `AssistantStreamAttempt` 类（assistant-stream.ts +18-140）

| 属性/方法 | 职责 |
|---|---|
| `push(chunk)` | 累积 chunk 到内部列表 |
| `start()` | 触发 `agent/assistant-stream { frame: 'start' }` |
| `finish` | getter：返回最后一个 chunk 的 kind |
| `blocks()` | 返回组装后的 ContentBlock[] |
| `interruptedBlocks()` | 返回中断时的可见内容 |
| `settle(type, appendFn)` | 将累积内容写入 Session（assistant/message 或 assistant/attempt） |
| `abandon()` | 标记 attempt 未沉降（用于异常路径） |

**沉降规则**：
- `finish.kind === 'completed'` → `assistant/message`（进入模型历史）
- `finish.kind === 'error' | 'aborted'` → `assistant/attempt`（不进入模型历史）
