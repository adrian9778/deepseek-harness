# Agent Note: 让 docs/sourceReader 保持中文单语库

Status: implemented

[English](2026-11-19-sourcereader-chinese-only-library.md) | 中文

## 问题

`docs/sourceReader/` 下的四层源码阅读库最初是中文先行编写的，随后被转换成全仓库通用的双语形态：一次改动为其中十六篇各自加了一份镜像 `.zh.md` 与一份 `.i18n.yaml` 一致性记录，下一次同步又延续了这个形态。这两次转换都已经进入公开历史，因此撤销它必须是对已发布内容的一次明确回退，而不是顺承既有形态。

2026-11-12 的回退伴随三个缺陷，它们是在审查「这个库为什么连续几次提交出了问题」时发现的：

- **双语形态把同步成本翻倍。** 这个库的读者本身就在读本仓库的英文源码。留一份镜像英文正文，意味着每次按代码刷新都要改三个文件而不是一篇：该库的双语同步触碰了四十一个文件，中文单语形态只触碰十九个。除非成对门禁持续约束两侧一致，这份镜像也不会被任何机制保证。
- **已提交的文档指向了一个未入库的工具。** `.progress/UPDATE-SPEC.md` 与 `.progress/state.json` 把 `.progress/sr_tools.py` 写成校验某一篇的手段，而 `git ls-files --error-unmatch` 报告该路径 Git 不认识，`git check-ignore` 也找不到覆盖它的规则。这条引用描述的是一个只存在于作者本机的文件，全新 clone 里没有它，所以文档里写的校验命令在别处跑不起来。
- **十二份 stash 快照被丢弃。** 改写进行中的工作被反复 `git stash` 保存，随后这些快照被丢弃，在对象库里留下悬空 commit，`git stash list` 已经看不到它们。把每一份都与随后的提交逐文件比对后，改动过的每个文件都逐字节相同，因此没有丢失任何东西——但工作现场已无法从正常的 stash 界面找回。

## 决策

`docs/sourceReader/` 是一个中文单语库，这个豁免由成对门禁执行，而不只写在文档里：

- **豁免登记**：`docs/sourceReader/` 已列入 [translation-pairing.manifest.json](../../../../scripts/translation-pairing.manifest.json) 的 `excluded` 数组，与 `docs/topics/` 并列。门禁因此会拒绝在库内重新出现的 `.zh.md` 或 `.i18n.yaml`。库自身的维护文档也写明同一条规则，库与门禁口径一致。
- **不带语言切换器**：库内各篇没有语言切换行，也没有 `.zh.md` 链接。任何重新出现这两者的页面都会让门禁失败。
- **删除而非库内归档**：十六个 `.zh.md` 页面与其十六个 `.i18n.yaml` 记录被删除。Git 历史保留着它们，库内不再留存一份自己旧形态的副本。
- **入库的校验工具**：`.progress/sr_tools.py` 与引用它的文档一起提交，使 `.progress/UPDATE-SPEC.md` 记录的校验命令在全新 clone 里可解析。已提交的文档不得指向本仓库未跟踪的文件。
- **本地排除 `.codegraph/`**：codegraph 索引目录通过 `.git/info/exclude` 排除，而不是加进共享的 `.gitignore`，使这个决定只留在本机，不改变每个贡献者的忽略规则。

## 考虑过的替代方案

**保留镜像英文副本。** 全仓库约定偏向成对文档，前两次转换也正是照这个约定做的。但那样每次按代码刷新都要改三个文件而非一篇，除非信任成对门禁持续捕捉漂移，否则镜像一定会失步；而这个库的读者本来就在读英文源码，新增受众很小。

**保留成对页面但把它排除出门禁。** 排除一个仍含成对文件的目录，会诱导下一次改动悄悄补回缺失的英文页而不被门禁察觉——这正是双语库退化成「只更新了一半」的成因。因此直接删除页面。

**只在库的维护文档里写明豁免。** 文档无法拒绝一个重新出现的成对文件。库的文档本来就已经写了这条规则，但执行必须落在清单里，让犯错时失败的是门禁而不是评审。

**删掉工具引用，而不是把工具入库。** `.progress/UPDATE-SPEC.md` 与 `.progress/state.json` 里的校验命令是该库检查文件路径与符号归属的既定手段。把它们改写成不指名工具，文档就不可复核了；把脚本入库只多一个文件，却能让文档里写的命令保持可运行。

