# Agent 创建到完成的完整生命周期

## Agent 生命周期状态机

```
                    ┌──────────────┐
                    │   CREATED    │ ← ctx.agents.create() / resume()
                    └──────┬───────┘
                           │ agent/created (serial event)
                           ▼
                    ┌──────────────┐
             ┌─────│    IDLE      │─────┐
             │     │ (waiting for  │     │ followup() / steer()
             │     │  inbox input) │     │
             │     └──────┬───────┘     │
             │            │ wake        │
             │            ▼             │
             │     ┌──────────────┐     │
             │     │   RUNNING    │     │
             │     │ (driving     │     │
             │     │  turns/steps)│     │
             │     └──────┬───────┘     │
             │            │ all turns   │
             │            │ complete    │
             │            ▼             │
             │     ┌──────────────┐     │
             └─────│   IDLE (done)│◄────┘
                   │ (inbox empty,│     │ cancel() / followup()
                   │  no more work)│
                   └──────┬───────┘
                          │ dispose()
                          ▼
                   ┌──────────────┐
                   │  DISPOSED    │ ← scope unwound, session closed
                   └──────────────┘
```

---

## 阶段 1：创建（Create）

**入口**：`ctx.agents.create(options)`  
**源码位置**：`packages/core/agent/src/index.ts` +100-200、`packages/core/agent-loop/src/index.ts` +150-300

### 步骤分解

```
1. 验证 FiberState
   if (INACTIVE_STATES.has(ctx.fiber.state)) throw NEW_CONTEXT_REQUIRED

2. 生成 sessionId（如未指定）
   const sessionId = SessionId(randomUUID())

3. 创建 Session
   const session = ctx.sessions.create(sessionId, { meta, seed, inheritedEventCount })
     → 触发 session/created 事件

4. 创建 ReactLoopAgent
   const agent = new ReactLoopAgent(ctx, sessionId, agentOptions, session)

5. 执行 setup 回调（如有）
   await setup(agentCtx, agent)
     → agentCtx 是 agent.scope.ctx，仅在此 Agent 作用域内生效
     → 可注册 tool、prompt section、listener 等

6. 注册到 AgentRegistry
   await registry.enter(agent)
     → 等待所有 agent/created 监听器 settle（serial）

7. 发布 agent/created 事件
   dispatch.emit('agent/created', { agent })

8. 返回 AgentHandle
   return { agent, dispose: handleDispose.bind(null, agent) }
```

### 回滚保证

如果步骤 5（setup）或步骤 6（enter）失败：
- Session 被 close
- Scope 被 unwind
- 所有 setup 期间注册的 effect 被清理
- 不触发 agent/created（因为 enter 尚未完成）

---

## 阶段 2：驱动（Drive）

**入口**：`ReactLoopAgent` 内部的状态机循环  
**源码位置**：`packages/core/agent-loop/src/agent.ts` +98（类声明）、+298（turn）、+400（step）

### 相位（Phase）类型

```ts
type Phase =
  | { kind: 'idle'; lastTurn: number }
  | { kind: 'maintenance'; abort: AbortController; lastTurn: number; wakeRequested: boolean }
  | { kind: 'running'; abort: AbortController; turn: number; step: number; wakeRequested: boolean }
```

- `idle`：等待 Inbox 中有输入
- `maintenance`：准备阶段（AbortController 已创建但未开始 Turn）
- `running`：正在执行 Turn/Step

### 唤醒机制

```
followup(message)  →  Inbox.append(message, target='next-turn')
steer(message)     →  Inbox.append(message, target='next-step')
inject(context)    →  Inbox.append(message, target='next-step', wake=false)

当 phase == idle 时，append 会自动触发 maintenance → running 转换
当 phase != idle 时，wakeRequested = true，下次循环边界会检查
```

### 主循环伪代码

