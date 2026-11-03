# 主数据流与整体运行流程

[English](02-data-flow.md) | 中文

这一页沿着"一条请求"从头走到尾：从命令行进来到插件装好、Agent 开跑、模型流式回复、工具执行、日志落盘，每一站都给一张图。看不懂某个名词时先查 [README.zh.md](../README.zh.md) 的术语表；想看更细的某一段，每节末尾都指向对应的详细文档。

## 请求流转全景图（ASCII）

先看大图：一条任务从命令行进入，先后经过"启动环境 → 装插件 → 建 Agent → 循环推理"，所有关键动作最后都汇入同一份 Session 日志。

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

## 数据流详解

全景图里的每一站，下面逐个拆开看。每段流程图保持源码里的真实调用名，方便直接跳进代码。

### 1. 启动阶段数据流

启动就是把三样东西（环境变量、Profile 配置、用户覆盖）拼成一份插件清单，然后交给 Cordis 装好：

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

插件装好后还没有任何对话；创建 Agent 才真正开始一轮工作。创建过程串了四件事：开日志、建驱动、跑 setup、广播诞生：

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

### 3. 用户输入流入数据流

用户每发一条消息，先写进日志（保证丢不了），再唤醒 Agent 去取：

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

### 4. 一次完整 Turn 的数据流

这是整个系统的"发动机"：一轮对话 = 准备材料 → 问模型 → 执行工具 → 再问模型……直到模型不再要工具为止：

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

### 5. Session 日志写入数据流

每次 append 都过五道关：类型对不对、内容合不合法、快照能不能无损序列化，然后才进内存、发事件：

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

### 6. 持久化数据流

内存日志之外，还有一个持久化后端负责"落盘"和"重启恢复"。写入走批量缓冲，恢复时逐行验证、不认识的类型直接拒绝：

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

## 错误传播路径

出错时谁接住、传给谁、最后用户看到什么。注意设计取向：工具级失败不拖垮整轮对话，只有模型级故障才终止 Turn：

| 错误发生点 | 传播路径 | 最终表现 |
|---|---|---|
| Provider 调用失败 | `llm/stream` → `LlmError.failure` | attempt 记录，`agent/request-error` 决定重试 |
| 工具执行超时 | timeout policy 包装 `tools/execute` | 返回 error result，不中断 Turn |
| 工具调用崩溃 | `ToolBatchExecutionError(error, partial)` | 已完成结果保留，剩余调用补合成结果 |
| 用户取消 | `AbortSignal` 带 `AgentCancelCause` | 当前 Step 中止，已流式文本保留（`interrupted: true`） |
| 模型上下文溢出 | `LlmError` code `CONTEXT_WINDOW_EXCEEDED` | Turn 以 error 结束，可触发 compaction |

---

## 关键流程中的不可变对象

系统在关键位置把数据"冻结"起来，防止下游偷偷修改导致日志与实况对不上。这四类对象一旦生成就不可再改：

- **Request**: `ctx.llm.prepareCall()` 返回的配置经 deep-freeze，Provider 不可修改
- **Tool args**: 通过 `snapshotJsonValue()` 后 deep-freeze，执行函数收到不可变参数
- **Session events**: 追加后 `deepFreeze()`，不可被后续修改
- **Assistant stream**: 累积后 compact 存储，完整嵌入 `assistant/message` 事件
