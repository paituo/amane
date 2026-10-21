"""worker 失败分支: 环境性失败退避重排, 其余照旧落终态."""

import asyncio
from contextlib import suppress
from datetime import UTC, datetime

import pytest

from amane.config import HotSettings, WorkerConfig
from amane.db.models import TaskStatus, TaskType
from amane.db.repository import Repository
from amane.handlers.protocol import TaskHandler, TaskResult
from amane.scheduler.retry_budget import RetryBudget
from amane.scheduler.worker import CANCEL_ERROR, AsyncWorker
from tests.helpers import LOAD_TOLERANT_TIMEOUT


class RetryableFailHandler(TaskHandler):
    payload_type = dict

    def __init__(self, *, patched: dict | None = None) -> None:
        self.patched = patched
        self.calls = 0

    async def handle(self, payload: dict) -> TaskResult[dict]:
        self.calls += 1
        return TaskResult(success=False, error="未找到 TEST-1 的元数据", retryable=True)

    def retry_payload(self, payload: dict) -> dict | None:
        return self.patched


class TerminalFailHandler(TaskHandler):
    payload_type = dict

    def __init__(self) -> None:
        pass

    async def handle(self, payload: dict) -> TaskResult[dict]:
        return TaskResult(success=False, error="未找到 TEST-1 的元数据")


class CrashHandler(TaskHandler):
    payload_type = dict

    def __init__(self) -> None:
        pass

    async def handle(self, payload: dict) -> TaskResult[dict]:
        raise RuntimeError("boom")


class BlockingHandler(TaskHandler):
    payload_type = dict

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def handle(self, payload: dict) -> TaskResult[dict]:
        self.started.set()
        await self.release.wait()
        return TaskResult(success=True)


def _worker(repo: Repository, handler: TaskHandler, hot: HotSettings) -> AsyncWorker:
    return AsyncWorker(repo=repo, handlers={TaskType.SCRAPE: handler}, get_hot=lambda: hot)


async def _run_claimed(repo: Repository, worker: AsyncWorker, payload: dict | None = None):
    """建任务 → 认领 → 执行一次, 返回 task_id."""
    task = await repo.create_task(TaskType.SCRAPE, payload=payload or {"number": "TEST-1"})
    assert task.id is not None
    claimed = await repo.claim_next_task()
    assert claimed is not None and claimed.id == task.id
    await worker._execute(claimed)
    return task.id


@pytest.mark.asyncio(loop_scope="function")
async def test_retryable_failure_returns_task_to_queue(repo: Repository) -> None:
    """环境性失败退回队列并写入 retry_at, 不再落 FAILED."""
    worker = _worker(repo, RetryableFailHandler(), HotSettings())
    before = datetime.now(UTC)

    task_id = await _run_claimed(repo, worker)

    fetched = await repo.get_task(task_id)
    assert fetched is not None
    assert fetched.status == TaskStatus.QUEUED
    assert fetched.retries == 1
    assert fetched.retry_at is not None
    # 首次退避 180s, 抖动 ±20%.
    waited = (fetched.retry_at.replace(tzinfo=UTC) - before).total_seconds()
    assert 140 <= waited <= 225, waited
    assert fetched.error == "未找到 TEST-1 的元数据", "错误文本应保留 handler 原文, 不追加重试说明"


@pytest.mark.asyncio(loop_scope="function")
async def test_terminal_failure_still_fails(repo: Repository) -> None:
    """业务性失败 (站点可达但无数据) 保持终态, 不写 retry_at."""
    worker = _worker(repo, TerminalFailHandler(), HotSettings())

    task_id = await _run_claimed(repo, worker)

    fetched = await repo.get_task(task_id)
    assert fetched is not None
    assert fetched.status == TaskStatus.FAILED
    assert fetched.retry_at is None
    assert fetched.retries == 1


@pytest.mark.asyncio(loop_scope="function")
async def test_auto_retry_disabled_by_config(repo: Repository) -> None:
    """retry_max_attempts=0 时环境性失败也直接落终态."""
    hot = HotSettings(worker=WorkerConfig(retry_max_attempts=0))
    worker = _worker(repo, RetryableFailHandler(), hot)

    task_id = await _run_claimed(repo, worker)

    fetched = await repo.get_task(task_id)
    assert fetched is not None
    assert fetched.status == TaskStatus.FAILED
    assert fetched.retry_at is None


