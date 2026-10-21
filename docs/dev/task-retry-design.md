# 任务失败自动重排设计方案

> 范围：仅处理「环境性失败」的自动重排。业务性失败（查无此片、解析不符、配置缺失）保持终态 `FAILED`。

状态：**已实现**（组 1–5 与 `retry_at` 迁移已落地）。第 7 节的存量补救脚本尚未执行；落地时与草案的三处差异见 5.8。

---

## 1. 问题与实测数据

10-21 13:55–17:41 的批量刮削里，333 个番号以「未找到 X 的元数据」落库为终态 `FAILED`，实际原因是连接健康熔断器开路后请求被短路，任务没有发出真实请求。归因缺陷已在上一轮修复（`net/http.py` 的 `_PROXY_BLAME_CURL_CODES` 分流 + 非 2xx 复位）。本轮处理剩余问题：**即便归因正确，被短路的任务仍然会被记为终态失败，且不会自动重来**。

库内实测快照（`data/amane.db`，2026-10-22 02:39:41；当时仍有 121 条 `SCRAPE` 排队、3 条运行中，失败数随运行持续增长，下表为单一时点数值）：

| 项目 | 数值 |
| --- | --- |
| `SCRAPE` 失败任务 | 399 |
| 按第 3 节规则判定为可重试 | 393（98.5%） |
| 判定为不可重试 | 6（全部站点 `no_usable_metadata`） |
| 失败番号去重 | 333 |
| 失败任务涉及媒体文件 | 333，其中仍为 `PENDING` 的 186，已 `SCRAPED` 的 147 |
| 可重试失败且媒体文件仍 `PENDING` 的任务 | 247（对应 183 个媒体文件，64 个文件存在多条失败任务） |
| 失败任务 `retries` 分布 | 全部为 1 |
| 失败耗时分布（`started_at` → `finished_at`） | `<1s` 14 条，`1–20s` 372 条，`20–120s` 13 条 |

按站点 outcome 与 reason 的精确组合（共 8 种）：

| 条数 | 组合 |
| --- | --- |
| 294 | `failed/circuit_open` |
| 90 | `failed/circuit_open` + `failed/no_usable_metadata` |
| 6 | `failed/no_usable_metadata` |
| 3 | `failed/circuit_open` + `failed/network` + `failed/server_error` |
| 2 | `failed/circuit_open` + `failed/server_error` |
| 2 | `failed/circuit_open` + `failed/no_usable_metadata` + `failed/server_error` |
| 1 | `failed/circuit_open` + `failed/network` + `failed/no_usable_metadata` |
| 1 | `failed/circuit_open` + `failed/network` |

结论：**98.5%（393/399）的失败含至少一个环境性原因**，其中 294 条的失败原因**只有** `circuit_open`，即该任务的每个站点都是被熔断短路、没有取得任何站点结论。当前的失败率不能用于判断站点覆盖能力。

## 2. 现状机制（代码依据）

| 事实 | 位置 |
| --- | --- |
| `retries` 只做计数，不参与任何重排判定 | `db/repos/tasks.py:260`、`:277`、`:449`（均为 `retries += 1`） |
| 任务失败即终态，无自动重来 | `scheduler/worker.py:356-366` 直接调用 `repo.fail_task` |
| 唯一的重排入口是人工重试（克隆为无根裸任务） | `db/repos/tasks.py:456` `retry_tasks`；`api/support/task_batch.py:137` |
| `tasks` 表无任何「可执行时间」列 | `db/models.py:334-349`（仅 `created_at` / `started_at` / `finished_at`） |
| 认领只按 `status = QUEUED` + `priority desc, created_at asc` | `db/repos/tasks.py:182-200` |
| 刮削失败不修改媒体文件状态，仍为 `PENDING` | 全仓无 `MediaFileStatus.FAILED` 写入点；`handlers/scrape.py` 失败路径不触碰 `media_files` |
| 站点级失败原因已逐条落库（含 `circuit_open`） | `observability/source.py:15-29` 写 `SiteOutcomeRecord`；`handlers/scrape.py:159-170` 装入 `TaskResult.result` |
| 存在「可执行时间」的同类先例 | `Feed.next_fetch_at`（`db/models.py:461`）+ `list_due_feeds`（`db/repos/feeds.py:58-69`） |
| 进程重启把全部 `RUNNING` 标为 `FAILED` | `db/repos/tasks.py:283` `fail_all_running_tasks` |

关键缺口：没有「可执行时间」列，因此**任何重排都必须解决退避问题**，否则任务被立即重新认领并在熔断开路期内空转。

