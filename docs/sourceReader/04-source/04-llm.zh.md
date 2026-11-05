# llm 包源码逐函数解析

[English](04-llm.md) | 中文

llm 包是"和模型说话"的抽象层：上层只认 LlmRuntime 服务和 StreamChunk 流，下层各家 Provider（DeepSeek、Pi AI…）各自实现 LlmAdapter 接入。这一篇按文件拆开，重点讲服务的注册、流式调用和错误结构。源码位置按"符号 + 偏移"标注（`+0` = 符号定义行，`+N` = 符号体内第 N 行，闭区间）。

## 模块概览

十三个文件：主服务、类型、调用配置、流组装、消息工厂、错误、重试策略等：

**源码目录**：`packages/llm/llm/src/`

| 文件 | 职责 | 行数 |
|---|---|---|
| `index.ts` | LlmRuntime Service、LlmAdapter 抽象类、LlmError | ~1222 |
| `types.ts` | Message、StreamChunk、GenerateOptions 等核心类型 | ~606 |
| `call-config.ts` | LlmCallConfig、callConfigEquals、markAgentLoopRequest | ~96 |
| `assembler.ts` | BlockAssembler：chunk → ContentBlock | ~208 |
| `message.ts` | freezeMessage、createAssistantMessage 等消息工厂 | ~307 |
| `assistant-stream.ts` | AssistantStreamRecord 类型与流压缩/展开 | ~534 |
| `content.ts` | 内容处理：图片投影、文件引用、工具更新 | ~510 |
| `error.ts` | HarnessError 基类、错误码 | ~175 |
| `retry-policy.ts` | ResolvedRetryPolicy、resolveRetryPolicy | ~195 |
| `brand.ts` | ProviderRequestId 品牌类型 | ~76 |
| `attribution.ts` | attributionHeaders() | ~68 |
| `api-key.ts` | normalizeApiKey | ~41 |
| `adapter-failure.ts` | normalizeLlmFailure | ~107 |

---

## index.ts — LlmRuntime Service

### `LlmRuntime` 类（index.ts · LlmRuntime）

服务的本体：管 adapter 路由表、provider 目录和模型发现回调。继承 TypertRemoteService 使其可通过 JSON-RPC 远程调用（SDK 场景）：

```ts
export class LlmRuntime extends TypertRemoteService {
  private adapters = new Map<string, AdapterRegistration>()
  private directory = new Map<string, LlmConfigurableProvider>()
  private discoveries = new Map<string, (request: LlmModelDiscoveryRequest, signal?: AbortSignal) => Promise<readonly LlmDiscoveredModel[]>>()
}
```

`adapters` 是 provider → adapter 的路由表（所有按 provider 的查询都读它），`directory` 是"可配置但未必已激活"的 provider 元数据，`discoveries` 是按 settings 命名空间登记的端点探测回调。

### `registerAdapter(providers, adapter)`（index.ts · LlmRuntime.registerAdapter）

把 adapter 绑到一组 provider 路由上。关键是"原子替换"：先删旧再加新，中间无空窗期，不会出现半路由状态：

```ts
registerAdapter(providers: string[], adapter: LlmAdapter): AdapterRegistrationHandle
```

**前置条件**：每个 provider 名不能已被其他 registration 持有。

**注册流程**（`registerAdapter +0~+29`）：
1. `prepareRoutes` 全量校验、不落盘（`registerAdapter` 内调用，`prepareRoutes +0~+22`）：
   - provider 名非空、本轮不重复；空列表直接抛 `INVALID_ADAPTER`
   - 已被其他 registration 持有 → 抛 `DUPLICATE_ADAPTER`
   - 调用 `adapter.providerInfo(provider)` 获取元数据，要求 `id` 与路由名相同、`name` 非空，否则抛 `INVALID_ADAPTER`
   - 调用 `adapter.providerRetryPolicy(provider)` 获取重试策略（缺省用 `resolveRetryPolicy` 的默认值）
2. `commitRoutes` — 原子替换（`commitRoutes +0~+8`：先删除旧 route，再添加新 route，最后 emit `llm/adapters-updated`）
3. 返回 handle，其 disposer 在 fiber 卸载时调用

**handle.replace(nextProviders)**：
- 全量校验候选集（不与其它 registration 冲突），校验失败时现有路由保持不动
- 原子替换：先删后加，中间无空窗期
- 空数组合法（settings 清空后的 registration 可以持有零路由），但首次注册不行
- 已 dispose 的 handle 调用 replace 抛 `REGISTRATION_DISPOSED`

### `registerConfigurableProviders(entries)`（index.ts · LlmRuntime.registerConfigurableProviders）

