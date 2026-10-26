# 主数据流与整体运行流程

## 请求流转全景图（ASCII）

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
                    │  4. boot(cordisTree)      ← Cordis Loader    │
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
                    │   │    each call  │   → append tool/result    │
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
                    │   位置: $DSH_HOME/sessions/<id>/             │
                    └─────────────────────────────────────────────┘
```

---

## 数据流详解

### 1. 启动阶段数据流

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

### 2. Agent 创建数据流

```
ctx.agents.create({
  sessionId: "abc-123",
  agentOptions: { provider: "deepseek", model: "deepseek-chat" }
})
  │
  ├─► SessionStore.create(sessionId, { meta })
  │     → 创建内存 Session 实例
  │     → 触发 session/created 事件
  │
  ├─► ReactLoopAgent(ctx, sessionId, options, session)
  │     → 创建 ReactLoopInbox（维护 next-turn / next-step 队列）
  │     → 创建 RuntimeContextProjection
  │     → 注册 turnBoundary 投影
  │
  ├─► agent/created 事件（waterfall，监听器可注入 context）
  │
  └─► 返回 AgentHandle { agent, dispose }
```

### 3. 用户输入流入数据流

```
用户调用 handle.agent.followup({ content: [...] })
  │
  ├─► Inbox.append(message, { target: 'next-turn' })
  │     → 触发 agent/inbox/inserted 事件
  │
  ├─► 唤醒 Agent Loop（如果处于 idle 状态）
  │
  └─► Agent Loop 在 Turn 边界时：
        Inbox.claimNextTurnInput()
          → 取出消息，触发 agent/inbox/claimed 事件
          → 传入 pre-step 组装阶段
```

### 4. 一次完整 Turn 的数据流

```
┌─────────────────────────────────────────────────────────┐
│ TURN START                                              │
│  session.append('turn/start', { turn: N })              │
├─────────────────────────────────────────────────────────┤
│ STEP LOOP (每轮)                                        │
│                                                         │
│  [1] 系统提示词组装                                       │
│      systemPrompt.assemble(agentCtx)                    │
│        → 拼接所有 PromptSection                         │
│        → 注入当前工具 Schema                             │
│        → 返回 { systemText, toolSchemas, ... }          │
│                                                         │
│  [2] 历史推导                                            │
│      session.deriveMessages()                           │
│        → 读取所有 surface 事件                           │
│        → 过滤出模型可见的 Message[]                       │
│        → 附加最新 system/message 节点                    │
│                                                         │
│  [3] 模型请求                                            │
│      ctx.llm.stream({                                  │
│        provider, model,                                 │
│        messages: derivedHistory,                        │
│        tools: toolSchemas,                              │
│        signal                                          │
│      })                                                 │
│        → Provider 适配器（如 DeepSeek）处理 HTTP 请求    │
│        → 返回 AsyncIterable<StreamChunk>                │
│                                                         │
│  [4] Stream 处理                                         │
│      AssistantStreamAttempt(stream)                     │
│        → 累积 text/reasoning/tool-calls chunks          │
│        → 结束时生成 AssistantMessage 或 Attempt         │
│                                                         │
│  [5] 工具调用执行（如有）                                  │
│      FOR EACH toolCall in response.toolCalls:           │
│        ├─ tools/pre-execute (waterfall)                 │
│        │   → 权限策略检查、限流                          │
│        ├─ tools/execute (waterfall)                     │
│        │   → 执行 ToolDefinition.execute()              │
│        │   → 返回 ToolExecutionResult                   │
│        ├─ tools/post-execute (waterfall)                │
│        │   → 结果替换/审计                               │
│        └─ session.append('tool/result', ...)            │
│            → 触发 tool/result 事件                       │
│                                                         │
│  [6] Step 结束判断                                       │
│      无更多 tool calls？→ 退出 Step 循环                │
│      有？→ 回到 [1] 重新推导历史（含工具结果）            │
│                                                         │
├─────────────────────────────────────────────────────────┤
│ TURN END                                                │
│  session.append('turn/end', { turn: N, reason })        │
└─────────────────────────────────────────────────────────┘
```

### 5. Session 日志写入数据流

```
session.append(type, data, opts?)
  │
  ├─► 类型检查：type 必须在 SessionEventMap 中
  │
  ├─► Surface 事件验证（system/user/assistant/developer/tool/result）:
  │     - 必须有 surfaceOp
  │     - 必须有 source
  │     - 替换操作必须引用有效的 startSeq/endSeq
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
  ├─► emit('session/event', event)  — 广播给所有监听器
  │
  └─► 如果有 persistence 后端：
        批量缓冲，等待 session/flush 或自动 checkpoint
```

### 6. 持久化数据流

```
SessionPersistence 后端（如 session-log-deepseek）
  │
  ├─► 监听 session/event 事件
  │     → 写入 JSONL 文件（压缩为 .zstd）
  │
  ├─► 监听 session/flush 事件
  │     → fsync 确保磁盘落盘
  │
  └─► 监听 session/disposed 事件
        → 关闭文件句柄，释放资源

读取（resume）:
  sessionPersistence.open(sessionId, 'write')
    → 扫描目录，找到最高版本号文件
    → 解析 JSONL，逐事件验证
    → 应用相邻迁移链（vN → vN+1）
    → 返回 SessionHandle（可追加）
```

---

## 错误传播路径

| 错误发生点 | 传播路径 | 最终表现 |
|---|---|---|
| Provider 认证失败 | `llm/stream` → `LlmError.AUTH` | 步骤失败，记录 attempt 事件 |
| 工具执行超时 | `tools/execute` → `TOOL_TIMEOUT` | 返回 error result，不中断 Turn |
| Session 写入失败 | `session.append()` 抛出 | Agent 驱动 catch 后转为 attempt |
| 用户取消 | `AbortSignal.reason` = `cancel()` | 当前 Step 中止，已流式文本保留 |
| 模型上下文溢出 | `LlmError.CONTEXT_WINDOW_EXCEEDED` | 步骤失败，可触发 compaction |

---

## 关键流程中的不可变对象

- **Request**: `ctx.llm.prepareCall()` 返回 deep-frozen 配置，Provider 不可修改
- **Tool args**: 通过 `snapshotJsonValue()` 后 deep-freeze，执行函数收到不可变参数
- **Session events**: 追加后 `deepFreeze()`，不可被后续修改
- **Assistant stream**: 累积后 compact 存储，完整嵌入 `assistant/message` 事件