## 3. 判定规则

### 3.1 原因分类

依据 `FailureReason`（`net/errors.py:37-67`）：

| 可重试（环境性） | 不可重试（业务性/配置性） |
| --- | --- |
| `circuit_open`（未触网，熔断短路） | `not_found` |
| `network`（连接层失败） | `no_usable_metadata`（站点可达但无数据） |
| `timeout` | `parse_error`（站点结构变更） |
| `server_error`（5xx） | `api_error` |
| `http_error`（其余 4xx/5xx） | `login_required` |
| `rate_limited`（429） | `geo_restricted` |
| `empty_response`（空应答，多为拦截） | `ip_banned` / `cloudflare_challenge` / `cloudflare_blocked` / `age_verification` |
| | `crawler_unavailable`（配置或插件缺失） |
| | `unexpected`（未归类异常） |

`cloudflare_*` / `ip_banned` 归入不可重试：它们需要改配置或换出口，重试不改变结果。此项列为待决（见 9.4）。

### 3.2 聚合规则

对一次刮削任务的 `result.outcomes`：

1. 无 `outcomes`（参数校验失败、无可用来源、处理器缺失）→ 不可重试。
2. 存在 `outcome == failed` 且 `reason` 属于可重试集合 → **可重试**。
3. 全部 `outcome == skipped`（批次可用性探测把来源全部判为不可用，一个请求都没发）→ **可重试**。理由：这是「环境未验证」而非「确认无数据」。退避用常规档位，不单独取上限：重排时 `disabled_sources` 会被丢弃（5.8），下次运行真正发请求；首次退避 180s 与可用性缓存 TTL 同量级，到点时探测结论通常已刷新。
4. 其余（含全部 `failed` 且原因均不可重试）→ 不可重试。

规则 3 必须显式排除 `skipped`：`handlers/scrape.py:93-100` 把 `disabled_sources` 记为 `SKIPPED + CIRCUIT_OPEN`，若这类也计入可重试集合会造成长期空转。

### 3.3 预算与退避

- 计数沿用现有 `retries`（语义由「失败次数」变更为「自动重排次数」，见 6.5）。
- 上限 `retry_max_attempts = 8`。
- 退避：`delay(n) = min(retry_base_delay_s × 2^(n-1), retry_max_delay_s) × jitter`，`jitter ∈ [0.8, 1.2]`。
- 取值 `base = 180s`（必须大于代理熔断冷却 120s）、`max = 3600s`。八次累计覆盖约 **4.5 小时**，与实测故障窗口（约 4 小时）匹配。
- 在 `retry_at` 到点前，任务不参与认领（3.2 的规则不产生热循环）。

## 4. 候选方案对比

| 维度 | A：新增状态 `DEFERRED` | B：复用 `QUEUED` + `retry_at`（推荐） | C：保留 `FAILED` + 后台扫描重排 |
| --- | --- | --- | --- |
| 语义清晰度 | 最好，「待重试」与「排队中」分离 | 中，靠 `retry_at` 与 `retries` 区分 | 最差，失败与待重试混在同一状态 |
| 表结构变更 | 需加 `retry_at` | 需加 `retry_at` | 不需（复用 `result` JSON + `finished_at`） |
| 状态枚举变更 | 需要（后端 + 前端 + API + i18n） | 不需要 | 不需要 |
| 需后台循环 | 不需要 | 不需要 | 需要（新增周期扫描，重启后行为需额外验证） |
| 失败明细可观测 | 好 | 好（`error` / `result` 保留，`retry_at` 暴露） | 好 |
| 同番号重复行 | 无（原地重排） | 无（原地重排） | 有（克隆新行，与手动重试叠加） |
| 影响文件数（估） | 18 | 12 | 8 |
| 与并行改动冲突 | 高（`exhaustive-maps.ts` / `tasks.json` 正在被并行修改） | 低 | 低 |
| 主要风险 | 前端枚举遗漏即编译/运行报错 | UI 上「排队中」的语义不够直白 | 扫描循环与克隆重排的幂等性 |

方案 C 的克隆语义还会与现有手动重试冲突：`_EXCLUSIVE_FIELDS`（`db/repos/tasks.py:18`）只对 `ACTOR_SCRAPE` 去重，`SCRAPE` 每次新建行，自动克隆会使同一番号在列表中出现多行。

**推荐 B**：退避所需的列本来就得加，加列之后 B 与 A 的后端成本几乎相同，而 B 省掉状态枚举的全部前端与 API 改动；可与 3.2 的规则 3、与取消语义（`QUEUED` 已被批量取消覆盖）直接兼容。A 作为语义升级路径保留：若后续需要独立的「待重试」筛选项，可在 B 之上只加状态与前端映射。

