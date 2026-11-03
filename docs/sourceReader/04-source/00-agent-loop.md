# agent-loop package reference

English | [中文](00-agent-loop.zh.md)

这是第四层的第一篇：逐文件、逐函数拆开 agent-loop 包。前三层讲了"它做什么"，这一层讲"代码长什么样"。每节先给文件和职责表，再逐个方法展开；行号便于直接跳转源码。

## Module overview

agent-loop 包一共六个文件，分工明确：入口服务、驱动状态机、收件箱、流式累积、运行时上下文、常量：

**Source directory**: `packages/core/agent-loop/src/`

| 文件 | 职责 | 行数 |
|---|---|---|
| `index.ts` | AgentLoop Service、工厂注册、resume、配置 | ~838 |
| `agent.ts` | ReactLoopAgent 驱动：Turn/Step 状态机、取消 | ~750 |
| `inbox.ts` | ReactLoopInbox：durable Inbox 实现 + 投影 | ~244 |
| `assistant-stream.ts` | AssistantStreamAttempt：流式累积 + 沉降 | ~140 |
| `runtime-context.ts` | RuntimeContextProjection + SystemPromptProjection | ~169 |
| `constants.ts` | DEFAULT_MAX_PARALLEL_TOOL_CALLS = 10 | ~6 |

---

## index.ts — the AgentLoop service

入口文件：把驱动注册成 AgentFactory，让 `ctx.agents.create()` 能创建出 ReactLoopAgent 驱动的 Agent。

### The `AgentLoop` Service (+291)

```ts
export class AgentLoop extends Service implements AgentFactory {
  static override inject = ['agents', 'sessions', 'llm', 'tools', 'systemPrompt', 'sessionProjections']
```

**职责**：注册 `AgentFactory` 到 `ctx.agents`，提供声明式 Agent 启动。

**核心方法**：

| 方法 | 行范围 | 职责 |
|---|---|---|
| `[Service.init]()` | +329-338 | 注册 inbox/modelSelection 投影，setFactory，system-prompt 变量 |
| `create(id, options, meta)` | +668 | 创建 Session → ReactLoopAgent → 注册 → 返回 handle |
| `createAgent(ownerCtx, options)` | +717 | AgentFactory 入口（按 identity 分派） |
| `resume(ownerCtx, options)` | +754 | 从持久化恢复 Session → 创建 Agent → 重放历史 |

### `INACTIVE_STATES` (+41)

三个"关门谢客"状态：卸载中、已销毁、已失败。此时拒绝创建或恢复 Agent：

```ts
const INACTIVE_STATES: ReadonlySet<FiberState> = new Set([
  FiberState.UNLOADING,
  FiberState.DISPOSED,
  FiberState.FAILED,
])
```

**职责**：拒绝在不可用的 Context 状态下创建或恢复 Agent。

### Turn boundary projection

想知道"现在跑到第几轮第几步"？读这个投影。它由 `ctx.agents` 注册，Host 端也能通过 `ctx.sessionProjections` 读 inbox 投影：

Turn/Step 边界投影定义在 `packages/core/agent/src/projection.ts`（`turnBoundaryProjectionDefinition`，key `turnBoundary`），由 `ctx.agents` 注册，`ctx.agents.turnBoundaryOf(session)` 读取当前处于哪一轮/哪一步。Host 端也可通过 `ctx.sessionProjections` 读取 `inbox` 投影。

---

## agent.ts — the ReactLoopAgent driver

包里最核心的文件：Turn/Step 状态机、取消、唤醒全在这里。

### Constructor (agent.ts +137)

```ts
constructor(
  private loopCtx: Context,
  public readonly id: SessionId,
  public readonly options: AgentOptions,
  public readonly session: Session,
  projections: LoopProjections,
  lastTurn: number,
)
```

**前置条件**：`loopCtx` 必须已包含 `sessions`、`systemPrompt`、`llm`、`tools`、`agentLoop` 服务。

