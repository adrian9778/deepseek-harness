# Agent lifecycle: create → drive → finish

English | [中文](00-agent-lifecycle.zh.md)

An Agent's life has exactly four stages: create → drive → cancel → dispose. This page draws out "who does what when, and what happens on failure" for each stage, with a source-location table at the end. The state-machine diagram up top is the global map; the four sections below expand it piece by piece.

## Agent lifecycle state machine

The global picture first: an Agent's whole life is cycling between IDLE (standing by) and RUNNING (working) until disposed:

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

## Phase 1: Create

Creation is four jobs: open the log, build the driver, run setup, announce the birth. Any step that fails rolls back cleanly, never leaving a half-built Agent:

**Entry**: `ctx.agents.create(options)`. **Source**: `packages/core/agent/src/index.ts` create/enter/announce, `packages/core/agent-loop/src/index.ts` create/createAgent/initializeAgent

### Step breakdown

```
1. 验证 FiberState
   if (INACTIVE_STATES.has(ctx.fiber.state)) throw NEW_CONTEXT_REQUIRED

2. 生成 sessionId（如未指定）
   const sessionId = SessionId(randomUUID())

3. 创建 Session
   const session = ctx.sessions.prepare(sessionId, { meta, ... })
     → 触发 session/created 事件

4. 创建 ReactLoopAgent
   const agent = new ReactLoopAgent(loopCtx, sessionId, options, session, projections, lastTurn)

5. 执行 setup 回调（如有）
   await setup(agentCtx, agent)
     → agentCtx 是 agent.scope.ctx，仅在此 Agent 作用域内生效
     → 可注册 tool、prompt section、listener 等

6. 进入注册表
   const detach = registry.enter(agent, owner)

7. 发布 agent/created 事件
   await registry.announce(agent, source)
     → serial dispatch，监听器失败会拒绝创建

8. 返回 AgentHandle
   return { agent, dispose: () => teardown() }
```

### Rollback guarantees

If the setup callback or registration fails, the system cleans the scene as if nothing had happened:
- the session is closed
- the scope is unwound
- every effect registered during setup is disposed
- `agent/created` is not announced (announce never ran to completion)

---

## Phase 2: Drive

Driving is the Agent's "heartbeat": standing by (idle) when there is no work, working (running) when there is, with a preparation gear (maintenance) in between. Once you know the three phases and the wake mechanism, the main loop reads itself:

**Entry**: the `ReactLoopAgent` state machine loop. **Source**: `packages/core/agent-loop/src/agent.ts` +112 (class), +268 (kick), +327 (turn), +435 (step)

### The Phase type

```ts
type Phase =
  | { kind: 'idle'; lastTurn: number }
  | { kind: 'maintenance'; abort: AbortController; lastTurn: number; wakeRequested: boolean }
  | { kind: 'running'; abort: AbortController; turn: number; step: number; wakeRequested: boolean }
```

- `idle`：等待 Inbox 中有输入
- `maintenance`：准备阶段（AbortController 已创建但未开始 Turn）
- `running`：正在执行 Turn/Step

The three message entry points map to three delivery targets: `followup` delivers to the next turn, `steer` slips into the next step, `inject` stores without waking.

### Wake mechanism

```
followup(message)  →  Inbox.append(message, target='next-turn')
steer(message)     →  Inbox.append(message, target='next-step')
inject(context)    →  Inbox.append(message, target='next-step', wake=false)

当 phase == idle 时，append 会自动触发 maintenance → running 转换
当 phase != idle 时，wakeRequested = true，下次循环边界会检查
```

### Driver loop in pseudocode

把三个相位串起来，主循环其实只有三种状态各做一件事：

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

## Phase 3: Cancel

Cancellation has four causes but one path: raise the abort signal → clear the Inbox (optional) → wait for current activity to wind down. Everything already produced (such as streamed text) is always kept:

**Entry**: `handle.agent.cancel(cause, options?)`. **Source**: `packages/core/agent-loop/src/agent.ts` cancel, `packages/core/session/src/types.ts` +194 (AgentCancelCause)

### Cancel semantics

```ts
interface AgentCancelCause {
  kind: 'user' | 'parent' | 'disposed' | 'hook'
  reason?: string
}
```

- `'user'`：用户主动取消（如点击停止按钮），保留 Inbox 中的 pending 消息
- `'parent'`：父 Agent 取消（子 Agent 被回收）
- `'disposed'`：AgentHandle 被 dispose
- `'hook'`：`agent/turn-stopping` 监听器触发的取消

### Cancel execution path

取消永远走同一条路：中止信号 → 清 Inbox（可选）→ 等收尾：

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

## Phase 4: Dispose

Disposal follows a strict order: stop the loop first, unwind the scope next, close the log last. Out-of-order disposal fails loudly — the order guarantees a complete log and no missed cleanup:

**Entry**: `handle.dispose()`. **Source**: `packages/core/agent/src/index.ts` enter/detach, `packages/core/agent-loop/src/index.ts` +500-545

### Dispose steps

六步依次执行，每步都在为下一步铺路：

```
1. agent.cancel({ kind: 'disposed' }, { keepInbox: !ready })
   → 停止正在运行的 Turn/Step

2. await agent.whenIdle()
   → 等待当前 Turn 完全结束（包括已排队的后续操作）

3. machine.scope.dispose()
   → 解开此 Agent 的所有 scoped registration
   → 触发的 disposer 按逆序执行

4. registry detach
   → 从 AgentRegistry 中移除
   → 触发 agent/disposed（emit，监听器失败被包含）

5. session detach + stored handle close
   → 触发 session/disposed 事件
   → persistence 后端收到 close（close 前 drain）

6. 释放 AgentHandle
   → id 可被重新使用（新 create 可用相同 id）
```

### Dispose ordering guarantees

Why this order? Each step closes off one class of "something can still write here":
1. **Stop the loop first**: no further Session appends
2. **Unwind the scope next**: no further effect registration
3. **Close the session last**: all events have been processed

---

## Key source map

When you want to jump straight into the source, this table goes from operation to file and method:

| 操作 | 源码文件 | 关键部分 |
|---|---|---|
| `create()` | `packages/core/agent/src/index.ts` | create/enter/announce |
| `resume()` | `packages/core/agent/src/index.ts` | resume |
| `cancel()` | `packages/core/agent-loop/src/agent.ts` | cancel 方法 |
| `dispose()` | `packages/core/agent-loop/src/index.ts` | +500-545 |
| Phase 状态机 | `packages/core/agent-loop/src/agent.ts` | +53-61（Phase 类型）+327（turn） |
| `turn()` | `packages/core/agent-loop/src/agent.ts` | +327 |
| `step()` | `packages/core/agent-loop/src/agent.ts` | +435 |
| `whenIdle()` | `packages/core/agent-loop/src/agent.ts` | whenIdle 方法 |