注册"可由用户激活"的 provider 元数据（设置界面里看到的那些）。它只登记目录，不绑 adapter：

```ts
registerConfigurableProviders(entries: readonly LlmConfigurableProvider[]): DirectoryRegistrationHandle
```

**用途**：注册可通过 settings UI 激活的 provider 条目（带 displayName、settingsNs、baseURLEnv 等元数据）。

**注册流程**（`registerConfigurableProviders +0~+53`）：
1. 空列表抛 `INVALID_DIRECTORY`；每个条目的 provider / displayName / settingsNs 必须非空，`settingsPath` 不能有空段
2. 条目已被其他 registration 声明、或本轮重复 → 抛 `DUPLICATE_DIRECTORY`
3. 全部通过后才整体切换（`commit`），最后 emit `llm/adapters-updated`
4. 返回 handle：disposer 撤销本 registration 的全部条目；`replace` 同样先全量校验再整体切换，dispose 后调用抛 `REGISTRATION_DISPOSED`

**与 registerAdapter 的区别**：
- `registerAdapter`：直接绑定 adapter 实例，立即生效
- `registerConfigurableProviders`：只注册元数据，adapter 由 settings 驱动动态注册

### `stream(options)`（index.ts · LlmRuntime.stream）

流式调用的统一入口：进入 waterfall → 默认分支解析 adapter 并做请求投影 → 逐段 yield：

```ts
stream(options: GenerateOptions): AsyncIterable<StreamChunk> {
  return this.streamWithRegistration(options)
}
```

**执行流程**：
1. `stream` 是薄壳：交给 `streamWithRegistration`，后者调用 `this.ctx.waterfall(this, 'llm/stream', options, () => this.adapterStream(options))`
2. waterfall 监听器可以包装、replay、监控或直接产出自己的 chunk 短路；调用 `next()` 才进入默认分支 `adapterStream`
3. `adapterStream` 在首次迭代时：`registration(options.provider)` 取 adapter（没有则抛 `NO_ADAPTER`）→ `adapter.prepareCall(...)` → `normalizeModelInfo` → `resolveCallWithInfo` → 投影（文件一律转句柄文本；纯文本模型把图片转占位文本；按 `toolUpdate` 投影工具声明与 developer 消息；`forAdapter` 丢弃属于别家 adapter 的 `replayState`）
4. 逐 chunk yield
5. adapter 选择、派发、迭代的失败统一收敛成终态 `finish` chunk：`reason.kind` 为 `'error'` 或 `'aborted'`，failure 由 `normalizeLlmFailure` 归一；中间件与消费者的失败仍然抛给调用方

**waterfall 拦截点**：
- `llm-retry` 包在此挂载重试包装器
- 其他监听器可 replay、监控、或短路由

### `prepareCall(config, signal, configure?)`（index.ts · LlmRuntime.prepareCall）

发起请求前的"装备检查"：绑 adapter、填默认值、验证 reasoning effort，最后把配置冻结。关键是流绑定同一个 adapter registration，不允许中途换 adapter：

```ts
type PrepareCallSignature = (
  config: LlmCallConfig,
  signal?: AbortSignal,
  configure?: ConfigureCall,
) => Promise<PreparedLlmCall>
```

**执行步骤**（`prepareCall +0~+58`）：
1. `signal?.throwIfAborted()`，拆出 `provider` / `model`，其余字段作为 controls
2. `this.registration(provider)` 取回 registration（没有 adapter → 抛 `NO_ADAPTER`）
3. 调用 `adapter.prepareCall(provider, model, signal)` → 获取 `PreparedAdapterCall`（含 model metadata + stream entry point），再过 `normalizeModelInfo`
4. 应用 `configure` 函数（如果提供）：传入的是脱钩并深冻结的 controls 与 modelInfo，返回新的 controls，再与原 provider / model 合并：
   ```ts
   const proposed = configure === undefined
     ? config
     : { ...configure(deepFreeze(structuredClone(controls)), deepFreeze(modelInfo)), provider, model }
   ```
5. `resolveCallWithInfo` 校验并物化默认值：`defaultMaxTokens` 与 `reasoning.defaultEffort` 会补进 config；不支持的 reasoningEffort 抛 `UNSUPPORTED_REASONING_EFFORT`
6. 冻结结果并返回 `PreparedLlmCall`：
   ```ts
   type PreparedLlmCall = {
     config: LlmCallConfig
     retryPolicy: ResolvedRetryPolicy
     adapterDefaults: LlmCallConfigAdapterDefaults
     context?: LlmModelContext
     inputModalities?: readonly ModelModality[]
     systemPromptUpdate?: SystemPromptUpdate
     toolUpdate?: ToolUpdate
     stream: (options: GenerateOptions) => AsyncIterable<StreamChunk>
   }
   ```