**初始化步骤**：
1. 创建 `dispatch = agentEvents(loopCtx, this)` — 事件分发器
2. 创建 `scope = createScope(loopCtx, this)` — Agent 作用域
3. 创建 `model = new AgentModel(...)` 与 `inbox = new ReactLoopInbox(...)` — 持久化收件箱
4. 创建 `runtimeContext` 和 `systemPrompt` 投影
5. 初始 phase = `{ kind: 'idle', lastTurn }`

### `send()` / `followup()` / `steer()` / `inject()` (agent.ts +170-190)

三种发消息方式，一张表分清：

| 方法 | target | wakeup | 用途 |
|---|---|---|---|
| `followup(msg)` | `next-turn` | `true` | 用户发起新一轮对话 |
| `steer(msg)` | `next-step` | `true` | 在当前 Turn 内引导下一步 |
| `inject(msg)` | `next-step` | `false` | 注入模型上下文但不唤醒 |

**关键逻辑**（`send`）：
```ts
const wakingAfterAbort = wakeup && this.phase.kind !== 'idle' && this.phase.abort.signal.aborted
const resolvedTarget = wakingAfterAbort ? 'next-turn' : target
this.inbox.splice(resolvedTarget, Infinity, 0, [message])
if (wakeup) this.wakeDriver(wakingAfterAbort)
```
如果 Agent 处于 aborted 状态且需要 wakeup，消息被重新分类到 `next-turn`（下一轮），避免在已中止的活动上浪费唤醒。

### `cancel()` (agent.ts +191)

取消很简短：清 Inbox（可选）+ 中止当前活动。注意它是幂等的，对已 idle 的 Agent 调用不做任何事：

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
- 如果 phase 已是 `idle`，cancel 不中止任何活动（幂等）

### `turn()` — the main Turn loop (agent.ts +327)

一个 Turn 的完整生命周期：打开、循环跑 Step、确保关闭（finally 写 turn/end）：

```ts
private async turn(): Promise<boolean>
```

**返回值**：`true` 表示还有更多 Turn 待处理（inbox 中有 pending 消息），`false` 表示进入 idle。

**流程**：
1. `session.append('turn/start', { turn })` — 打开 Turn
2. `while (true)` 循环：先 `preStep(target, position)` 获取准入决策；reject 则 `turnEnds = { kind: 'blocked' }` 退出；否则 append `step/start` → 执行 `this.step(decision)` → append `step/end`；再检查 `turnEnds` 与 `inbox.nextStep.length`：有结束原因且无 next-step 消息 → `dispatch.serial('agent/turn-stopping', ...)` 后 break，否则 `target = 'next-step'` 继续循环
3. `finally: session.append('turn/end', { turn, reason })` — 确保 Turn 始终关闭

**max-tokens 粘性**：如果任何 Step 返回 `max-tokens`，后续 completed Step 不会降级此结果。

### `step()` — one Step execution (agent.ts +435)

单次"问模型"的全部动作，内含重试循环（失败时可重试）：

```ts
private async step(decision: Extract<PreparedStep, { kind: 'enter' }>): Promise<StepEndReason | null>
```

**内部 while(true) 循环（支持重试）**：

1. `prepareRequest(turn, step, selection, signal)` — 解析 provider/model，触发 `agent/request` waterfall
2. `systemPrompt.project(renderedPrompt, { inHistory, startsSeries })` — 确定 system message 写入策略
3. 写入 `system/message` 事件（按 commits 数组逐个追加）
4. 写入 `user/message` 事件（首次尝试时写入所有 admitted messages）
5. 构建 `request`（deep-freeze 的 GenerateOptions）
6. 创建 `AssistantStreamAttempt` 对象
7. `for await (const chunk of stream)` — 流式处理
   - 每个 chunk 调用 `live.push(chunk)`
   - 流结束时根据 `finish.kind` 决定：
     - `'error' | 'aborted'` → `assistant/attempt` + `agent/request-error` waterfall（决定是否重试）
     - `'stop' | 'tool-calls'` 等 → `assistant/message` + 提取 toolCalls

8. 如果有 toolCalls → `ctx.tools.execute(...)` → 返回 `null`（继续循环）
9. 如果无 toolCalls → 返回 `{ kind: 'completed' }` 或 `{ kind: 'max-tokens' }`

### `prepareRequest()` (agent.ts +613)

