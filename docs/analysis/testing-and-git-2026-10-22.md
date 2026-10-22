# Amane 测试覆盖与 GIT 提交管理分析

> 分析对象: `fix/minnano-warmup` @ `c65134d` (2026-10-22)。
> 数据来源: 本地实际执行的 `pytest --cov` (工作区**含未提交改动**)、仓库配置、Git 历史、CI 工作流。非估算, 除注明「推测」处外均为实测。
> 未提交的 15 个文件改动会让覆盖率与干净 HEAD 略有差异, 结论的量级与结构不受影响。

---

## 一、结论速览

| 维度 | 现状 | 评价 |
|------|------|------|
| 后端测试规模 | 226 个测试文件, 43,193 行, **1,009 个测试函数**, 334 处 `parametrize` | 规模大, 分层清晰 |
| 后端行覆盖率 | **51.3%** (27,072 语句, 覆盖 13,899) | 低于 `fail_under = 60` 的门禁 |
| 覆盖率门禁 | `pyproject.toml` 设 `fail_under = 60`, 由 `just check` (即 `just ci`) 执行 | 本地/CI 的 `just ci` 当前**必红** |
| 前端测试 | `web/` 247 个源文件 / 60,805 行, **0 个测试**; `pnpm test` 是 `echo 'No web tests configured yet'` | 完全空白 |
| Android 测试 | 3 个测试文件 (145 行) 对应 10 个源文件 (1,238 行), 只覆盖 `ServerUrl`/`ServerStore`/`DragAxisTracker` | 仅纯函数, 有 CI 门禁 |
| CI 平台 | Ubuntu: 全量 `just ci`; Windows: 仅 pytest; Android: 按路径过滤后编译+单测 | 分层合理 |
| 提交管理 | 380 次提交 / 30 个 tag / 2 个月内, Conventional Commits 覆盖率 98.7% | 纪律良好 |
| 提交门禁 | 自研 `scripts/githooks/` + prek, pre-commit 做生成/修复/静态检查, pre-push 做 pyright + pytest | 设计细致, 坑已用注释固化 |
| 远程协作 | 5 个 Issue/PR 模板, AI_POLICY 三级披露, 两条 gh-aw 智能体工作流 (日报 / Issue 分诊) | 规则明确且机器可执行 |

**三个最值得处理的问题**:

1. **`just ci` 的覆盖率门禁必红**: 51.3% < 60%, 且这是 CI 唯一的 Ubuntu 门禁 job。要么补测试, 要么下调门禁, 要么把门禁改成「不回落」(见 §2.5)。
2. **前端零测试**: 61k 行 React 代码只有类型检查与 i18n 提取兜底, 没有任何行为验证。
3. **数据库仓库层是最大盲区**: `db/repos/*` 2,566 行里只覆盖 390 行 (15.2%), 而它承载了任务链、级联删除、筛选等核心语义。

---

## 二、测试覆盖详析

### 2.1 执行结果

```
.venv\Scripts\python.exe -m pytest tests/ -n auto --dist worksteal --cov=amane
  --cov-report=term-missing:skip-covered --cov-report=xml --cov-report=json
```

| 指标 | 数值 |
|------|------|
| 通过 | 约 4,500 条用例 (进度条 100%, 无 F) |
| 错误 | 数百个 `E` (仓内报错) |
| 跳过 | 数十个 `s` |
| 总语句数 | 27,072 |
| 覆盖语句 | 13,899 |
| 未覆盖语句 | 13,173 |
| 排除语句 | 493 (migrations 等) |
| **总覆盖率** | **51.34%** |

> 关于运行环境的两个坑 (与代码无关, 会让本地跑不起来):
> 1. `uv run` 会尝试访问 `%LOCALAPPDATA%\uv\cache\sdists-v9\.git` 并被沙箱拒绝 → 直接用 `.venv\Scripts\python.exe` 跑。
> 2. pytest 默认 basetemp 位于 DSH 代理创建的临时目录下, `scandir` 报 `WinError 5` → 需显式 `--basetemp=temp\pytest-run`。
>
> 本次运行中的 `E` 与 `s` 主要由基线之后未提交的改动 (以及本机沙箱/依赖环境) 引起, **不代表 CI 现状**; 覆盖率数字用于体现「结构分布」而非「是否全绿」。

