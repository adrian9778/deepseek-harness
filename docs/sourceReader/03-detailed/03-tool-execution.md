# 工具执行完整管线

## 工具管线概览

每个工具调用经过固定的 5 阶段管线：

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
│    - undefined → 保留原始结果                                 │
│    - 新的 ToolExecutionResult → 替换结果                     │
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

## defineTool 定义结构

**源码位置**：`packages/core/tools/src/types.ts` +ToolDefinition、`packages/core/tools/src/schema.ts` +ValueSchemaSpec

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

---

## defineTool DSL

**源码位置**：`packages/core/tools/src/schema.ts` +defineTool

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

## ToolRegistry 执行管线实现

**源码位置**：`packages/core/tools/src/index.ts` +execute、+register、+restrict

```ts
class ToolRegistry {
  // 存储：分层的 scoped ToolDefinition[]
  private layers: ScopedLayer[] = []

  register(def: ToolDefinition, scope?: Scope): void {
    // 添加到当前层；scope 非空时绑定到该作用域
  }

  restrict(filter: ToolFilter): Disposer {
    // 返回一个 filter 作用下的可见子集
    // 多层 mask 取交集
    // disposer 调用时移除限制
  }

  schemas(scope): ToolSchema[] {
    // 仅返回模型可见字段（name, description, parameters）
    // output/execute/projectContent 等绝不泄露
  }

  async execute(name: string, args: unknown, execContext: ToolExecutionContext)
    : Promise<ToolExecutionResult> {
    // 1. 查找 ToolDefinition
    const def = this.resolve(name, execContext.scope)
    if (!def) throw NEW_TOOL_NOT_FOUND

    // 2. tools/pre-execute waterfall
    const preDecision = await this.dispatch.waterfall(
      'tools/pre-execute',
      { name, args, scope: execContext.scope },
    )
    if (preDecision?.kind === 'deny') throw TOOL_DENIED(preDecision.reason)
    if (preDecision?.kind === 'ask') { /* 等待审批 */ }

    // 3. 单调守卫检查
    for (const guard of this.guards) {
      const reason = guard(def, args)
      if (reason) throw TOOL_GUARDED(reason)
    }

    // 4. tools/execute waterfall
    const executed = await this.dispatch.waterfall(
      'tools/execute',
      { name, args, def, signal: execContext.signal },
    )
    // executed 包含: { result, content, error? }

    // 5. tools/post-execute waterfall
    const postResult = await this.dispatch.waterfall(
      'tools/post-execute',
      { ...executed, def },
    )

    // 6. finalizeContent（工具自定义最后转换）
    const finalContent = def.finalizeContent
      ? def.finalizeContent(executed.exec, postResult)
      : postResult.content

    // 7. 触发 tools/result 事件（observe-only）
    this.dispatch.emit('tools/result', freeze(postResult))

    return postResult
  }
}
```

---

## 并行与独占工具调度

**源码位置**：`packages/core/agent-loop/src/tool-calls.ts` +executeToolCalls

```ts
async function executeToolCalls(
  calls: ToolCall[],
  options: { maxParallel: number; session: Session; ... },
): Promise<ToolResult[]> {
  // 分离独占和并行工具
  const exclusive = calls.filter(c => !c.def.isConcurrencySafe?.(c.args))
  const concurrent = calls.filter(c => c.def.isConcurrencySafe?.(c.args) ?? false)

  const results: Array<ToolResult | undefined> = new Array(calls.length)

  // 独占工具：串行执行（作为屏障）
  for (const call of exclusive) {
    const idx = calls.indexOf(call)
    results[idx] = await executeOne(call, options)
  }

  // 并行工具：有界并发池
  if (concurrent.length > 0) {
    const pool = createBoundedPool({ max: options.maxParallel })
    await Promise.all(
      concurrent.map(async (call) => {
        const idx = calls.indexOf(call)
        results[idx] = await pool.run(() => executeOne(call, options))
      }),
    )
  }

  return results.filter(r => r !== undefined)
}
```

---

## 工具调用上下文（ToolRunContext）

**源码位置**：`packages/core/tools/src/types.ts` +ToolRunContext

```ts
interface ToolRunContext {
  /** 取消信号。execute() 必须 cooperative 检查此信号。 */
  readonly signal: AbortSignal
  /** 工具调用的唯一 ID，用于关联 request/response */
  readonly callId: string
  /** 当前 Agent 的 scope */
  readonly scope: Scope
  /**  defer: 将异步工作推迟到后续阶段 */
  defer<T>(fn: () => Promise<T>): Promise<T>
}
```

---

## 关键源码定位

| 功能 | 源码文件 | 关键行 |
|---|---|---|
| ToolDefinition 类型 | `packages/core/tools/src/types.ts` | +1-100 |
| defineTool DSL | `packages/core/tools/src/schema.ts` | +1-150 |
| ToolRegistry 实现 | `packages/core/tools/src/index.ts` | +808（类）、+1377（execute） |
| 执行管线事件 | `packages/core/tools/src/index.ts` | +pipeline（waterfall 注册） |
| 并行调度 | `packages/core/agent-loop/src/tool-calls.ts` | +60-260 |
| JSON Schema 验证 | `packages/core/tools/src/json-schema.ts` | +1-全 |
| UI 呈现 | `packages/core/tools/src/presentation.ts` | +1-全 |