请求前的配置准备：读上次配置、拼 seed、放行拦截、绑定 adapter。监听器改不了 provider/model——那是 Step 准备时固化的：

```ts
private async prepareRequest(turn, step, selection, signal)
  : Promise<{ config: LlmCallConfig; preparedCall?: PreparedLlmCall }>
```

**逻辑**：
1. 从 `session.requestHeader()` 读取上次请求配置
2. 构建 `seedConfig`：
   - 首次：使用 `options.maxTokens` + 当前模型选择
   - 后续：从 header 中提取非 provider/model/effort 的控制值
3. 触发 `agent/request` waterfall（监听器不能改 provider/model/effort，那是 step 准备时固化的）
4. 调用 `ctx.llm.prepareCall(proposedConfig, signal)` 绑定 adapter；`NO_ADAPTER` 时回退为未绑定配置

**取消安全**：任何异步阶段（waterfall、prepareCall）在抛出前会先 `signal.throwIfAborted()`。

### `buildRequest()` (agent.ts +656)

最终请求的组装：请求头（首次/变化/新系列三种原因）、工具增减说明、历史消息冻结：

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
4. `deriveMessages()` → `frozenMessages` 冻结所有历史消息
5. 返回 `markAgentLoopRequest(Object.freeze({...}))` 的 `GenerateOptions`

---

## inbox.ts — ReactLoopInbox

Agent 的"待办箱"：持久化到日志、崩溃后可恢复，操作全部通过一个核心 splice 实现。

### The projection definition (inbox.ts +26)

```ts
export const inboxProjectionDefinition = {
  key: 'inbox',
  stateSchema: inboxProjectionSchema,  // { 'next-turn': UserMessage[], 'next-step': UserMessage[] }
  init: () => ({ 'next-turn': [], 'next-step': [] }),
  apply: (state, event) => { /* 处理 agent/inbox/spliced */ },
}
```

**折叠逻辑**：对每个 `agent/inbox/spliced` 事件，应用 toSpliced 得到新状态。

### The ReactLoopInbox class (inbox.ts +73)

| 方法 | 行范围 | 职责 |
|---|---|---|
| `constructor` | +74-78 | 保存 projections/session/dispatch 引用 |
| `get nextTurn/nextStep/hasPending` | +81-95 | 读取投影状态 |
| `clear()` | +97 | 清空两个队列 |
| `claim(target, turn)` | +108 | 取出消息并触发 claimed 事件 |
| `append/prepend/replace/remove` | +120-165 | 基本操作，委托给 splice |
| `splice(target, start, deleteCount, inserted)` | +166-175 | 核心操作：写持久化事件 + 触发通知 |

**`splice()` 内部流程**（+186-230）：
1. 规范化坐标（处理负数、越界）
2. 验证插入消息无重复 id
3. 调用 `session.append('agent/inbox/spliced', splice)` 持久化
4. 对删除的消息触发 `agent/inbox/discarded`
5. 对插入的消息触发 `agent/inbox/inserted`
6. 返回被删除的消息数组

---

## assistant-stream.ts — stream accumulation

模型的流式回复一段段到达，这个类负责把它们攒起来，最后决定"算正式回复还是算失败尝试"：

### The AssistantStreamAttempt class (assistant-stream.ts +18)

| 属性/方法 | 职责 |
|---|---|
| `push(chunk)` | 累积 chunk 到内部列表 |
| `start()` | 触发 `agent/assistant-stream { frame: 'start' }` |
| `finish` | getter：返回最后一个 chunk 的 kind |
| `blocks()` | 返回组装后的 ContentBlock[] |
| `interruptedBlocks()` | 返回中断时的可见内容 |
| `settle(type, appendFn)` | 将累积内容写入 Session（assistant/message 或 assistant/attempt） |
| `abandon()` | 标记 attempt 未沉降（用于异常路径） |

**沉降规则**（一次性定终身）：成功原因进模型历史，失败原因只留档不进历史：
- `finish.reason === 'stop' | 'tool-calls' | 'max-tokens'` → `assistant/message`（进入模型历史）
- `finish.reason` 为 `aborted` / `error` → `assistant/attempt`（不进入模型历史）
