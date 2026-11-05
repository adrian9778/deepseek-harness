# The tool execution pipeline

English | [中文](03-tool-execution.zh.md)

模型说"帮我跑个命令"，这条命令要经过怎样的安检才能落地？答案是固定的五阶段管线：预检（可拒绝）→ 守卫（不可逆政策）→ 执行（可超时重试）→ 后检（可拦截结果）→ 记日志。这一页按这条管线展开，附工具定义结构、DSL 写法和并行调度规则，末尾是源码定位表。

## Pipeline overview

每个工具调用经过固定的 5 阶段管线，可以把它想成机场安检：值机（放行与否）→ 安检门（硬性规定）→ 登机（真正执行）→ 落地检查（结果审查）→ 入档：

```
Model 返回 tool_call
        │
        ▼
┌─────────────────────────────────────────────────────────────┐
│  Phase 1: tools/pre-execute (Waterfall)                     │
│  目的：决定是否允许执行此工具                                 │
│  监听器可返回：                                               │
│    - undefined → 继续到下一监听器                            │
│    - { kind: 'allow' } → 放行                                │
│    - { kind: 'deny', reason } → 拒绝，后续监听器不执行       │
│    - { kind: 'ask', ... } → 请求人工审批                     │
└──────────────────────────┬──────────────────────────────────┘
                           │ allowed
                           ▼
┌─────────────────────────────────────────────────────────────┐
│  Phase 2: 守卫检查（Monotonic Guards）                       │
│  目的：不可逆的政策检查（一旦拒绝无法恢复）                    │
│  ctx.tools.guard(guard) 注册的单调守卫                        │
│  返回 reason → 拒绝，无后续                                   │
└──────────────────────────┬──────────────────────────────────┘
                           │ passed
                           ▼
┌─────────────────────────────────────────────────────────────┐
│  Phase 3: tools/execute (Waterfall)                         │
│  目的：实际执行工具，支持超时/重试包装                         │
│  监听器可：                                                   │
│    - 替换 signal（超时控制）                                  │
│    - 添加重试逻辑                                             │
│    - 记录调用日志                                             │
│  最终调用 definition.execute(args, execContext)              │
└──────────────────────────┬──────────────────────────────────┘
                           │ result
                           ▼
┌─────────────────────────────────────────────────────────────┐
│  Phase 4: tools/post-execute (Waterfall)                    │
│  目的：检查/替换执行结果                                       │
│  监听器可返回：                                               │
│    - { kind: 'accept', ... } → 保留或替换结果                 │
│    - { kind: 'block', feedback } → 转为 error result         │
└──────────────────────────┬──────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│  Phase 5: 结果材料化 + Session 记录                          │
│  1. definition.finalizeContent(exec, result)                 │
│     → 工具自定义的最后内容转换                                 │
│  2. 转换为 ContentBlock[]                                     │
│  3. session.append('tool/result', { ... })                   │
│  4. dispatch.emit('tools/result', frozenResult)              │
└─────────────────────────────────────────────────────────────┘
```

---

## The ToolDefinition structure

注册一个工具要声明哪些东西？必填两项：输出声明（返回值长什么样）和执行函数；可选项负责超时、并行、UI 呈现。注意 `when`（条件可见性）不在这里，它是注册选项：

**Source**: `packages/core/tools/src/contracts.ts` · ToolDefinition, `packages/core/tools/src/schema.ts` · defineTool

```ts
interface ToolDefinition extends ToolSchema {
  // === 输出声明（必填）===
  output: ToolOutputDefinition
    schema: JsonSchemaNode           // JSON Schema 约束返回值
    render(args, value): ContentBlock[]  // 将返回值转为模型可见内容
    presentationMeta?(args, value): JsonValue  // UI 展示元数据

  // === 执行函数（必填）===
  execute(args: unknown, exec: ToolRunContext): Promise<unknown>
    // args: 已通过 schema 验证的原始参数
    // exec.signal: 取消信号（必须 cooperative 检查）
    // 返回值必须是 output.schema 允许的 JSON 值

  // === 内容投影（可选）===
  projectContent?(exec, result): ContentBlock[] | undefined
    // 在 post-execute 之前安装执行准备的内容
    // 策略仍可替换

  finalizeContent?(exec, result): ContentBlock[] | undefined
    // 最终内容转换，在所有策略之后执行
    // 必须 total（不抛异常），恰好调用一次

  // === 执行元数据（可选）===
  timeoutMs?: number          // 协作超时（毫秒）
  isConcurrencySafe?(args): boolean  // 是否可以并行执行
  presentCall?(args): ToolCallView      // UI 待定状态渲染
  presentResult?(args, result): ToolResultView  // UI 完成状态渲染
}
```