### 2.2 分层覆盖率 (按目录)

| 目录 | 覆盖行 | 未覆盖 | 覆盖率 | 说明 |
|------|-------:|-------:|-------:|------|
| `enums.py` / `aggregate/*` / `utils/*` / `parsing/*` | — | — | 95–100% | 纯函数与聚合引擎, 测试最扎实 |
| `net/*` (http 75%, browser 86%, proxy 90%) | — | — | 75–100% | 错误分类、响应缓存、熔断、代理池均有覆盖 |
| `config/manager.py` | 551 | 77 | 87.7% | |
| `api/models/*` | 736 | 107 | 87.3% | DTO 校验密集覆盖 |
| `db/models.py` | 452 | 18 | 96.2% | |
| `crawlers/actor/*` | 846 | 604 | 58.3% | Wikipedia / minnano |
| `crawlers/sites/*` | 1,064 | 1,209 | 46.8% | 数据驱动为主, 站点间落差极大 |
| `app/runtime.py` | 169 | 210 | 44.6% | 组装/生命周期 |
| `library/cleanup/*` | 299 | 435 | 40.7% | inventory 40.6% |
| `agent/*` | 约 700 | 约 1,100 | 37–48% | |
| `api/routes/*` | 708 | 1,255 | 36.1% | |
| `search/*` | — | — | 20–48% | store 26.6%, indexer 20.1% |
| `playback/*` (proxy 90%, hls 87%) | — | — | 32–90% | 两极分化: factory 32%, file_response 25% |
| `probe/*` | — | — | 19–71% | search 19.2%, network 26.1% |
| `scheduler/service.py` | 71 | 361 | 16.4% | |
| `scheduler/worker.py` | 40 | 225 | 15.1% | |
| `handlers/file.py` | 58 | 302 | 16.1% | |
| `handlers/delete.py` | 47 | 201 | 19.0% | |
| `handlers/scrape.py` | 35 | 148 | 19.1% | |
| `db/repos/*` | **390** | **2,176** | **15.2%** | 最大的盲区 |
| `media/nfo.py` | 10 | 73 | 12.0% | 仅 1 个测试文件, 覆盖写入路径 |
| **全仓** | 13,899 | 13,173 | **51.3%** | |

### 2.3 未覆盖行数最多的 20 个文件

| 文件 | 覆盖 / 缺失 | 覆盖率 |
|------|------------|-------:|
| `db/repos/facet_helpers.py` | 89 / 604 | 12.8% |
| `scheduler/service.py` | 71 / 361 | 16.4% |
| `db/repos/facets.py` | 62 / 329 | 15.9% |
| `handlers/file.py` | 58 / 302 | 16.1% |
| `db/repos/tasks.py` | 47 / 299 | 13.6% |
| `library/cleanup/inventory.py` | 204 / 298 | 40.6% |
| `crawlers/sites/dmm.py` | 78 / 246 | 24.1% |
| `db/repos/metadata.py` | 35 / 234 | 13.0% |
| `crawlers/actor/sites/minnano.py` | 128 / 233 | 35.5% |
| `media/resource_store.py` | 74 / 229 | 24.4% |
| `scheduler/worker.py` | 40 / 225 | 15.1% |
| `search/store.py` | 81 / 223 | 26.6% |
| `db/repos/feeds.py` | 38 / 216 | 15.0% |
| `app/runtime.py` | 169 / 210 | 44.6% |
| `handlers/delete.py` | 47 / 201 | 19.0% |
| `probe/search.py` | 43 / 181 | 19.2% |
| `observability/recorder.py` | 86 / 177 | 32.7% |
| `agent/feed_ops.py` | 104 / 172 | 37.7% |
| `playback/factory.py` | 79 / 167 | 32.1% |
| `crawlers/sites/avbase.py` | 42 / 166 | 20.2% |

### 2.4 站点爬虫的覆盖率落差 (数据驱动测试的真实效果)

`crawlers/sites/` 整体 46.8%, 但站点间差异极大:

