---
name: release
description: >-
  <PROJECT> 发版: 判定 semver、写 CHANGELOG、运行 just bump、推送提交与 tag,
  由 CI 生成 Release 正文与制品.
  Use when the user asks to 发版, bump, cut a release, write changelog, or run `just bump`.
---

# 发版

## 硬约束

- 发版提交必须用 `just bump patch|minor|major`. 禁止手改版本文件、手打 tag、直接运行底层版本命令.
- Release 正文只来自 `CHANGELOG.md` 里与 tag 匹配的 `##` 节. 不要手工创建或编辑 Release.
- 禁止把每条 commit 摊成一条 changelog.
- 推送提交与 tag 属于远程操作, 必须经用户明确批准.

## 流程

```
判定 bump 段 → 起草 CHANGELOG 节 (等用户 review) → just bump → push 提交与 tag
```

`just bump` 允许工作区里**只有** `CHANGELOG.md` 未提交. 先写 changelog 再 bump, 不要先 bump.

### 1. 版本判定

用户明示了 patch / minor / major 就用那个. 否则检查上一个 tag 以来的变更:

```bash
git describe --tags --abbrev=0
git log <tag>..HEAD --format='%s%n%b'
just bump-dry <kind>
```

| 条件 | bump |
|------|------|
| `BREAKING CHANGE:` 或 `type!:` | major |
| 至少一条 `feat` (无 breaking) | minor |
| 其余用户可见变更 (通常是 `fix`) | patch |

`docs` / `test` / `ci` / `chore` / `refactor` 单独出现不够发版. 没有用户可见变更就停下来问用户.

向用户报一次 `x.y.z => a.b.c` 和判定理由.

### 2. 写 CHANGELOG

git log 遵循约定式提交, 用来分组和归并. 不要把自动生成的 changelog 原样发出: 合并同一主题,
润色成用户能看懂的短句.

在最上面 (H1 之后) 插入新节, 分组顺序:

```
## vX.Y.Z

### ✨ 新功能
### 🐛 修复
### ⚡ 性能
### ⚠️ 破坏性变更
```

对应 `feat` / `fix` / `perf` / breaking. 其它 type 默认不上. 同一 issue / PR 下的 feat + docs + test
合成一条.

`🐛 修复` 只收**已发布版本中存在的缺陷**. 缺陷由本区间的功能或改动引入时, 并入对应条目或删除,
不单列一行.

条目格式:

```
- **短标题** (#13), 一句话
- **短标题** (#27) @someone
```

- 只写重点, 不写限定条件、实现细节、迁移边角、注意小节;
- Issue / PR 写成 `#13`, 不用链接包裹;
- 英文标点 `,` `;` `:` `()`, 不用中文标点;
- 社区贡献: PR 作者不是仓库 owner、也不是 bot 时, 条目末尾加 `@login`, 并带 PR 号.

写好后**停下让用户 review**. 未经审阅不要 bump / push.

### 3. bump 与推送

用户确认稿后:

```bash
just bump <kind>
git push origin HEAD
git push origin vX.Y.Z
```

制品工作流由 tag 触发, 并从 CHANGELOG 抽取对应节填入 Release 正文. 不必轮询, 也不必手工改正文.
把 Release 地址告诉用户即可.
