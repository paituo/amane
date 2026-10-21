"""P2-⑦ 多代理池: 健康轮选 / 半开探针回池 / 请求内轮换 / 池耗尽短路 / 缓存归一 / 向后兼容.

边界与非法用例 (符合 docs/dev/testing.md): 池空、全部开路、全部已试过、站点级覆盖优先于池、
use_proxy=False 不碰池、未配池时行为与改造前一致.
"""

import pytest
from curl_cffi import CurlError

from amane.net.connectivity_breaker import ConnectivityBreaker
from amane.net.errors import FailureReason, RequestError
from amane.net.http import RateLimiters, WebClient
from amane.net.proxy_pool import ProxyPool
from amane.net.response_cache import ResponseCache

P1 = "socks5h://127.0.0.1:10808"
P2 = "socks5h://127.0.0.1:10809"
P3 = "http://127.0.0.1:10810"


class _StubResponse:
    def __init__(self, *, url: str = "", content: bytes = b"", status: int = 200) -> None:
        self.url = url
        self.content = content
        self.status_code = status
        self.headers = {"Content-Type": "text/html"}


class _ProxyAwareSession:
    """按 proxy 决定成败: ``failures`` 中的代理抛对应异常, 其余返回 200."""

    def __init__(self, failures: dict[str, BaseException] | None = None) -> None:
        self.failures = dict(failures or {})
        self.calls: list[dict] = []

    async def request(self, method: str, url: str, **kwargs):
        proxy = kwargs.get("proxy")
        self.calls.append({"method": method, "url": url, "proxy": proxy})
        exc = self.failures.get(proxy)
        if exc is not None:
            raise exc
        return _StubResponse(url=url, content=b"ok")


def _client(
    *,
    proxy: str | None = None,
    proxy_pool: ProxyPool | None = None,
    connectivity: ConnectivityBreaker | None = None,
    response_cache: ResponseCache | None = None,
    proxy_overrides: dict[str, str | None] | None = None,
    max_retries: int = 0,
) -> WebClient:
    return WebClient(
        proxy=proxy,
        proxy_overrides=proxy_overrides,
        limiters=RateLimiters(default_rate=1000),
        connectivity=connectivity,
        response_cache=response_cache,
        proxy_pool=proxy_pool,
        max_retries=max_retries,
    )


class TestProxyPool:
    def test_round_robin_order(self):
        pool = ProxyPool([P1, P2, P3])
        assert [pool.pick() for _ in range(4)] == [P1, P2, P3, P1]

    def test_empty_pool_returns_none(self):
        pool = ProxyPool([])
        assert pool.size == 0
        assert pool.pick() is None

    def test_proxies_property_is_config_order(self):
        assert ProxyPool([P3, P1]).proxies == (P3, P1)

    @pytest.mark.asyncio(loop_scope="function")
    async def test_skips_open_proxy(self):
        breaker = ConnectivityBreaker(proxy_fail_max=1)
        await breaker.record_failure(f"proxy:{P1}")
        assert breaker.is_open(f"proxy:{P1}")
        pool = ProxyPool([P1, P2], breaker)
        assert pool.pick() == P2  # P1 开路 (冷却 120s 内), 跳过

    @pytest.mark.asyncio(loop_scope="function")
    async def test_exclude_skips_tried(self):
        pool = ProxyPool([P1, P2, P3])
        assert pool.pick(exclude=frozenset({P1})) == P2
        assert pool.pick(exclude=frozenset({P1, P2})) == P3

    @pytest.mark.asyncio(loop_scope="function")
    async def test_all_open_or_excluded_returns_none(self):
        breaker = ConnectivityBreaker(proxy_fail_max=1)
        await breaker.record_failure(f"proxy:{P1}")
        assert ProxyPool([P1], breaker).pick() is None
        assert ProxyPool([P1, P2]).pick(exclude=frozenset({P1, P2})) is None

    @pytest.mark.asyncio(loop_scope="function")
    async def test_cooled_open_proxy_reenters_as_single_probe(self):
        """冷却结束后 pick 是第一个查询 → 放行一个探针; 探针在飞时其余请求继续选别的."""
        breaker = ConnectivityBreaker(proxy_fail_max=1, proxy_cooldown_s=0.0)
        await breaker.record_failure(f"proxy:{P1}")
        pool = ProxyPool([P1, P2], breaker)
        assert pool.pick() == P1  # 冷却已到 (cooldown=0) → 半开探针放行
        assert pool.pick() == P2  # 探针在飞 → P1 阻塞
        await breaker.record_success(f"proxy:{P1}")  # 探针成功 → 关闭回池
        assert pool.pick() == P1


