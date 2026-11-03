# Agent 创建到完成的完整生命周期

[English](00-agent-lifecycle.md) | 中文

一个 Agent 从诞生到销毁只有四个阶段：创建 → 驱动 → 取消 → 销毁。这一页给每个阶段画清楚"谁在什么时候做什么、失败会怎样"，末尾附一张源码定位表。开头的状态机图是全局地图，后面四节逐段展开。

## Agent 生命周期状态机

先看全局：一个 Agent 的一生就是在 IDLE（待命）和 RUNNING（干活）之间打转，直到被 dispose：

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

创建就是"开日志、建驱动、跑 setup、广播诞生"四件事。任何一步失败都有回滚保证，不会留下半成品：

**入口**：`ctx.agents.create(options)`。**源码位置**：`packages/core/agent/src/index.ts` create/enter/announce、`packages/core/agent-loop/src/index.ts` create/createAgent/initializeAgent

### 步骤分解

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

### 回滚保证

如果 setup 回调或注册失败，系统会像什么都没发生过一样清干净现场：
- Session 被 close
- Scope 被 unwind
- 所有 setup 期间注册的 effect 被清理
- 不发布 agent/created（announce 从未完成）

---

## 阶段 2：驱动（Drive）

驱动是 Agent 的"心跳"：平时待命（idle），有活就跑（running），中间有个准备档（maintenance）。理解了三个相位和唤醒机制，主循环就一目了然：

**入口**：`ReactLoopAgent` 内部的状态机循环。**源码位置**：`packages/core/agent-loop/src/agent.ts` +112（类声明）、+268（kick）、+327（turn）、+435（step）

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

三种消息入口对应三个投递目标：`followup` 投给下一轮、`steer` 插进下一步、`inject` 只入库不唤醒。

### 唤醒机制

```
followup(message)  →  Inbox.append(message, target='next-turn')
steer(message)     →  Inbox.append(message, target='next-step')
inject(context)    →  Inbox.append(message, target='next-step', wake=false)

当 phase == idle 时，append 会自动触发 maintenance → running 转换
当 phase != idle 时，wakeRequested = true，下次循环边界会检查
```

### 主循环伪代码

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

## 阶段 3：取消（Cancel）

取消分四种原因，但路径都一样：发中止信号 → 清理 Inbox（可选）→ 等当前活动收尾。已产出的成果（如已流式输出的文本）一律保留：

**入口**：`handle.agent.cancel(cause, options?)`。**源码位置**：`packages/core/agent-loop/src/agent.ts` cancel、`packages/core/session/src/types.ts` +194（AgentCancelCause）

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
- `'hook'`：`agent/turn-stopping` 监听器触发的取消

### Cancel 执行路径

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

## 阶段 4：销毁（Dispose）

销毁有严格的顺序：先停循环、再解作用域、最后关日志。顺序错了会报错——这是为了保证日志完整、没有遗漏的清理：

**入口**：`handle.dispose()`。**源码位置**：`packages/core/agent/src/index.ts` enter/detach、`packages/core/agent-loop/src/index.ts` +500-545

### Dispose 步骤

六步依次执行，每一步都在为下一步铺路：

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

### Dispose 顺序保证

为什么是这个顺序？每一步都封死一类"还能往里写东西"的可能：
1. **先停 Loop**：确保不会再有新的 Session append
2. **再解 Scope**：确保不会再有新的 effect 注册
3. **最后关 Session**：确保所有事件已处理完毕

---

## 关键源码定位

想直接跳进源码时用这张表，按操作直达文件和方法：

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