**把 `.codegraph/` 加进共享 `.gitignore`。** 该目录是本机生成的状态。把规则写进共享忽略文件会改变整个仓库对其他贡献者的行为，包括没有这个目录的机器；`.git/info/exclude` 在本机就能达到同样效果，不需要一次入库的决定。

**保留被丢弃的 stash 快照。** 把每份快照与随后的提交比对后内容是逐字节相同的，恢复它们不会带来任何新信息。比对完成后用 `git prune` 清掉。

## 后果

一次按代码刷新现在每篇只改一个文件而不是三个，成对门禁也不再扫描这个库，因此语料里没有任何机制能单独发现过期页面。该库文件路径与符号归属的正确性，因此完全依赖 `.progress/sr_tools.py` 的 `verify` 与 `symbols` 子命令，加上 `pnpm run test:docs`。

想要某篇的英文版本的人需要另写一篇独立文档。这里没有生成器，也不承诺提供任何一篇的英文覆盖。

在 `docs/sourceReader/` 下重新出现 `.zh.md` 或 `.i18n.yaml` 时，`verify-translation-pairing` 会报出该库并失败——这是库内文档单独提供不了的执行力。

上述比对之后，那些悬空 stash commit 已从对象库清除，因此那次改写的审计痕迹只存在于随后的提交与本条笔记中。

## 校验台账

| 块 | 记录 |
|---|---|
| 日期 | 2026-11-19 |
| 环境 | 本 fork 的 `master` 本地工作副本，不涉及任何远端部署 |
| 库 / 仓库范围 | `docs/sourceReader/`、`docs/topics/`、`scripts/translation-pairing.manifest.json`、`.agents/notes/implemented/process/` |
| 一句话结论 | 之前几次提交不是损坏的 Git 对象：其中一次提交信息不合规范，两次把后来被放弃的形态发了出去，而回退连同配套工具一直未提交 |
| 操作前状态 | 工作树含 32 个删除、19 个修改、3 个未跟踪路径；该库三个提交都是 `origin/master` 的祖先；存在 12 个悬空 commit，全部是 stash 快照 |
| 前置条件与确认结果 | 用祖先关系检查确认三个库提交都已发布；用 `git check-ignore` 确认没有任何规则覆盖该校验脚本；用 `git grep` 确认本次改动之前没有任何已提交文件提到过该脚本 |
| 开工前手工步骤 | 无需手工前置；在落提交之前，这次审计是只读的 |
| 脚本清单与结果 | `sr_tools.py`（11.8 KB，本次改动一并入库）提供 `verify` 与 `symbols`；`pnpm run test:docs` 跑了文档聚合门禁，21 项通过、0 项失败，含成对门禁、markdown links、markdown wrap 与 repository references |
| 执行命令原文 | 见下方命令块 |
| 终态 | 三个提交：中文单语化连同入库的工具、`docs/topics/` 及其豁免、本条笔记；`.codegraph/` 在本地被排除；悬空对象已清理 |
| 本次独有发现 | stash 快照与随后的提交逐字节相同，说明反复 stash 产生的是噪音，不是数据损失；每份恰好只改十六个文件，且全部在 `docs/sourceReader/` 之下。`git prune` 一个都没删掉它们，因为它们已经进 pack，真正清掉需要 repack |
| 幂等性验证 | 重跑 `git fsck --no-reflogs --dangling` 报告没有悬空 commit；重跑文档聚合门禁仍是同一组 21 项通过；重新加回一个被排除的成对文件会再次让成对门禁失败 |
| 可复用复核语句 | `git merge-base --is-ancestor <commit> origin/master` 用来区分已发布与仅本地的工作；`git diff <stash> <commit> -- <path>` 用来区分快照与已交付内容 |
| 遗留与后续 | 未执行任何远端推送；发布这些提交由用户手动完成 |

```sh
git reflog --date=iso -40                       # commit, stash, and pull history with timestamps
git fsck --no-reflogs --dangling                # locate dropped stash snapshots
git ls-files --error-unmatch docs/sourceReader/.progress/sr_tools.py   # untracked: no rule and no index entry
git check-ignore -v docs/sourceReader/.progress/sr_tools.py            # exit 1: not ignored either
for s in <stash commits>; do                    # prove each snapshot matches shipped content
  git diff "$s" <following commit> -- docs/sourceReader/ | wc -l
done
pnpm run test:docs                              # 21 gates, 0 failures
git prune                                       # remove only the proven-redundant dangling objects
```
