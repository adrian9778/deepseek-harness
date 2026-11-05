# System prompt assembly

English | [中文](04-system-prompt-assembly.zh.md)

每次问模型之前，系统都要重新拼一份完整的系统提示词：身份、策略、工具说明、运行时上下文，一段都不能少。这一页讲清这份提示词是怎么"分层组装、按序渲染、支持中途替换"的，末尾附源码定位表。

## The assembly architecture

系统提示词不是单一字符串，而是由多个 **PromptSection** 有序拼接而成。每个 Section 可以来自：
- 静态配置（Bundle 内联）
- 动态注入（Agent setup 阶段）
- 工具 Schema（自动注入）
- 运行时上下文（工作区指令、时间等）

可以把它想成一份"每日简报"：固定栏目按版面顺序排好，动态栏目每期重新采写，最后总编（waterfall）审一遍才能发布：

```
┌──────────────────────────────────────────────────────────────┐
│                 PromptAssembly 流程                           │
│                                                              │
│  systemPrompt.assemble(context)                              │
│       │                                                      │
│       ├─► 1. 收集所有 PromptSection                          │
│       │     按注册的 order 升序排列（相同 order 按名称）      │
│       │                                                      │
│       ├─► 2. 注入当前工具 Schema                              │
│       │     ctx.tools 注册的 tools provider                  │
│       │     → projectToolSchema 只保留模型可见字段            │
│       │                                                      │
│       ├─► 3. 渲染各 Section                                   │
│       │     renderPrompt(assembly)                           │
│       │     → 静态文本或 provider(context) 生成文本片段       │
│       │     → {{variable}} 插值后拼接为完整文本               │
│       │                                                      │
│       ├─► 4. 触发 system-prompt/assemble waterfall           │
│       │     → 监听器可改写最终呈现                            │
│       │     → 完整 section（complete: true）随后恢复          │
│       │                                                      │
│       └─► 5. 返回 PromptAssembly                             │
│             {                                                │
│               sections,      // 已命名的 PromptSection 列表  │
│               contexts,      // 运行时上下文章节              │
│               variables,     // 变量替换映射                  │
│               tools,         // 工具 Schema 数组              │
│             }                                                │
└──────────────────────────────────────────────────────────────┘
```

---

## The PromptSection type

一个 Section 就是一条"固定栏目"：有名字（防重复）、有次序（order 决定位置）、有内容（静态文本或动态 provider）：

**Source**: `packages/core/system-prompt/src/index.ts` · PromptSection

```ts
interface PromptSection {
  /** 唯一标识，重复注册会抛错 */
  name: string
  /** 渲染优先级（数字越小越靠前） */
  order: number
  /** 静态文本或每次组装时求值的 provider */
  text: string | ((context: AssembleContext) => string)
  /** 是否插值 prompt 变量；默认 true，false 保留字面文本 */
  interpolate?: boolean
  /** 视为完整系统提示词；多于一个生效的 complete section 会组装失败 */
  complete?: boolean
}
```

### Built-in section positions (SECTION_ORDERS)

| Section | order | 职责 |
|---|---|---|
| `HARNESS_IDENTITY` | -1000 | 身份标识（可配置关闭） |
| `DEPLOYMENT_PERSONA_PREFIX` | 0 | 部署 persona 前缀 |
| `PLAN_POLICY` / `TEAM_POLICY` | 500/600 | 计划与团队协作策略 |
| `PTC_ONLY` | 800 | PTC 模式说明 |
| `TOOL_*` | 1000-3000 | 各内置工具的使用说明 |
| `MCP_SERVERS` | 3100 | 外部 MCP 服务器 |
| `TOOLS_SDK` | 5000 | SDK 工具说明 |
| `STRUCTURED_OUTPUT` | 9900 | 结构化输出 |
| `HARNESS_SOURCE` / `WEB_SURFACE` | 10000+ | 本地路径与端点 |
| `DEPLOYMENT_PERSONA_SUFFIX` | 10200 | 部署 persona 后缀 |

---

## Key decisions during assembly

组装里有三个容易踩坑的位置，逐个说明：

### 1. Tool schema injection

工具 Schema 在每次 Step 开始前重新组装，因为工具的可见性可能因 Agent preset 而变化。

```ts
// packages/core/system-prompt/src/index.ts
tools(provider: (context: AssembleContext) => ToolProviderResult): () => void
// tools 包在构造时注册 provider：
ctx.systemPrompt.tools(({ scope, toolSet }) => ({ ...executor.describe(...), knownNames }))
// projectToolSchema 只保留 { name, description, parameters, deferLoading? }
```