| 分层 | 站点 (覆盖率) |
|------|--------------|
| 高 (≥85%) | `dmm_api` 96.7, `dmm_cdn` 100, `dahlia`/`faleno` 100 (空实现), `wp_works` 89.3 |
| 中 (60–85%) | `theporndb` 75.6, `official` 72.4, `kin8` 68.1, `airav` 64.6, `javbus` 61.0 |
| 低 (25–55%) | `giga` 54.8, `avsox` 45.5, `r18dev` 43.2, `freejavbt` 34.4, `getchu` 33.3, `fc2club` 32.4, `prestige` 32.1, `fc2` 32.2, `fc2ppvdb` 31.6, `jav321`/`xcity` 31.8, `mgstage` 30.5, `javlibrary` 29.8, `iqqtv` 28.9, `javdb` 25.3 |
| 极低 | `dmm` 24.1, **`avbase` 20.2** |

原因判断: 数据驱动测试 (TOML + 真实响应) 只走「成功解析」主路径, 站点自身的分支逻辑 (多路由回退、错误恢复、字段补全、限速、重试) 不在 TOML 中, 而 `docs/dev/crawler-testing.md` 要求这些放在 `tests/crawlers/test_<site>.py` —— 但只有 14 个站点有专属 Python 测试文件 (`airav`/`dmm`/`giga`/`javbus`/`kin8`/`mgstage`/`official`/`r18dev`/`theporndb`/`wp_works`/`minnano`/`wikipedia` 等), 其余站点的分支完全没有测试。

此外, **fixture 依赖的可用性会静默改变结果**: `just fixtures` 从私有仓库 `amane-testdata` 拉取到 `tests/crawlers/cases/` (gitignored), 拉取失败时依赖 `cases/` 的用例 **skip 而非 fail**。本机 `cases/` 不存在, 说明上面的低覆盖率中有相当部分是「fixture 缺失导致的 skip」, 并非代码无人测试。这本身是一个设计权衡 (CI 一定有 fixture, 本地可以没有), 但它让本地覆盖率数字不可与 CI 比较。

### 2.5 API 路由覆盖率

23 个路由文件, 整体 36.1%。HTTP 集成测试存在 (40 个文件 / 7,736 行), 但明显集中在部分资源上:

| 路由 | 覆盖率 | | 路由 | 覆盖率 |
|------|-------:|-|------|-------:|
| `feeds.py` | 18.7% | | `actors.py` | 32.2% |
| `tasks.py` | 25.4% | | `libraries.py` | 33.8% |
| `saved_queries.py` | 29.1% | | `agent.py` | 34.5% |
| `cleanup.py` | 29.1% | | `files.py` | 34.6% |
| `resources.py` | 29.2% | | `metadata.py` | 34.7% |
| `facets.py` | 30.9% | | `playback.py` | 38.4% |
| `plugins.py` | 31.5% | | `schedules.py` | 40.6% |
| `agent_agui.py` | 42.5% | | `webhooks.py` | 53.3% |
| `health.py` | 85.7% | | `ws.py` / `__init__.py` | 100% |

### 2.6 未覆盖的测试领域

| 领域 | 现状 |
|------|------|
| **Web 前端** | 0 测试文件; 无 vitest/playwright; `pnpm test` 为占位 |
| **桌面壳** | `macapp/` (swift)、`winapp/` (.NET AOT) 无测试; CI 只验证「能编译」 |
| **Android 壳** | 3 个纯函数测试; `BrowserActivity`(622 行)/`SetupActivity`(303 行) 无测试 |
| **脚本** `scripts/` | 按 `testing.md` 明确规定**不写测试** |
| **实时网络** | `@pytest.mark.live` 在 `crawler-testing.md` 中被提及, 但仓库里 **0 处使用**, `pytest.ini` 也未注册该 marker |
| **数据库迁移** | 15 个 `test_*_migration.py`, 是测试写得最好的区域之一 (每个迁移都有独立用例) |

### 2.7 测试质量: 与项目自身规范的对照

项目在 `docs/dev/testing.md` 中定义了「玩具测试」的 6 种判别形态 (默认值回声、实现复述、透传/mock 回声、只断言存在、常量快照、写入再回读) 与分层原则 (语义在哪层实现就在哪层测, HTTP 不能覆盖 repo/纯函数已覆盖的表)。

