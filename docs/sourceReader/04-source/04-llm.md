# llm 包源码逐函数解析

## 模块概览

**源码目录**：`packages/llm/llm/src/`

| 文件 | 职责 |
|---|---|
| `index.ts` | LlmRuntime Service、LlmAdapter 抽象类、LlmError |
| `types.ts` | Message、StreamChunk、LlmCallConfig 等核心类型 |
| `call-config.ts` | LlmCallConfig、ConfigureCall、prepareCall 逻辑 |
| `assembler.ts` | BlockAssembler：chunk → ContentBlock |
| `message.ts` | freezeMessage、createAssistantMessage 等消息工厂 |
| `assistant-stream.ts` | AssistantStreamRecord 类型 |
| `content.ts` | 内容处理：图片投影、文件引用、工具更新 |
| `error.ts` | HarnessError 基类、错误码 |
| `retry-policy.ts` | ResolvedRetryPolicy、resolveRetryPolicy |
| `brand.ts` | ProviderRequestId 品牌类型 |
| `attribution.ts` | attributionHeaders() |
| `api-key.ts` | normalizeApiKey、assertUsableApiKey |
| `adapter-failure.ts` | normalizeLlmFailure |

---

## index.ts — LlmRuntime Service

### `LlmRuntime` 类（llm/index.ts +354）

```ts
export class LlmRuntime extends TypertRemoteService {
  private adapters = new Map<string, AdapterRegistration>()
  private directory = new Map<string, LlmConfigurableProvider>()
  private discoveries = new Map<string, DiscoveryFn>()
}
```

继承 `TypertRemoteService` 使其可通过 JSON-RPC 远程调用（SDK 场景）。

### `registerAdapter(providers, adapter)`（+580-640）

```ts
registerAdapter(providers: string[], adapter: LlmAdapter): AdapterRegistrationHandle
```

**前置条件**：每个 provider 名不能已被其他 registration 持有。

**注册流程**：
1. 对每个 provider：
   - 验证非空、不重复
   - 调用 `adapter.providerInfo(provider)` 获取元数据
   - 调用 `adapter.providerRetryPolicy(provider)` 获取重试策略
2. `commitRoutes()` — 原子替换（先删除旧 route，再添加新 route，最后 emit 通知）
3. 返回 handle，其 disposer 在 fiber 卸载时调用

**handle.replace(nextProviders)**：
- 全量校验候选集（不与其它 registration 冲突）
- 原子替换：先删后加，中间无空窗期
- 已 dispose 的 handle 调用 replace 抛 `REGISTRATION_DISPOSED`

### `registerConfigurableProviders(entries)`（+700-800）

```ts
registerConfigurableProviders(entries: readonly LlmConfigurableProvider[]): DirectoryRegistrationHandle
```

**用途**：注册可通过 settings UI 激活的 provider 条目（带 displayName、settingsNs、baseURLEnv 等元数据）。

**与 registerAdapter 的区别**：
- `registerAdapter`：直接绑定 adapter 实例，立即生效
- `registerConfigurableProviders`：只注册元数据，adapter 由 settings 驱动动态注册

### `stream(options)`（llm/index.ts +1166）

```ts
async *stream(options: GenerateOptions): AsyncGenerator<StreamChunk>
```

**执行流程**：
1. 查找 `this.adapters.get(options.provider)`
   - 未找到 → 抛 `LlmError('NO_ADAPTER', 'NO_ADAPTER')`
2. 触发 `llm/stream` waterfall：
   ```ts
   const result = await this.dispatch.waterfall('llm/stream', options, async () => {
     const adapter = this.registration(options.provider).adapter
     return adapter.stream(options)
   })
   ```
3. 逐 chunk yield

**waterfall 拦截点**：
- `llm-retry` 包在此挂载重试包装器
- 其他监听器可 replay、监控、或短路由

### `prepareCall(config, signal, configure?)`（llm/index.ts +956）

```ts
async prepareCall(
  config: LlmCallConfig,
  signal: AbortSignal,
  configure?: ConfigureCall,
): Promise<PreparedLlmCall>
```

**执行步骤**：
1. 查找 adapter
2. 调用 `adapter.prepareCall(config.provider, config.model, signal)`
   → 获取 `PreparedAdapterCall`（含 model metadata + stream entry point）
3. 应用 `configure` 函数（如果提供）：
   ```ts
   const controls = { ...config, ...adapterModel.defaults }
   const finalControls = configure(controls, adapterModel)
   ```
