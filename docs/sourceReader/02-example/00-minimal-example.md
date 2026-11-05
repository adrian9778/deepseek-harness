# Minimal runnable example: one headless task

English | [中文](00-minimal-example.zh.md)

This page walks one real task from start to finish: from typing the command, through plugin installation, Agent start, model thinking, and tool execution, to the result and process exit. Every hop carries its source location; after reading it you can trace the whole "command line to model response" chain. A Mermaid sequence diagram and a source quick-reference table at the end make it easy to come back and locate things.

## Scenario

The user runs `dsh headless "list every .ts file in this directory"`. The framework launches, creates an Agent, runs one model round, executes tools, prints the result, and exits. The whole trip takes fewer than ten hops, one job per hop:

---

## Full path trace

Ten hops at a glance: parse the command → load config → mount plugins → create the Agent → wake it → assemble the prompt → call the model → run tools → call the model again → wrap up and exit.

### Hop 1: CLI entry → argument parsing

**Source**: `apps/cli/src/bin.ts` · runCli, `apps/cli/src/args.ts` · parseDshArgs

```
进程启动
  → node apps/cli/lib/bin.js  (或 tsx 直接运行 src/bin.ts)
  → runCli({ packageManager })
    → parseDshArgs(argv.slice(2), version, manageDesktopProfile)
       argv = ['headless', '帮我列出当前目录下所有 .ts 文件']
       → 识别 mode: 'profile'
       → profile: 'headless'
       → args: ['帮我列出当前目录下所有 .ts 文件']
```

**Parsing rules** (`args.ts`):
- `dsh <name>` rewrites to `--profile <name>`
- the first token the launcher does not recognize starts the inner args
- `--patch` repeats and collects the overlay list

### Hop 2: Profile Boot → load configuration

This hop translates the "launch menu" into a concrete plugin list: find the directory, read the manifest, then stack the user's own overlays on top.

**Source**: `apps/cli/src/profile-boot.ts`, `packages/boot/app-boot/src/`

```
runProfile({
  environment: loadLayeredEnv('dsh'),  // 加载 .env
  profile: 'headless',
  args: ['帮我列出...'],
})
  → resolveProfileDir('headless', home)
     → $DSH_HOME/profiles/headless/
  → readProfilePatches(...)
     → 读取 package.json 中的 dsh.profile.bundles
     → 读取 cordis.patch.yml
  → loadOverlayPatches(invocation.patches)  // --patch 覆盖层
  → boot(...)
```

**Bundle stacking order**:

```
空配置 []
  + dsh-base Bundle          → [llm, session, tools, agent, agent-loop, ...]
  + dsh-headless Bundle      → [+ headless runner plugin]
  + profile cordis.patch.yml → [+ 用户自定义覆盖]
  + --patch overlays         → [+ 命令行覆盖层]
```

### Hop 3: Cordis Loader → mount the plugin tree

With the list ready, Cordis installs every plugin into the Context in dependency order; each capability ends up owning a key on ctx:

**Source**: `vendor/` Cordis Loader + `packages/boot/app-boot/src/index.ts` (`boot`)

```
boot(...)
  │
  ├─ new Context() + ctx.plugin(Loader)   ← @deepseek-ai/cordis-plugin-loader
  ├─ mountRootInclude(...)                ← cordis:include 条目树 + patch 层
  ├─ prepare(ctx)                         ← profile 解析、插件包解析、cmdline 注入
  └─ loader.await()                       ← 按依赖顺序挂载全部条目
       ├─ @deepseek-ai/dsh-session        → ctx.sessions 就绪
       ├─ @deepseek-ai/dsh-llm            → ctx.llm 就绪
       ├─ @deepseek-ai/dsh-llm-deepseek-api-key → DeepSeek Provider 注册
       ├─ @deepseek-ai/dsh-agent          → ctx.agents 就绪
       ├─ @deepseek-ai/dsh-agent-loop     → AgentLoop 工厂注册
       ├─ @deepseek-ai/dsh-tools          → ctx.tools 就绪
       ├─ @deepseek-ai/dsh-tool-bash      → bash 工具注册
       └─ ... (其他工具)
```

### Hop 4: Headless runner → create the Agent

The headless menu ships its own "one-shot runner" plugin: wait for the full tree, read the task text from the command line, create the Agent:

**Source**: `packages/bundle/headless/src/startup.ts`, `packages/bundle/headless/src/index.ts`

```
headless-startup 行解析 cmdline（Commander: --json/--session-id/[task...]）
headless-runner 行 apply(ctx)
  │
  ├─ loader.await() 等待全树就绪
  ├─ ctx.cmdlineArgs 读取 inner args
  │   → ['帮我列出当前目录下所有 .ts 文件']
  │
  ├─ agents.create({ sessionId, meta: { cwd }, agentOptions, setup })
  │     agentOptions: { provider/model 来自 agentDefaultModel }
  │
  └─ 返回 AgentHandle
```