`when`（条件可见性）不在定义上：它是注册选项 `ToolRegistrationOptions.when`，在 Step 准入时通过 `ctx.tools.resolve()` 求值。

---

## The defineTool DSL

写一个新工具时参数怎么声明？DSL 用一层受限的 JSON Schema 方言——够表达模型需要的类型，又不会写出执行器处理不了的复杂结构：

**Source**: `packages/core/tools/src/schema.ts` · defineTool

```ts
// 参数 Schema 规范
type ValueSchemaSpec =
  | { type: 'string'; enum?: string[]; const?: string }
  | { type: 'number'; enum?: number[]; const?: number }
  | { type: 'integer'; enum?: number[]; const?: number }
  | { type: 'boolean'; const?: boolean }
  | { type: 'null'; const: null }
  | { type: 'array'; items: ValueSchemaSpec }
  | { type: 'object'; properties?: { [k: string]: ParameterPropertySpec }; additionalProperties?: boolean }
  | { type: 'json' }                          // 作者专用：任意 JSON
  | { oneOf: ValueSchemaSpec[] }              // 精确多选一

// defineTool 的使用方式
const readTool = defineTool({
  name: 'read_file',
  description: 'Read a file from disk.',
  parameters: {
    path: { type: 'string', required: true, description: 'Absolute file path' },
    offset: { type: 'number' },
    limit: { type: 'number' },
  },
  output: {
    schema: { type: 'string' },
    render: (_args, value) => [{ type: 'text', text: value as string }],
  },
  async execute(args, exec) {
    // args 是类型安全的: { path: string; offset?: number; limit?: number }
    return readFile(args.path, { encoding: 'utf8', signal: exec.signal })
  },
})
```

---

## The ToolsService pipeline implementation

定义只是声明，真正把五阶段串起来的是执行管线。这一节先看服务入口，再看单次调用的十步流程：

**Source**: `packages/core/tools/src/index.ts` · ToolsService.execute / register / restrict, `packages/core/tools/src/pipeline.ts` · ToolExecutionPipeline

```ts
class ToolsService extends Service {
  // 存储：分层的 scoped ToolDefinition[]（NamedEntries / ScopedLayers）
  register(def: ToolDefinition, options?): Disposer
  restrict(filter: ToolRestriction): Disposer
  schemas(scope?, toolSet?): ToolSchema[]

  async execute(input: ToolBatchInput): Promise<ToolBatchResult> {
    // 委托给本作用域的 executor（Native 或 PTC）
    return this.selectExecutor(input.agent).execute(input)
  }
}
```

单次调用的管线（`ToolExecutionPipeline`）：

```
1. 查找 ToolDefinition
   const def = this.resolve(name, scope, toolSet)
   if (!def) throw new ToolNotFoundError(name)

2. 快照并冻结 arguments
   const frozenArgs = snapshotJsonValue(input.arguments)

3. 构建 ToolExecution（添加 token / rootCallId）

4. tools/pre-execute waterfall
   const preDecision = await ctx.waterfall(carrier, 'tools/pre-execute', exec, next)
   switch (preDecision.kind):
     - 'allow' → continue
     - 'deny' → return { isError: true, content, error: { info } }
     - 'cancel' → return canonical cancel result
     - 'ask' → 等待 approval service（超时/拒绝 → deny）

5. 单调守卫检查
   const reason = service.checkGuards(exec)
   if (reason) return deny(result)

6. tools/execute waterfall
   const dispatchExec: ToolDispatchExecution = { ...exec, signal: fusedSignal }
   const innerResult = await ctx.waterfall(carrier, 'tools/execute', dispatchExec, () =>
     this.dispatchToolBody(...))
     → 默认 next() 调用：
         const value = await def.execute(exec.arguments, runContext)
         → 验证 value 符合 output.schema
         → render: output.render(exec.arguments, value)
         → 返回 { isError: false, value, content, meta }

7. tools/post-execute waterfall
   const postDecision = await ctx.waterfall(carrier, 'tools/post-execute', exec, result, next)
   → 'accept': 使用 listener 提供的 content/value
   → 'block': 转为 error result

8. finalizeContent（工具自定义）
   const finalContent = def.finalizeContent
     ? def.finalizeContent(exec, finalResult)
     : finalResult.content

9. 触发 tools/result（observe-only）
   ctx.emit(carrier, 'tools/result', freeze(exec), freeze(finalResult))

10. 返回 finalResult
```