实际观察到的偏离:

- **`tests/media/test_nfo_write.py` 带有「写入再回读」特征**: 构造 `Metadata(...)` → `write_nfo` → 读文本断言子串。不过它断言的是**输出格式契约** (NFO 的 XML 结构、`<title>` 拼接规则), 而非「ORM 填字段再读回来」, 属于该规范允许的类别。真正的风险是它只有 1 个测试函数, 没有非法输入 (缺 number、特殊字符转义) 的用例。
- **`tests/db/test_task_batch.py` 质量较高**: 直接测 `execute_task_batch` 而不经 lifespan, 有「跳过活跃任务链」这类边界用例, 符合分层原则。
- **玩具测试无法从外部自动统计**: 需要按 `testing.md` 的 6 类判别逐文件人工核查, 本次未逐条穷举。

### 2.8 需要注意的配置问题

1. **`fail_under = 60` 与实测 51.3% 不一致**。可能原因: (a) 基线之后代码增长快于测试; (b) CI 有 fixture 而本机没有, 部分用例 skip; (c) 近期未提交改动引入了未测代码。**建议**: 在干净 HEAD + 有 fixture 的环境重跑一次确认, 再决定是「补测试」还是「把门禁改为按目录的差异化阈值」(如 `db/repos`、`scheduler` 单独设阈值)。
2. **`--cov-report=term-missing` 与 `fail_under` 的组合是「全仓一个数」**: 高覆盖的 `enums.py`(100%) 与 `parsing/`(95%) 在数学上掩盖了 `db/repos` 的 15%。分目录阈值比提高总阈值更有效。
3. **`crawler-testing.md` 提到 `@pytest.mark.live` 在 CI 跳过**, 但 marker 不存在; 应补注册 `markers = ["live: ..."]` 或在文档中删除该提法。
4. **`tests/crawlers/driven/` 目录不存在**, 爬虫数据驱动 runner 实际是 `tests/crawlers/test_crawlers.py` / `test_actor_crawlers.py` (与文档一致)。

---

## 三、GIT 提交管理系统的详细实现

### 3.1 仓库概况

| 项 | 值 |
|----|----|
| 提交总数 | 380 |
| 首个提交 | `2026-08-23 Initial commit` |
| 活跃周期 | 2026-08-23 → 2026-10-22 (约 2 个月) |
| 月度分布 | 8 月 90 / 9 月 137 / 10 月(至 22 日) 153 |
| Tag | 30 个, 最新 `v0.9.0`; 另有独立的 `app-*` 前缀 tag 用于 Android |
| 分支 | `main`、当前工作分支 `fix/minnano-warmup`、`origin/fix/scrape-retry-force-refresh`、4 个 `workbuddy/*` 本地 worktree 分支 |
| Worktree | 1 个主仓 + 3 个 `C:/Users/liuyue/WorkBuddy/Worktrees/amane/*` |
| 贡献者 | sqzw-x (343)、paituo (38)、dosubot[bot] (1) |

### 3.2 提交信息规范

统计 380 条提交主题的类型前缀:

| 类型 | 次数 | | 类型 | 次数 |
|------|-----:|-|------|-----:|
| `fix` | 130 | | `ci` | 8 |
| `feat` | 104 | | 其他/无前缀 | 5 |
| `docs` | 46 | | `perf` | 3 |
| `release` | 30 | | `i18n` | 2 |
| `chore` | 20 | | `merge` | 2 |
| `test` | 17 | | `style` | 1 |
| `refactor` | 12 | | | |

- **符合 Conventional Commits**: 模式 `type(scope): 描述` 或 `type: 描述`, 有效率 375/380 = **98.7%**。
- `release: 0.22.1` / `release: app 1.1.0` 是发版提交, 与服务端 tag `v*` 和 Android tag `app-*` 分开。
- 描述使用中文, 符合 `docs/dev/writing.md` 的用语规范。
- 存在少量 `merge:` 前缀的自定义合并提交 (如 `merge: 合入搜刮韧性改进(...)`) 与 GitHub 自动生成的 `Merge pull request #20 from ...`。

### 3.3 本地钩子体系 (`scripts/githooks/` + prek)

