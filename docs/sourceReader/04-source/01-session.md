# session 包源码逐函数解析

## 模块概览

**源码目录**：`packages/core/session/src/`

| 文件 | 职责 | 行数 |
|---|---|---|
| `index.ts` | Session 类 + SessionStore 服务 | ~1453 |
| `types.ts` | SessionEventMap、SessionHeader、品牌类型 | ~562 |
| `surface.ts` | SurfaceManager、deriveEventMessage、foldSurface | ~712 |
| `request-header.ts` | EpochHeader、foldRequestHeader | ~69 |
| `fork.ts` | buildForkSeed | ~30 |
| `repair.ts` | ToolCallRecovery、interruptedTurnClosers | ~211 |
| `seq-ranges.ts` | SessionSeqCursor 辅助 | ~75 |
| `tool-history.ts` | ToolHistoryProjection | ~75 |
| `known-event-types.ts` | KNOWN_SESSION_EVENT_TYPES | ~90 |
| `preparation.ts` | SessionPreparation | ~49 |

---

## types.ts — 类型定义

### 品牌类型（+1-60）

```ts
type SessionId       = Branded<'SessionId'>       // string
type SessionSeq      = BrandedNumber<'SessionSeq'>      // 已有事件的位置（≥0）
type SessionLogOffset = BrandedNumber<'SessionLogOffset'> // 边界/间隙（≥0）
type SessionSeqCursor  = SessionSeq | -1                      // -1 表示"尚无事件"
type OptionalSessionSeq = SessionSeq | null                   // null 表示无
```

**构造函数验证**：`SessionSeq(value)` 要求非负 safe integer，否则抛 TypeError。

### SESSION_FORMAT_VERSION（+65-120）

```ts
export const SESSION_FORMAT_VERSION = 4
```

当前逻辑格式版本。新 reader 必须能读取此版本及所有历史版本。Bump 条件：现有 discriminator 无法防止不安全解读。

### SessionEventMap（+150-350）

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
  'tool/result':             { turn: number; step: number; message: ToolResultMessage; error?: ToolErrorInfo; meta?: JsonValue }
  
  // 请求元数据（非 surface）
  'request/header':          { reason: RequestHeaderReason; header: EpochHeader; startsSeries?: true }
  'request/context':         { context: RequestContext }
  
  // 内部标记
  'session/end-seed':        { inherited: true }  // fork 继承标记
}
```

### TurnEndReason（+350-420）

```ts
type TurnEndReason =
  | { kind: 'completed' }
  | { kind: 'aborted'; reason: AgentCancelCause }
  | { kind: 'error'; error: LlmFailure }
  | { kind: 'blocked' }
  | { kind: 'max-tokens' }
  | { kind: 'cancelled' }
  | { kind: 'forked' }
```

---

## index.ts — Session 类

### 构造函数（+550-700）

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

### `append()`（session/index.ts +798）

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

### `commit()`（session/index.ts +876）

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

### `deriveMessages()`（session/index.ts +984）

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

### `requestHeader()`（session/index.ts +912）

```ts
requestHeader(): EpochHeader | undefined
```

**增量折叠**：
- `this.headerFoldSeq` 跟踪已折叠到的位置
- 有新 `request/header` 事件时才重新折叠
- 返回 deep-frozen 结果（防止外部修改导致 fold 失同步）

### `toolHistory()`（session/index.ts +953）

```ts
toolHistory(): ToolHistory
```

**职责**：折叠 `developer/message` 中的工具增减事件，返回工具定义历史快照。用于在模型请求中携带历史工具定义（支持工具动态增删）。

---

## surface.ts — Surface Manager

### `SurfaceManager` 类

**核心状态**：
- `nodes: SessionSeq[]` — 按序排列的 surface 事件 seq
- `contentGeneration: number` — 每次 replacement 或 projection 变化时递增
- `replaceGeneration: number` — 每次 positional replacement 时递增

### `validateNext(event)`（+1-200）

**验证规则**：
1. 非 surface 事件：直接跳过（不进入 nodes）
2. Append：验证 source 合法，追加到 nodes 末尾
3. Replace：验证 `startSeq` 和 `endSeq` 在 nodes 范围内，且替换范围完整覆盖目标节点

### `deriveEventMessage(event)`（+200-400）

将单个 surface 事件转换为 `Message` 对象：
- `system/message` → `{ role: 'system', content, source: { kind: 'system-prompt' } }`
- `user/message` → `{ role: 'user', content, source }`
- `assistant/message` → `{ role: 'assistant', content, source: { provider, model } }`
- `tool/result` → `{ role: 'tool', content, toolCallId, source: { kind: 'tool', callId } }`
- `developer/message` → `{ role: 'developer', content, source }`

### `foldSurface(events, projections)`（+400-500）

 detached 模式下的 surface 折叠（用于恢复/测试），不依赖 live Session 实例。

---

## request-header.ts — 请求头折叠

### `foldRequestHeader(events, previous?)`（+1-50）

```ts
function foldRequestHeader(events: SessionEvent[], previous?: EpochHeader): EpochHeader | undefined
```

**逻辑**：遍历 `request/header` 事件，保留最后一个（latest wins）。每个事件携带 `reason`（`initial`/`resume`/`change`/`series`）和可选的 `startsSeries`。

### `canonicalHeader(config, adapterDefaults?, tools?)`（+50-70）

构建标准化的 EpochHeader。

### `headerEquals(a, b)`（+70-90）

比较两个 header 是否等价（用于检测工具/配置变化）。
