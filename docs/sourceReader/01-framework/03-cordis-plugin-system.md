# Cordis 插件系统基础

> Cordis 是 vendored 到 `vendor/` 的插件框架。dsh 的一切都是插件——包括模型适配器、工具注册表、Session 日志、Agent Loop 本身。

---

## 五个核心概念

### 1. Plugin（插件）

Plugin 是实现 `Service` 接口的对象。可以是函数或 `Service` 子类：

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

### 2. Context（上下文）

Context 是服务的仓库。插件通过 `ctx.<key>` 访问其他服务：

```ts
ctx.sessions.create(sessionId, options)   // 创建 Session
ctx.tools.register(toolDef)               // 注册工具
ctx.llm.stream(options)                   // 调用 LLM
ctx.agents.create(options)                // 创建 Agent
```

**关键设计**：插件之间不直接导入，通过 ctx key 解耦。

### 3. Typed Events（类型化事件）

服务间通信通过事件，TypeScript 声明合并定义事件类型：

```ts
// 在某个包的 types.ts 中声明
declare global {
  interface SessionEventMap {
    'my-plugin/event': { foo: string }
  }
}
```

五种分发模式：

| 模式 | 是否 await | 顺序 | 有返回值？ | 典型用途 |
|---|---|---|---|---|
| `emit` | 否 | 注册顺序 | 否 | 纯通知 |
| `waterfall` | 否 | 注册顺序 | 是 | 可拦截改写的中间件 |
| `parallel` | 是 | 并行 | 否 | 多后端同时写 |
| `serial` | 是 | 顺序 | 是 | 串行策略检查 |
| `bail` | 否 | 顺序，首个 bail 值终止 | 是 | 权限否决 |

### 4. Waterfall 语义

Waterfall 监听器接收 `(...args, next)`：
- 调用 `next(result)` → 将（可能改写的）结果传给下一个监听器
- 直接 `return newValue` → 短路，后续监听器只看到新值

```ts
ctx.on('tools/pre-execute', (call, next) => {
  if (isBlocked(call.toolName)) {
    return { kind: 'denied', reason: 'policy' }  // 短路
  }
  return next()  // 委托给下一个
})
```

### 5. Registrations are Effects（注册是可逆效果）

所有挂载通过 `ctx.effect()` 或 `ctx.on()`，卸载时自动回滚：

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

## 插件生命周期

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

**关键约束**：
- `inject` 声明的依赖必须先于当前插件加载
- 返回的 disposer 函数在卸载时按逆序调用
- 卸载失败不会提前释放依赖的投影（projection）

---

## dsh 中的 Plugin 示例

### 工具注册插件（tools/）

Source: [`packages/core/tools/src/index.ts`](../../packages/core/tools/src/index.ts)

```ts
export class ToolRuntime implements Service {
  static override inject = []  // 不依赖其他服务

  override apply(ctx: Context): Disposer {
    const registry = new ToolRegistry()

    // 暴露 ctx.tools 键
    ctx.set('tools', {
      register(toolDef) { registry.register(toolDef) },
      restrict(filter) { return registry.restrict(filter) },
      execute(name, args) { return registry.execute(name, args) },
    })

    // 注册工具执行事件管线
    ctx.on('tools/pre-execute', async (call, next) => { ... })
    ctx.on('tools/execute', async (call, next) => { ... })
    ctx.on('tools/post-execute', async (call, next) => { ... })
    ctx.on('tools/result', (result) => { ... })

    return () => { /* 清理 */ }
  }
}
```

### LLM 服务插件（llm/llm）

Source: [`packages/llm/llm/src/index.ts`](../../packages/llm/llm/src/index.ts)

```ts
export class LlmService implements Service {
  static override inject = []

  override apply(ctx: Context): Disposer {
    const adapters = new Map<string, LlmAdapter>()

    ctx.set('llm', {
      async stream(options) {
        const adapter = adapters.get(options.provider)
        if (!adapter) throw new LlmError('NO_ADAPTER')
        return adapter.stream(options)
      },
      registerAdapter(id, adapter) { ... },
      listProviders() { return [...adapters.keys()] },
    })

    return () => { adapters.clear() }
  }
}
```

---

## Service Definition / Provider / Consumer 三角色

这是 dsh 架构的核心模式（称为 **Seam**）：

```
┌─────────────────────────────────────────────────────────┐
│  Service Definition（接口契约）                          │
│  位置：packages/shell/shell/src/types.ts                │
│  内容：ShellExecutor 接口、ShellRequest、ShellSpec       │
│  目的：让 Provider 和 Consumer 不互相依赖                │
├─────────────────────────────────────────────────────────┤
│  Service Provider（实现）                                │
│  位置：packages/shell/bash-local/src/index.ts           │
│  内容：实现 ShellExecutor，spawn bash -c 进程            │
│  注册：ctx.set('shell', executor)                       │
├─────────────────────────────────────────────────────────┤
│  Consumer（使用者）                                      │
│  位置：packages/shell/tool-bash/src/index.ts            │
│  内容：defineTool({ name: 'bash', execute() {           │
│           const result = await ctx.shell.run(req)       │
│         }})                                             │
│  注入：inject: ['shell']                                │
└─────────────────────────────────────────────────────────┘
```

一个 Seam 必须同时具备这三个角色才算完整。缺少任何一个，能力缝就没有形成。

---

## FiberState（上下文状态机）

Cordis Context 有生命周期状态，插件在加载/卸载时必须尊重：

| State | 含义 |
|---|---|
| `RESOLVING` | 正在解析依赖 |
| `APPLYING` | 正在调用 `apply()` |
| `APPLIED` | 全部就绪，可正常调用服务 |
| `UNLOADING` | 正在卸载，禁止新注册 |
| `DISPOSED` | 完全清理完毕 |

Agent Loop 在 `INACTIVE_STATES`（`UNLOADING`、`DISPOSED`、`FAILED`）时拒绝创建新 Agent：

Source: [`packages/core/agent-loop/src/index.ts`](../../packages/core/agent-loop/src/index.ts) +1-5

```ts
const INACTIVE_STATES: ReadonlySet<FiberState> = new Set([
  FiberState.UNLOADING,
  FiberState.DISPOSED,
  FiberState.FAILED,
])
```