## 5. 推荐方案细则

### 5.1 数据模型与迁移

`db/models.py`（`Task`）：

```python
retry_at: datetime | None = None
"""下次允许认领的时间; None 表示立即可认领. 由 defer 写入, 认领时过滤."""
```

迁移 `db/migrations/versions/<rev>_task_retry_at.py`：

```python
def upgrade() -> None:
    op.add_column("tasks", sa.Column("retry_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.drop_column("retry_at")
```

不加索引：认领已由 `status` 索引缩小到待执行集，`retry_at` 只在该集合内做行级过滤。与 `Feed.next_fetch_at` 的既有做法一致（该列亦无索引）。

### 5.2 认领过滤

`db/repos/tasks.py::claim_next_task`：

```python
now = _utcnow()
stmt = (
    select(Task)
    .where(
        col(Task.status) == TaskStatus.QUEUED,
        (col(Task.retry_at).is_(None)) | (col(Task.retry_at) <= now),
    )
    .order_by(col(Task.priority).desc(), asc(col(Task.created_at)))
    .limit(1)
)
```

排序不变，因此重排任务不会插队到新任务之前。此处沿用 `list_due_feeds` 的写法（`db/repos/feeds.py:64`），并需在测试中验证 SQLite 的时间比较与 `_utcnow()` 写入格式一致。

### 5.3 重排落库

新增 `db/repos/tasks.py::defer_task`，与 `fail_running_task` 同样做状态条件写（仅 `RUNNING` 生效，避免覆盖已终态）：

```python
async def defer_task(self, task_id: int, *, error: str, result: dict | None, retry_at: datetime) -> bool:
    task.status = TaskStatus.QUEUED
    task.retries += 1
    task.error = error
    task.result = result
    task.retry_at = retry_at
    task.started_at = None
    task.finished_at = _utcnow()
```

保留 `error` 与 `result`：`retry_at` 到点前，详情面板据此显示「上次失败原因」，不依赖日志文件。

### 5.4 重试判定与策略

`handlers/protocol.py`：`TaskResult` 增加 `retryable: bool = False`。让 handler 判定而非 worker 解析 `result` JSON，避免调度器耦合刮削结果的字段结构。

新增 `src/amane/handlers/retry_policy.py`：

```python
RETRYABLE_REASONS: frozenset[FailureReason] = frozenset({...})  # 见 3.1 左列


def is_environmental_failure(outcomes: Sequence[SiteOutcomeRecord]) -> bool:
    """按 3.2 的规则判定; 空集合返回 False."""
```

`handlers/scrape.py`：仅「站点请求已执行但未取得数据」的分支（`:159-170`）设置 `retryable=is_environmental_failure(current().site_outcomes())`。「内容类型没有可用来源」（`:87`）与「番号没有可用来源」（`:105`）属于配置问题，保持 `retryable=False`。

新增 `src/amane/scheduler/retry_budget.py`：`RetryBudget` 从热配置读取预算与退避参数，提供 `should_defer(result, task)`（判定 `result.retryable` 且 `task.retries < retry_max_attempts`）与 `delay_for(attempt)`（含抖动）。worker 复用既有的 `self._get_hot()` 取值（与 `_debug_capture` 相同的空值兜底），不新增构造参数。

### 5.5 Worker 失败分支

`scheduler/worker.py:356-366`：

```python
else:
    err = result.error or "Unknown error"
    budget = RetryBudget.from_config(self._get_hot() if self._get_hot is not None else None)
    delay = budget.delay_for(task.retries) if budget.should_defer(result, task) else None
    if delay is None:
        await self._repo.fail_task(task_id, error=err, result=result.as_dict())
        emit(TASK_FAILED, {"task_id": ..., "type": ..., "error": err})
    else:
        retry_at = datetime.now(UTC) + timedelta(seconds=delay)
        await self._repo.defer_task(
            task_id, error=err, result=result.as_dict(), retry_at=retry_at,
            payload=handler.retry_payload(typed_payload),   # 可丢弃本次已失效的载荷字段
        )
        # 复用 task.failed 事件并在 data 内标注, 避免新增前端事件分支 (web/src/lib/connection.ts 的联合类型)
        emit(TASK_FAILED, {"task_id": ..., "type": ..., "error": err, "deferred": True, "retry_in_s": round(delay)})
```

