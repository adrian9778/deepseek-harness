# llm package reference

English | [中文](04-llm.zh.md)

llm 包是"和模型说话"的抽象层：上层只认 LlmRuntime 服务和 StreamChunk 流，下层各家 Provider（DeepSeek、Pi AI…）各自实现 LlmAdapter 接入。这一篇按文件拆开，重点讲服务的注册、流式调用和错误结构。

## Module overview

十三个文件：主服务、类型、调用配置、流组装、消息工厂、错误、重试策略等：

**Source directory**: `packages/llm/llm/src/`

| 文件 | 职责 |
|---|---|
| `index.ts` | LlmRuntime Service、LlmAdapter 抽象类、LlmError |
| `types.ts` | Message、StreamChunk、LlmCallConfig 等核心类型 |
| `call-config.ts` | LlmCallConfig、callConfigEquals、markAgentLoopRequest |
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

## index.ts — the LlmRuntime service

### The `LlmRuntime` class (llm/index.ts +354)

服务的本体：管 adapter 路由表和 provider 目录。继承 TypertRemoteService 使其可通过 JSON-RPC 远程调用（SDK 场景）：

```ts
export class LlmRuntime extends TypertRemoteService {
  private adapters = new Map<string, AdapterRegistration>()
  private directory = new Map<string, LlmConfigurableProvider>()
}
```

继承 `TypertRemoteService` 使其可通过 JSON-RPC 远程调用（SDK 场景）。

### `registerAdapter(providers, adapter)` (+401)

把 adapter 绑到一组 provider 路由上。关键是"原子替换"：先删旧再加新，中间无空窗期，不会出现半路由状态：

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

### `registerConfigurableProviders(entries)` (+495)

注册"可由用户激活"的 provider 元数据（设置界面里看到的那些）。它只登记目录，不绑 adapter：

```ts
registerConfigurableProviders(entries: readonly LlmConfigurableProvider[]): DirectoryRegistrationHandle
```

**用途**：注册可通过 settings UI 激活的 provider 条目（带 displayName、settingsNs、baseURLEnv 等元数据）。

**与 registerAdapter 的区别**：
- `registerAdapter`：直接绑定 adapter 实例，立即生效
- `registerConfigurableProviders`：只注册元数据，adapter 由 settings 驱动动态注册

### `stream(options)` (llm/index.ts +1170)

流式调用的统一入口：找 adapter → 过 waterfall（重试包装器在此挂载）→ 逐段 yield：

```ts
async *stream(options: GenerateOptions): AsyncGenerator<StreamChunk>
```

**执行流程**：
1. 查找 `this.adapters.get(options.provider)`
   - 未找到 → 抛 `LlmError('NO_ADAPTER', ...)`
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

### `prepareCall(config, signal, configure?)` (llm/index.ts +960)

发起请求前的"装备检查"：绑 adapter、填默认值、验证 reasoning effort，最后把配置冻结。关键是流绑定同一个 adapter generation，不允许中途换 adapter：

```ts
async prepareCall(
  config: LlmCallConfig,
  signal?: AbortSignal,
  configure?: ConfigureCall,
): Promise<PreparedLlmCall>
```

**执行步骤**：
1. 查找 adapter
2. 调用 `adapter.prepareCall(config.provider, config.model, signal)` → 获取 `PreparedAdapterCall`（含 model metadata + stream entry point）
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

### `listModels(provider)` (+718)

列出 provider 的模型清单，去重并验证元数据：

```ts
async listModels(provider: string): Promise<LlmModelInfo[]>
```

调用 `adapter.listModels(provider)`，去重并验证每个 model 的 metadata 合法性。

### `resolveModelInfo(provider, model)` (+760)

解析单个模型的完整元数据（上下文窗口、最大输出、reasoning 档位、输入模态、system prompt 模式）：

解析 exact model 的完整元数据（contextWindow、maxTokens、reasoning efforts、inputModalities、systemPromptUpdate）。

---

## The LlmAdapter abstract class (+208)

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
- `providerInfo`：自定义 display metadata
- `providerRetryPolicy`：provider 专属重试策略
- `resolveModel`：动态解析 model metadata（如从 API 获取）
- `prepareCall`：需要动态绑定 adapter state 时覆盖

---

## The LlmError class (+96)

模型调用失败的结构化表达。failure 可序列化、可存进 Session 日志，code 是稳定的机器可读错误码：

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

## types.ts — core types

流式回复的最小单位是 StreamChunk（七种），请求参数集中在 GenerateOptions，调用配置和模型元数据分开建模：

### StreamChunk (+496)

```ts
type StreamChunk =
  | { type: 'block-start'; index: number; blockType: string }
  | { type: 'text-delta'; index: number; text: string }
  | { type: 'reasoning-delta'; index: number; text: string }
  | { type: 'tool-call-delta'; index: number; id: string; name?: string; argumentsDelta: string }
  | { type: 'block-end'; index: number; block: ContentBlock }
  | { type: 'usage'; usage: TokenUsage }
  | { type: 'finish'; reason: FinishReason; replayState?: ReplayEnvelope }
```

### GenerateOptions (+564)

```ts
interface GenerateOptions {
  provider: string
  model: string
  messages: RequestMessage[]
  tools?: ToolSchema[]
  toolHistory?: ToolHistory
  reasoningEffort?: string
  maxTokens?: number
  temperature?: number
  stop?: string[]
  signal?: AbortSignal
  sessionId?: SessionId
  purpose?: Purpose
}
```

### LlmCallConfig (+200-280)

```ts
interface LlmCallConfig {
  provider: string
  model: string
  reasoningEffort?: string
  maxTokens?: number
  temperature?: number
  stop?: string[]
}

interface LlmCallConfigAdapterDefaults {
  reasoningEffort?: true   // true 表示此值由 adapter 默认填充，非用户显式设置
  maxTokens?: true
}
```

### LlmResolvedModelInfo (+280-350)

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

### SystemPromptUpdate (+350-380)

```ts
type SystemPromptUpdate = 'in-history'
// absent（not in union）表示 head-only（只在对话头部接受 system 消息）
```
