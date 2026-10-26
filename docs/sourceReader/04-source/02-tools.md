# tools 包源码逐函数解析

## 模块概览

**源码目录**：`packages/core/tools/src/`

| 文件 | 职责 | 行数 |
|---|---|---|
| `index.ts` | ToolRuntime Service、执行管线、注册表 | ~2000+ |
| `schema.ts` | defineTool DSL、ValueSchemaSpec、InferValue | ~300 |
| `types.ts` | ToolDefinition、ToolExecutionResult、决策类型 | ~400 |
| `json-schema.ts` | JSON Schema 子集定义 + 验证 | ~200 |
| `presentation.ts` | ToolCallView / ToolResultView UI 呈现类型 | ~150 |
| `ptc.ts` | PTC 模式：run_code 工具 + SDK 生成 | ~300 |

---

## index.ts — ToolRuntime Service

### Service 声明（+1-100）

```ts
declare module '@deepseek-ai/cordis' {
  interface Context { tools: ToolRuntime }
  interface Events {
    'tools/pre-execute'(exec: ToolExecution, next: () => Promise<PreToolDecision>): Promise<PreToolDecision>
    'tools/execute'(exec: ToolDispatchExecution, next: () => Promise<ToolExecutionResult>): Promise<ToolExecutionResult>
    'tools/post-execute'(exec: ToolExecution, result: Readonly<ToolExecutionResult>, next: () => Promise<PostToolDecision>): Promise<PostToolDecision>
    'tools/result'(exec: Readonly<ToolExecution>, result: Readonly<ToolExecutionResult>): undefined
    'tools/change'(): void
  }
}
```

### ToolLayer 类（+250-350）

```ts
class ToolLayer implements ScopeLayer {
  readonly tools: NamedEntries<ToolDefinition>
  readonly restrictions = new AnonymousEntries<CompiledToolRestriction>()
  readonly guards = new AnonymousEntries<ToolGuard>()
  mode: ToolPresentationMode | undefined

  isEmpty(): boolean
  admits(name: string): boolean        // 检查所有限制是否允许此工具名
  guardReason(exec: ToolExecution): string | undefined  // 第一个单调守卫的拒绝原因
}
```

### ToolView 接口（+350-400）

```ts
interface ToolView {
  visible: ReadonlyMap<string, ToolDefinition>   // 过滤后的可见工具
  knownNames: ReadonlySet<string>                // 注册过的全部名称（用于 order 验证）
  restrictableNames: ReadonlySet<string>         // 全局工具名（scoped 注册不受限制影响）
}
```

---

## ToolRuntime 类方法

### `register(def, scope?)`（tools/index.ts +1071）

```ts
register(def: ToolDefinition, scope?: Scope): Disposer
```

**执行步骤**：
1. 验证 `def.name` 未被同 scope 重复注册
2. 验证 `def.output.schema` 是合法 JSON Schema
3. 将 def 加入当前层的 `tools` 表
4. 触发 `tools/change` 事件（无 veto，失败仅 warn）
5. 返回 disposer，卸载时移除 def 并再次触发 `tools/change`

### `restrict(filter, scope?)`（+580-640）

```ts
restrict(filter: ToolRestriction, scope?: Scope): Disposer
```

**语义**：
- `allow` 白名单：只保留指定名称
- `deny` 黑名单：排除指定名称
- 多层 restriction 取交集
- scoped 注册的工具不受 global restriction 影响

### `schemas(scope?)`（+640-680）

```ts
schemas(scope?: Scope): ToolSchema[]
```

**返回**：仅模型可见字段（`name`、`description`、`parameters`）。`output`、`execute`、`projectContent` 等绝不泄露。

### `executionMode(exec)`（+680-700）

```ts
executionMode(exec: ToolExecutionInput): ToolExecutionMode
```

调用 `def.isConcurrencySafe?.(exec.arguments)` 判断是否可并行。

### `execute(name, input)`（tools/index.ts +1377）— 核心执行管线

```ts
async execute(exec: ToolExecutionInput): Promise<ToolExecutionResult>
```

**完整流程**：

