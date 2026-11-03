# Session 日志读写与持久化

[English](02-session-log.md) | 中文

Session 是全系统的"唯一事实来源"：对话里发生过的每件事都追加进一份事件日志，模型看到的历史从日志现算，持久化也只落这份日志。这一页先讲清这个设计（Event Sourcing），再拆开 append 验证、历史推导、磁盘格式和 Fork 四个环节，末尾附源码定位表。

## Session 的核心设计：Event Sourcing

Session 是一个 **append-only 事件日志**。所有模型可见的历史都从日志中实时推导，而非单独存储。好处是：日志永远完整、任何视图都能重建、写入路径只有一条：

```
┌─────────────────────────────────────────────────────────────┐
│  Session (内存)                                              │
│                                                              │
│  events: [                                                   │
│    { type: 'turn/start', data: { turn: 1 }, seq: 0 },       │
│    { type: 'step/start', data: { turn: 1, step: 1 }, seq: 1 },│
│    { type: 'system/message', data: {...}, seq: 2 },         │
│    { type: 'user/message', data: {...}, seq: 3 },           │
│    { type: 'assistant/message', data: {...}, seq: 4 },      │
│    { type: 'tool/result', data: {...}, seq: 5 },            │
│    { type: 'step/end', data: {...}, seq: 6 },               │
│    { type: 'turn/end', data: {...}, seq: 7 },               │
│  ]                                                           │
│                                                              │
│  derived:                                                    │
│    messages: deriveMessages()  ← 每次 append 后增量更新      │
│    surface:  SurfaceManager    ← 跟踪替换和投影             │
│                                                              │
└─────────────────────────────────────────────────────────────┘
                          │
                          │ append()
                          ▼
              ┌───────────────────────┐
              │  1. 类型检查           │
              │  2. Surface 验证       │
              │  3. Lossless JSON 快照 │
              │  4. 追加到数组         │
              │  5. 递增 seq          │
              │  6. 刷新 surface 投影  │
              │  7. 触发 session/event │
              └───────────────────────┘
                          │
                          │ flush / checkpoint
                          ▼
              ┌───────────────────────┐
              │  Persistence Backend  │
              │  (JSONL .zstd 文件)    │
              └───────────────────────┘
```

---

## 数据类型体系

日志里每条记录长什么样、用什么标识？这节列出核心类型：品牌化的 ID（防拿错）、事件类型表（哪些事件存在）、Surface 事件（哪些会进模型历史）、Turn 结束原因（为什么停了）。

**源码位置**：`packages/core/session/src/types.ts` +1-100

### SessionId 与 Sequence 品牌类型

"品牌化"（Branded）是 TypeScript 的一个技巧：运行时还是普通 string/number，但类型系统上不可混用，把 SessionId 传给要 Sequence 的参数会直接编译报错：

```ts
// 品牌化类型：运行时是普通 string/number，类型系统上不可混用
type SessionId    = Branded<'SessionId'>    // string
type SessionSeq   = BrandedNumber<'SessionSeq'>   // 已有事件的位置（≥0）
type SessionLogOffset = BrandedNumber<'SessionLogOffset'> // 边界/间隙位置（≥0）
type SessionSeqCursor   = SessionSeq | -1                        // -1 表示空
type OptionalSessionSeq = SessionSeq | null                      // null 表示无
```

### SessionEventMap（扩展事件类型）

事件类型表是开放的：插件可以声明合并往里加新事件（比如 compaction、hook），所以叫"可扩展"：

```ts
interface SessionEventMap {
  'turn/start':          { turn: number }
  'turn/end':            { turn: number; reason: TurnEndReason }
  'step/start':          { turn: number; step: number }
  'step/end':            { turn: number; step: number }
  'user/message':        UserMessage
  'system/message':      { turn: number; step: number; message: SystemMessage }
  'assistant/message':   { turn: number; step: number; message: AssistantMessage; stream: AssistantStreamRecord[]; usage?: TokenUsage; interrupted?: true }
  'assistant/attempt':   { turn: number; step: number; stream: AssistantStreamRecord[] }
  'tool/result':         { turn: number; step: number; message: ToolResultMessage }
  'developer/message':   { turn: number; step: number; message: DeveloperMessage; headerSeq?: SessionSeq }
  'request/header':      { reason: RequestHeaderReason; header: EpochHeader }
  'request/context':     { context: RequestContext }
  // ... 扩展事件（compaction、hook 等，见 known-event-types.ts）
}
```

### SurfaceEventType（模型可见事件）