`_finalize_recorder` 两支都要调用，重排支的 `success` 传 `False`（本次运行按失败记录）。`tasks.error` 只保存 handler 给出的原文，不追加「将于 N 秒后重试」这类随时间变化的信息，便于按错误文本聚合统计；重试进度由 `retries` 与 `retry_at` 表达。

### 5.6 配置项

`config/manager.py::WorkerConfig` 新增三项（均 `x-hidden`）：

| 字段 | 默认 | 约束 | 含义 |
| --- | --- | --- | --- |
| `retry_max_attempts` | 8 | 0–20 | 0 关闭自动重排 |
| `retry_base_delay_s` | 180 | 30.0–3600.0 | 首次重排延迟 |
| `retry_max_delay_s` | 3600 | 60.0–86400.0 | 退避上限 |

`x-hidden` 字段同样需要 `zh-CN` / `en` 的 `fields.worker.*` 文案（`web/src/i18n/check/setting-schema.ts` 为编译期强制断言）。

### 5.7 可观测性

- API：`TaskListItem`（列表与详情共用）增加 `retry_at`（`api/models/tasks.py`），已有 `retries` 的语义改为「已自动重排次数」。
- 前端：列表状态徽标旁加「待重试」小徽标（悬停显示到点时间），详情面板同样加徽标并单列「下次重试」；判定收敛在 `lib/task/display.ts::retryAtOf`（要求 `queued` 且 `retry_at` 非空）。
- 日志：`task deferred` 事件带 `retry_in_s`、`attempt`、`retry_at`，便于按时间统计重排分布。
- 不新增 `EventType`：`web/src/lib/connection.ts` 的事件名是联合类型，新增需同步三处；`task.failed` + `deferred` 标注即可让列表失效刷新。
- 契约生成：`TaskListItem` 与 `WorkerConfig` 的变更会进入 `web/openapi.json`，须重跑 `scripts/export_openapi.py` 与 `pnpm gen-client`（CI 会比对生成物，缺失即失败）。

### 5.8 落地时的三处偏差

1. **规则 3 不单独取退避上限**：改为常规档位。原设想是「全部跳过说明环境很糟，直接等到最长间隔」，但 `disabled_sources` 已在重排时被丢弃（下条），下次运行会真正发请求；若站点仍不可用，请求级熔断会快速失败并再次进入重排，按常规退避反而更快恢复。同时省掉「按失败形态区分档位」的第二处判定分支。
2. **新增 `TaskHandler.retry_payload` 钩子（草案未含）**：这是规则 3 成立的前提。`ScrapePayload.disabled_sources` 是扇出那一刻的可用性探测结论并落在 payload 里，若不改写，重排会逐字复现「全部跳过」，规则 3 就退化为纯空转。钩子默认返回 `None`（沿用原载荷），`ScrapeHandler` 覆写为清空 `disabled_sources`。
3. **前端标记位置**：列表在状态徽标旁加「待重试」小徽标（悬停显示到点时间），详情面板在状态徽标旁加同一徽标并单列「下次重试」。草案里的「第 n 次重试」并入既有的 `detail.retries` 字段，不新增重复计数展示。

## 6. 不变量与风险

1. **取消不重排**：`cancel_task` 走 `fail_running_task(CANCEL_ERROR)`（`worker.py:227`、`:311`），不经 `defer_task`。
2. **崩溃不重排**：`except Exception` 分支保持 `fail_task`。未归类异常一次暴露比反复重试更有价值。
3. **状态条件写**：`defer_task` 只对 `RUNNING` 生效，与 `fail_running_task` 同构，避免与终态写入竞争。
4. **重启清扫不改**：`fail_all_running_tasks` 仍把 `RUNNING` 置 `FAILED`，不重排（列为待决 9.2）。
5. **`retries` 语义变更**：由「失败次数」变为「自动重排次数」。影响面：详情面板文案、任何按 `retries` 判断的运维查询、既有 399 条历史数据（值仍为 1，对重排无影响，补救脚本会把参与重排的行重置为 0）。
6. **手动重试不变**：`retry_tasks` 仍克隆。自动重排原地复用，两者语义不同，文档需在 `task-system.md` 说明。
7. **队列饥饿**：新任务优先级不变，重排任务因 `retry_at` 延后，不会挤占新任务。
8. **批量涌入**：抖动 ±20% 使 393 条重排不会在同一秒到点；即便同时到点，worker 并发上限（当前 `worker.concurrency = 4`）与熔断短路共同限制实际开销，单次短路失败的耗时在毫秒级（快照中 14 条全短路任务整体耗时 < 1s）。
9. **可用性探测与重排叠加**：`SourceAvailability` 缓存 TTL 180s，与首次退避 180s 同量级，重排到点时探测结论通常已刷新，不会重复使用过期死站结论。

