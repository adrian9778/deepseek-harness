# tools package reference

English | [中文](02-tools.zh.md)

tools 包管两件事：工具的"花名册"（注册、限制、查找）和"安检流水线"（单次调用的五阶段管线、批量调度）。这一篇按文件拆开，重点讲 ToolsService 的方法和 defineTool DSL 的编译过程。

## Module overview

九个文件：主服务、契约、DSL、管线、批调度、执行器、事件、Schema 验证、UI 呈现：

**Source directory**: `packages/core/tools/src/`

| 文件 | 职责 | 行数 |
|---|---|---|
| `index.ts` | ToolsService、注册表、执行入口 | ~412 |
| `contracts.ts` | ToolDefinition、ToolExecution、决策类型 | ~700 |
| `schema.ts` | defineTool DSL、ValueSchemaSpec、projectToolSchema | ~700 |
| `pipeline.ts` | ToolExecutionPipeline：单次调用 5 阶段管线 | ~700 |
| `batch.ts` | executeToolBatch：分组 + 有界并发池 | ~300 |
| `executor.ts` | ToolExecutor 契约 + ToolBatchInput/Result | ~100 |
| `events.ts` | tools/* 事件声明 | ~60 |
| `json-schema.ts` | JSON Schema 子集定义 + 验证 | ~200 |
| `presentation.ts` | ToolCallView / ToolResultView UI 呈现类型 | ~150 |

---

## index.ts — the ToolsService

### Service declaration (+1-100)

```ts
declare module '@deepseek-ai/cordis' {
  interface Context { tools: ToolsService }
}
```

`ToolsService extends Service`：构造时 `super(ctx, 'tools')`，`static inject = ['systemPrompt']`（注册工具 schema provider）。别名导出 `ToolRuntime`。执行器由 `setExecutor()` 挂载（`tool-executor-native` / `tool-executor-ptc`）。

### The ToolLayer class (+63-100)

每层作用域一个 ToolLayer，装着该层的工具、限制和守卫：

```ts
class ToolLayer implements ScopeLayer {
  readonly tools: NamedEntries<ToolDefinition>
  readonly restrictions = new AnonymousEntries<CompiledToolRestriction>()
  readonly guards = new AnonymousEntries<ToolGuard>()

  isEmpty(): boolean
  admits(name: string): boolean        // 检查所有限制是否允许此工具名
  guardReason(exec: ToolExecution): string | undefined  // 第一个单调守卫的拒绝原因
}
```

### The ToolView interface (+54)

给模型看的"工具橱窗"：可见的、注册过的、可被限制的三个视图分开维护：

```ts
interface ToolView {
  visible: ReadonlyMap<string, ToolDefinition>   // 过滤后的可见工具
  knownNames: ReadonlySet<string>                // 注册过的全部名称（用于 order 验证）
  restrictableNames: ReadonlySet<string>         // 全局工具名（scoped 注册不受限制影响）
}
```

---

## ToolsService methods

### `register(def, options?)` (index.ts +128)

注册工具的六步：验证输出、验证参数 Schema、预留名、入层、广播、返回卸载器：

```ts
register(definition: ToolDefinition, options?: ToolRegistrationOptions): () => void
```

**执行步骤**：
1. 验证 `def.output` 的 schema/render 与 `timeoutMs`
2. `assertSupportedJsonSchema(def.parameters)`
3. 预留 `run_code` 名称（PTC 专用）
4. 将 def 加入当前层的 `tools` 表
5. 触发 `tools/change` 事件（无 veto，失败仅 warn）
6. 返回 disposer，卸载时移除 def 并再次触发 `tools/change`

`ToolRegistrationOptions.when`：条件可见性，Step 准入时通过 `resolve()` 求值。

### `restrict(filter)` (+162)

限制可见工具，语义是"多层取交集"：

```ts
restrict(filter: ToolRestriction): () => void
```

**语义**：
- `allow` 白名单：只保留指定名称
- `deny` 黑名单：排除指定名称
- 多层 restriction 取交集
- 需要 scoped ctx；拒绝空 filter 与 `run_code`

### `schemas(scope?, toolSet?)` (+316)

给模型的工具 Schema 走白名单投影，内部字段（execute、output 等）绝不外泄：

```ts
schemas(scope?: Scope, toolSet?: ResolvedToolSet): ToolSchema[]
```

**返回**：仅模型可见字段（`name`、`description`、`parameters`）。`output`、`execute`、`projectContent` 等绝不泄露（`projectToolSchema` 白名单）。

### `execute(input)` (+375) — the execution entry

```ts
async execute(input: ToolBatchInput): Promise<ToolBatchResult>
```

委托给本作用域的 executor；一次 Step 的全部 tool calls 作为一个批次。完整管线见 [03-tool-execution.md](../03-detailed/03-tool-execution.md)。

```
1. 查找 ToolDefinition
   const def = this.resolve(name, scope, toolSet)
   if (!def) throw new ToolNotFoundError(name)

2. 快照并冻结 arguments
   const frozenArgs = snapshotJsonValue(input.arguments)

3. 构建 ToolExecution（添加 token / rootCallId）

4. tools/pre-execute waterfall
   → 'allow' 继续；'deny' 返回 error result；'cancel' 返回取消结果
   → 'ask' 等待 approval service（超时/拒绝 → deny）

5. 单调守卫检查
   const reason = service.checkGuards(exec)
   if (reason) return deny(result)

6. tools/execute waterfall
   → 默认 next() 调用 def.execute(exec.arguments, runContext)
   → 验证 value 符合 output.schema，render 为 ContentBlock[]

7. tools/post-execute waterfall
   → 'accept' 使用 listener 结果；'block' 转为 error result

8. finalizeContent（工具自定义最后转换）

9. 触发 tools/result（observe-only）

10. 返回 finalResult
```

### `get(name, scope?, toolSet?)` (+239)

按 scope 查找定义（用于内部查找，非模型路径）：

```ts
get(name: string, scope?: Scope, toolSet?: ResolvedToolSet): ToolDefinition | undefined
```

按 scope 查找定义（用于内部查找，非模型路径）。

---

## schema.ts — the defineTool DSL

### `defineTool(options)` (+555)

四步把声明式 DSL 编译成完整 ToolDefinition，类型推断一路保留：

```ts
function defineTool<const S extends ParameterSchemaSpec, const O extends ValueSchemaSpec>(
  options: DefineToolOptions<S, O>,
): ToolDefinition & InferValue<S> & InferOutput<O>
```

**构建步骤**：
1. 验证 `options.name` 非空
2. 编译 `options.parameters` → JSON Schema（`parameterSchemaSpecToJsonSchema`）
3. 验证 `options.output.schema` 是合法 JSON Schema
4. 返回合并的 ToolDefinition（保持参数与输出的类型推断）

### `valueSchemaSpecToJsonSchema(spec)`

将 `ValueSchemaSpec` 转换为 JSON Schema Node：
- `{ type: 'string' }` → `{ type: 'string' }`
- `{ type: 'object', properties }` → `{ type: 'object', properties, additionalProperties }`
- `{ oneOf: [...] }` → `{ oneOf: [compiled] }`
- `{ type: 'json' }` → `{}` (annotation-only, unconstrained)

### `projectToolSchema(definition, detachParameters)` (+641)

白名单投影：只保留 `{ name, description, parameters, deferLoading? }`，丢弃 `output`/`execute`/presentation 回调；`detachParameters` 时对 parameters 做快照脱离（非 lossless JSON 时抛错）。

---

## Key error types

三类错误对应三种失败，排查时按 code 定位：

| 类 | code | 触发条件 |
|---|---|---|
| `ToolNotFoundError` | `UNKNOWN_TOOL` | 工具名未注册 |
| `ToolOutputError` | `INVALID_TOOL_OUTPUT` | 返回值不符合 output.schema |
| `ToolActivationError` | — | Step 准入时工具不可激活 |

---

## PTC mode notes

PTC（可编程工具调用）是另一条执行路径：模型写代码，代码内部再调度工具。`run_code` 是保留名，内部调用的 Schema 不落日志。

PTC 模式（`run_code` 工具 + 内部调度）由 `packages/core/tool-executor-ptc/` 实现：`RUN_CODE_NAME = 'run_code'`（contracts.ts +12）为保留名，注册表拒绝其他工具占用；PTC 执行器把内部 tool calls 作为子调度运行，schema 仅在绑定时刻传入（`ToolExecutionInput.schema`，从不落日志）。
