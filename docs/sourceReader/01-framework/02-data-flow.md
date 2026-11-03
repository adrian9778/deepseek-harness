# Main data flow end to end

English | [中文](02-data-flow.zh.md)

This page follows "one request" from start to finish: from the command line through plugin installation, Agent start, streaming model replies, tool execution, and log persistence, with a diagram at every stop. When a term is unfamiliar, check the glossary in [README.md](../README.md); for a finer look at any stage, each section points to its detailed document.

## Request flow panorama (ASCII)

The big picture first: a task enters from the command line and passes through "launch environment → install plugins → create Agent → inference loop", and all key actions converge into the same Session log.

```
                    ┌─────────────────────────────────────────────┐
                    │              dsh CLI (argv)                  │
                    │  "run the tests"  →  ProfileInvocation      │
                    └──────────────────┬──────────────────────────┘
                                       │
                                       ▼
                    ┌─────────────────────────────────────────────┐
                    │           Profile Boot                       │
                    │  1. loadLayeredEnv()    ← .env 文件         │
                    │  2. resolveProfileDir() ← $DSH_HOME          │
                    │  3. readProfilePatches() ← Bundle 列表       │
                    │  4. boot(...)             ← Cordis Loader    │
                    └──────────────────┬──────────────────────────┘
                                       │
               ┌───────────────────────┼───────────────────────┐
               │                       │                       │
               ▼                       ▼                       ▼
    ┌──────────────────┐   ┌──────────────────┐   ┌──────────────────┐
    │  Plugin: session  │   │  Plugin: tools    │   │  Plugin: llm     │
    │  ctx.sessions     │   │  ctx.tools        │   │  ctx.llm         │
    │  (内存 Store)     │   │  (注册表 + 管线)   │   │  (Provider 管理) │
    └────────┬─────────┘   └────────┬─────────┘   └────────┬─────────┘
             │                      │                      │
             │                      │                      │
             └──────────────────────┼──────────────────────┘
                                    │
                                    ▼
                    ┌─────────────────────────────────────────────┐
                    │         Agent Loop (ReactLoopAgent)          │
                    │                                              │
                    │   ┌─ Turn Boundary ─┐                       │
                    │   │ Inbox: claim     │                       │
                    │   │   next-turn msg  │                       │
                    │   └────────┬─────────┘                       │
                    │            │                                  │
                    │            ▼                                  │
                    │   ┌─ Step Loop ──┐                           │
                    │   │ 1. assemble   │ ← systemPrompt + tools    │
                    │   │    prompt     │   + deriveMessages()      │
                    │   │              │                           │
                    │   │ 2. stream     │ ← ctx.llm.stream()        │
                    │   │    request    │   → token chunks          │
                    │   │              │                           │
                    │   │ 3. parse      │                           │
                    │   │    tool calls │                           │
                    │   │              │                           │
                    │   │ 4. execute    │ → ctx.tools.execute()     │
                    │   │    the batch  │   → append tool/result    │
                    │   │              │                           │
                    │   │ 5. loop?      │ ← model wants more tools? │
                    │   └──────────────┘                           │
                    │            │                                  │
                    │            ▼ (no more tool calls)             │
                    │   Turn End → session/append assistant/message │
                    └─────────────────────────────────────────────┘
                                    │
                                    ▼
                    ┌─────────────────────────────────────────────┐
                    │           Session Log (持久化)                │
                    │                                              │
                    │   turn/start → step/start → user/message     │
                    │     → assistant/message → step/end → turn/end│
                    │                                              │
                    │   写入: JSONL v4 (.jsonl.zstd)               │
                    │   位置: $DSH_HOME/sessions/                  │
                    └─────────────────────────────────────────────┘
```

---

## Flow details

Each stop in the panorama, unpacked below. Every flow diagram keeps the real call names from the source so you can jump straight into the code.

### 1. Launch-time flow

Launching means assembling three inputs (environment variables, Profile config, user overlay) into one plugin list and handing it to Cordis to install:

