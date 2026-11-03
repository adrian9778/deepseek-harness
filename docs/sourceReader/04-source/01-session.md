# session package reference

English | [中文](01-session.zh.md)

session 包是全系统最底层的包：类型、日志类、表面管理、请求头、分叉、修复，全在这里。这一篇按文件逐个拆开，每个类和函数都给出行号、职责和关键逻辑，可直接当源码地图用。

## Module overview

十个文件各管一摊：类型、主类、表面管理、请求头、分叉、修复、辅助工具。读的时候建议先看 types.ts 建立词汇，再看 index.ts 主类：

**Source directory**: `packages/core/session/src/`

| 文件 | 职责 | 行数 |
|---|---|---|
| `index.ts` | Session 类 + SessionStore 服务 | ~1474 |
| `types.ts` | SessionEventMap、SessionHeader、品牌类型 | ~562 |
| `surface.ts` | SurfaceManager、deriveEventMessage、foldSurface | ~712 |
| `request-header.ts` | EpochHeader、foldRequestHeader | ~69 |
| `fork.ts` | buildForkSeed | ~30 |
| `repair.ts` | ToolCallRecovery、interruptedTurnClosers | ~211 |
| `seq-ranges.ts` | SessionSeq 辅助 | ~75 |
| `tool-history.ts` | ToolHistoryProjection | ~75 |
| `known-event-types.ts` | KNOWN_SESSION_EVENT_TYPES | ~90 |
| `preparation.ts` | SessionPreparation | ~49 |

---

## types.ts — type definitions

整个包的"词汇表"：品牌 ID、格式版本、事件表、结束原因。后面所有文件都用这里定义的类型。

### Branded types (+1-60)

运行时是普通值，类型系统上不可混用——防拿错 ID、防把 offset 当 seq 用：

```ts
type SessionId       = Branded<'SessionId'>       // string
type SessionSeq      = BrandedNumber<'SessionSeq'>      // 已有事件的位置（≥0）
type SessionLogOffset = BrandedNumber<'SessionLogOffset'> // 边界/间隙（≥0）
type SessionSeqCursor  = SessionSeq | -1                      // -1 表示"尚无事件"
type OptionalSessionSeq = SessionSeq | null                   // null 表示无
```

**构造函数验证**：`SessionSeq(value)` 要求非负 safe integer，否则抛 TypeError。

### SESSION_FORMAT_VERSION (+65-120)

日志格式的"版本号"。读取方必须能读当前版本和所有历史版本；只有当现有 discriminator 无法防止不安全解读时才允许 bump：

```ts
export const SESSION_FORMAT_VERSION = 4
```

当前逻辑格式版本。新 reader 必须能读取此版本及所有历史版本。Bump 条件：现有 discriminator 无法防止不安全解读。

### SessionEventMap (+150-350)

事件类型总表，按用途分四组：生命周期（轮次边界）、Surface（模型可见）、请求元数据、内部标记：

```ts
interface SessionEventMap {
  // 生命周期事件（非 surface）
  'turn/start':              { turn: number }
  'turn/end':                { turn: number; reason: TurnEndReason }
  'step/start':              { turn: number; step: number }
  'step/end':                { turn: number; step: number }

  // Surface 事件（模型可见）
  'system/message':          { turn: number; step: number; message: SystemMessage }
  'developer/message':       { turn: number; step: number; message: DeveloperMessage; headerSeq?: SessionSeq }
  'user/message':            UserMessage
  'assistant/message':       { turn: number; step: number; message: AssistantMessage; stream: AssistantStreamRecord[]; usage?: TokenUsage; interrupted?: true }
  'assistant/attempt':       { turn: number; step: number; stream: AssistantStreamRecord[] }
  'tool/result':             { turn: number; step: number; message: ToolResultMessage }

  // 请求元数据（非 surface）
  'request/header':          { reason: RequestHeaderReason; header: EpochHeader; startsSeries?: true }
  'request/context':         { context: RequestContext }

  // 内部标记
  'session/end-seed':        { inherited: true }  // fork 继承标记
}
```

### TurnEndReason (+206-238)

七种结束原因，排查"为什么停了"从这单查：

```ts
type TurnEndReason =
  | { kind: 'completed' }
  | { kind: 'aborted'; reason: AgentCancelCause }
  | { kind: 'error'; error: LlmFailure }
  | { kind: 'blocked' }
  | { kind: 'max-tokens' }
  | { kind: 'interrupted' }
  | { kind: 'forked' }
```

---

## index.ts — the Session class

包的主类：内存日志、验证、推导、投影全在一个对象上。下面按生命周期顺序拆方法。

### Constructor (+550-700)

私有构造器：外部只能通过 SessionStore 创建。种子里的事件要全量验证后才能进日志：

```ts
private constructor(
  id: SessionId,
  seed?: readonly SessionEvent[],
  header?: SessionHeader,
  mode: 'snapshot' | SessionSeedEventState = 'snapshot',
  suppliedInheritedEventCount?: SessionLogOffset,
  projections: readonly SessionMessageProjection[] = [],
)
```

**种子验证流程**：
1. 对每个 seed 事件：`snapshotJsonValue()` → `assertSessionEventEnvelope()` → `surfaceManager.validateNext()`
2. 验证 `seq` 连续（从 0 开始）
3. 追加到 `this.log` 并 `deepFreeze()`
4. 验证 `header.isSeeded` 与 `inheritedEventCount` 一致性
5. 如果 seed 以非标记结尾且 `isSeeded=true`，追加 `session/end-seed` 标记

