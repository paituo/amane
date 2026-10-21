"""多代理池 (P2-⑦): ``network.proxies`` 非空时, 无站点级覆盖的 host 从池内选代理.

健康态复用 :class:`~amane.net.http.ConnectivityBreaker` 的 ``proxy:<url>`` 键
(``proxy_fail_max=2``, ``cooldown=120s``): 被熔断开路的代理不再被选中, 冷却结束自动
放出一个半开探针回池. 与 P0-① 共用同一把锁, **不新增状态机**.

语义 (对应设计文档 3.6 之 ⑦):
- 池是**唯一**代理来源: 运营者显式配置了池, 池内全部开路时请求快速失败
  (``CIRCUIT_OPEN``), 不静默回退到单值 ``network.proxy`` —— 静默回退会把流量
  悄悄灌回运营者想 relief 的那个代理, 且掩盖"池全灭"的信号.
- 站点级代理覆盖 (``site_config.proxy`` / ``use_proxy=false``) 优先于池, 不进池.
- 单次请求内轮换由 ``WebClient`` 驱动: 连接级失败后选下一个未试过的健康代理.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .connectivity_breaker import ConnectivityBreaker


class ProxyPool:
    """有序代理池 + 健康态轮选 (round-robin, 跳过开路代理).

    线程模型: 与 ``WebClient`` 同在 event loop 内, 轮询游标推进是同步无 await 的原子操作;
    健康态读取走熔断器的无锁读路径 (``is_open``), 因此本类自身不需要锁.
    """

    __slots__ = ("_breaker", "_cursor", "_proxies")

    def __init__(self, proxies: Iterable[str], breaker: ConnectivityBreaker | None = None) -> None:
        self._proxies: tuple[str, ...] = tuple(proxies)
        self._cursor = itertools.count()
        self._breaker = breaker

    @property
    def size(self) -> int:
        """池内代理数; ``0`` 表示未配置 (调用方不应持有空池, 见 ``build_network_stack``)."""
        return len(self._proxies)

    @property
    def proxies(self) -> tuple[str, ...]:
        """池内代理 (配置顺序), 观测用."""
        return self._proxies

    def pick(self, *, exclude: frozenset[str] | None = None) -> str | None:
        """按轮询顺序选下一个健康代理; 跳过 ``exclude`` 中的 (本次请求已试过) 与开路的.

        全部被排除/开路时返回 ``None``; 调用方 (``WebClient.request``) 据此短路,
        不白付连接超时预算. 冷却结束的开路代理会被熔断器翻成 HALF_OPEN 并放行一个
        探针 —— 本方法选到它即构成该探针.
        """
        if not self._proxies:
            return None
        excluded = exclude or frozenset()
        start = next(self._cursor) % len(self._proxies)
        for offset in range(len(self._proxies)):
            proxy = self._proxies[(start + offset) % len(self._proxies)]
            if proxy in excluded:
                continue
            if self._breaker is not None and self._breaker.is_open(f"proxy:{proxy}"):
                continue
            return proxy
        return None
