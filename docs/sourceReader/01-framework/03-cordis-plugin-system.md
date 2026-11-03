# Cordis plugin system basics

English | [中文](03-cordis-plugin-system.zh.md)

> Cordis is the plugin framework vendored under `vendor/`. Everything in dsh is a plugin — model adapters, the tool registry, the Session log, the Agent Loop itself.

Before reading dsh source you must understand Cordis, because dsh has no "functions you call directly" — every capability is installed as a plugin, reached through ctx, and coordinated through events. This page explains the mechanics with five concepts plus one lifecycle; every rule has a corresponding implementation in `vendor/cordis/`.

---

## Five core concepts

| Concept | In one sentence |
|---|---|
| Plugin | One functional unit installed into the system |
| Context | The "service repository" shared by all functional units |
| Typed Events | The "broadcast station" between functional units |
| Waterfall | An event pipeline that can be intercepted and rewritten station by station |
| Registrations are Effects | Everything installed can be uninstalled automatically |

### 1. Plugin

A Plugin is an object implementing the `Service` interface. It can be a function or a `Service` subclass:

```ts
// 函数式插件
const myPlugin: Service = {
  inject: ['tools'],          // 依赖的其他服务键
  apply(ctx) {                // 挂载时调用
    ctx.tools.register(defineTool({...}))
    return () => { /* 卸载时的清理 */ }
  }
}

// 类式插件
class MyPlugin extends Service {
  static override inject = ['tools']
  override apply(ctx: Context) { ... }
}
```

### 2. Context

The Context is the service repository. Plugins reach other services through `ctx.<key>`:

```ts
ctx.sessions.create(sessionId, options)   // 创建 Session
ctx.tools.register(toolDef)               // 注册工具
ctx.llm.stream(options)                   // 调用 LLM
ctx.agents.create(options)                // 创建 Agent
```

**Key design**: plugins never import each other; they decouple through ctx keys.

### 3. Typed Events

Services communicate through events instead of calling each other: one plugin "broadcasts" something, and interested plugins each listen. Event types come from TypeScript declaration merging; a misspelled event name fails at compile time:

```ts
// 在某个包的 types.ts 中声明
declare module '@deepseek-ai/cordis' {
  interface Events {
    'my-plugin/event': { foo: string }
  }
}
```

The five dispatch modes:

| 模式 | 是否 await | 顺序 | 有返回值？ | 典型用途 |
|---|---|---|---|---|
| `emit` | 否 | 注册顺序 | 否 | 纯通知 |
| `waterfall` | 否 | 注册顺序 | 是 | 可拦截改写的中间件 |
| `parallel` | 是 | 并行 | 否 | 多后端同时写 |
| `serial` | 是 | 顺序 | 是 | 串行策略检查 |
| `bail` | 否 | 顺序，首个 bail 值终止 | 是 | 权限否决 |

### 4. Waterfall semantics

Waterfall is the most special of the five dispatch modes: the event flows through every listener like an assembly line, and each station may rewrite the result or short-circuit outright. This is the bedrock of dsh's interception logic such as permission checks and content rewriting.

A Waterfall listener receives `(...args, next)` and picks one of two actions:
- Calling `next(result)` passes the (possibly rewritten) result to the next listener
- Returning `newValue` directly short-circuits; later listeners only see the new value

```ts
ctx.on('tools/pre-execute', (call, next) => {
  if (isBlocked(call.toolName)) {
    return { kind: 'denied', reason: 'policy' }  // 短路
  }
  return next()  // 委托给下一个
})
```

### 5. Registrations are effects

Everything mounts through `ctx.effect()` or `ctx.on()` and is rolled back automatically at unload:

```ts
ctx.effect(() => {
  ctx.tools.register(toolDef)
  ctx.on('agent/pre-step', listener)
  return () => {
    // 卸载时清理
  }
})
```

---

## Plugin lifecycle

```
┌──────────────────────────────────────────────────────────┐
│ Cordis Loader                                            │
│                                                          │
│  1. RESOLVING   — 解析所有 plugin 条目的依赖              │
│  2. APPLYING    — 依次调用 plugin.apply(ctx)             │
│     • inject 声明的服务必须已存在                         │
│     • 返回 disposer 函数                                 │
│  3. APPLIED     — 所有插件挂载完毕，Context 就绪          │
│  4. UNLOADING   — 按相反顺序调用 disposer                │
│  5. DISPOSED    — 全部清理完成                           │
└──────────────────────────────────────────────────────────┘
```

