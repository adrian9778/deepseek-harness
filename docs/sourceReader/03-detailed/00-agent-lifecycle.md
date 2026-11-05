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

**Entry**: `ctx.agents.create(options)`. **Source**: `packages/core/agent/src/index.ts` · AgentRegistry.create/enter/announce, `packages/core/agent-loop/src/index.ts` · AgentLoop.create/createAgent/initializeAgent

### Step breakdown

```
1. 验证调用方 FiberState
   ownerCtx.fiber.assertActive()   → 已卸载的 fiber 抛 INACTIVE_EFFECT

2. 确定 sessionId（如未指定）
   → 由调用方策略生成（如 headless runner 用 session-${randomUUID()}；
     未传 id 时 SessionStore 兜底 session-N 递增）

3. 创建 Session
   const session = ctx.sessions.prepare(sessionId, { meta, ... })
     → 尚未发布，session/created 在第 5 步随 enter/announce 发布

4. 创建 ReactLoopAgent
   const agent = new ReactLoopAgent(loopCtx, sessionId, options, session, projections, lastTurn)

5. 发布 Session 并执行 setup 回调（如有）
   agent.ctx.sessions.enter(session)；sessions.announce(session)
   await setup(agentCtx, agent)
     → agentCtx 是 agent.scope.ctx，仅在此 Agent 作用域内生效
     → 可注册 tool、prompt section、listener 等

6. 进入注册表
   const detach = registry.enter(agent)

7. 发布 agent/created 事件
   await registry.announce(agent, source)
     → serial dispatch，监听器失败会拒绝创建

8. 返回 AgentHandle
   return { agent, dispose }
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

**Entry**: the `ReactLoopAgent` state machine loop. **Source**: `packages/core/agent-loop/src/agent.ts` · ReactLoopAgent（kick / turn / step）

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

Cancellation has four causes but one path: clear the Inbox (optional) → raise the abort signal → wait for current activity to wind down. Everything already produced (such as streamed text) is always kept:

**Entry**: `handle.agent.cancel(cause, options?)`. **Source**: `packages/core/agent-loop/src/agent.ts` · ReactLoopAgent.cancel, `packages/core/session/src/types.ts` · AgentCancelCause

### Cancel semantics

```ts
type AgentCancelCause =
  | { kind: 'user' }
  | { kind: 'parent' }
  | { kind: 'hook'; reason: string }
  | { kind: 'disposed' }
```

- `'user'`：用户主动取消（如点击停止按钮），保留 Inbox 中的 pending 消息
- `'parent'`：父 Agent 取消（子 Agent 被回收）
- `'disposed'`：AgentHandle 被 dispose
- `'hook'`：`agent/turn-stopping` 监听器触发的取消（唯一携带 `reason` 的变体）

### Cancel execution path

取消永远走同一条路：中止信号 → 清 Inbox（可选）→ 等收尾：

```
cancel(cause, { keepInbox? })
  │
  ├─► 如果 keepInbox 为 false（默认）：
  │     Inbox.clear()  → 丢弃所有 pending 消息
  │     → 触发 agent/inbox/discarded 事件
  │
  ├─► phase.abort.abort(cause)（仅当 phase != idle）
  │     → 当前正在进行的 LLM stream 收到 AbortSignal
  │     → 当前正在执行的工具调用收到 exec.signal
  │
  └─► 调用方 await whenIdle() 等待当前活动收尾（drain）
        → 已流式输出的文本保留在 assistant/message 中（interrupted: true）
        → 未完成的工具调用补记合成 error 结果（真实结果不写入）
```

---

## Phase 4: Dispose

Disposal follows a strict order: stop the loop first, unwind the scope next, close the log last. Out-of-order disposal fails loudly — the order guarantees a complete log and no missed cleanup:

**Entry**: `handle.dispose()`. **Source**: `packages/core/agent/src/index.ts` · AgentRegistry.enter/detach, `packages/core/agent-loop/src/index.ts` · AgentLoop.initializeAgent +33~+89 (teardown/dispose closures)

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

4. stored handle close
   → persistence 后端收到 close（close 前 drain 已路由进 handle 的事件）

5. registry detach + session detach
   → 从 AgentRegistry / SessionStore 移除
   → 触发 agent/disposed、session/disposed（emit，监听器失败被包含）

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

When you want to jump straight into the source, this table goes from operation to file and symbol:

| 操作 | 源码文件 | 关键部分 |
|---|---|---|
| `create()` | `packages/core/agent/src/index.ts` | `AgentRegistry.create`（工厂侧 `AgentLoop.createAgent`） |
| `resume()` | `packages/core/agent/src/index.ts` | `AgentRegistry.resume` |
| `cancel()` | `packages/core/agent-loop/src/agent.ts` | `ReactLoopAgent.cancel` |
| `dispose()` | `packages/core/agent-loop/src/index.ts` | `AgentLoop.initializeAgent · teardown/dispose` |
| Phase 状态机 | `packages/core/agent-loop/src/agent.ts` | `Phase` 类型 |
| `turn()` | `packages/core/agent-loop/src/agent.ts` | `ReactLoopAgent.turn` |
| `step()` | `packages/core/agent-loop/src/agent.ts` | `ReactLoopAgent.step` |
| `whenIdle()` | `packages/core/agent-loop/src/agent.ts` | `ReactLoopAgent.whenIdle` |
