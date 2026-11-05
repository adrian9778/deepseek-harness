# sourceReader 同步规范（2026-11-05 批次）

本规范约束本轮全库同步。基准 = 当前 HEAD（用 `git log -1 --format='%h %ad' -- docs/sourceReader/` 取文档库自身最后一次提交；本批次基线为 2026-11-03 的全库重写提交）。**不要在本目录任何文件里写提交号字面量**：`verify-repository-references` 门禁会拒绝 maintained 文件里的 commit hash（写日期 + 上面这条取值命令代替）。

## 定位记法（本轮硬性要求，覆盖全部 .md 与 .zh.md）

依据 doc-sync-with-code：**不写绝对行号**。本库现有 `+N` 绝对行号引用全部按下列规则转换：

| 场景 | 旧写法 | 新写法 |
|---|---|---|
| 小节标题锚点 | `### \`AgentLoop\` Service（+291）` | `### \`AgentLoop\` Service（\`AgentLoop\`）` 或直接 `### \`AgentLoop\` Service`；标题里不再出现行号 |
| 方法表「行范围」列 | `create(id, options, meta) \| +668` | 位置列给 `AgentLoop.create`（索引式：符号 → 文件，不给行号） |
| 函数体内区间 | `+329-338` | `init +0~+9`（`+0` = 定义行，`+N` = 符号体内第 N 行，闭区间） |
| 单一定义行 | `INACTIVE_STATES（+41）` | `INACTIVE_STATES`（常量/类型只给符号） |
| 跨文件 | `session/index.ts +806` | `session/index.ts · Session.append` |

- 区间偏移相对**小节锚点符号**的定义行；小节内第二个符号要写全名（`SurfaceManager.validateNext`）。
- 模块概览表的「行数」列保留 `~N` 近似值，但必须与当前文件 `wc -l` 一致（±3 内可保留旧值）。
- 索引表（文末「Source index」类）只给「符号 → 文件」，删行号列。

## 已确认的架构级变化清单（基线 → HEAD）

1. **`packages/team/` 新组**：`agent-team`（子 Agent 树持久化共享任务看板，CAS 任务所有权）+ `tool-agent-team`（模型任务工具，四个任务 schema；默认 Team 模式关闭）。来源：experimental/agent-team 家族毕业后重组。
2. **`packages/mcp/mcp-manager` 新包**：管理 MCP server 舰队（`config.servers` + 插件注册两个来源），逐条目挂 `dsh-mcp-client` 实例，`ctx.mcpManager` 聚合连接状态，`restart`/`probe`/`watch`。
3. **`packages/agent` 移除 `parentAgent` 运行时所有权**（`CreateAgentOptions`/`ResumeAgentOptions`/`AgentEntry` 均删该字段）：文档库无此概念描述，无需改写，但不得新写。
4. **删除**：`web/web-search-exa`、`web/web-search-perplexity`、experimental 下四个 agent-team 旧路径。文档库零引用，无需处理。
5. **行漂移**：`agent-loop/src/index.ts` 净 −3 行（AgentLoop 类 +290、init +328、create +667、createAgent +716、resume +752）；`tools/src/index.ts` ToolView +51；`system-prompt/src/index.ts` tools() +551、assemble() +620；`llm/llm/src` 类型小节三处文件归属错误（LlmCallConfig 实在 `call-config.ts +31`，LlmResolvedModelInfo 实在 `types.ts +454`，SystemPromptUpdate 实在 `types.ts +440`）。
6. **核对过且无需改动**：`agent.ts`/`inbox.ts`/`assistant-stream.ts` 全部引用、session 全部引用、cli 全部引用、llm 类方法引用（LlmError +96、LlmAdapter +208、LlmRuntime +354、registerAdapter +401、registerConfigurableProviders +495、listModels +718、resolveModelInfo +760、prepareCall +960、stream +1170）、StreamChunk types.ts +496、GenerateOptions types.ts +564。

## 内容红线

- 以代码为 source of truth；语义描述与代码冲突时改文档，不改代码。
- 正文只留当前状态；演进过程一律不写（本库无「变更记录」节，保持无）。
- 不新增提交号、不新增 absolute line number、不新增 `as any` 类代码内容。
- `.md` 与 `.zh.md` 成对改：中文版是主视角（更细），英文版同步；结构、标题、表格行数保持成对一致。
- 每篇改完跑：`pnpm run verify-translation-pairing --write <该篇.md>`，确认 yaml 键随标题变化再生成且无报错。
- 只改派发的文件；发现其他篇的问题写进回报，不跨文件修。

## 校验命令

```sh
pnpm run verify-translation-pairing --write docs/sourceReader/<篇>.md   # 成对哈希重录
pnpm run test:docs                                                      # doc-quick 聚合门禁
```