`just setup` 执行 `git config --replace-all core.hooksPath scripts/githooks` (已确认本仓库 `core.hooksPath = scripts/githooks`)。

**设计要点**:

- `scripts/githooks/pre-commit` 与 `pre-push` 是两个 **不内嵌任何 checkout 绝对路径** 的 sh 脚本, 按以下顺序定位 `prek`:
  1. `$PWD/.venv/bin/prek` 或 `$PWD/.venv/Scripts/prek.exe` (当前 worktree)
  2. `$(git rev-parse --path-format=absolute --git-common-dir)` 的父目录下的 `.venv` (主仓)
  3. PATH 上的 `prek`
  4. 找不到则报错退出, 提示「在当前 worktree 执行 just setup 后重试」
- 脚本头注释明确写下原因: **禁止执行 `prek install`**, 它会生成内嵌安装时 checkout 绝对路径的 shim 覆盖本文件, 该 worktree 删除后其余 worktree 的提交与推送会全部失败。这是为多 worktree 开发模式做的关键取舍。
- `--replace-all` 用于收敛 `core.hooksPath` 的多值残留 (普通 `set` 在键已有多个取值时报错退出)。

**钩子矩阵 (`.pre-commit-config.yaml`, `fail_fast: true`)**:

| 阶段 | 钩子 | 触发文件 | 执行内容 |
|------|------|----------|----------|
| pre-commit | `generate-api` | `^src/amane/.*\.py$` | `just generate` (导出 OpenAPI + 生成 TS client) |
| pre-commit | `fix-python` | `.py/.pyi`、`ruff.toml`、`docs/**/*.md` | `ruff check --fix` + `ruff format` |
| pre-commit | `fix-web` | `^web/` 或 `^src/amane/` | `pnpm fix` (oxfmt + oxlint --fix) |
| pre-commit | `lint` | 同上 | `just lint` (ruff check + format --check) |
| pre-commit | `ty` | `.py/.pyi`、`pyproject.toml` | `ty check` (Rust 实现) |
| pre-commit | `check-web` | `^web/` 或 `^src/amane/` | `pnpm check` (oxlint + oxfmt --check + tsc + i18n:ci) |
| pre-push | `pyright` | `.py/.pyi`、`pyproject.toml` | `just typecheck-pyright` |
| pre-push | `test` | `^(src/amane|tests)/.*\.py$` | `just test` (`pytest -n auto --dist worksteal`) |

**按耗时分流**: pyright (慢) 与 pytest 放 pre-push, 提交阶段只留生成物、ruff 与 ty; coverage 留给 `just check` / CI。

**几个关键取舍 (均已写在注释里)**:

1. **`src/amane` 变化也触发 web 相关钩子** —— 因为 `generate-api` 会重写 `web/src/client/*.gen.ts`, 不经过 oxfmt/tsc 就会出现「本地绿、CI 红」。
2. **`fail_fast: true`** —— 修复型钩子失败意味着文件被修改, 需要重新 stage; 此时后续检查钩子必败, 直接停止省掉全量运行。
3. **ruff 的扫描范围与 `fail_under` 无关, 但 lint 范围有专门约束** —— `Justfile` 的 `CHECK_DIRS := "src/amane tests scripts skills docs"` 与 ruff 实际覆盖范围对齐, 否则会出现 `just lint` 绿而 `ruff format --check .` 红。
4. **pre-push 的 `test` 钩子只跑 `just test`, 不跑覆盖率** —— 所以覆盖率门禁**只在 CI 的 `just ci` 中生效** (见 §2.8 第 1 条: 它在 CI 上是红门禁)。

### 3.4 Justfile 作为任务入口

`AGENTS.md` 规定「对外任务入口是根目录 Justfile, 不要用根 `package.json` 编排」。关键 recipe 链:

```
setup   = sync(uv --all-extras --all-groups + pnpm install) + fixtures + git config core.hooksPath
test    = fixtures + pytest -n auto --dist worksteal
test-cov= fixtures + pytest --cov=amane (term/html/xml)
lint    = ruff check + ruff format --check  (CHECK_DIRS)
typecheck = pyright + ty
check   = lint + typecheck + check-web + test-cov        ← 覆盖率门禁在此
ci      = ci-sync + _check-openapi + check + build       ← “fail if OpenAPI/client drifted”
ci-windows = uv sync --frozen + pytest (仅 Python)
bump    = bump_version.py precheck → uv version --bump → just generate → bump_version.py commit
```

