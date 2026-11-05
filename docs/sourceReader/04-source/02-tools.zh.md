# tools 包源码逐函数解析

[English](02-tools.md) | 中文

tools 包管两件事：工具的"花名册"（注册、限制、查找）和"安检流水线"（单次调用的五阶段管线、批量调度）。这一篇按文件拆开，重点讲 ToolsService 的方法和 defineTool DSL 的编译过程。

## 模块概览

九个文件：主服务、契约、DSL、管线、批调度、执行器、事件、Schema 验证、UI 呈现：

**源码目录**：`packages/core/tools/src/`

| 文件 | 职责 | 行数 |
|---|---|---|
| `index.ts` | ToolsService、注册表、执行入口 | ~412 |
| `contracts.ts` | ToolDefinition、ToolExecution、决策类型 | ~391 |
| `schema.ts` | defineTool DSL、ValueSchemaSpec、projectToolSchema | ~646 |
| `pipeline.ts` | ToolExecutionPipeline：单次调用 5 阶段管线 | ~771 |
| `batch.ts` | executeToolBatch：分组 + 有界并发池 | ~285 |
| `executor.ts` | ToolExecutor 契约 + ToolBatchInput/Result | ~102 |
| `events.ts` | tools/* 事件声明 | ~58 |
| `json-schema.ts` | JSON Schema 子集定义 + 验证 | ~656 |
| `presentation.ts` | ToolCallView / ToolResultView UI 呈现类型 | ~389 |

---

## index.ts — ToolsService

### Service declaration (`ToolsService`)

```ts
declare module '@deepseek-ai/cordis' {
  interface Context { tools: ToolsService }
}
```

`ToolsService extends Service`：构造时 `super(ctx, 'tools')`，`static inject = ['systemPrompt']`（构造时注册工具 schema provider，由所选 executor 的 `describe()` 产出）。别名导出 `ToolRuntime`。执行器由 `setExecutor()` 挂载（`tool-executor-native` / `tool-executor-ptc`）。`static Config` 的 `mode`、`maxParallelSubCalls` 都是 `z.never()`：旧配置键在加载时即报错，挂载 executor 才是现在的做法。

### ToolLayer 类 (`ToolLayer`)

每层作用域一个 ToolLayer，装着该层的工具和限制：

```ts
class ToolLayer implements ScopeLayer {
  readonly tools: NamedEntries<ToolRegistration>       // { definition, when }
  readonly restrictions = new AnonymousEntries<CompiledToolRestriction>()

  isEmpty(): boolean
  admits(name: string): boolean        // 检查所有限制是否允许此工具名
}
```

守卫不在 ToolLayer 里：同级的 `GuardLayer` 单独一层（`AnonymousEntries<ToolGuard>`），由 `ToolsService.guard()` 注册、`ToolsService.checkGuards()` 在 pre-execute 之后按全局层 + 作用域链逐个求值，返回第一个拒绝原因。

### ToolView 接口 (`ToolView`)

给模型看的"工具橱窗"：可见的、注册过的、可被限制的三个视图分开维护（`ToolsService.view()` 单次层遍历推导）：

```ts
interface ToolView {
  visible: ReadonlyMap<string, ToolDefinition>     // 限制与作用域遮蔽过滤后的可见工具
  registrations: ReadonlyMap<string, ToolRegistration>  // visible 背后的注册（含 when 条件）
  knownNames: ReadonlySet<string>                  // 限制前的全部名称（用于 prompt order 验证）
  restrictableNames: ReadonlySet<string>           // 当前全局工具名（scoped 注册不受限制影响）
}
```

更近的注册遮蔽更远的；查看作用域自己的注册豁免限制。

---

## ToolsService 类方法

### `register(def, options?)` (index.ts · ToolsService.register)

注册工具的六步：验证输出、验证 timeoutMs、预留名、入层、广播、返回卸载器：

```ts
register(definition: ToolDefinition, options: ToolRegistrationOptions = {}): () => void
```

**执行步骤**：
1. 验证 `def.output` 形状（`schema`/`render`/可选 `presentationMeta`）与 `timeoutMs` 为正有限数
2. `assertSupportedJsonSchema(output.schema)`
3. 预留 `run_code` 名称（PTC 展示传输专用，不可注册或遮蔽）
4. 将 `{ definition, when }` 加入当前层的 `tools` 表
5. 触发 `tools/change` 事件（无 veto，失败仅 warn）
6. 返回 disposer，卸载时移除注册并再次触发 `tools/change`

参数 Schema 不在这里验证：`defineTool` 编译时已过 `assertSupportedJsonSchema`，直接注册的原始 `parameters` 由作者自证。

`ToolRegistrationOptions.when`：条件可见性，Step 准入时通过 `resolve()` 求值；条件拒绝抛 `ToolActivationError`。

### `restrict(filter)` (index.ts · ToolsService.restrict)

限制可见工具，语义是"多层取交集"：

```ts
restrict(filter: ToolRestriction): () => void
```

**语义**：
- `allow` 白名单：只保留指定名称
- `deny` 黑名单：排除指定名称
- 多层 restriction 取交集；scoped 注册不受影响
- 需要 scoped ctx；拒绝空 filter、`run_code` 与不在 `restrictableNames` 里的未知名

### `schemas(scope?, toolSet?)` (index.ts · ToolsService.schemas)

给模型的工具 Schema 走白名单投影，内部字段（execute、output 等）绝不外泄：

```ts
schemas(scope?: ScopeKey, toolSet?: ResolvedToolSet): ToolSchema[]
```

**返回**：每个可见工具一份深拷贝 schema，仅模型可见字段（`name`、`description`、`parameters`，`deferLoading` 为 `true` 时保留）。`output`、`execute`、`projectContent` 等绝不泄露（`projectToolSchema` 白名单）。

### `execute(input)` (index.ts · ToolsService.execute) — 核心执行入口

```ts
async execute(input: ToolBatchInput): Promise<ToolBatchResult>
```

委托给本作用域的 executor；一次 Step 的全部 tool calls 作为一个批次。完整管线见 [03-tool-execution.md](../03-detailed/03-tool-execution.zh.md)。

```
1. 查找 ToolDefinition
   const def = this.executable(name, scope, toolSet)   // 带 step 准入集时由它决定
   if (!def) throw new ToolNotFoundError(name)

2. 快照并冻结 arguments
   const frozenArgs = snapshotJsonValue(input.arguments)

3. 构建 ToolExecution（添加 token / rootCallId）

4. tools/pre-execute waterfall
   → 'allow' 继续；'deny' 返回 error result；'cancel' 返回取消结果
   → 'ask' 等待 approval service（allowed-once → allow；拒绝/取消/无通道 → deny）

5. 单调守卫检查
   const reason = service.checkGuards(exec)
   if (reason) return deny(result)

6. tools/execute waterfall
   → 默认 next() 调用 def.execute(exec.arguments, runContext)
   → 验证 value 符合 output.schema，render 为 ContentBlock[]

7. tools/post-execute waterfall
   → 'accept' 使用 listener 结果（可替换 content 或 value）；'block' 转为 error result

8. finalizeContent（工具自定义最后转换）

9. 触发 tools/result（observe-only）

10. 返回 finalResult
```

### `get(name, scope?, toolSet?)` (index.ts · ToolsService.get)

按 scope 查找定义（用于内部查找，非模型路径）：

```ts
get(name: string, scope?: ScopeKey, toolSet?: ResolvedToolSet): ToolDefinition | undefined
```

按 scope 解析可见定义：scoped 注册遮蔽全局，被限制掉的全局读作不存在；presenters 传入调用方 agent，让渲染卡片与实际执行的定义一致。

---

## schema.ts — defineTool DSL

### `defineTool(options)` (schema.ts · defineTool)

把声明式 DSL 编译成完整 ToolDefinition，参数与输出类型推断一路保留：

```ts
function defineTool<const S extends ParameterSchemaSpec, const O extends ValueSchemaSpec>(
  options: DefineToolOptions<S, O>,
): ToolDefinition
```

**构建步骤**：
1. 校验 `options.timeoutMs` 为正有限数
2. 编译 `options.parameters` → JSON Schema（`parameterSchemaSpecToJsonSchema`，隐式开放对象根）
3. 编译 `options.output.schema` → JSON Schema（`valueSchemaSpecToJsonSchema`，内含 `assertSupportedJsonSchema`）
4. 返回 ToolDefinition：`execute` 先跑参数验证，违规抛 `ToolArgsError`；`presentCall`/`presentResult`/`isConcurrencySafe` 做软验证——参数不合当前 schema 时分别回退 `undefined`/`false`（replay 旧日志不抛错）

### `valueSchemaSpecToJsonSchema(spec)`

将 `ValueSchemaSpec` 转换为 JSON Schema Node（编译走栈安全的迭代任务图，循环引用报错）：
- `{ type: 'string' }` → `{ type: 'string' }`
- `{ type: 'object', properties }` → `{ type: 'object', properties, additionalProperties }`（`additionalProperties` 必填）
- `{ oneOf: [...] }` → `{ oneOf: [逐条编译] }`
- `{ type: 'json' }` → `{}` (annotation-only, unconstrained)

### `projectToolSchema(definition, detachParameters)` (schema.ts · projectToolSchema)

白名单投影：只保留 `{ name, description, parameters, deferLoading? }`，丢弃 `output`/`execute`/presentation 回调；`detachParameters` 时对 parameters 做快照脱离（非 lossless JSON 时抛错）。

---

## 关键错误类型

排查时按 code 定位：

| 类 | code | 触发条件 |
|---|---|---|
| `ToolNotFoundError` | `UNKNOWN_TOOL` | 工具名未注册 |
| `ToolOutputError` | `INVALID_TOOL_OUTPUT` | 返回值不符合 output.schema |
| `ToolArgsError` | `INVALID_ARGS` | 模型给的参数不符合参数 Schema |
| `ToolActivationError` | `TOOL_ACTIVATION_FAILED` | Step 准入时工具条件求值失败 |

---

## PTC 模式相关

PTC（可编程工具调用）是另一条执行路径：模型写代码，代码内部再调度工具。`run_code` 是保留名，内部调用的 Schema 不落日志。

PTC 模式（`run_code` 工具 + 内部调度）由 `packages/core/tool-executor-ptc/` 实现：`RUN_CODE_NAME = 'run_code'`（`contracts.ts · RUN_CODE_NAME`）为保留名，注册表拒绝其他工具占用；PTC 执行器把内部 tool calls 作为子调度运行，schema 仅在绑定时刻传入（`ToolExecutionInput.schema`，从不落日志）。子调度靠 `ToolExecutionInput.parent` 标记；选定 PTC 执行器后，无 `parent` 的模型直调在执行策略管线之前即按 `UNKNOWN_TOOL` 拒绝。