### 2. The system message in the Session

拼好的提示词去哪里？第一次写入 Session 作为 node 0；之后工具变了就"替换"这个节点，而不是追加新消息——这保证了模型历史里始终只有一份最新系统提示词：

```ts
// 首次：写入 surface node 0（Step 循环提交 SystemPromptProjection 产出的 commit）
session.append('system/message',
  { turn, step, message: createSystemMessage(systemText) },
  { surfaceOp: 'append' })

// 后续替换（如工具变化）：
session.append('system/message',
  { turn, step, message: createSystemMessage(newSystemText) },
  { surfaceOp: { op: 'replace', startSeq: lastSysSeq, endSeq: lastSysSeq } })
```

### 3. In-history vs head update modes

不同模型对系统提示词的支持方式不同：有的允许它在历史任意位置，有的只认开头一条。系统按模型能力选择更新策略：

| `systemPromptUpdate` 模式 | 含义 | 适用 Provider |
|---|---|---|
| `'in-history'` | system 消息可出现在对话历史任意位置 | 支持该能力的模型 |
| 缺省 | system 消息只在对话头部 | 大多数 OpenAI 兼容 API |

**Source**: `packages/llm/llm/src/types.ts` · LlmResolvedModelInfo

---

## System prompt replacement semantics

系统提示词在 Session 里是一个"表面节点"（surface node），有严格的替换规则。核心思想：node 0（第一次的提示词）受保护，后续节点可以替换或清空：

**Source**: `packages/core/session/src/surface.ts` assertSystemHeadRewrite

```
System prompt 在 Session 中是一个 "surface node"（表面节点）。

规则：
1. 第一个 system/message 是 node 0，受到保护
   - 不能被非 system/message 事件替换
   - 只能被恰指该节点的 system/message 改写（assertSystemHeadRewrite）

2. 后续 system/message 是 node 1, 2, ...
   - 可以被替换
   - 空渲染（empty text）清除该节点

3. incapable route（不支持 in-history 的模型）：
   - 每次 Step 重读：node 0 被重写为当前渲染文本（文本未变则不产生事件）
   - 非空 later nodes 被清空（logged empty replacement）
   - 空 rendered 重写 node 0 为空并清空后续，等效清除所有 active nodes

4. capable route（支持 in-history 的模型）：
   - 同一 series 内文本变化时，非空 text 追加为新 system 节点
   - 旧 node 原样保留（保持请求前缀，无 superseded 标记）
   - 系列开始且最新存活节点已是该文本时，不产出任何事件
```

---

## Runtime context injection

除了系统提示词，每个 Step 开始前还会刷新一份"当下事实快照"（工作目录、时间、沙箱策略等），以 durable user-role 消息写进日志：

**Source**: `packages/core/agent-loop/src/runtime-context.ts`

Agent Loop 在每个 Step 开始前刷新运行时上下文：

```ts
class RuntimeContextProjection {
  // 维护 per-step 的运行时事实快照
  project(joined: string, sections: readonly ContextSnapshotSection[]): UserMessage | undefined {
    // 收集：工作目录、沙箱策略、审批策略、时间等上下文章节
    // 返回"未提交"的候选 user 消息：内容与上一个快照一致时返回 undefined
    // （全部清除时返回 CLEARED 哨兵文本的快照）
    // 不在此处写日志——提交发生在 ReactLoopAgent.step：
    //   session.append('user/message', context, { surfaceOp: 'append' })
    // （每个请求系列只 admitted 一次）
  }
}
```

SystemPromptProjection 决定 `system/message` 的提交：它按 `renderPrompt(assembly)` 与 capability flags（`inHistory`、`startsSeries`、工具变化）产出 `SystemPromptCommit[]`，由 Step 循环逐个 append。

---

## Key source map

想直接跳进源码时用这张表，按功能直达文件与符号：

| 功能 | 源码文件 | 关键部分 |
|---|---|---|
| PromptSection 类型 | `packages/core/system-prompt/src/index.ts` | `PromptSection` |
| assemble() 主逻辑 | `packages/core/system-prompt/src/index.ts` | `assemble` |
| 工具 Schema 注入 | `packages/core/system-prompt/src/index.ts` | `tools` |
| System message 节点管理 | `packages/core/session/src/surface.ts` | `assertSystemHeadRewrite` |
| Runtime context 刷新 | `packages/core/agent-loop/src/runtime-context.ts` | `SystemPromptProjection`、`RuntimeContextProjection` |
| Provider system prompt 模式 | `packages/llm/llm/src/types.ts` | `LlmResolvedModelInfo` |