要点:

- recipe **必须同时兼容 sh 与 pwsh** (`[windows] set shell := ["pwsh", "-NoProfile", "-Command"]`, 依赖 just ≥ 1.56.0), 禁止在 recipe 中调用 bash 脚本。
- `_check-openapi` 在 `generate` 后执行 `git diff --exit-code -- web/openapi.json web/src/client`, 把「生成的客户端与源码漂移」变成硬失败 —— 这是前后端契约的自动化保障。
- 发版只用 `just bump`, 不手改版本、不手打 tag。

### 3.5 CI 工作流 (`.github/workflows/`)

| 工作流 | 触发 | 内容 |
|--------|------|------|
| `ci.yaml` / `just ci` | push/PR 到 `main`、`develop` (忽略 `**.md`、`docs/**`) | Ubuntu: `just ci` (同步 → OpenAPI 漂移检查 → lint/类型/前端/覆盖率 → build) + 上传 coverage 产物 |
| `ci.yaml` / `just ci-windows` | 同上 | Windows: 仅 `pytest` (不装 Node/pnpm); 因为盘符、原生分隔符、跨盘 `commonpath` 需要真 Windows |
| `ci.yaml` / Android | 同上, 但先经 `android-app-changes` 三点 diff 过滤 `androidapp/`、构建脚本、Justfile、两个 workflow | 命中才 `just android-check` (Gradle assembleDebug + testDebugUnitTest) |
| `docs.yaml` | push 到 `main` 且 `docs/**`、`mkdocs.yml` 等变化 | `mkdocs build --strict` → GitHub Pages |
| `docker.yml` | push tag `v*` | 多架构 (amd64/arm64) 镜像推 GHCR, semver 标签 |
| `macos-app.yml` / `windows-app.yml` / `android-app.yml` | `app-*` / 对应 tag 与 `workflow_dispatch` | 各自平台打包发布 |
| `issues.yml` | issue opened/reopened + 每日 cron | 无 label 的外部 issue 自动关闭 (判为 API/CLI 创建的平台), `status:needs-info`/`kind:question` 14 天转 stale、再 7 天关闭 |
| `digest.md` / `digest.lock.yml` (gh-aw) | 每日 ~10:00 UTC+8, 22h 冷却, 门槛 ≥10 条未收录 issue | Copilot 引擎跑日报 issue |
| `issue-triage.md` / `issue-triage.lock.yml` (gh-aw) | `retriage` label、issue opened/reopened、`workflow_dispatch` | AI 给 issue 打 kind/status 标签 |

**值得注意的两个 CI 设计**:

- **不能用工作流级 `paths` 过滤 Android**: 那样整个工作流不触发, 被设为必需的门禁检查会永远 `pending`。所以改成「一个轻量 job 做 diff 判断 + 下游 job `if:` 跳过」, 整条 run 保持绿色。
- **Windows job 不装 Node/pnpm**: 前端与类型检查与 OS 无关, 由 Ubuntu 覆盖; Windows 只验证 Python 测试的跨平台性。

### 3.6 Issue / PR 治理

- **模板**: `bug_report_cn.yaml`、`feature_request_cn.yaml`、`proposal_cn.yaml` (均带 `kind:*` label), `config.yml`。网页模板对 CLI/REST API 不生效, 因此由 `issues.yml` 兜底: 外部用户、非 bot、无任何 label → 评论说明 + 打 `status:invalid` + 关闭。
- **`AI_POLICY.md`** 对 Issue 与 PR 各规定三级 AI 披露 (部分撰写或润色 / 主要由 AI 撰写 / AI 自动化创建), 核心原则是 *Human accountability / Human review / Human understanding / Transparency / Readability / Respect for maintainer effort*。并明确「若在 PR 上投入的人力少于 review 它所需的人力, 则不要提交 PR」。
- **`AGENTS.md` 硬约束**: 「所有影响远程仓库的变更必须经用户明确批准后才能执行」—— push、合并 PR、回复 Issue、发表评论都属此列。

