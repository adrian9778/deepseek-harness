# session 包源码逐函数解析

[English](01-session.md) | 中文

session 包是全系统最底层的包：类型、日志类、表面管理、请求头、分叉、修复，全在这里。这一篇按文件逐个拆开，每个类和函数都给出符号锚点、职责和关键逻辑，可直接当源码地图用。

## 模块概览

十个文件各管一摊：类型、主类、表面管理、请求头、分叉、修复、辅助工具。读的时候建议先看 types.ts 建立词汇，再看 index.ts 主类：

**源码目录**：`packages/core/session/src/`

| 文件 | 职责 | 行数 |
|---|---|---|
| `index.ts` | Session 类 + SessionStore 服务 | ~1475 |
| `types.ts` | SessionEventMap、SessionHeader、品牌类型 | ~569 |
| `surface.ts` | SurfaceManager、deriveEventMessage、foldSurface | ~712 |
| `request-header.ts` | canonicalHeader、headerEquals、foldRequestHeader | ~69 |
| `fork.ts` | buildForkSeed | ~30 |
| `repair.ts` | ToolCallRecovery、interruptedTurnClosers | ~211 |
| `seq-ranges.ts` | SessionSeq 辅助 | ~73 |
| `tool-history.ts` | ToolHistoryProjection | ~75 |
| `known-event-types.ts` | KNOWN_SESSION_EVENT_TYPES、MESSAGE_PROJECTION_EVENT_TYPES | ~91 |
| `preparation.ts` | SessionPreparation | ~49 |

---

## types.ts — 类型定义

整个包的"词汇表"：品牌 ID、格式版本、事件表、结束原因。后面所有文件都用这里定义的类型。

### 品牌类型

运行时是普通值，类型系统上不可混用——防拿错 ID、防把 offset 当 seq 用：

```ts
type SessionId       = Branded<'SessionId'>       // string
type SessionSeq      = BrandedNumber<'SessionSeq'>      // 已有事件的位置（≥0）
type SessionLogOffset = BrandedNumber<'SessionLogOffset'> // 边界/间隙（≥0）
type SessionSeqCursor  = SessionSeq | -1                      // -1 表示"尚无事件"
type OptionalSessionSeq = SessionSeq | null                   // null 表示无
```

**构造函数验证**：`SessionSeq(value)` 与 `SessionLogOffset(value)` 都要求非负 safe integer，否则抛 TypeError。

### SESSION_FORMAT_VERSION

日志格式的"版本号"。读取方必须能读当前版本和所有历史版本；只有当现有 discriminator 无法防止不安全解读时才允许 bump：

```ts
export const SESSION_FORMAT_VERSION = 4
```

历史物理头在进入 `SessionHeader` 前由 adjacent migration 链翻译成当前版本；同版本读取保留既有表示，迁移链在存储版本与当前版本一致时不运行。

### SessionEventMap

事件类型总表，可被插件 merge 扩展（未收录类型须带 `ignorable` 标记才能被 reader 安全跳过）。按用途分四组：生命周期（轮次边界）、Surface（模型可见）、请求元数据、内部标记：

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
  'tool/call':               { turn: number; step: number; callId: ToolCallId; name: string; arguments: string }
  'tool/result':             { turn: number; step: number; message: ToolResultMessage; error?: { name: string; code: string; reason?: string }; meta?: JsonValue }

  // 请求元数据（非 surface）
  'request/header':          { header: EpochHeader; reason: RequestHeaderReason; startsSeries?: true }
  'request/context':         RequestContext

  // 内部标记
  'session/end-seed':        { inherited?: true }  // fork/restore 继承切点标记（仅构造器与 buildForkSeed 可建）
}
```

### TurnEndReason

结束原因总表：merge-extensible，插件可并入新变体；排查"为什么停了"从这单查：

```ts
interface TurnEndReasonMap {
  completed:    { kind: 'completed' }
  aborted:      { kind: 'aborted'; reason: TurnEndCancelCause }  // TurnEndCancelCause = AgentCancelCause | { kind: 'legacy' }
  blocked:      { kind: 'blocked' }
  error:        { kind: 'error'; error: LlmFailure }
  'max-tokens': { kind: 'max-tokens' }
  interrupted:  { kind: 'interrupted' }  // 崩溃孤儿轮的事后闭合标记，循环不实时发出
  forked:       { kind: 'forked' }       // fork 种子闭合开放轮的标记，仅 fork 种子携带
}