```ts
async function drive() {
  while (phase.kind !== 'disposed') {
    if (phase.kind === 'idle') {
      await waitForWake()       // 等待 Inbox 有消息
      startMaintenance()        // 创建 AbortController
    }

    if (phase.kind === 'maintenance') {
      if (!wakeRequested) continue  // 等待新的唤醒
      startRunning()                 // 进入 Turn 循环
    }

    if (phase.kind === 'running') {
      const turnResult = await runTurn()
      // turnResult 决定下一步：
      //   'completed' → 检查是否有更多 pending，若有继续，否则 idle
      //   'cancelled' → 退出，进入 disposed
    }
  }
}
```

---

## 阶段 3：取消（Cancel）

**入口**：`handle.agent.cancel(cause, options?)`  
**源码位置**：`packages/core/agent/src/types.ts` +cancel、`packages/core/agent-loop/src/agent.ts` +cancel

### Cancel 语义

```ts
interface AgentCancelCause {
  kind: 'user' | 'parent' | 'disposed' | 'hook'
  reason?: string
}
```

- `'user'`：用户主动取消（如点击停止按钮），保留 Inbox 中的 pending 消息
- `'parent'`：父 Agent 取消（子 Agent 被回收）
- `'disposed'`：AgentHandle 被 dispose
- `'hook'`：agent/turn-stopping 监听器触发的取消

### Cancel 执行路径

```
cancel(cause, { keepInbox? })
  │
  ├─► 设置 phase.abort.abort(cause)
  │     → 当前正在进行的 LLM stream 收到 AbortSignal
  │     → 当前正在执行的工具调用收到 exec.signal
  │
  ├─► 如果 keepInbox 为 false（默认）：
  │     Inbox.clear()  → 丢弃所有 pending 消息
  │     → 触发 agent/inbox/discarded 事件
  │
  └─► 等待当前活动完成（drain）
        → 已流式输出的文本保留在 assistant/message 中（interrupted: true）
        → 未执行的工具调用不记录结果
```

---

## 阶段 4：销毁（Dispose）

**入口**：`handle.dispose()`  
**源码位置**：`packages/core/agent/src/index.ts` +dispose、`packages/core/agent-loop/src/index.ts` +dispose

### Dispose 步骤

```
1. agent.cancel({ kind: 'disposed' })
   → 停止正在运行的 Turn/Step

2. await agent.whenIdle()
   → 等待当前 Turn 完全结束（包括已排队的后续操作）

3. registry.detach(agent)
   → 从 AgentRegistry 中移除
   → 等待所有监听器 settle

4. scope.dispose()
   → 解开此 Agent 的所有 scoped registration
   → 触发的 disposer 按逆序执行

5. session.close()
   → 关闭 Session 的写入路径
   → 触发 session/disposed 事件
   → persistence 后端收到 flush + retire

6. 释放 AgentHandle
   → id 可被重新使用（新 create 可用相同 id）
```

### Dispose 顺序保证

Dispose 顺序是严格定义的，违反顺序会报错：
1. **先停 Loop**：确保不会再有新的 Session append
2. **再解 Scope**：确保不会再有新的 effect 注册
3. **最后关 Session**：确保所有事件已处理完毕

---

## 关键源码定位

| 操作 | 源码文件 | 关键行范围 |
|---|---|---|
| `create()` | `packages/core/agent/src/index.ts` | +100-250 |
| `resume()` | `packages/core/agent/src/index.ts` | +250-400 |
| `cancel()` | `packages/core/agent/src/types.ts` | cancel 方法 |
| `dispose()` | `packages/core/agent/src/index.ts` | +dispose |
| Phase 状态机 | `packages/core/agent-loop/src/agent.ts` | +60-100（Phase 类型）+298-399（turn） |
| `turn()` | `packages/core/agent-loop/src/agent.ts` | +298-399 |
| `step()` | `packages/core/agent-loop/src/agent.ts` | +400-560 |
| `whenIdle()` | `packages/core/agent/src/types.ts` | whenIdle 方法 |