4. 验证 reasoningEffort 在 model.reasoning.efforts 中
5. 返回 `PreparedLlmCall`：
   ```ts
   {
     config: deepFreeze(finalConfig),
     retryPolicy: adapterRegistration.retryPolicy,
     context: adapterModel.context,
     inputModalities: adapterModel.inputModalities,
     systemPromptUpdate: adapterModel.systemPromptUpdate,
     toolUpdate: adapterModel.toolUpdate,
     adapterDefaults: capturedDefaults,
     stream: (options) => preparedCall.stream(options),  // 绑定到同一 generation
   }
   ```

**关键不变量**：`preparedCall.stream()` 必须使用 preparation 时的 adapter generation，不允许中途切换 adapter（防止 settings 变更导致的不一致）。

### `listModels(provider)`（+920-960）

```ts
async listModels(provider: string): Promise<LlmModelInfo[]>
```

调用 `adapter.listModels(provider)`，去重并验证每个 model 的 metadata 合法性。

### `resolveModel(provider, model)`（+960-1000）

```ts
async resolveModel(provider: string, model: string): Promise<LlmResolvedModelInfo>
```

解析 exact model 的完整元数据（contextWindow、maxTokens、reasoning efforts、inputModalities、systemPromptUpdate）。

---

## LlmAdapter 抽象类（+200-500）

```ts
export abstract class LlmAdapter {
  providerInfo(provider): LlmProviderInfo
  providerRetryPolicy(provider): ResolvedRetryPolicy | undefined
  imageRequestPricing(provider, model): LlmImageRequestPricing | undefined
  imageRequestTarget(provider, model, image): ImageRequestTarget
  listModels(provider): Promise<LlmModelInfo[]>
  resolveModel(provider, model, signal?): Promise<LlmResolvedModelInfo>
  prepareCall(provider, model, signal?): Promise<PreparedAdapterCall>
  abstract stream(options: GenerateOptions): AsyncIterable<StreamChunk>
}
```

**必须实现**：`stream()`

**可选覆盖**：
- `providerInfo`：自定义 display metadata
- `providerRetryPolicy`：provider 专属重试策略
- `resolveModel`：动态解析 model metadata（如从 API 获取）
- `prepareCall`：需要动态绑定 adapter state 时覆盖

---

## LlmError 类（+100-180）

```ts
export class LlmError extends HarnessError {
  readonly failure: LlmFailure
  constructor(message: string, code: string, options?: LlmErrorOptions)
}
```

**failure 结构**（可序列化，存入 Session 日志）：
```ts
interface LlmFailure {
  message: string
  code: string       // AUTH, RATE_LIMIT, CONTEXT_WINDOW_EXCEEDED, NO_ADAPTER, ...
  status?: number    // HTTP status
  providerRetryAfterMs?: number
  requestId?: string
  offloadImages?: number
}
```

**构造验证**：
- message 非空 string
- code 非空 string
- status 在 100-599 范围
- providerRetryAfterMs 正有限数
- requestId 非空 string

---

## types.ts — 核心类型

### StreamChunk（+1-100）

```ts
type StreamChunk =
  | { kind: 'block-start'; block: ContentBlock }
  | { kind: 'text-delta'; delta: string }
  | { kind: 'reasoning-delta'; delta: string }
  | { kind: 'tool-call-delta'; delta: ToolCallDelta }
  | { kind: 'tool-call-start'; toolCall: ToolCallBlock }
  | { kind: 'usage'; usage: TokenUsage }
  | { kind: 'finish'; finishReason: 'end_turn' | 'stop' | 'tool_calls' | 'length' | string; completionDetails?: CompletionDetails }
```

### GenerateOptions（+100-200）

```ts
interface GenerateOptions {
  provider: string
  model: string
  messages: Message[]
  tools?: ToolSchema[]
  toolChoice?: ToolChoice
  signal?: AbortSignal
  reasoningEffort?: string
  maxTokens?: number
  temperature?: number
  [key: string]: unknown  // adapter-specific extras
}
```

### LlmCallConfig（+200-280）

```ts
interface LlmCallConfig {
  provider: string
  model: string
  reasoningEffort?: string
  maxTokens?: number
  temperature?: number
  [key: string]: unknown
}

interface LlmCallConfigAdapterDefaults {
  reasoningEffort?: true   // true 表示此值由 adapter 默认填充，非用户显式设置
  maxTokens?: true
}
```

### LlmResolvedModelInfo（+280-350）

```ts
interface LlmResolvedModelInfo {
  provider: string
  id: string
  name: string
  contextWindow?: number
  maxTokens?: number
  reasoning?: { efforts: ReasoningEffortInfo[] }
  inputModalities?: ModelModality[]
  systemPromptUpdate: SystemPromptUpdate  // 'in-history' | absent
}
```

### SystemPromptUpdate（+350-380）

```ts
type SystemPromptUpdate = 'in-history'
// absent（not in union）表示 head-only（只在对话头部接受 system 消息）
```