五种事件会进入模型历史（Surface），因此也接受"替换"操作（比如压缩时替换旧消息）；其余事件只是记录，模型永远看不到：

```ts
type SurfaceEventType =
  | 'system/message'
  | 'developer/message'
  | 'user/message'
  | 'assistant/message'
  | 'tool/result'
```

Surface 事件是模型历史的一部分，接受替换操作（replacement）。

### TurnEndReason（Turn 结束原因）

```ts
type TurnEndReason =
  | { kind: 'completed' }         // 正常完成
  | { kind: 'aborted'; reason: AgentCancelCause }
  | { kind: 'blocked' }           // pre-step 拒绝
  | { kind: 'error'; error: LlmFailure }
  | { kind: 'max-tokens' }        // 达到最大 token 限制
  | { kind: 'interrupted' }       // 进程中断后恢复
  | { kind: 'forked' }            // 被 fork 中断
```

---

## Append 验证流程

往日志里追加一条事件要过七道关。这样设计的原因：日志一旦写入就不可改，所以必须在入口把不合法的数据全部拦下：

**源码位置**：`packages/core/session/src/index.ts` append、`packages/core/session/src/surface.ts`

```
session.append(type, data, opts?)
  │
  ├─ [1] 类型检查
  │     if (!isSessionEventType(type)) throw UNKNOWN_EVENT_TYPE
  │
  ├─ [2] Surface 事件的额外验证
  │     if (isSurfaceEventType(type)) {
  │       if (!opts?.surfaceOp) throw MISSING_SURFACE_OP
  │       if (opts.surfaceOp === 'replace') {
  │         validateReplacement(event, surface, opts.surfaceOp)
  │           → seq 范围必须在当前 surface 节点内
  │           → 替换范围必须完整覆盖目标节点
  │       }
  │     }
  │
  ├─ [3] Lossless JSON 快照
  │     const snapshot = snapshotJsonValue(data)
  │       → 递归遍历，对每个值：
  │         - 调用 toJSON() （如果存在）
  │         - 拒绝 BigInt（JSON 不支持）
  │         - 拒绝循环引用（通过 WeakSet 检测）
  │         - 拒绝稀疏数组
  │         - 拒绝 -0（规范化为 0）
  │         - 拒绝 exotic prototype 对象
  │       → 返回深度冻结的副本
  │
  ├─ [4] 分配 seq
  │     const seq = SessionSeq(events.length)
  │
  ├─ [5] 追加事件
  │     events.push({ type, data: snapshot, seq })
  │     deepFreeze(event)  ← 不可变
  │
  ├─ [6] 刷新 Surface Manager
  │     surfaceManager 应用 pendingPlan
  │       → 追加操作：新增 surface 节点
  │       → 替换操作：标记旧节点为 replaced，新增替换节点
  │       → 更新 contentGeneration / replaceGeneration
  │
  └─ [7] 触发事件
        emit('session/event', event)  — 广播给所有监听器
```

---

## deriveMessages() 推导逻辑

模型看到的历史不是存的，是算的。推导有缓存：日志没变就直接返回上次结果，变了才重新折叠：

**源码位置**：`packages/core/session/src/index.ts` deriveMessages、`packages/core/session/src/surface.ts` foldSurface

```
session.deriveMessages()
  │
  ├─ 如果 contentGeneration 未变 → 返回缓存的 messages
  │
  └─ 重新折叠 surface 事件：
       foldSurface(events, projections)
         │
         ├─ 遍历所有 events
         ├─ 对每个 surface 事件：
         │   ├─ 应用注册的 messageProjection（插件自定义）
         │   └─ 生成对应的 Message 对象
         │       - system/message → { role: 'system', content }
         │       - user/message   → { role: 'user', content }
         │       - assistant/message → { role: 'assistant', content }
         │       - tool/result    → { role: 'tool', content, tool_call_id }
         │       - developer/message → { role: 'developer', content }
         │
         ├─ 处理 replacement：
         │   - 被 replace 的旧节点从 messages 中移除
         │   - 替换事件生成新的 Message 节点
         │
         └─ 返回新的 Message[]，缓存
```

---

## 持久化后端

内存日志之外，持久化后端负责把日志落成磁盘文件、重启后再读回来。先看文件长什么样，再看接口怎么定义。

### 物理格式

目录按"项目工作目录 → Session 编号"两级组织，同一 Session 的各代格式文件并存，旧文件永不改名或删除：

