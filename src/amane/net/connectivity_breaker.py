"""跨任务共享的「连接健康」熔断: 某代理 / 某 host 连续失败达阈值即开路, 冷却后半开探测, 成功则关闭.

进程级单例 (挂在 :class:`~amane.net.http.WebClient` 上), 内存态、不落库、不跨重启 —— 进程或热重载重启后
重新学习, 代价仅首批几次请求. 并发安全由 ``asyncio.Lock`` 保证 (参考
``amane.app.proxy_failure_cache``).

典型用法 (key 形如 ``proxy:<url>`` / ``host:<host>``)::

    breaker = ConnectivityBreaker()  # proxy 锁激进, host 锁保守
    if breaker.is_open(key):
        raise RequestError(...)      # 短路, 不触网
    try:
        ...
    except (CurlError, TimeoutError):
        await breaker.record_failure(key)
        raise
    else:
        await breaker.record_success(key)
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from enum import StrEnum

# 未知前缀 (既非 proxy 也非 host) 的兜底阈值; 正常不会命中, 仅作防御.
_DEFAULT_FAIL_MAX = 3
_DEFAULT_COOLDOWN_S = 120.0


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"
    """冷却结束后的「放行一个探针」状态; 探针在飞时其余请求继续阻塞."""


@dataclass(slots=True)
class _Entry:
    fail_max: int
    cooldown_s: float
    state: BreakerState = BreakerState.CLOSED
    failures: int = 0
    opened_at: float = 0.0
    """OPEN 时刻 (monotonic); 半开探测即从此刻起算冷却."""


class ConnectivityBreaker:
    """连接健康熔断器.

    两把锁阈值分设: 代理死影响所有走它的站, 应快速开路 (``proxy_fail_max`` 小); 单站抖动不应秒杀全站,
    ``host_fail_max`` 取较大值. ``cooldown_s`` 决定开路后多久放出一个探针.
    """

    def __init__(
        self,
        *,
        proxy_fail_max: int = 2,
        proxy_cooldown_s: float = 120.0,
        host_fail_max: int = 5,
        host_cooldown_s: float = 300.0,
        max_entries: int = 4096,
    ) -> None:
        self._proxy_fail_max = proxy_fail_max
        self._proxy_cooldown_s = proxy_cooldown_s
        self._host_fail_max = host_fail_max
        self._host_cooldown_s = host_cooldown_s
        self._max_entries = max_entries
        self._entries: dict[str, _Entry] = {}
        self._lock = asyncio.Lock()

    # -- 读路径 (锁无关) ----------------------------------------------------
    # asyncio 单线程且无 await, 故 dict 读与属性赋值原子, 无需加锁. 这是热路径 (每个请求入口都调用).

    def is_open(self, key: str) -> bool:
        """返回 ``True`` 表示该请求应被短路 (熔断开路且冷却未到, 或半开探针在飞).

        冷却结束的 OPEN 状态首次查询会原子地转为 HALF_OPEN 并放行一个探针; 此后在飞探针解决
        (成功→CLOSED / 失败→重开) 之前, 其余查询继续返回 ``True`` 阻塞, 避免开路前被瞬时重试冲垮.
        """
        entry = self._entries.get(key)
        if entry is None or entry.state is BreakerState.CLOSED:
            return False
        if entry.state is BreakerState.HALF_OPEN:
            # 已有探针在飞, 其余请求继续阻塞
            return True
        # entry.state is OPEN
        if time.monotonic() >= entry.opened_at + entry.cooldown_s:
            entry.state = BreakerState.HALF_OPEN  # 原子: 放行一个探针
            return False
        return True

    def state(self, key: str) -> BreakerState:
        entry = self._entries.get(key)
        if entry is None:
            return BreakerState.CLOSED
        if entry.state is BreakerState.OPEN and time.monotonic() >= entry.opened_at + entry.cooldown_s:
            return BreakerState.HALF_OPEN
        return entry.state

    def snapshot(self) -> dict[str, str]:
        """观测用: 当前所有 key 的状态 (字符串). 供网络检测页展示哪些代理/host 开路."""
        now = time.monotonic()
        out: dict[str, str] = {}
        for k, e in self._entries.items():
            if e.state is BreakerState.OPEN and now >= e.opened_at + e.cooldown_s:
                out[k] = BreakerState.HALF_OPEN.value
            else:
                out[k] = e.state.value
        return out

    # -- 写路径 (加锁) ------------------------------------------------------

    async def record_failure(self, key: str) -> None:
        async with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                fail_max, cooldown = self._spec_for(key)
                entry = _Entry(fail_max=fail_max, cooldown_s=cooldown)
                self._entries[key] = entry
                self._evict_if_needed(key)
            if entry.state is BreakerState.HALF_OPEN:
                # 探针失败: 重新开路, 刷新冷却
                entry.state = BreakerState.OPEN
                entry.opened_at = time.monotonic()
                entry.failures += 1
                return
            entry.failures += 1
            if entry.failures >= entry.fail_max:
                entry.state = BreakerState.OPEN
                entry.opened_at = time.monotonic()

    async def record_success(self, key: str) -> None:
        # 锁无关快路径: 无状态或已健康, 高吞吐的正常请求不进锁.
        entry = self._entries.get(key)
        if entry is None or entry.state is BreakerState.CLOSED:
            return
        async with self._lock:
            entry = self._entries.get(key)
            if entry is None or entry.state is BreakerState.CLOSED:
                return
            # HALF_OPEN 探针成功, 或冷却结束后的探针成功 → 关闭并清零
            entry.state = BreakerState.CLOSED
            entry.failures = 0

    def forget(self, key: str) -> None:
        self._entries.pop(key, None)

    def clear(self) -> None:
        self._entries.clear()

    # -- 内部 ---------------------------------------------------------------

    def _spec_for(self, key: str) -> tuple[int, float]:
        if key.startswith("proxy:"):
            return self._proxy_fail_max, self._proxy_cooldown_s
        if key.startswith("host:"):
            return self._host_fail_max, self._host_cooldown_s
        return _DEFAULT_FAIL_MAX, _DEFAULT_COOLDOWN_S

    def _evict_if_needed(self, inserted: str) -> None:
        if len(self._entries) <= self._max_entries:
            return
        # FIFO 淘汰: 弹出最先插入的 (近似 LRU, 已开路的 key 优先保留的价值不高, 简化实现)
        oldest = next(iter(self._entries))
        if oldest == inserted:
            # 仅当插入项本身是最旧时, 退而求其次弹第二个
            for k in self._entries:
                if k != inserted:
                    oldest = k
                    break
        self._entries.pop(oldest, None)