### Hop 5: Agent Loop starts driving

The Agent exists but is standing by; the runner pushes the user's task into the Inbox and wakes it:

**Source**: `packages/core/agent-loop/src/agent.ts`, `packages/core/agent-loop/src/index.ts`

```
ReactLoopAgent 构造器
  │
  ├─ 创建 ReactLoopInbox（折叠 agent/inbox/spliced 投影）
  ├─ 创建 RuntimeContextProjection + SystemPromptProjection
  └─ 等待 inbox 中有消息（由 runner 注入）
```

The AgentLoop service registers the `inbox` projection with `ctx.sessionProjections` at init.

The runner injects the user message:

```
handle.agent.followup(createUserMessage(...))
  → Inbox.append(message, target='next-turn')
  → 唤醒 Agent Loop（如果 idle）
```

### Hop 6: Turn starts → prompt assembly

Once the Turn starts, it gathers materials first: claim the user message, assemble the system prompt, derive history from the log:

**Source**: `packages/core/agent-loop/src/agent.ts`, `packages/core/system-prompt/src/`

```
[Turn N Start]
  session.append('turn/start', { turn: N })

  // 从 Inbox 取出用户消息
  const messages = inbox.claim(target, turn)
    → 触发 agent/inbox/claimed 事件

  // preStep：组装 + agent/pre-step waterfall
  const tools = ctx.tools.resolve({ agent, modelSelection, signal })
  const assembly = await ctx.systemPrompt.assemble({ modelSelection, toolSet })
    → 拼接所有 PromptSection
    → 注入当前可见工具 Schema
    → 返回 PromptAssembly

  // 历史推导（buildRequest 内）
  const history = session.deriveMessages()
    → 读取所有 surface 事件
    → 附加最新 system/message 节点
    → 返回 Message[]
```

### Hop 7: LLM streaming call

Materials ready, it fires a streaming request at the model. The reply does not arrive all at once; it streams back chunk by chunk:

**Source**: `packages/core/agent-loop/src/agent.ts` · ReactLoopAgent.prepareRequest, `packages/llm/llm/src/index.ts`, `packages/llm/llm-deepseek/src/`

```
// 准备 Call 配置（prepareRequest 内）
const preparedCall = await ctx.llm.prepareCall(proposedConfig, signal)
  → 验证 adapter 已注册
  → 应用 adapter defaults（reasoningEffort、maxTokens）
  → 返回 PreparedLlmCall

// 流式调用（preparedCall 绑定同一 adapter generation）
for await (const chunk of stream) {
  // chunk 类型（StreamChunk.type）：
  // { type: 'block-start' }     — 新内容块开始
  // { type: 'text-delta' }      — 文本增量
  // { type: 'tool-call-delta' } — 工具调用增量
  // { type: 'usage' }           — Token 用量
  // { type: 'finish', reason }  — 终止
}
```

**DeepSeek provider implementation** (`packages/llm/llm-deepseek/src/`):
- POST to the DeepSeek API
- set `Authorization: Bearer <API_KEY>`
- parse the SSE stream response
- convert to `StreamChunk` frames

### Hop 8: Tool execution

The model says "I need to see the directory", so its requested tool runs. One batch passes through four stations: pre-check → execute → post-process → log:

**Source**: `packages/core/agent-loop/src/agent.ts` · ReactLoopAgent.step, `packages/core/tools/src/batch.ts` · executeToolBatch, `packages/core/tools/src/pipeline.ts` · ToolExecutionPipeline

Assume the model returned one tool call:

```
// 一个 Step 的全部 tool calls 作为一个批次执行
await ctx.tools.execute({
  calls: toolCalls, signal, agent, log: { turn, step },
  maxParallelToolCalls, toolSet,
})

// 对每个 toolCall：
// 1. tools/pre-execute waterfall
//    → 权限策略检查（是否允许执行此工具）
// 2. tools/execute waterfall
//    → 找到 ToolDefinition
//    → 调用 definition.execute(args, execContext)
//    → bash 工具：spawn bash -c "ls *.ts"
//    → 返回 ShellResult { stdout, stderr, exitCode }
// 3. tools/post-execute waterfall
//    → 结果替换/审计
// 4. session.append('tool/result', { ... })
//    → 追加到 Session 日志
```

### Hop 9: Second Step (the model continues on tool results)

With tool results in the log, the model takes one more look at the whole picture; this time it answers directly and asks for no more tools:

```
工具结果已追加到 Session 日志
  → session.deriveMessages() 现在包含 tool/result 消息
  → 再次进入 Step 循环

[Step 2]
  → 新的 prompt（含工具结果）
  → 新的 LLM 请求
  → 模型返回最终文本响应（无更多 tool calls）
  → session.append('assistant/message', { ... })
```