```
$DSH_HOME/sessions/
  └── --<normalized-cwd>--/          ← 项目目录（或 _no-cwd/）
       └── <encoded-id>/             ← Session 专属目录
            ├── session.jsonl.zstd      ← v0（历史）
            ├── session.v1.jsonl.zstd   ← v1（历史）
            ├── session.v2.jsonl.zstd   ← v2（历史）
            ├── session.v3.jsonl.zstd   ← v3/v4（当前）
            └── session.vN.jsonl.zstd   ← 后续版本使用 vN
```

- 每次写入是追加操作，不覆盖
- 压缩格式：zstd checksummed frames（或 `'none'` 明文）
- 版本文件名小写，旧版本不重命名
- 每个 Format Migration 包负责一个 `vN → vN+1` 的转换

### SessionHeader

每个 Session 文件开头都有一份"档案"：版本号、创建时间、来源（子 Agent？fork？），读取时先看这个再决定怎么解析：

**源码位置**：`packages/core/session/src/types.ts` +95

```ts
interface SessionHeader {
  version: typeof SESSION_FORMAT_VERSION  // 当前为 4
  id: SessionId
  createdAt: number                        // Unix ms
  cwd?: string                             // 创建时的工作目录
  parentSession?: SessionId                // 子 Agent 直接创建者
  isSeeded: boolean                        // 是否有 fork 继承历史
  origin?: 'subagent'                      // 子 Agent 标记
  delegationDepth?: number                 // 委托深度
  agentPreset?: string                     // Agent preset 名称
}
```

### Persistence Seam（持久化缝）

持久化同样走"接口 / 实现 / 使用者"三角色分离：定义方只说"要能创建、打开、刷盘、列举"，具体存哪里、什么格式由实现方决定：

```
┌─────────────────────────────────────────────┐
│  Service Definition                          │
│  packages/session/session-persistence/src/   │
│    abstract class SessionPersistence         │
│      create(header, options?): SessionHandle │
│      open(id, access, options?): SessionHandle│
│      flush(): Promise<void>                  │
│      stat(id): Promise<Snapshot | undefined> │
│      list(): Promise<Snapshot[]>             │
├─────────────────────────────────────────────┤
│  Service Provider                            │
│  packages/session/session-persistence-jsonl/ │
│    → JSONL + zstd 文件系统实现               │
├─────────────────────────────────────────────┤
│  Consumer                                    │
│  packages/core/agent-loop/                   │
│    → create() 时调用 persistence.create()    │
│    → resume() 时调用 persistence.open()      │
└─────────────────────────────────────────────┘
```

---

## Fork（会话分叉）

Fork 把一个活着的 Session 从某个位置"切开复制"出一个新 Session：父 Session 继续跑，子 Session 带走之前的历史。典型用途是子 Agent 委托：

**源码位置**：`packages/core/session/src/fork.ts` buildForkSeed、`packages/core/session/src/index.ts` SessionStore.fork

```
ctx.sessions.fork(source, boundary?, childSessionId?)
  │
  ├─ 验证 source 是 live Session
  ├─ 确定 cut point（默认：最后一个事件的 seq）
  ├─ buildForkSeed：复制 events[0..cut] 作为 seed
  ├─ 追加 session/end-seed 标记（inheritedEventCount = cut）
  ├─ 为 open step 补充缺失的 tool result
  ├─ 追加 openTurnClosers（关闭父 session 的未完成 turn/step，cause 'forked'）
  ├─ 创建新 Session（childSessionId 或自动生成）
  └─ 返回新 Session
```

Fork 后的父子 Session：
- 子 Session 的 `ownEvents()` 返回 cut 之后的事件
- `isOwnSeq(seq)` 只接受子 Session 自己的位置
- 父 Session 继续独立运行

---

## 关键源码定位

想直接跳进源码时用这张表，按功能直达文件和行号：

| 功能 | 源码文件 | 关键部分 |
|---|---|---|
| Session 类定义 | `packages/core/session/src/index.ts` | +525 |
| append() 验证 | `packages/core/session/src/index.ts` | +806 |
| deriveMessages() | `packages/core/session/src/index.ts` | +999 |
| Surface 投影 | `packages/core/session/src/surface.ts` | SurfaceManager (+612) |
| 类型定义 | `packages/core/session/src/types.ts` | +1-200 |
| Fork | `packages/core/session/src/fork.ts` | buildForkSeed |
| Request Header | `packages/core/session/src/request-header.ts` | +1-69 |
| 修复 | `packages/core/session/src/repair.ts` | ToolCallRecovery (+105) |