## 7. 存量补救

自动重排只对落地之后产生的失败生效；库里已落终态的那批由 `scripts/requeue_failed_scrapes.py` 处理，判定规则与运行时同源（`handlers/retry_policy.py`）：

- 默认预演，打印待重排数量与失败形态统计，不写库；`--apply` 才写。
- 同一 `media_file_id`（无文件时按番号）只重排**最新一条**失败任务，其余保持 `FAILED`。
- 写入 `status = QUEUED`、`retries = 0`、`retry_at = now + uniform(0, 600s)`，`error` / `result` 保留；单事务提交。
- 媒体文件已不是 `PENDING` 的行直接排除（这些番号后来已刮削成功，重跑没有收益）。

2026-10-22 03:26 预演（当时代码仍在跑，失败数已增长到 359 条）：可重排 82 条，其中 86 行形态为纯 `circuit_open`、61 行为 `circuit_open + no_usable_metadata`；排除 263 条（媒体文件已不是 `PENDING`）与 11 条（原因不可重排）。该脚本尚未在正式库执行。

## 8. 测试计划

| 层次 | 用例 |
| --- | --- |
| 判定 | `circuit_open` 单独、`circuit_open + no_usable_metadata`、纯 `no_usable_metadata`、全部 `skipped`、`outcomes` 为空、`server_error`/`rate_limited`（`tests/handlers/test_scrape_retry_policy.py`） |
| Worker | 可重试失败写 `retry_at` 且状态回 `QUEUED`；不可重试走 `FAILED`；达到上限后 `FAILED`；取消与崩溃两条路径不写 `retry_at`（`tests/scheduler/test_worker_retry.py`） |
| 仓储 | `claim_next_task` 跳过 `retry_at` 未到点的行；`defer_task` 对非 `RUNNING` 返回 `False`；`retries` 递增（`tests/db/test_tasks_repo.py`） |
| 迁移 | 由既有库升级：`retry_at` 列为 NULL 可读可写，`pre-migrate` 备份生成（`sqlite_migrate` 既有机制） |
| API | `TaskRead` 暴露 `retry_at`；`GET /tasks?status=queued` 含重排任务 |
| 回归 | `just ci-sync` 后跑 `tests/handlers`、`tests/scheduler`、`tests/db`、`tests/api`；`ruff check` / `ruff format` / `ty check`；改动前端后 `pnpm check` |
| 实验 | 手工把代理改为不可达地址，提交 5 个 `SCRAPE`，验证：失败后 180s 内不再被认领、日志出现 `task deferred`、退避按 180/360/720 递增、上限后落 `FAILED` |

## 9. 待决问题

已定：预算取 8 次 / 180s–3600s（问题 1）；重启清扫本轮不重排（问题 2）；`ACTOR_SCRAPE` 本轮不接入（问题 3）；`cloudflare_*` / `ip_banned` 维持不可重试（问题 4）；存量补救脚本待单独执行（问题 5）。

## 10. 实施拆分

| 组 | 内容 | 依赖 | 状态 |
| --- | --- | --- | --- |
| 1 | `Task.retry_at` + 迁移 + `claim_next_task` 过滤 + `defer_task` | 无 | 已完成 |
| 2 | `handlers/retry_policy.py`（原因判定）+ `scheduler/retry_budget.py`（预算与退避）+ `TaskResult.retryable` + `retry_payload` 钩子 + `scrape.py` 接线 | 组 1 | 已完成 |
| 3 | worker 失败分支 + 配置项 + i18n 文案 | 组 2 | 已完成 |
| 4 | API `retry_at` + 前端标记与文案 + 契约生成物 | 组 3 | 已完成 |
| 5 | 测试补齐、`task-system.md` 与本文章节 | 组 4 | 已完成 |
| 6 | 存量补救脚本（独立） | 组 1 | 未开始 |

测试落点：`tests/handlers/test_scrape_retry_policy.py`（判定规则）、`tests/scheduler/test_worker_retry.py`（worker 分支与退避序列）、`tests/db/test_tasks_retry.py`（认领过滤与条件写）、`tests/db/test_task_retry_at_migration.py`（迁移升降级）、`tests/api/test_tasks.py::TestRetryAtExposure`（契约暴露）、`tests/handlers/test_scraper.py::TestScrapeRetryable`（handler 接线）。

组 4 的契约生成物（`web/openapi.json`、`web/src/client`）会连带其它未提交改动的 schema 差异，提交时需按组拆分。
