---
name: pr
description: >-
  <PROJECT> PR 规范: 正文组织、跟踪并修复 CI、用 Close/Fix 关联 Issue、squash/rebase
  合入并清理、以及数据库迁移的 multi-head 处理.
  Use when creating, updating, or merging a pull request, 开 PR, 更新 PR,
  写 PR 正文, 合并 PR, 处理 CI 失败.
---

# PR 规范

正文的完整格式与示例见 `<DOCS_DIR>/pr.md`; 本文只规定执行顺序与边界.

## 工作区

PR 默认用**独立 worktree**, 不在主检出开发, 除非用户明确要求.

```bash
git worktree add ../<repo>-<topic> -b <type>/<topic> <BRANCH>
```

分支名用 `<type>/<topic>` (`fix/` `feat/` `docs/`). 收尾时删除 worktree 与本地分支.

## 正文

顺序: **Summary → 接口与数据变更 → Implementation → Test plan → Close/Fix 或 Related**.

- Summary 是无标点单句列表, 写对外交付的能力或解决的缺陷; 禁止标识符、调用链描述、实现细节.
- 接口与数据变更用表格; 大规模 PR 必写, 小修复可省略.
- Implementation 开头先给一句整体形状, 每条用粗体小标题领起, 单条一行左右.
- Test plan 用 check-list, 写验证方式与结果.
- 只描述最终形态; 开发过程中的增强、返工、重命名不写入.
- 相对 base 的行为改变与范围声明 (未实现项、只写 `Related` 的原因) 必须写.

## CI

**推送时机**: 不要每提交一次就推一次. 本地做完、用户确认功能或修复已完整实现后再推.

推送前按逻辑变更 squash 本地提交, 保留可分节的粒度.

推送后必须跟踪这一轮 CI, 失败则修复:

- 只需改测试代码时可直接操作; 要改业务代码时先报告用户并获得许可;
- 修复需要改动本 PR 的设计方案时, 必须向用户报告;
- 修完按同样节奏攒着, 等用户确认后再推.

PR 的 CI 通过之后再合入. 合入后主分支上的那一轮 CI 不必等待 —— 那是同一棵树的重复运行.
rebase / force-push 会作废上一轮, 这类改动必须重新等这一轮通过.

## Issue

正文末尾按是否彻底解决区分:

- **彻底解决**: 用 `Close` / `Fix`, 合并时自动关闭;
- **只是关联**: 单独一节 `Related` 列出. 已被 `Close` / `Fix` 的不必重复.

```markdown
## Related

- #56
- #78

Close #12
Fix #34
```

## 合入

- 首选 **squash**;
- 非常简单的 PR 可用 **rebase**;
- **永远不要用 merge commit**;
- 合并使用 PR 自身标题, 不另指定 message;
- 合入后删除该 PR 的 worktree 与本地分支; 从本仓库分支创建的还要删除远程分支.

### 堆叠式 PR

需求涉及多个领域、预期规模较大时, 按依赖分成多层, 上层 PR 的目标分支是下层分支.

- `gh stack submit` 创建 Stack 对象; `init` / `add` 只维护本地跟踪;
- CI 依据 Stack 对象决定 base, 只手工 `gh pr edit --base` 会导致整条栈不触发检查;
- 堆叠内不能逐个合并, 用 `gh stack merge --yes --squash` 原子合入;
- 自下而上合入, 下层合入后把上层剩余提交重基到新主分支;
- 重新核对生成物与数据库迁移.

## 数据库迁移

PR 含 schema 变更 / 新 migration 时, 正文最前面固定放:

```markdown
> ❗ **此 PR 包含 DB migration**
```

多个并行 PR 各自基于主分支最后一个 revision, 直接合入会让迁移图分叉 (multiple heads).
合并这类 PR 之前:

1. 把 `origin/<BRANCH>` merge 或 rebase 进该 PR 分支;
2. `alembic heads` — 必须只剩一个 head;
3. 若多个 head: 不要 `alembic merge`; 把本 PR 最早那条 revision 的 `down_revision` 改成当前主分支的 head,
   排成一条链; 禁止手写新的 revision ID;
4. 推上去, 再等 CI, 再 squash.

连续合多个带 migration 的 PR 时, 每合一个都重新执行 1–3.