@pytest.mark.asyncio(loop_scope="function")
async def test_retry_budget_exhausted_fails(repo: Repository) -> None:
    """用尽重排次数后落终态, 不再无限退避."""
    task = await repo.create_task(TaskType.SCRAPE, payload={"number": "TEST-1"})
    assert task.id is not None
    claimed = await repo.claim_next_task()
    assert claimed is not None
    # 先消耗一次预算: 退避到已过点, 任务可立即被再次认领.
    assert await repo.defer_task(task.id, error="x", result=None, retry_at=datetime.now(UTC).replace(year=2020))

    hot = HotSettings(worker=WorkerConfig(retry_max_attempts=1))
    worker = _worker(repo, RetryableFailHandler(), hot)
    again = await repo.claim_next_task()
    assert again is not None and again.id == task.id
    assert again.retries == 1
    await worker._execute(again)

    fetched = await repo.get_task(task.id)
    assert fetched is not None
    assert fetched.status == TaskStatus.FAILED
    assert fetched.retry_at is not None, "历史退避时间保留, 便于观察"

    # 再认领不到: 已终态.
    assert await repo.claim_next_task() is None


@pytest.mark.asyncio(loop_scope="function")
async def test_retry_rewrites_payload(repo: Repository) -> None:
    """handler 可改写载荷: 批次级禁用来源被丢弃, 否则重排必然复现同一失败."""
    handler = RetryableFailHandler(patched={"number": "TEST-1", "disabled_sources": []})
    worker = _worker(repo, handler, HotSettings())

    task_id = await _run_claimed(repo, worker, payload={"number": "TEST-1", "disabled_sources": ["official"]})

    fetched = await repo.get_task(task_id)
    assert fetched is not None
    assert fetched.payload == {"number": "TEST-1", "disabled_sources": []}


@pytest.mark.asyncio(loop_scope="function")
async def test_crash_is_not_retried(repo: Repository) -> None:
    """未归类异常一次暴露, 不重排."""
    worker = _worker(repo, CrashHandler(), HotSettings())

    task_id = await _run_claimed(repo, worker)

    fetched = await repo.get_task(task_id)
    assert fetched is not None
    assert fetched.status == TaskStatus.FAILED
    assert fetched.retry_at is None
    assert fetched.error == "boom"


@pytest.mark.asyncio(loop_scope="function")
async def test_cancel_is_not_retried(repo: Repository) -> None:
    """取消不重排: 用户意图优先, 与重启清扫区分."""
    handler = BlockingHandler()
    worker = _worker(repo, handler, HotSettings())
    task = await repo.create_task(TaskType.SCRAPE, payload={"number": "CANCEL-1"})
    assert task.id is not None
    worker.start()
    async with asyncio.timeout(LOAD_TOLERANT_TIMEOUT):
        await handler.started.wait()

    worker.retire()
    await worker.wait_stopped()
    await worker.shutdown_active()

    fetched = await repo.get_task(task.id)
    assert fetched is not None
    assert fetched.status == TaskStatus.FAILED
    assert fetched.error == CANCEL_ERROR
    assert fetched.retry_at is None


@pytest.mark.asyncio(loop_scope="function")
async def test_deferred_task_is_not_claimed_before_retry_at(repo: Repository) -> None:
    """退避期内 worker 不会把它拉起来空转."""
    handler = RetryableFailHandler()
    worker = _worker(repo, handler, HotSettings())
    worker.start()
    try:
        task = await repo.create_task(TaskType.SCRAPE, payload={"number": "TEST-1"})
        assert task.id is not None
        async with asyncio.timeout(LOAD_TOLERANT_TIMEOUT):
            await worker._done_queue.get()

        assert handler.calls == 1
        await asyncio.sleep(0.2)
        assert handler.calls == 1, "退避未到点却被重新认领"

        fetched = await repo.get_task(task.id)
        assert fetched is not None
        assert fetched.status == TaskStatus.QUEUED
    finally:
        worker.retire()
        await worker.wait_stopped()
        with suppress(Exception):
            await worker.shutdown_active()


def test_delay_grows_exponentially_and_caps() -> None:
    """退避序列: 180 → 360 → 720 …, 到上限后固定; 抖动为 0 时可直接比对."""
    budget = RetryBudget(jitter=lambda: 0.0)
    assert [budget.delay_for(n) for n in range(6)] == [180.0, 360.0, 720.0, 1440.0, 2880.0, 3600.0]


def test_delay_jitter_stays_within_band() -> None:
    """抖动只在 ±20% 内, 且不会小到无效值."""
    budget = RetryBudget()
    delays = [budget.delay_for(0) for _ in range(50)]
    assert all(144.0 <= d <= 216.0 for d in delays)
    assert len(set(delays)) > 1, "抖动未生效"


def test_budget_reads_config() -> None:
    """预算与退避取自 worker 配置; 无配置时用默认值."""
    hot = HotSettings(worker=WorkerConfig(retry_max_attempts=3, retry_base_delay_s=60, retry_max_delay_s=600))
    budget = RetryBudget.from_config(hot)
    assert (budget.max_attempts, budget.base_delay_s, budget.max_delay_s) == (3, 60.0, 600.0)

    fallback = RetryBudget.from_config(None)
    assert fallback.max_attempts == RetryBudget().max_attempts
