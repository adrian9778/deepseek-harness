# 系统提示词组装过程

## 提示词组装的整体架构

系统提示词不是单一字符串，而是由多个 **PromptSection** 有序拼接而成。每个 Section 可以来自：
- 静态配置（Bundle 内联）
- 动态注入（Agent setup 阶段）
- 工具 Schema（自动注入）
- 运行时上下文（工作区指令、时间等）

```
┌──────────────────────────────────────────────────────────────┐
│                 PromptAssembly 流程                           │
│                                                              │
│  systemPrompt.assemble(agentCtx)                             │
│       │                                                      │
│       ├─► 1. 收集所有 PromptSection                          │
│       │     ctx.systemPrompt.getSections(agentCtx)           │
│       │     → 按注册顺序排列                                 │
│       │                                                      │
│       ├─► 2. 注入当前工具 Schema                              │
│       │     ctx.tools.schemas(agentCtx)                      │
│       │     → 编译为 JSON Schema 字符串                      │
│       │     → 追加到 system prompt 的工具说明区域            │
│       │                                                      │
│       ├─► 3. 渲染各 Section                                   │
│       │     renderPrompt(sections, tools)                    │
│       │     → 每个 section.render(ctx) 生成文本片段          │
│       │     → 拼接为完整 systemText                          │
│       │                                                      │
│       ├─► 4. 生成 PromptAssembly 结果                        │
│       │     {                                                │
│       │       systemText,         // 完整系统提示词文本       │
│       │       toolSchemas,        // 工具 Schema 数组        │
│       │       contextSections,    // 运行时上下文章节         │
│       │       variables           // 变量替换映射            │
│       │     }                                                │
│       └─► 5. 触发 system/prompt-assembled 事件               │
│                                                                           
└──────────────────────────────────────────────────────────────┘
```

---

## PromptSection 类型

**源码位置**：`packages/core/system-prompt/src/types.ts`

```ts
interface PromptSection {
  /** 唯一标识，用于替换操作 */
  id: string
  /** 渲染优先级（数字越小越靠前） */
  priority: number
  /** 是否可被替换 */
  replaceable: boolean
  /** 渲染函数 */
  render(ctx: PromptRenderContext): string | undefined
}
```

### 内置 PromptSection 类型

| Section 类型 | 来源 | 职责 |
|---|---|---|
| `static` | Bundle 配置 | 固定文本章节 |
| `tool-schema` | ctx.tools.schemas() | 当前可见工具列表 |
| `workspace` | workspace 包 | 工作区 AGENTS.md 等内容 |
| `time-context` | context 包 | 当前时间、时区 |
| `skill` | skill 包 | 激活的 Skill 内容 |
| `goal` | goal 包 | 当前目标描述 |
| `variable` | 用户变量 | 变量替换 |

---

## 组装过程中的关键决策

### 1. 工具 Schema 注入

工具 Schema 在每次 Step 开始前重新组装，因为工具的可见性可能因 Agent preset 而变化。

```ts
// packages/core/system-prompt/src/index.ts
async function assembleToolSchema(agentCtx: Context): Promise<string> {
  const schemas = ctx.tools.schemas(agentCtx)
  if (schemas.length === 0) return ''
  return `## Available Tools\n\n${schemas.map(s =>
    jsonSchemaToDescription(s)
  ).join('\n\n')}`
}
```

### 2. System Message 在 Session 中的表示

第一次组装的 system prompt 以 `system/message` 事件写入 Session：

```ts
// 首次：写入 surface node 0
session.append('system/message', {
  turn, step,
  message: { text: systemText, nodes: [{ id: 'node-0', text: systemText }] },
}, { surfaceOp: { op: 'append' } })

// 后续替换（如工具变化）：
session.append('system/message', {
  turn, step,
  message: { text: newSystemText, nodes: [...] },
}, { surfaceOp: { op: 'replace', startSeq: lastSysSeq, endSeq: lastSysSeq } })
```

### 3. In-history vs Head 更新模式

不同 LLM Provider 对 system prompt 的支持方式不同：

| `systemPromptUpdate` 模式 | 含义 | 适用 Provider |
|---|---|---|
| `'in-history'` | system 消息可出现在对话历史任意位置 | DeepSeek 某些模型 |
| `'head-only'` | system 消息只在对话头部 | 大多数 OpenAI 兼容 API |

**源码位置**：`packages/llm/llm/src/types.ts` +LlmResolvedModelInfo

---

## System Prompt 替换语义

**源码位置**：`packages/core/session/src/surface.ts` +system-message-replacement

```
System prompt 在 Session 中是一个 "surface node"（表面节点）。

规则：
1. 第一个 system/message 是 node 0，受到保护
   - 不能被非 system/message 事件替换
   - 不能被覆盖范围包含 node 0 的替换操作删除

2. 后续 system/message 是 node 1, 2, ...
   - 可以被替换
   - 空渲染（empty text）清除该节点

3.  incapable route（不支持 in-history system 的模型）：
   - 新 series 时，非空 text 合并到 node 0
   - 非空 later nodes 被清空（logged empty replacement）
   - 空 text 直接清除所有 active nodes

4. capable route（支持 in-history 的模型）：
   - 在同一 series 内，新 text 追加为更新的 system node
   - 旧 node 保留但标记为 superseded
```

---

## 运行时上下文注入

**源码位置**：`packages/core/agent-loop/src/runtime-context.ts`

Agent Loop 在每个 Step 开始前刷新运行时上下文：

```ts
class RuntimeContextProjection {
  // 维护 per-step 的运行时事实快照
  async refresh(agentCtx: Context, session: Session): Promise<void> {
    const facts = await collectRuntimeFacts(agentCtx)
      // 包括：cwd、激活的 skill、当前 goal、时间等

    // 以 developer/message 形式写入 Session
    session.append('developer/message', {
      turn, step,
      message: {
        content: [{ type: 'text', text: formatFacts(facts) }],
      },
    }, { surfaceOp: { op: 'append' } })
  }
}
```

---

## 关键源码定位

| 功能 | 源码文件 | 关键行 |
|---|---|---|
| PromptSection 类型 | `packages/core/system-prompt/src/types.ts` | +1-全 |
| assemble() 主逻辑 | `packages/core/system-prompt/src/index.ts` | +assemble |
| 工具 Schema 注入 | `packages/core/system-prompt/src/index.ts` | +tools |
| System message 节点管理 | `packages/core/session/src/surface.ts` | +system |
| Runtime context 刷新 | `packages/core/agent-loop/src/runtime-context.ts` | +1-164 |
| Provider system prompt 模式 | `packages/llm/llm/src/types.ts` | +LlmResolvedModelInfo |