```
.env 文件
  ↓ loadLayeredEnv()
process.env (DEEPSEEK_API_KEY 等)

package.json (dsh.profile.bundles)
  ↓ readProfilePatches()
cordis.yml 条目列表

cordis.patch.yml (用户覆盖)
  ↓ 叠加到条目列表
最终插件树

Cordis Loader
  ↓ 按依赖顺序 mount 每个 Plugin
Context (ctx) ← 所有服务注入完毕
```

### 2. Agent creation flow

After plugins are installed there is no conversation yet; creating an Agent is where work actually starts. Creation wires up four things: open the log, build the driver, run setup, announce the birth:

```
ctx.agents.create({ sessionId, agentOptions })
  │
  ├─► ctx.sessions.prepare(sessionId, { meta })
  │     → 创建内存 Session 实例
  │     → 触发 session/created 事件
  │
  ├─► ReactLoopAgent(loopCtx, sessionId, options, session, projections, lastTurn)
  │     → 创建 ReactLoopInbox（折叠 agent/inbox/spliced 投影）
  │     → 创建 RuntimeContextProjection + SystemPromptProjection
  │
  ├─► agent.scope 内执行 setup 回调
  │
  ├─► ctx.agents.enter(agent)
  │     → 等待 agent/created 监听器 settle（serial）
  │
  └─► 返回 AgentHandle { agent, dispose }
```

### 3. User input flow

Every user message is first written to the log (so it can never be lost), and only then wakes the Agent to pick it up:

```
用户调用 handle.agent.followup(message)
  │
  ├─► Inbox.append(message, target='next-turn')
  │     → session.append('agent/inbox/spliced', ...)
  │     → 触发 agent/inbox/inserted 事件
  │
  ├─► 唤醒 Agent Loop（如果处于 idle 状态）
  │
  └─► Agent Loop 在 Turn 边界时：
        inbox.claim('next-turn', turn)
          → 取出消息，触发 agent/inbox/claimed 事件
          → 传入 pre-step 组装阶段
```

### 4. One complete Turn

This is the system's "engine": one conversation round = prepare materials → ask the model → run tools → ask the model again ... until the model stops asking for tools:

```
┌─────────────────────────────────────────────────────────┐
│ TURN START                                              │
│  session.append('turn/start', { turn: N })              │
├─────────────────────────────────────────────────────────┤
│ STEP LOOP (每轮)                                        │
│                                                         │
│  [1] 准入与组装                                          │
│      inbox.claim(target, turn)                          │
│      tools.resolve({ agent, modelSelection, signal })   │
│      systemPrompt.assemble({ modelSelection, toolSet }) │
│        → 拼接所有 PromptSection                         │
│        → 返回 PromptAssembly { sections, contexts, ... }│
│      agent/pre-step waterfall                           │
│        → 默认决策：改写进入的 messages 或拒绝            │
│                                                         │
│  [2] 请求准备                                            │
│      prepareRequest()                                   │
│        → 从 session.requestHeader() 读取上次配置        │
│        → agent/request waterfall                        │
│        → ctx.llm.prepareCall(config, signal)            │
│                                                         │
│  [3] 请求构建                                            │
│      buildRequest(...)                                  │
│        → canonicalHeader + request/header 事件          │
│        → 工具增减写 developer/message                   │
│        → session.deriveMessages()（deep-freeze 后发送） │
│        → markAgentLoopRequest(Object.freeze(...))       │
│                                                         │
│  [4] Stream 处理                                         │
│      AssistantStreamAttempt(stream)                     │
│        → 累积 text/reasoning/tool-call chunks           │
│        → 结束时生成 AssistantMessage 或 Attempt         │
│                                                         │
│  [5] 工具调用执行（如有）                                  │
│      ctx.tools.execute({ calls, maxParallelToolCalls }) │
│        ├─ tools/pre-execute (waterfall)                 │
│        ├─ 守卫检查 + 审批路由                            │
│        ├─ tools/execute (waterfall)                     │
│        ├─ tools/post-execute (waterfall)                │
│        ├─ tools/result (emit)                           │
│        └─ session.append('tool/call'/'tool/result')     │
│                                                         │
│  [6] Step 结束判断                                       │
│      无更多 tool calls？→ 退出 Step 循环                │
│      有？→ 回到 [1] 重新组装（含工具结果）                │
│                                                         │
├─────────────────────────────────────────────────────────┤
│ TURN END                                                │
│  session.append('turn/end', { turn: N, reason })        │
└─────────────────────────────────────────────────────────┘
```

