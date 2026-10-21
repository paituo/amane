"""SourceAvailability: 非阻塞读 + 后台刷新 + TTL 缓存 + 仅 FAILED 被判不可用 + 异常退避."""

from __future__ import annotations

import asyncio
import contextlib
import time

import pytest

from amane.crawlers.connectivity import SourceCheck, SourceKind
from amane.net.connectivity import ConnectivityOutcome, ConnectivityStatus, SkipReason
from amane.net.errors import FailureReason
from amane.scrape_availability import SourceAvailability


def _check(source_id: str, status: ConnectivityStatus, reason: FailureReason | None = None) -> SourceCheck:
    if status is ConnectivityStatus.FAILED:
        outcome = ConnectivityOutcome.failed(reason or FailureReason.NETWORK, detail="x")
    elif status is ConnectivityStatus.SKIPPED:
        outcome = ConnectivityOutcome.skipped(SkipReason.UNKNOWN_SOURCE)
    else:
        outcome = ConnectivityOutcome.ok("https://x.test/", 200)
    return SourceCheck(source_id=source_id, name=source_id, kind=SourceKind.FILM, outcome=outcome, elapsed_ms=1)


class _FakeAvailability(SourceAvailability):
    """跳过需要 factory/hot 的真实构造, 直接注入假探测结果."""

    def __init__(self, checks: list[SourceCheck] | None = None) -> None:
        self._checker = None  # type: ignore[assignment]
        self._cache: frozenset[str] | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()
        self._ttl = 180.0
        self._refresh_task: asyncio.Task[None] | None = None
        self._retry_at = 0.0
        self._checks = checks or []
        self.probe_calls = 0

    async def _safe_check(self) -> list[SourceCheck]:
        self.probe_calls += 1
        return list(self._checks)


async def _cancel_background(a: SourceAvailability) -> None:
    """收尾: 取消在飞的后台探测, 避免悬挂任务告警污染其它用例."""
    task = a._refresh_task
    if task is not None and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio(loop_scope="function")
async def test_disabled_extracts_only_failed() -> None:
    a = _FakeAvailability(
        [
            _check("javdb", ConnectivityStatus.OK),
            _check("dmm", ConnectivityStatus.FAILED, FailureReason.NETWORK),
            _check("javbus", ConnectivityStatus.SKIPPED),
        ]
    )
    # 冷缓存: 立即返回空集并在后台探测, 不阻塞调用方.
    assert await a.disabled_sources() == frozenset()
    await a.wait_idle()
    # 只有 FAILED 被判不可用; OK / SKIPPED 都不计入 (SKIPPED 的来源仍可能经常规 fetch 取到).
    assert await a.disabled_sources() == frozenset({"dmm"})


@pytest.mark.asyncio(loop_scope="function")
async def test_ttl_cache_avoids_reprobe() -> None:
    a = _FakeAvailability([_check("dmm", ConnectivityStatus.FAILED, FailureReason.NETWORK)])
    assert await a.disabled_sources() == frozenset()
    await a.wait_idle()
    assert await a.disabled_sources() == frozenset({"dmm"})
    # 缓存命中: TTL 内不重复探测.
    assert await a.disabled_sources() == frozenset({"dmm"})
    assert a.probe_calls == 1


@pytest.mark.asyncio(loop_scope="function")
async def test_invalidate_refreshes() -> None:
    a = _FakeAvailability([_check("dmm", ConnectivityStatus.FAILED, FailureReason.NETWORK)])
    assert await a.disabled_sources() == frozenset()
    await a.wait_idle()
    assert await a.disabled_sources() == frozenset({"dmm"})

    a._checks = [_check("dmm", ConnectivityStatus.OK)]
    a.invalidate()
    # invalidate 后重新探测: dmm 已恢复, 不再被判不可用.
    assert await a.disabled_sources() == frozenset()
    await a.wait_idle()
    assert await a.disabled_sources() == frozenset()
    assert a.probe_calls == 2


@pytest.mark.asyncio(loop_scope="function")
async def test_cold_call_does_not_block_on_probe() -> None:
    """无网/慢网下探测可达数十秒: 冷调用必须立即返回, 探测在后台跑."""
    a = _FakeAvailability()
    started = asyncio.Event()

    async def hanging_check() -> list[SourceCheck]:
        started.set()
        await asyncio.sleep(3600)
        return []

    a._safe_check = hanging_check  # type: ignore[method-assign]
    t0 = time.monotonic()
    assert await a.disabled_sources() == frozenset()
    assert time.monotonic() - t0 < 0.5
    await asyncio.sleep(0)  # 让出循环, 后台探测获得调度
    assert started.is_set()  # 后台确实已经在探
    await _cancel_background(a)


@pytest.mark.asyncio(loop_scope="function")
async def test_stale_cache_served_while_background_refreshes() -> None:
    """TTL 过期后先服务旧结论 (死站大概率仍死), 新结论由后台探测异步落库."""
    a = _FakeAvailability([_check("dmm", ConnectivityStatus.FAILED, FailureReason.NETWORK)])
    await a.disabled_sources()
    await a.wait_idle()

    a._expires_at = time.monotonic() - 1.0  # 手动令其过期

    async def hanging_check() -> list[SourceCheck]:
        await asyncio.sleep(3600)
        return []

    a._safe_check = hanging_check  # type: ignore[method-assign]
    assert await a.disabled_sources() == frozenset({"dmm"})  # 旧结论立即 served
    await _cancel_background(a)


@pytest.mark.asyncio(loop_scope="function")
async def test_concurrent_cold_calls_share_one_probe() -> None:
    a = _FakeAvailability([_check("dmm", ConnectivityStatus.FAILED, FailureReason.NETWORK)])
    await asyncio.gather(a.disabled_sources(), a.disabled_sources(), a.disabled_sources())
    await a.wait_idle()
    assert a.probe_calls == 1


@pytest.mark.asyncio(loop_scope="function")
async def test_probe_failure_sets_retry_backoff() -> None:
    """探测抛错不毒化扇出: 记录退避, 退避期内不再重启注定失败的探测."""
    a = _FakeAvailability()

    async def boom() -> list[SourceCheck]:
        a.probe_calls += 1
        raise RuntimeError("probe exploded")

    a._safe_check = boom  # type: ignore[method-assign]
    assert await a.disabled_sources() == frozenset()
    assert await a.wait_idle() is None
    assert a.probe_calls == 1
    assert a._retry_at > time.monotonic()

    # 退避期内: 后续扇出不再触发新探测.
    for _ in range(3):
        assert await a.disabled_sources() == frozenset()
    await asyncio.sleep(0)
    assert a.probe_calls == 1

    # 退避结束: 恢复探测.
    a._retry_at = 0.0
    await a.disabled_sources()
    await a.wait_idle()
    assert a.probe_calls == 2


@pytest.mark.asyncio(loop_scope="function")
async def test_wait_idle_returns_none_when_idle() -> None:
    a = _FakeAvailability()
    assert await a.wait_idle() is None


@pytest.mark.asyncio(loop_scope="function")
async def test_cancelled_background_probe_is_silent() -> None:
    """取消在飞探测 (关停/测试收尾) 不产生未取回的 task 异常."""
    a = _FakeAvailability()

    async def hanging_check() -> list[SourceCheck]:
        await asyncio.sleep(3600)
        return []

    a._safe_check = hanging_check  # type: ignore[method-assign]
    await a.disabled_sources()
    await _cancel_background(a)
    # done 回调已清理引用; 再次冷读重新起一轮 (没有退避污染).
    assert a._refresh_task is None
    await _cancel_background(a)