**关键不变量**：`preparedCall.stream()` 只允许派发一次（第二次抛 `INVALID_PREPARED_CALL`）；请求的 call-config 字段必须与 `config` 逐字段相等（`callConfigEquals`），否则同样抛 `INVALID_PREPARED_CALL`；派发始终使用 preparation 时捕获的 registration 与 `adapterCall.stream`，settings 变更不会中途切换 adapter。

### `listModels(provider)`（index.ts · LlmRuntime.listModels）

列出 provider 的模型清单，去重并验证元数据：

```ts
async listModels(provider: string): Promise<LlmModelInfo[]>
```

调用 `adapter.listModels(provider)`，要求每条 `provider` 与路由一致、`id` / `name` 非空、`description` 为字符串，并按 `id` 去重；任何非法或重复条目抛 `INVALID_CATALOG`。返回的条目是脱钩副本（`inputModalities` 与 `badge` 都会复制）。目录成员资格只是建议，不约束核心路由。

### `resolveModelInfo(provider, model)`（index.ts · LlmRuntime.resolveModelInfo）

解析单个模型的完整元数据（上下文窗口、默认输出上限、reasoning 档位、输入模态、system prompt / tool 更新模式）：

调用 `adapter.resolveModel(provider, model, signal)`，再经 `normalizeModelInfo` 逐字段校验并脱钩：identity 必须与请求路由一致，`context.contextWindow` 为正整数，`defaultMaxTokens` 为正安全整数，`systemPromptUpdate` 只认 `'in-history'`，`toolUpdate` 只认 `'in-history' | 'addition-only'`，`reasoning.efforts` 非空且不重复、`defaultEffort` 必须在其中。非法分别抛 `INVALID_MODEL_INFO` / `INVALID_MODEL_CONTEXT` / `INVALID_MODEL_MAX_TOKENS` / `INVALID_MODEL_REASONING`。

---

## LlmAdapter 抽象类（index.ts · LlmAdapter）

新 Provider 接入点：实现这个抽象类，注册进 LlmRuntime 即可。必须实现 stream()，其余可按需覆盖：

```ts
export abstract class LlmAdapter {
  providerInfo(provider): LlmProviderInfo
  providerRetryPolicy(provider): ResolvedRetryPolicy | undefined
  imageRequestPricing(provider, model): LlmImageRequestPricing | undefined
  imageRequestTarget(provider, model, image): ImageRequestTarget
  listModels(provider): Promise<readonly LlmModelInfo[]>
  resolveModel(provider, model, signal?): Promise<LlmResolvedModelInfo>
  prepareCall(provider, model, signal?): Promise<PreparedAdapterCall>
  abstract stream(options: GenerateOptions): AsyncIterable<StreamChunk>
}
```

**必须实现**：`stream()`

**可选覆盖**：
- `providerInfo`：自定义 display metadata（默认 `{ id: provider, name: provider }`）
- `providerRetryPolicy`：provider 专属重试策略（默认 `undefined`，用通用默认值）
- `imageRequestPricing` / `imageRequestTarget`：图片请求的计价与投影目标（默认不声明计价、按规范化尺寸原样发送）
- `listModels`：可广告的模型清单（默认空目录，GUI 选择器因此看不到任何模型）
- `resolveModel`：动态解析 model metadata（如从 API 获取；默认只用 provider/model 生成 identity）
- `prepareCall`：需要把 metadata 与派发绑到同一个 adapter generation 时覆盖（默认组合 `resolveModel` 与 `stream`）

**请求头契约**：每个 provider HTTP 请求都必须带 `attributionHeaders()`；接入方要在实际 wire 请求或库的 header hook 上证明它确实加上了。

---

## LlmError 类（index.ts · LlmError）

模型调用失败的结构化表达。failure 可序列化、可存进 Session 日志，code 是稳定的机器可读错误码：

```ts
export class LlmError extends HarnessError {
  readonly failure: LlmFailure
  constructor(message: string, code: string, options?: LlmErrorOptions)
}
```

**failure 结构**（`types.ts` · `LlmFailure`；`Object.freeze` 冻结，可序列化，存入 Session 日志）：
```ts
interface LlmFailure {
  message: string
  code: string       // AUTH, RATE_LIMIT, CONTEXT_WINDOW_EXCEEDED, NO_ADAPTER, ...
  status?: number    // HTTP status
  providerRetryAfterMs?: number
  requestId?: string
  offloadImages?: number  // 仅 IMAGE_OFFLOAD_REQUIRED：还要再卸载的最旧图片数
}
```

