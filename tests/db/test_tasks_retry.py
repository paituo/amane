"""任务仓储: 退避重排的认领过滤与状态写入."""

from datetime import UTC, datetime, timedelta

import pytest

from amane.db.models import TaskStatus, TaskType
from amane.db.repository import Repository


async def _queue_deferred(repo: Repository, *, retry_in_s: float, payload: dict | None = None):
    """建一个任务 → 认领 → 退避重排, 返回 (task_id, retry_at)."""
    task = await repo.create_task(TaskType.SCRAPE, payload=payload or {"number": "DEFER-1"}, priority=5)
    assert task.id is not None
    claimed = await repo.claim_next_task()
    assert claimed is not None and claimed.id == task.id
    retry_at = datetime.now(UTC) + timedelta(seconds=retry_in_s)
    deferred = await repo.defer_task(
        task.id, error="未找到 DEFER-1 的元数据", result={"outcomes": []}, retry_at=retry_at
    )
    assert deferred is True
    return task.id, retry_at


@pytest.mark.asyncio(loop_scope="function")
async def test_defer_returns_task_to_queue_with_backoff(repo: Repository) -> None:
    """退避重排把行退回 QUEUED, 保留失败事实, 清掉本次运行的开始时间."""
    task_id, retry_at = await _queue_deferred(repo, retry_in_s=180)

    fetched = await repo.get_task(task_id)
    assert fetched is not None
    assert fetched.status == TaskStatus.QUEUED
    assert fetched.retries == 1
    assert fetched.retry_at is not None
    # SQLite 读回的是 naive UTC; 只比较到秒 (列精度为微秒, 回读会丢掉时区信息).
    assert fetched.retry_at.replace(tzinfo=UTC).replace(microsecond=0) == retry_at.replace(microsecond=0)
    assert fetched.error == "未找到 DEFER-1 的元数据"
    assert fetched.result == {"outcomes": []}
    assert fetched.started_at is None
    assert fetched.finished_at is not None


@pytest.mark.asyncio(loop_scope="function")
async def test_claim_skips_task_waiting_for_retry(repo: Repository) -> None:
    """退避未到点的行不参与认领, 也不阻塞排在它后面的任务."""
    deferred_id, _ = await _queue_deferred(repo, retry_in_s=300)
    ready = await repo.create_task(TaskType.SCRAPE, payload={"number": "READY-1"}, priority=1)
    assert ready.id is not None

    claimed = await repo.claim_next_task()
    assert claimed is not None
    assert claimed.id == ready.id, "高优先级的退避任务不应被提前认领"

    # 队列里只剩退避中的那一行 → 认领返回 None, 而不是把它拉起来.
    assert await repo.claim_next_task() is None
    still = await repo.get_task(deferred_id)
    assert still is not None
    assert still.status == TaskStatus.QUEUED


@pytest.mark.asyncio(loop_scope="function")
async def test_claim_takes_task_once_retry_at_passed(repo: Repository) -> None:
    """到点后重新参与认领, 排序仍按优先级与创建时间."""
    task_id, _ = await _queue_deferred(repo, retry_in_s=-1)

    claimed = await repo.claim_next_task()
    assert claimed is not None
    assert claimed.id == task_id
    assert claimed.status == TaskStatus.RUNNING
    assert claimed.retries == 1


@pytest.mark.asyncio(loop_scope="function")
async def test_defer_ignores_task_not_running(repo: Repository) -> None:
    """只对 RUNNING 生效: 终态行不得被重排覆盖."""
    task = await repo.create_task(TaskType.SCRAPE, payload={"number": "QUEUED-1"})
    assert task.id is not None
    ok = await repo.defer_task(task.id, error="x", result=None, retry_at=datetime.now(UTC) + timedelta(seconds=60))
    assert ok is False

    await repo.fail_task(task.id, error="terminal")
    ok_after_terminal = await repo.defer_task(
        task.id, error="x", result=None, retry_at=datetime.now(UTC) + timedelta(seconds=60)
    )
    assert ok_after_terminal is False
    fetched = await repo.get_task(task.id)
    assert fetched is not None
    assert fetched.status == TaskStatus.FAILED


@pytest.mark.asyncio(loop_scope="function")
async def test_defer_can_rewrite_payload(repo: Repository) -> None:
    """重排可改写载荷 (丢弃本次已失效的批次级参数)."""
    task_id, _ = await _queue_deferred(
        repo, retry_in_s=-1, payload={"number": "DEFER-2", "disabled_sources": ["official"]}
    )
    # 该行此刻是 QUEUED: 先到点认领, 再改写载荷退避.
    claimed = await repo.claim_next_task()
    assert claimed is not None and claimed.id == task_id
    ok = await repo.defer_task(
        task_id,
        error="all skipped",
        result=None,
        retry_at=datetime.now(UTC) + timedelta(seconds=60),
        payload={"number": "DEFER-2", "disabled_sources": []},
    )
    assert ok is True
    fetched = await repo.get_task(task_id)
    assert fetched is not None
    assert fetched.payload == {"number": "DEFER-2", "disabled_sources": []}
    assert fetched.retries == 2


@pytest.mark.asyncio(loop_scope="function")
async def test_defer_missing_task(repo: Repository) -> None:
    retry_at = datetime.now(UTC) + timedelta(seconds=60)
    ok = await repo.defer_task(10_000, error="x", result=None, retry_at=retry_at)
    assert ok is False