### `append()` (session/index.ts +806)

追加事件的主入口，所有写入都过这里。五步：快照验证、surface 检查、冻结、类型验证、提交：

```ts
append<T extends SessionEventType>(
  type: T,
  data: SessionEventMap[T],
  ...opts: T extends SurfaceEventType ? [opts: SurfaceIntent<T>] : []
): SessionEvent<T>
```

**执行步骤**：
1. `snapshotJsonValue(data)` — 单次迭代验证+拷贝+冻结，拒绝非 lossless JSON
2. 对 surface 事件：验证 `surfaceOp` 和 `sourceEventSeqs`
3. `deepFreeze({ type, seq, time, data, ...surfaceMetadata })`
4. `validateSessionEventData(event)` — 类型特定验证
5. `this.commit(event, entry)` — 提交

### `commit()` (session/index.ts +892)

提交的最后一步：验证 surface 转换、入日志、广播事件。广播采用"逐个包含"策略——一个监听器失败只记警告，不拖累其他监听器：

```ts
private commit(event: SessionEvent, entry: SessionEntry | undefined): void
```

**执行步骤**：
1. `surfaceManager.validateNext(event)` — 验证 surface 转换合法性
2. 设置 `entry.appending = true`（防止重入）
3. `this.log.push(event)` — 追加到内存日志
4. 重置 `this.eventsSnapshot = undefined`
5. 收集 `session/event` 监听器回调（`collectSessionCallbacks`）
6. `invokeContainedSessionObservers()` — 逐个调用，失败仅 warn，不影响后续
7. finally：`entry.appending = false`，检查是否需要 detach

### `deriveMessages()` (session/index.ts +999)

从日志推导模型历史。带两层缓存：日志没变就只追加新节点，变了才全量重推：

```ts
deriveMessages(): Message[]
```

**缓存策略**：
- `this.derivedGeneration` 记录上次推导时的 `surface.contentGeneration`
- 如果未变 → 只增量追加新节点
- 如果变了（replacement 或 projection）→ 全量重推

**推导逻辑**：
```ts
for (const seq of nodes.slice(this.derivedNodes)) {
  const msg = this.deriveEventMessage(this.log[seq]!)
  if (msg !== null) this.derived.push(msg)
}
this.derivedNodes = nodes.length
this.derivedGeneration = generation
return [...this.derived]  // 返回浅拷贝，内部 Message 对象共享（已冻结）
```

### `requestHeader()` (session/index.ts +927)

最近一次请求的"配置快照"。增量折叠：有新 header 事件才重新折叠，结果 deep-frozen：

```ts
requestHeader(): EpochHeader | undefined
```

**增量折叠**：
- `this.headerFoldSeq` 跟踪已折叠到的位置
- 有新 `request/header` 事件时才重新折叠
- 返回 deep-frozen 结果（防止外部修改导致 fold 失同步）

### `toolHistory()` (session/index.ts +968)

工具定义的"变更史"：折叠 developer/message 里的工具增减事件，让模型请求能携带历史工具定义（支持工具动态增删）：

**职责**：折叠 `developer/message` 中的工具增减事件，返回工具定义历史快照。用于在模型请求中携带历史工具定义（支持工具动态增删）。

---

## surface.ts — the Surface Manager

"表面"是模型看得见的那部分日志。SurfaceManager 跟踪哪些事件在表面上、谁替换了谁。

### The SurfaceManager class

**核心状态**：
- `nodes: SessionSeq[]` — 按序排列的 surface 事件 seq
- `contentGeneration: number` — 每次 replacement 或 projection 变化时递增
- `replaceGeneration: number` — 每次 positional replacement 时递增

### `validateNext(event)` (+637)

新事件能否上台的三条规则：非 surface 直接跳过；追加要验来源；替换要验范围完整覆盖目标节点：

**验证规则**：
1. 非 surface 事件：直接跳过（不进入 nodes）
2. Append：验证 source 合法，追加到 nodes 末尾
3. Replace：验证 seq 范围在 nodes 内，且替换范围完整覆盖目标节点

### `deriveEventMessage(event)` (+667)

把一条 surface 事件翻译成模型消息（五种角色一一对应）：

将单个 surface 事件转换为 `Message` 对象：
- `system/message` → `{ role: 'system', content, source: { kind: 'system-prompt' } }`
- `user/message` → `{ role: 'user', content, source }`
- `assistant/message` → `{ role: 'assistant', content, source: { provider, model } }`
- `tool/result` → `{ role: 'tool', content, toolCallId, source: { kind: 'tool', callId } }`
- `developer/message` → `{ role: 'developer', content, source }`

### `foldSurface(events, projections)` (+594)

 detached 模式下的 surface 折叠（用于恢复/测试），不依赖 live Session 实例。

---

## request-header.ts — request header folding

请求头的"最新生效"逻辑：只有一个 header 生效，新事件直接覆盖旧的。

### `foldRequestHeader(events, previous?)` (+63)

```ts
function foldRequestHeader(events: SessionEvent[], previous?: EpochHeader): EpochHeader | undefined
```

**逻辑**：遍历 `request/header` 事件，保留最后一个（latest wins）。每个事件携带 `reason`（`initial`/`resume`/`change`/`series`）和可选的 `startsSeries`。

### `canonicalHeader(config, adapterDefaults?, tools?)` (+21)

构建标准化的 EpochHeader。

### `headerEquals(a, b)` (+43)

比较两个 header 是否等价（用于检测工具/配置变化）。