**Key constraints**:
- Dependencies declared in `inject` must load before the current plugin
- Returned disposers run in reverse order at unload time
- An unload failure does not release dependent projections early

---

## Plugin examples in dsh

### Tool service (tools/)

Source: [`packages/core/tools/src/index.ts`](../../../packages/core/tools/src/index.ts)

```ts
export class ToolsService extends Service {
  static override inject = ['systemPrompt']

  constructor(ctx: Context) {
    super(ctx, 'tools')

    // 注册工具执行事件管线
    ctx.on('tools/pre-execute', async (exec, next) => { ... })
    ctx.on('tools/execute', async (exec, next) => { ... })
    ctx.on('tools/post-execute', async (exec, result, next) => { ... })
    ctx.on('tools/result', (exec, result) => { ... })
  }

  register(definition, options) { ... }   // 注册工具，返回 disposer
  restrict(filter) { ... }                // 限制可见工具，返回 disposer
  async execute(input) { ... }            // 批量执行入口，委托给 executor
}
```

### LLM service (llm/llm)

Source: [`packages/llm/llm/src/index.ts`](../../../packages/llm/llm/src/index.ts)

```ts
export class LlmRuntime extends TypertRemoteService {
  registerAdapter(providers, adapter) { ... }   // 绑定 adapter 实例，原子替换路由
  registerConfigurableProviders(entries) { ... } // 注册可由 settings 激活的 provider 元数据
  async prepareCall(config, signal, configure?) { ... } // 绑定 adapter 并应用默认值
  async *stream(options) { ... }                 // llm/stream waterfall 内委托 adapter
  listProviders() { ... }
}
```

---

## Service Definition / Provider / Consumer 三角色

This is the core dsh architecture pattern (called a **Seam**). Think of it as "a power socket": the definer specifies the socket shape (the interface), the implementer builds appliances to that shape (the implementation), and the consumer just plugs in and uses it (the call) — the three never know each other, yet they work together:

```
┌─────────────────────────────────────────────────────────┐
│  Service Definition（接口契约）                          │
│  位置：packages/shell/shell/src/index.ts                │
│  内容：ShellExecutor 抽象类、ShellRequest、ShellSpec     │
│  目的：让 Provider 和 Consumer 不互相依赖                │
├─────────────────────────────────────────────────────────┤
│  Service Provider（实现）                                │
│  位置：packages/shell/bash-local/src/index.ts           │
│  内容：实现 ShellExecutor，spawn bash -c 进程            │
│  注册：super(ctx, 'shell')                              │
├─────────────────────────────────────────────────────────┤
│  Consumer（使用者）                                      │
│  位置：packages/shell/tool-bash/src/index.ts            │
│  内容：defineTool({ name: 'bash', execute() {           │
│           const result = await ctx.shell.run(req)       │
│         }})                                             │
│  注入：inject: ['shell']                                │
└─────────────────────────────────────────────────────────┘
```

A Seam is complete only when all three roles are present; missing any one, the capability seam has not formed.

---

## FiberState（上下文状态机）

The Context itself is also a lifecycle state machine, and plugins must respect it while loading and unloading. When you may register new things and when the system is "closing its doors", this table tells you:

| State | 含义 |
|---|---|
| `PENDING` | 已创建，尚未开始加载 |
| `LOADING` | 正在解析依赖并调用 `apply()` |
| `ACTIVE` | 全部就绪，可正常调用服务 |
| `FAILED` | 加载失败 |
| `UNLOADING` | 正在卸载，禁止新注册 |
| `DISPOSED` | 完全清理完毕 |

The Agent Loop refuses to create a new Agent while `INACTIVE_STATES` (`UNLOADING`, `DISPOSED`, `FAILED`) applies:

Source: [`packages/core/agent-loop/src/index.ts`](../../../packages/core/agent-loop/src/index.ts) +1-5

```ts
const INACTIVE_STATES: ReadonlySet<FiberState> = new Set([
  FiberState.UNLOADING,
  FiberState.DISPOSED,
  FiberState.FAILED,
])
```