---

## Parallel and exclusive tool scheduling

一批工具调用怎么排先后？规则：能并行的进有界并发池，必须独占的串行当路障，但日志里的顺序永远和模型要求的顺序一致：

**Source**: `packages/core/tools/src/batch.ts` · executeToolBatch

```ts
async function executeToolBatch(
  input: ToolBatchInput, pipeline, maxParallel: () => number,
): Promise<ToolBatchResult> {
  // 分类：isConcurrencySafe(args) === true → parallel
  // 失败封闭：未声明或求值失败 → exclusive
  // 按模型顺序扫描未启动的调用，不预先分组：
  while (next < planned.length) {
    const mode = pipeline.executionMode(first.exec).kind
    // exclusive → 单调用屏障（串行）；parallel → 取剩余调用组成并发池
    const group = mode === 'parallel' ? planned.slice(next) : [first]
    // 并发上限：input.maxParallelToolCalls 覆盖，否则读 executor 的 maxParallel()
    await runGroup(pipeline, settlement, limit, group, mode, signal)
  }
  // 池内每次启动前重新分类；变 exclusive 即断开本池、留作下一个屏障
  // 结果与上下文严格按模型顺序提交（commitReady 只推进连续槽位）
}
```

- 每个 call 启动前重新分类（调用间状态可能变化）
- abort 时已启动的调用等待完成，未启动的补合成 skip 结果
- 批次失败以 `ToolBatchExecutionError(error, partial)` 抛出，partial 携带已完成结果

---

## The tool run context (ToolRunContext)

工具执行时拿到的不只是参数，还有一份"现场环境"：取消信号、调用 ID，以及两个特殊动作（推迟追加上下文、标记结束 Turn）：

**Source**: `packages/core/tools/src/contracts.ts` · ToolRunContext

```ts
interface ToolRunContext extends ToolExecution {
  // 以下两项继承自 ToolExecution / ToolExecutionInput
  // readonly signal: AbortSignal   取消信号。execute() 必须 cooperative 检查此信号。
  // readonly callId: ToolCallId    工具调用的唯一 ID，用于关联 request/response

  /** 将上下文推迟到 tool/result 之后追加 */
  deferContext(context: UserMessage): void
  /** 将成功结果标记为结束当前 Turn */
  concludeTurn(): void
}
```

---

## Key source map

想直接跳进源码时用这张表，按功能直达文件与符号：

| 功能 | 源码文件 | 关键部分 |
|---|---|---|
| ToolDefinition 类型 | `packages/core/tools/src/contracts.ts` | `ToolDefinition` |
| defineTool DSL | `packages/core/tools/src/schema.ts` | `defineTool` |
| ToolsService 实现 | `packages/core/tools/src/index.ts` | `ToolsService` |
| 执行管线事件 | `packages/core/tools/src/events.ts` | `tools/pre-execute` … `tools/result` |
| 批量调度 | `packages/core/tools/src/batch.ts` | `executeToolBatch` |
| JSON Schema 验证 | `packages/core/tools/src/json-schema.ts` | `JsonSchemaNode`、`validateJsonSchemaValue` |
| UI 呈现 | `packages/core/tools/src/presentation.ts` | `ToolCallView`、`ToolResultView` |