type TurnEndReason = TurnEndReasonMap[keyof TurnEndReasonMap]
```

---

## index.ts — Session 类

包的主类：内存日志、验证、推导、投影全在一个对象上。下面按生命周期顺序拆方法。

### 构造函数（`Session` constructor）

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

**种子验证流程**（`constructor +0~+73`）：
1. `snapshot` 模式对每个 seed 事件走 `snapshotJsonValue()`（非 lossless JSON 抛错）；restore 模式直接采用独占事件
2. `assertSessionEventEnvelope()` 验证信封字段白名单、类型特定不变量与 request/header、消息事件的当前 LLM 形状
3. `seq === index` 连续性检查（从 0 开始）
4. `surfaceManager.validateNext()` 在入日志前规划 surface 转换，失败不会部分变更 surface
5. 追加进 `this.log`（`snapshot` 模式 `deepFreeze()`）
6. header 一致性：seeded 必须带 seed 与 `inheritedEventCount`；unseeded 时 count 必须为 0；count 不得超过日志长度；`snapshot` 模式的 seed 必须恰等于继承前缀或在切点带 tagged marker，且 tagged marker 必须是最后一个
7. 追加边界标记：fresh fork child 在继承切点补 `{ inherited: true }`（`constructor +69`）；其余 seed 若末尾没有 `session/end-seed` 则补未打标的 `{}`（resume 生命周期边界，`constructor +71`）

### `append()`（session/index.ts · Session.append）

追加事件的主入口，所有写入都过这里。先过 `publicationEntry()` 门（未进入 store、创建公告未完成、重入发布都拒绝），再五步：快照验证、surface 元数据快照、冻结、类型验证、提交：

```ts
append<T extends SessionEventType>(
  type: T,
  data: SessionEventMap[T],
  ...opts: T extends SurfaceEventType ? [opts: SurfaceIntent<T>] : []
): SessionEvent<T>
```

**执行步骤**：
1. `snapshotJsonValue(data)` — 单次迭代验证+拷贝，拒绝非 lossless JSON
2. surface 元数据（`surfaceOp`/`sourceEventSeqs`）单独快照，同样拒绝非 lossless JSON
3. `deepFreeze({ type, seq, time, data, ...surfaceMetadata })`
4. `validateSessionEventData(event)` — 事件局部不变量（header 规范字段、tool 错误一致性、developer 角色等）
5. `this.commit(event, entry)` — 提交；surface 转换在 commit 内验证

### `commit()`（session/index.ts · Session.commit）

提交的最后一步：验证 surface 转换、入日志、广播事件。广播采用"快照先行、逐个包含"策略——监听器快照在入日志前解析，一个监听器失败只记警告，不拖累其他监听器：

```ts
private commit(event: SessionEvent, entry: SessionEntry | undefined): void
```

**执行步骤**：
1. `surfaceManager.validateNext(event)` — 验证 surface 转换合法性（不改已提交 surface）
2. `collectSessionCallbacks` 先解析 `session/event` 监听器快照（在入日志之前）
3. `this.log.push(event)` — 追加到内存日志
4. 重置 `this.eventsSnapshot = undefined`
5. `invokeContainedSessionObservers()` — 逐个调用快照，失败（含 promise rejection）仅 warn，不影响后续
6. finally：`entry.appending = false`；若已请求 detach 且公告不在派发中则 detach
7. 独立 Session（`entry === undefined`）入日志但不广播

### `deriveMessages()`（session/index.ts · Session.deriveMessages）

从日志推导模型历史。带两层缓存：日志没变就只追加新节点，变了才全量重推：

```ts
deriveMessages(): Message[]
```

**缓存策略**：
- `this.derivedGeneration` 记录上次推导时的 `surface.contentGeneration`
- 未变 → 只增量追加新节点
- 变了（positional replacement 或消息投影）→ 清空缓存全量重推

**推导逻辑**：
```ts
if (generation !== this.derivedGeneration) {
  this.derived = []; this.derivedNodes = 0; this.derivedGeneration = generation
}
for (const seq of nodes.slice(this.derivedNodes)) {
  const msg = this.deriveEventMessage(this.log[seq]!)
  if (msg) this.derived.push(msg)  // 空内容 assistant/message 只承载 usage，不进 transcript
}
this.derivedNodes = nodes.length
return [...this.derived]  // 返回浅拷贝，内部 Message 对象共享（已冻结）
```

### `requestHeader()`（session/index.ts · Session.requestHeader）

最近一次请求的"配置快照"。增量折叠：日志变长才对新增事件重新折叠，结果 deep-frozen：

```ts
requestHeader(): EpochHeader | undefined
```

**增量折叠**：
- `this.headerFoldSeq` 跟踪已折叠到的位置
- 有新 `request/header` 事件时才重新折叠
- 返回 deep-frozen 结果（防止外部修改导致 fold 失同步）

### `toolHistory()`（session/index.ts · Session.toolHistory）

**职责**：增量折叠 `request/header`（按 `reason`/`startsSeries` 开新声明系列，登记各 header 的工具全表）与 `developer/message`（tool-addition/tool-removal，addition 从 `headerSeq` 引用的历史 header 解析出完整定义），返回 `ToolHistory { tools, updates }` 快照，供模型请求携带历史工具定义（支持工具动态增删）。旧日志的 header 缺少配套更新记录（活跃名集合对不上）时退回只报 `tools`。

---

## surface.ts — Surface Manager

"表面"是模型看得见的那部分日志。SurfaceManager 跟踪哪些事件在表面上、谁替换了谁。

### `SurfaceManager` 类

**核心状态**：
- `_state` — 共享折叠状态：`nodes`（按序 surface seq）、`replaceGeneration`（committed positional replacement 计数）、`contentGeneration`（positional replacement 与插件消息投影都递增）、`projectedMessages` 与已用投影集合
- `_lastProcessedSeq` — 已折叠到的绝对 seq
- `_pendingPlan` — `validateNext` 已验证、待日志确认后应用的候选转换
- 暴露 `nodes` / `replaceGeneration` / `contentGeneration` 三个 getter，读取前先补折叠增量

### `validateNext(event)`（surface.ts · SurfaceManager.validateNext）

新事件能否上台的规则。验证不改已提交 surface——候选先存成 pending plan，事件真正入日志后由 `_processDelta` 应用：

**验证规则**：
1. 非 surface 事件：不得携带 surface 元数据，直接跳过（不进入 nodes）
2. surface 事件必须带 marker；append 验证 `sourceEventSeqs`（非空、无重复、全部早于当前 seq）后追加到 nodes 末尾
3. replace：`startSeq`/`endSeq` 必须都存在于当前 nodes 且 start ≤ end；`sourceEventSeqs` 必须完整覆盖被遮蔽节点；`tool/result` 只能改写恰好一个当前结果且只许改 content；节点 0 的 system prompt 只能被恰好覆盖它的 `system/message` 改写
4. `developer/message` 的 `headerSeq` 必须引用更早的 `request/header`，tool-addition 须在该 header 中恰好解析出一个完整定义（`deferLoading: true`）
5. 消息投影事件要求已注册解释器，否则拒绝（`MESSAGE_PROJECTION_EVENT_TYPES`）

### `deriveEventMessage(event)`（surface.ts · SurfaceManager.deriveEventMessage）

把一条事件投影成模型消息；同一日志前缀的 `projectedMessages` 按 seq 优先返回：

- `user/message` → 事件数据本身（`{ role: 'user', content, source }`）
- `system/message` / `developer/message` → 对应角色的 message；空内容节点保留 surface 位置但不产生消息（返回 null）
- `assistant/message` → `{ role: 'assistant', content, source: { kind: 'model', provider, model } }`；空内容（max-tokens 步骤只承载 usage）返回 null
- `tool/result` → `{ role: 'tool', content, toolCallId, source: { kind: 'tool', callId } }`
- 非 surface 事件（边界、attempt、log-only 记录）→ null

### `foldSurface(events, projections)`（surface.ts · foldSurface）

对完整事件序列做一次规范 surface 折叠（detached，不依赖 live Session 实例），返回 `{ nodes, replacements, projectedMessages }`；session-query 冷读等不持有 Session 的读取方走这里。

---

## request-header.ts — 请求头折叠

请求头的"最新生效"逻辑：只有一个 header 生效，新事件直接覆盖旧的。

### `foldRequestHeader(events, from?)`（request-header.ts · foldRequestHeader）

```ts
function foldRequestHeader(events: readonly SessionEvent[], from?: EpochHeader): EpochHeader | undefined
```

**逻辑**：遍历 `request/header` 事件，保留最后一个（latest wins），每个 header 先过 `canonicalHeader` 归一化；其余事件跳过。每个 header 事件携带 `reason`（`initial`/`resume`/`change`/`series`）和可选的 `startsSeries`。

### `canonicalHeader(header)`（request-header.ts · canonicalHeader）

构建标准化的 EpochHeader：空 `tools` 变 absent 字段；`adapterDefaults` 仅在其标记 `reasoningEffort`/`maxTokens` 实际生效时保留——落日志、折叠、比较用同一种表示。

```ts
function canonicalHeader(header: EpochHeader): EpochHeader
```

### `headerEquals(a, b)`（request-header.ts · headerEquals）

比较两个 header 是否等价（用于检测工具/配置变化）：`config` 用 `callConfigEquals` 逐字段比较，`adapterDefaults` 比较两个标记位，`tools` 按顺序逐个 JSON 字符串比较。