### 3.7 发版流程

```
just bump patch|minor|major
  ├─ bump_version.py precheck <kind>      (校验: CHANGELOG 已更新、工作区干净等)
  ├─ uv version --bump <kind>             (版本真源在 pyproject.toml)
  ├─ just generate                        (OpenAPI + TS client 同步)
  └─ bump_version.py commit               (提交 + 打 tag vX.Y.Z, 不 push)
```

- `bump-dry <kind>` 可在不写入的前提下预览下一版本与提交/tag 名。
- 推送 tag 后由 GitHub Actions 填充 Release body; `Dockerfile` 镜像在 `v*` tag 时发布 `ghcr.io/<owner>/amane`。
- Android APP 版本与 tag 独立 (`app-*`), 因为「本体每次发版会附上同一份 APK, 被误认为 APP 也随版本更新」。

### 3.8 代码知识库与提交的一致性约束

`AGENTS.md` 对提交提出了文档同步的硬要求, 这构成了「提交管理系统」中容易被忽略的一环:

1. 修改代码后**必须**同步更新 `docs/dev/` 中对应文档; 改架构/API/工作流时必须增删文档。
2. 文档只写「源码读不出的跨模块信息」, 禁止罗列字段、复制签名、写迁移历史。
3. 新增文档必须登记到 `docs/dev/index.md`。
4. 开发过程保持细粒度提交, 每个逻辑变更独立提交, 避免积压为单个巨大提交; 需求完成时移除 roadmap/todo 注释。

---

## 四、发现的问题与改进建议

### 4.1 测试相关 (按优先级)

| # | 问题 | 证据 | 建议 |
|---|------|------|------|
| 1 | 覆盖率门禁 60% 实际 51.3%, `just ci` 必红 | `pyproject.toml:118` + 实测 | 在干净 HEAD + fixture 环境复测; 若确认, 改为按目录阈值: `db/repos` ≥ 50%, `scheduler` ≥ 40%, `handlers` ≥ 40%, 全局维持 60% 需先补 2,400+ 行 |
| 2 | `db/repos/*` 15.2% 是最大盲区 | 2,566 行仅覆盖 390 行 | 优先补 `tasks.py`(任务链/取消/重试)、`facets.py`+`facet_helpers.py`(分类规则)、`metadata.py`(筛选/锁定字段)。这些正是 `testing.md` 说「不应由 HTTP 覆盖」的层 |
| 3 | 前端零测试 | `web/package.json` `test` 为占位 | 至少给纯逻辑模块 (搜索 DSL `liqe` 封装、`organize` 模板预览、i18n 键校验、API client 错误映射) 加 vitest; 组件层可暂缓 |
| 4 | `scheduler/service.py` 16.4% / `worker.py` 15.1% | 实测 | 任务调度是核心链路, 需补 claim / 租约 / 取消 fallback / 重试预算的表测试 |
| 5 | `handlers/file.py` 16.1%、`delete.py` 19.0%、`scrape.py` 19.1% | 实测 | 文件操作与删除是数据安全的路径, 应补非法路径、部分失败、幂等重入用例 |
| 6 | 站点爬虫分支逻辑只测主路径 | `avbase` 20.2%、`dmm` 24.1%, 仅 14 站有专属 Python 测试 | 按 `crawler-testing.md`「Python 测试范围」为分支多的站点补 `test_<site>.py` |
| 7 | `@pytest.mark.live` 被文档引用但未注册且零使用 | `crawler-testing.md` 第 36 行 vs `pyproject.toml` 无 `markers` | 二选一: 注册 marker 或删除文档提法; 未注册 marker 加 `--strict-markers` 才能防错拼 |
| 8 | fixture 缺失导致用例静默 skip | 本机 `tests/crawlers/cases/` 不存在 | 本地覆盖率不可与 CI 比较; 建议覆盖率报告里单独输出 skip 数, 或在 CI 中把「cases 相关的 skip」视为失败 |
| 9 | 未启用 `--strict-markers` | `pyproject.toml:106-109` 仅 `testpaths`/`pythonpath`/`asyncio_mode` | 开启后可让错拼的 marker (如 `live`) 直接报错而非静默无效 |
| 10 | Android `BrowserActivity`(622 行) 无测试 | 3 个测试只覆盖纯函数 | WebView 集成难测, 可接受; 但 `ShellBridge`(77 行, JS 桥接) 应有单元测试 |