**构造验证**（`constructor +0~+25`）：
- message 非空 string
- code 非空 string
- status 是 100–599 的整数
- providerRetryAfterMs 正有限数
- requestId 非空 string

---

## types.ts — 核心类型

流式回复的最小单位是 StreamChunk（七种），请求参数集中在 GenerateOptions，调用配置和模型元数据分开建模：

### StreamChunk（types.ts · StreamChunk）

```ts
type StreamChunk =
  | { type: 'block-start'; index: number; blockType: ContentBlockType }
  | { type: 'text-delta'; index: number; text: string }
  | { type: 'reasoning-delta'; index: number; text: string }
  | { type: 'tool-call-delta'; index: number; id: ToolCallId; name?: string; argumentsDelta: string }
  | { type: 'block-end'; index: number; block: ContentBlock }
  | { type: 'usage'; usage: TokenUsage }
  | { type: 'finish'; reason: FinishReason; replayState?: ReplayEnvelope }
```

`index` 关联同一块的交错 delta；`blockType` 是 `ContentBlockMap` 的键（文本、推理、图片、文件、tool-call、tool-addition、tool-removal），`block-end` 携带组装好的 `ContentBlock`；`tool-call-delta` 的参数是模型产出的原始 JSON 字符串。adapter 在终态 `finish` 之前发 `usage`，之后不再发任何 chunk。adapter 实现可以抛错，`LlmRuntime.stream()` 在暴露给消费者前把失败归一为终态 `error` / `aborted` finish。

### GenerateOptions（types.ts · GenerateOptions）

```ts
interface GenerateOptions<Purpose extends string = LlmRequestPurpose> {
  provider: string
  model: string
  reasoningEffort?: ReasoningEffortId
  messages: RequestMessage[]
  system?: string
  tools?: ToolSchema[]
  toolHistory?: ToolHistory
  temperature?: number
  maxTokens?: number
  stop?: string[]
  signal?: AbortSignal
  sessionId?: Branded<'SessionId'>
  purpose?: Purpose
}
```

`system` 只给一次性调用方：adapter 会把它映射到 provider 的 system 槽；loop 构造的请求把 system prompt 放在 `messages` 的首条 system 消息里，这里留空。`toolHistory` 是 Session 折叠出的工具史，省略时按完整声明发送；`sessionId` 由 loop 打戳用于 replay 游标；`purpose` 是辅助请求的分类，普通对话不设。

### LlmCallConfig（call-config.ts · LlmCallConfig）

```ts
interface LlmCallControls {
  reasoningEffort?: ReasoningEffortId
  temperature?: number
  maxTokens?: number
  stop?: string[]
}

interface LlmCallConfig extends LlmCallControls {
  provider: string
  model: string
}

interface LlmCallConfigAdapterDefaults {
  reasoningEffort?: true   // true 表示此值由 adapter 默认填充，非用户显式设置
  maxTokens?: true
}
```

这三个接口都在 `call-config.ts`：`LlmCallConfig` 的每个字段 1:1 对应同名 `GenerateOptions` 字段；`LlmCallConfigAdapterDefaults` 标记哪些字段是 adapter 物化的默认值。同文件的 `callConfigEquals` 做逐字段比较（`stop` 逐元素比），`markAgentLoopRequest` / `isAgentLoopRequest` 用进程内 `WeakSet` 记录 loop 构造的请求对象身份。

### LlmResolvedModelInfo（types.ts · LlmResolvedModelInfo）

```ts
interface LlmResolvedModelInfo extends LlmModelInfo {  // provider / id / name / description? / inputModalities? / badge?
  context?: LlmModelContext                // { contextWindow: number }
  defaultMaxTokens?: number
  reasoning?: LlmModelReasoningInfo        // { efforts: LlmReasoningEffortInfo[]; defaultEffort?: ReasoningEffortId }
  systemPromptUpdate?: SystemPromptUpdate
  toolUpdate?: ToolUpdate
}
```

`context.contextWindow` 是请求与回复合并的上下文上限；`defaultMaxTokens` 是调用方省略时物化的单请求输出上限；`systemPromptUpdate` 缺省表示只在请求头部读取 system 消息，`toolUpdate` 缺省表示每次请求都声明完整工具列表。

### SystemPromptUpdate（types.ts · SystemPromptUpdate）

```ts
type SystemPromptUpdate = 'in-history'
// absent（not in union）表示 head-only（只在对话头部接受 system 消息）
```