```
1. 查找 ToolDefinition
   const def = this.resolve(name, input.agent?.ctx)
   if (!def) throw new ToolNotFoundError(name)

2. 快照并冻结 arguments
   const frozenArgs = snapshotToolValue(name, input.arguments)

3. 构建 ToolExecution（添加 token）
   const exec: ToolExecution = { ...input, arguments: frozenArgs, token, rootCallId }

4. tools/pre-execute waterfall
   const preDecision = await this.dispatchWaterfall('tools/pre-execute', exec)
   switch (preDecision.kind):
     - 'allow' → continue
     - 'deny' → return { isError: true, content, error: { info } }
     - 'cancel' → return canonical cancel result
     - 'ask' → 等待 approval service（超时/拒绝 → deny）

5. 单调守卫检查
   for (const layer of layers) {
     const reason = layer.guardReason(exec)
     if (reason) return deny(result)
   }

6. tools/execute waterfall
   const preparedSignal = fuseCallerSignal(input.signal, exec.signal)
   const dispatchExec: ToolDispatchExecution = { ...exec, signal: preparedSignal }
   const innerResult = await this.dispatchWaterfall('tools/execute', dispatchExec)
     → 默认 next() 调用：
         const value = await def.execute(exec.arguments, runContext)
         → 验证 value 符合 output.schema
         → render: output.render(exec.arguments, value)
         → 返回 { isError: false, value, content, meta }

7. tools/post-execute waterfall
   const postDecision = await this.dispatchWaterfall('tools/post-execute', exec, innerResult)
   → 'accept': 使用 listener 提供的 content/value
   → 'block': 转为 error result

8. finalizeContent（工具自定义）
   const finalContent = def.finalizeContent
     ? def.finalizeContent(exec, finalResult)
     : finalResult.content

9. 触发 tools/result（observe-only）
   this.dispatch.emit('tools/result', freeze(exec), freeze(finalResult))

10. 返回 finalResult
```

### `get(name, scope?)`（+900-920）

```ts
get(name: string, scope?: Scope): ToolDefinition | undefined
```

按 scope 查找定义（用于内部查找，非模型路径）。

---

## schema.ts — defineTool DSL

### `defineTool(options)`（+1-100）

```ts
function defineTool<T extends DefineToolOptions>(options: T): ToolDefinition & InferValue<T['output']['schema']>
```

**构建步骤**：
1. 验证 `options.name` 非空
2. 编译 `options.parameters` → JSON Schema（`parameterSchemaSpecToJsonSchema`）
3. 验证 `options.output.schema` 是合法 JSON Schema
4. 返回合并的 ToolDefinition：
   ```ts
   {
     name: options.name,
     description: options.description,
     parameters: compiledParams,
     output: options.output,
     execute: options.execute,
     projectContent: options.projectContent,
     finalizeContent: options.finalizeContent,
     timeoutMs: options.timeoutMs,
     isConcurrencySafe: options.isConcurrencySafe,
     presentCall: options.presentCall,
     presentResult: options.presentResult,
   }
   ```

### `valueSchemaSpecToJsonSchema(spec)`（+100-200）

将 `ValueSchemaSpec` 转换为 JSON Schema Node：
- `{ type: 'string' }` → `{ type: 'string' }`
- `{ type: 'object', properties }` → `{ type: 'object', properties, additionalProperties }`
- `{ oneOf: [...] }` → `{ oneOf: [compiled] }`
- `{ type: 'json' }` → `{}` (annotation-only, unconstrained)

### `validateArgs(def, rawArgs)`（+200-250）

```ts
function validateArgs(def: ToolDefinition, rawArgs: unknown): unknown
```

使用 `jsonSchemaToZod(def.parameters)` 生成 Zod schema，然后 `.parse(rawArgs)`。失败抛出 `ToolArgsError`。

---

## 关键错误类型

| 类 | code | 触发条件 |
|---|---|---|
| `ToolNotFoundError` | `UNKNOWN_TOOL` | 工具名未注册 |
| `ToolOutputError` | `INVALID_TOOL_OUTPUT` | 返回值不符合 output.schema |
| `ToolArgsError` | — | 参数不符合 parameter schema |

---

## PTC 模式相关（ptc.ts）

### `createRunCodeTool(ptcRuntime)`（+1-100）

生成 `run_code` 工具的定义，包含：
- 工具 Schema（program: string, language: string）
- execute 函数：调用 `ctx.ptcRuntime.run(program, language, signal)`
- output.render：将 PTC 结果转为 TextBlock

### `RUN_CODE_ONLY_INSTRUCTION`（+ptc.ts 常量）

PTC 模式下注入系统提示词的指令文本，告知模型只能通过 `run_code` 间接调用其他工具。