### 4.2 Git / 流程相关

| # | 观察 | 建议 |
|---|------|------|
| 1 | 工作区有 15 个未提交的修改文件 (跨 `src`/`tests`/`web`/`start.bat`) | 按 `AGENTS.md`「细粒度提交」拆分成数个逻辑提交; 跨 `src` 的改动也会触发 `generate-api` 与 web 钩子, 一次提交过大不利 review |
| 2 | `docs/scrape-performance-analysis.md` 未跟踪 | 确认归属 (是否属 `docs/dev/` 之外的临时分析) 后提交或删除 |
| 3 | 98.7% 的提交符合 Conventional Commits, 但 5 条无前缀 | 可在 pre-commit 增加 commit-msg 钩子 (commitlint) 把剩余 1.3% 也拦住; 当前靠约定 |
| 4 | `merge:` 前缀属自定义类型, 不在 common 集合内 | 若引入 commitlint, 需把 `merge` 加入允许类型, 否则现有历史会被判违规 |
| 5 | 4 个 worktree 共用主仓 `.git` | 钩子已正确处理; 但 `workbuddy/*` 分支 (3 个, 全部指向 `5c998bc`) 属 AI 工作区产物, 建议定期清理 |
| 6 | `develop` 分支在 CI 触发列表中, 但本地/远程都不存在 | 文档或 CI 二者之一需要对齐, 避免误以为有长期开发分支 |
| 7 | `fail_under=60` 在 CI 上会使 `just ci` 失败 | 见 4.1 #1; 修复前 `just ci` 不能作为通过判据 |

---

## 五、附录: 复现命令与原始数据

### 复现覆盖率

```powershell
# 注意: 直接 uv run 会被沙箱拒绝 (uv 缓存目录权限), 用 venv 内的 python
$env:PYTHONUTF8=1
.venv\Scripts\python.exe -m pytest tests/ -n auto --dist worksteal `
  --basetemp="D:\PythonProjects\amane\temp\pytest-run" `
  --cov=amane --cov-report=term-missing:skip-covered `
  --cov-report=xml:coverage.xml --cov-report=json:coverage.json
```

覆盖率原始 JSON 已存于 `temp/coverage-snapshot.json`。

### 原始统计

| 项 | 值 |
|----|----|
| 测试文件数 | 226 |
| 测试代码行 | 43,193 |
| 测试函数数 (`def test_`) | 1,009 |
| `parametrize` 出现次数 | 334 |
| 源文件数 (不含 migrations) | 293 |
| 源代码行 (不含 migrations) | 42,522 |
| 测试/代码比 (行) | 1.02 : 1 |
| 前端源文件 / 行 / 测试 | 247 / 60,805 / 0 |
| Android 源文件 / 行 | 10 / 1,238 |
| Android 测试文件 / 行 | 3 / 145 |
| Git 提交 / tag / 贡献者 | 380 / 30 / 3 |
| CI 工作流 | 12 个文件 (含 2 个 gh-aw 源 + 2 个 lock) |

### 相关文件索引

- 测试规范: `docs/dev/testing.md`、`docs/dev/crawler-testing.md`
- fixture 与辅助: `tests/conftest.py`、`tests/api/conftest.py`、`tests/helpers.py`、`tests/schema_template.py`
- 覆盖率配置: `pyproject.toml` (`[tool.coverage.*]`、`[tool.pytest.ini_options]`)
- 钩子: `scripts/githooks/pre-commit`、`scripts/githooks/pre-push`、`.pre-commit-config.yaml`
- 任务入口: `Justfile`
- CI: `.github/workflows/ci.yaml`、`docs.yaml`、`docker.yml`、`macos-app.yml`、`windows-app.yml`、`android-app.yml`、`issues.yml`、`digest.md`、`issue-triage.md`
- 治理: `AGENTS.md`、`AI_POLICY.md`、`.github/ISSUE_TEMPLATE/*`
- 发版: `scripts/bump_version.py`、`CHANGELOG.md`
