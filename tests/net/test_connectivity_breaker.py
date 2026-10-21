"""连接健康熔断状态机: OPEN → HALF_OPEN → CLOSED, 两把锁阈值, 并发安全, snapshot."""

import asyncio

import pytest

from amane.net.connectivity_breaker import BreakerState, ConnectivityBreaker


def _breaker(**kwargs: object) -> ConnectivityBreaker:
    # 短冷却便于测试半开; proxy 锁激进 / host 锁保守.
    return ConnectivityBreaker(
        proxy_fail_max=2,
        proxy_cooldown_s=0.05,
        host_fail_max=5,
        host_cooldown_s=0.05,
        **kwargs,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_closed_by_default():
    b = _breaker()
    assert b.is_open("proxy:http://127.0.0.1:7890") is False
    assert b.state("host:example.com") is BreakerState.CLOSED


@pytest.mark.asyncio
async def test_opens_after_fail_max():
    b = _breaker()
    key = "proxy:http://127.0.0.1:7890"
    await b.record_failure(key)
    assert b.is_open(key) is False  # 1 < fail_max(2)
    await b.record_failure(key)
    assert b.is_open(key) is True  # 开路


@pytest.mark.asyncio
async def test_half_open_after_cooldown_then_blocked_while_probe_in_flight():
    b = _breaker()
    key = "proxy:http://127.0.0.1:7890"
    await b.record_failure(key)
    await b.record_failure(key)
    assert b.is_open(key) is True  # 冷却中

    await asyncio.sleep(0.08)  # 超过冷却
    assert b.is_open(key) is False  # 首次查询 → 放行一个探针 (HALF_OPEN)
    assert b.state(key) is BreakerState.HALF_OPEN
    assert b.is_open(key) is True  # 探针在飞, 其余继续阻塞


@pytest.mark.asyncio
async def test_half_open_probe_success_closes():
    b = _breaker()
    key = "proxy:http://127.0.0.1:7890"
    await b.record_failure(key)
    await b.record_failure(key)
    await asyncio.sleep(0.08)
    assert b.is_open(key) is False  # 进入半开
    await b.record_success(key)
    assert b.state(key) is BreakerState.CLOSED
    assert b.is_open(key) is False


@pytest.mark.asyncio
async def test_half_open_probe_failure_reopens():
    b = _breaker()
    key = "proxy:http://127.0.0.1:7890"
    await b.record_failure(key)
    await b.record_failure(key)
    await asyncio.sleep(0.08)
    assert b.is_open(key) is False  # 进入半开
    await b.record_failure(key)
    assert b.state(key) is BreakerState.OPEN
    assert b.is_open(key) is True  # 重新开路


@pytest.mark.asyncio
async def test_record_success_on_healthy_is_noop():
    b = _breaker()
    # 无状态 key: 不应进锁, 也不产生条目
    await b.record_success("host:example.com")
    assert b.state("host:example.com") is BreakerState.CLOSED
    assert b.snapshot() == {}


@pytest.mark.asyncio
async def test_host_key_uses_more_conservative_threshold():
    b = _breaker()
    key = "host:example.com"
    for _ in range(4):
        await b.record_failure(key)
    assert b.is_open(key) is False  # 4 < host_fail_max(5)
    await b.record_failure(key)
    assert b.is_open(key) is True


@pytest.mark.asyncio
async def test_snapshot_reflects_state():
    b = _breaker()
    await b.record_failure("proxy:http://127.0.0.1:7890")
    await b.record_failure("proxy:http://127.0.0.1:7890")
    snap = b.snapshot()
    assert snap["proxy:http://127.0.0.1:7890"] == BreakerState.OPEN.value


@pytest.mark.asyncio
async def test_concurrent_record_failure_counts_all():
    b = _breaker()
    key = "host:example.com"  # host_fail_max=5, 一次性并发 5 次正好达阈值
    await asyncio.gather(*[b.record_failure(key) for _ in range(5)])
    assert b.is_open(key) is True


@pytest.mark.asyncio
async def test_forget_clears_entry():
    b = _breaker()
    key = "proxy:http://127.0.0.1:7890"
    await b.record_failure(key)
    await b.record_failure(key)
    assert b.is_open(key) is True
    b.forget(key)
    assert b.is_open(key) is False


@pytest.mark.asyncio
async def test_clear_removes_everything():
    b = _breaker()
    await b.record_failure("proxy:http://127.0.0.1:7890")
    await b.record_failure("host:example.com")
    b.clear()
    assert b.snapshot() == {}