### 5. Session log write flow

Every append passes five gates: is the type known, is the content valid, can the snapshot serialize losslessly — only then does it enter memory and fire events:

```
session.append(type, data, opts?)
  │
  ├─► 类型检查：type 必须在 SessionEventMap 中
  │
  ├─► Surface 事件验证（system/user/assistant/developer/tool/result）:
  │     - 必须有 surfaceOp
  │     - 替换操作必须引用有效的 seq 范围
  │
  ├─► lossless JSON 快照验证:
  │     snapshotJsonValue() — 一次性读取+验证+拷贝
  │     拒绝：BigInt、循环引用、稀疏数组、-0、异常原型
  │
  ├─► 追加到内存数组
  │
  ├─► 递增 seq，分配 SessionSeq
  │
  ├─► 刷新 surface 投影（contentGeneration / replaceGeneration）
  │
  └─► 触发 session/event（调用方各自订阅处理）
```

### 6. Persistence flow

Beyond the in-memory log, a persistence backend handles "flushing to disk" and "resuming after restart". Writes go through buffered batches; on resume every line is validated and unknown types are rejected outright:

```
SessionPersistence 后端（如 session-persistence-jsonl）
  │
  ├─► 监听 session/event 事件
  │     → 缓冲 live 事件，批量写入 JSONL（.jsonl.zstd 压缩帧）
  │
  ├─► 监听 session/flush 事件
  │     → drain + fsync 确保磁盘落盘
  │
  └─► handle close
        → 关闭文件句柄，释放资源

读取（resume）:
  sessionPersistence.open(sessionId, 'write')
    → 选择数值最高的 canonical generation 文件
    → 解码 zstd，逐行解析 JSONL，逐事件验证
    → 拒绝未知必需事件类型（fail-closed）
    → 返回 SessionHandle（可追加）
```

---

## Error propagation paths

Who catches an error, where it travels, and what the user finally sees. Note the design stance: tool-level failures do not sink the whole conversation; only model-level faults end the Turn:

| 错误发生点 | 传播路径 | 最终表现 |
|---|---|---|
| Provider 调用失败 | `llm/stream` → `LlmError.failure` | attempt 记录，`agent/request-error` 决定重试 |
| 工具执行超时 | timeout policy 包装 `tools/execute` | 返回 error result，不中断 Turn |
| 工具调用崩溃 | `ToolBatchExecutionError(error, partial)` | 已完成结果保留，剩余调用补合成结果 |
| 用户取消 | `AbortSignal` 带 `AgentCancelCause` | 当前 Step 中止，已流式文本保留（`interrupted: true`） |
| 模型上下文溢出 | `LlmError` code `CONTEXT_WINDOW_EXCEEDED` | Turn 以 error 结束，可触发 compaction |

---

## Immutable objects in key flows

The system "freezes" data at key positions so downstream code cannot quietly change it and desynchronize the log from reality. These four kinds of objects are immutable once created:

- **Request**: the config `ctx.llm.prepareCall()` returns is deep-frozen; providers cannot modify it
- **Tool args**: snapshotted through `snapshotJsonValue()` and deep-frozen; the execute body receives immutable arguments
- **Session events**: `deepFreeze()`d on append; later code cannot modify them
- **Assistant stream**: compacted once settled and embedded whole in the `assistant/message` event