### Hop 10: Turn end → cleanup

The answer is out; the Turn wraps up, the runner prints the result, tears down the Agent, and unloads every plugin in reverse order:

**Source**: `packages/core/agent-loop/src/agent.ts` · ReactLoopAgent.turn +68~+100

```
[Turn N End]
  dispatch.serial('agent/turn-stopping', { turn, signal })
  session.append('turn/end', { turn: N, reason: { kind: 'completed' } })
  → phase 回到 idle，status emit 'idle'

  Runner 收集最终输出，打印到 stdout
  → handle.dispose()
    → Agent Loop 停止
    → Scope 解开
    → Session 写入路径关闭
    → 所有插件 disposer 按逆序调用
```

---

## Full sequence diagram (Mermaid)

If the hop-by-hop text is too much, this diagram alone is enough: participants on the left, the timeline runs downward.

```mermaid
sequenceDiagram
    participant User
    participant CLI as apps/cli/bin.ts
    participant Boot as profile-boot.ts
    participant Loader as Cordis Loader
    participant Runner as headless runner
    participant Agent as ReactLoopAgent
    participant Session as ctx.sessions
    participant Prompt as ctx.systemPrompt
    participant LLM as ctx.llm
    participant Tools as ctx.tools

    User->>CLI: dsh headless "列出 .ts 文件"
    CLI->>CLI: parseDshArgs() → ProfileInvocation
    CLI->>Boot: runProfile({ profile: 'headless', args })
    Boot->>Boot: loadLayeredEnv() + readProfilePatches()
    Boot->>Loader: boot(...) + mountRootInclude(...)
    Loader->>Loader: 按依赖顺序 apply 所有 Plugin
    Loader-->>Boot: Context 就绪

    Boot->>Runner: 注入 cmdlineArgs
    Runner->>Agent: ctx.agents.create(options)
    Agent->>Session: create(sessionId)
    Session-->>Agent: Session 实例

    Runner->>Agent: handle.agent.followup(userMessage)
    Agent->>Agent: Inbox.append(next-turn)
    Agent->>Agent: 唤醒 Turn 驱动

    Note over Agent,Session: === TURN START ===
    Agent->>Session: append('turn/start', {turn:N})
    Agent->>Agent: claim next-turn input

    Note over Agent,Prompt: === STEP 1 ===
    Agent->>Prompt: assemble({ modelSelection, toolSet })
    Prompt-->>Agent: PromptAssembly
    Agent->>Session: deriveMessages()
    Session-->>Agent: history: Message[]

    Agent->>LLM: stream({messages, tools, signal})
    LLM->>LLM: DeepSeek Provider HTTP POST
    LLM-->>Agent: chunk* (text-delta, tool-call-delta)
    LLM-->>Agent: finish (reason: 'tool-calls')

    Agent->>Tools: execute({ calls, maxParallelToolCalls })
    Tools->>Tools: pre-execute → execute → post-execute
    Tools->>Session: append('tool/result', ...)
    Session-->>Tools: seq: 15

    Note over Agent,Session: === STEP 2 ===
    Agent->>Session: deriveMessages() (含工具结果)
    Agent->>LLM: stream({messages: ..., tools: ...})
    LLM-->>Agent: chunk* (text only)
    LLM-->>Agent: finish (reason: 'stop')

    Agent->>Session: append('assistant/message', {...})
    Note over Agent,Session: === TURN END ===
    Agent->>Session: append('turn/end', {reason: {kind: 'completed'}})
    Agent-->>Runner: status: idle

    Runner->>User: 打印输出
    User->>Runner: 任务完成
    Runner->>Agent: dispose()
    Agent->>Session: close write path
    Session->>Session: flush to disk
```

---

## Key source quick reference

One last table: when you return to change code or investigate an issue, jump straight from stage to source.

| 阶段 | 关键源码 |
|---|---|
| 参数解析 | `apps/cli/src/args.ts` · `parseDshArgs` |
| 入口分派 | `apps/cli/src/bin.ts` · `runCli` |
| Profile 启动 | `apps/cli/src/profile-boot.ts` runProfile |
| Agent 创建 | `packages/core/agent-loop/src/index.ts` create/createAgent |
| Turn/Step 驱动 | `packages/core/agent-loop/src/agent.ts` turn/step |
| 工具执行 | `packages/core/tools/src/batch.ts` executeToolBatch |
| Session 追加 | `packages/core/session/src/index.ts` append |
| 提示词组装 | `packages/core/system-prompt/src/index.ts` assemble |
| LLM 调用 | `packages/llm/llm/src/index.ts` prepareCall/stream |
| Turn 结束 | `packages/core/agent-loop/src/agent.ts` turn finally 块 |