class TestWebClientProxyRotation:
    @pytest.mark.asyncio(loop_scope="function")
    async def test_rotates_to_next_proxy_on_curl_error(self):
        client = _client(proxy_pool=ProxyPool([P1, P2]))
        session = _ProxyAwareSession({P1: CurlError("connection refused")})
        client._session = session
        try:
            resp = await client.request("GET", "https://www.example.com/x")
        finally:
            await client.close()
        assert resp.status_code == 200
        assert [c["proxy"] for c in session.calls] == [P1, P2]  # 死代理换下一个, 不占重试预算

    @pytest.mark.asyncio(loop_scope="function")
    async def test_rotates_on_timeout(self):
        client = _client(proxy_pool=ProxyPool([P1, P2]))
        session = _ProxyAwareSession({P1: TimeoutError("read timeout")})
        client._session = session
        try:
            resp = await client.request("GET", "https://www.example.com/x")
        finally:
            await client.close()
        assert resp.status_code == 200
        assert [c["proxy"] for c in session.calls] == [P1, P2]

    @pytest.mark.asyncio(loop_scope="function")
    async def test_all_pool_proxies_dead_fails_after_trying_each_once(self):
        client = _client(proxy_pool=ProxyPool([P1, P2]))
        session = _ProxyAwareSession({P1: CurlError("refused", 7), P2: CurlError("refused", 7)})
        client._session = session
        try:
            with pytest.raises(RequestError):
                await client.request("GET", "https://www.example.com/x")
        finally:
            await client.close()
        assert [c["proxy"] for c in session.calls] == [P1, P2]  # 每个代理恰好一次

    @pytest.mark.asyncio(loop_scope="function")
    async def test_dead_proxies_open_breaker_then_short_circuit_without_network(self):
        breaker = ConnectivityBreaker(proxy_fail_max=1)
        client = _client(proxy_pool=ProxyPool([P1, P2], breaker), connectivity=breaker)
        session = _ProxyAwareSession({P1: CurlError("refused", 7), P2: CurlError("refused", 7)})
        client._session = session
        try:
            with pytest.raises(RequestError):
                await client.request("GET", "https://www.example.com/x")
            assert breaker.is_open(f"proxy:{P1}")
            assert breaker.is_open(f"proxy:{P2}")
            calls_before = len(session.calls)
            with pytest.raises(RequestError) as ei:
                await client.request("GET", "https://www.example.com/y")
            assert len(session.calls) == calls_before  # 池耗尽: 不触网
            assert ei.value.reason == FailureReason.CIRCUIT_OPEN
        finally:
            await client.close()

    @pytest.mark.asyncio(loop_scope="function")
    async def test_cooled_proxy_is_probed_not_blocked_by_second_is_open(self):
        """pick 消耗半开名额后, 熔断复查不得再把该探针判阻塞 (否则自动恢复断裂)."""
        breaker = ConnectivityBreaker(proxy_fail_max=1, proxy_cooldown_s=0.0)
        await breaker.record_failure(f"proxy:{P1}")
        client = _client(proxy_pool=ProxyPool([P1, P2], breaker), connectivity=breaker)
        session = _ProxyAwareSession()  # 全部健康
        client._session = session
        try:
            resp = await client.request("GET", "https://www.example.com/x")
        finally:
            await client.close()
        assert resp.status_code == 200
        assert session.calls[0]["proxy"] == P1  # 探针真的发出去了
        assert breaker.state(f"proxy:{P1}").value == "closed"  # 探针成功 → 回池

    @pytest.mark.asyncio(loop_scope="function")
    async def test_pool_mode_cache_shared_across_proxies(self):
        """代理分量归一: 轮询换代理后同 URL 仍命中缓存 (不为每个代理各存一份)."""
        cache = ResponseCache(300.0, 16)
        client = _client(proxy_pool=ProxyPool([P1, P2]), response_cache=cache)
        session = _ProxyAwareSession()
        client._session = session
        try:
            await client.request("GET", "https://www.example.com/x")  # 走 P1, 写入
            await client.request("GET", "https://www.example.com/x")  # 轮询到 P2, 应命中
        finally:
            await client.close()
        assert len(session.calls) == 1  # 第二次没触网

    @pytest.mark.asyncio(loop_scope="function")
    async def test_site_override_beats_pool(self):
        client = _client(
            proxy="socks5h://global:1",
            proxy_pool=ProxyPool([P1, P2]),
            proxy_overrides={"www.example.com": "socks5h://site-level:2"},
        )
        session = _ProxyAwareSession()
        client._session = session
        try:
            await client.request("GET", "https://www.example.com/x")
        finally:
            await client.close()
        assert session.calls[0]["proxy"] == "socks5h://site-level:2"

    @pytest.mark.asyncio(loop_scope="function")
    async def test_use_proxy_false_bypasses_pool(self):
        client = _client(proxy_pool=ProxyPool([P1, P2]))
        session = _ProxyAwareSession()
        client._session = session
        try:
            await client.request("GET", "https://www.example.com/x", use_proxy=False)
        finally:
            await client.close()
        assert session.calls[0]["proxy"] is None


class TestNoPoolBackwardCompatible:
    @pytest.mark.asyncio(loop_scope="function")
    async def test_global_proxy_used_when_no_pool(self):
        client = _client(proxy="socks5h://global:1")
        session = _ProxyAwareSession()
        client._session = session
        try:
            await client.request("GET", "https://www.example.com/x")
        finally:
            await client.close()
        assert session.calls[0]["proxy"] == "socks5h://global:1"

    @pytest.mark.asyncio(loop_scope="function")
    async def test_curl_error_no_rotation_without_pool(self):
        """无池时连接级错误保持 fail-fast (P1-③), 不会去试别的代理."""
        client = _client(proxy="socks5h://global:1")
        session = _ProxyAwareSession({"socks5h://global:1": CurlError("refused")})
        client._session = session
        try:
            with pytest.raises(RequestError):
                await client.request("GET", "https://www.example.com/x")
        finally:
            await client.close()
        assert len(session.calls) == 1
