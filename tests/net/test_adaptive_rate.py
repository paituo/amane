"""P2-⑥ 自适应限速: 配置为上限 / 减速 / 恢复 / 下限钳制 / 关闭开关 / WebClient 接入.

覆盖 docs/dev/testing.md 要求的边界与非法用例: 429 显式限流立即减半、5xx 与连接错误
应激减速、慢/快响应分界、恢复步进被配置速率钳住、floor 钳制、adaptive=False 无操作、
404/403/406/3xx 等无关状态保持速率、并发 acquire 期间调速不丢许可.
"""

import asyncio

import pytest
from curl_cffi import CurlError

from amane.net.errors import RequestError
from amane.net.http import RateLimiters, WebClient, _AdaptiveLimiter


class _FakeClock:
    """可推进的单调时钟, 避免测试里真睡 1 秒等适应间隔."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _limiter(rate: float = 5.0, *, adaptive: bool = True, floor_ratio: float = 0.25) -> _AdaptiveLimiter:
    return _AdaptiveLimiter(rate, adaptive=adaptive, floor_ratio=floor_ratio, clock=_FakeClock())


def _clock_of(limiter: _AdaptiveLimiter) -> _FakeClock:
    return limiter._clock  # type: ignore[return-value]


class TestAdaptiveLimiter:
    def test_starts_at_configured_rate(self):
        lim = _limiter(5.0)
        assert lim.rate == pytest.approx(5.0)
        assert lim.time_period == pytest.approx(1 / 5)
        assert lim.floor_rate == pytest.approx(1.25)

    @pytest.mark.asyncio(loop_scope="function")
    async def test_429_halves_immediately_bypassing_interval(self):
        lim = _limiter(5.0)
        await lim.observe(None, 429)
        assert lim.rate == pytest.approx(2.5)
        # 429 是站点显式限流信号, 不受适应间隔约束: 紧接着第二次仍生效
        await lim.observe(None, 429)
        assert lim.rate == pytest.approx(1.25)

    @pytest.mark.asyncio(loop_scope="function")
    async def test_429_respects_floor(self):
        lim = _limiter(5.0)  # floor = 1.25
        for _ in range(6):
            await lim.observe(None, 429)
        assert lim.rate == pytest.approx(1.25)
        assert lim.time_period == pytest.approx(1 / 1.25)

    @pytest.mark.asyncio(loop_scope="function")
    async def test_stress_status_backs_off_once_per_interval(self):
        lim = _limiter(5.0)
        await lim.observe(1.0, 503)
        assert lim.rate == pytest.approx(5 * 0.85)
        await lim.observe(1.0, 503)  # 适应间隔内: 不叠加
        assert lim.rate == pytest.approx(5 * 0.85)
        _clock_of(lim).advance(1.0)
        await lim.observe(1.0, 503)
        assert lim.rate == pytest.approx(5 * 0.85 * 0.85)

    @pytest.mark.asyncio(loop_scope="function")
    async def test_connection_error_status_none_backs_off(self):
        lim = _limiter(5.0)
        await lim.observe(2.0, None)
        assert lim.rate == pytest.approx(5 * 0.85)

    @pytest.mark.asyncio(loop_scope="function")
    async def test_slow_latency_backs_off(self):
        lim = _limiter(5.0)
        await lim.observe(3.5, 200)
        assert lim.rate == pytest.approx(4.5)

    @pytest.mark.asyncio(loop_scope="function")
    async def test_fast_streak_recovers_and_never_exceeds_configured(self):
        lim = _limiter(5.0)
        await lim.observe(None, 429)  # → 2.5
        rates: list[float] = []
        for _ in range(30):
            _clock_of(lim).advance(1.0)
            await lim.observe(0.1, 200)
            rates.append(lim.rate)
        # 恢复步进被配置速率钳住: 任何时刻都不会比配置更快
        assert all(r <= 5.0 + 1e-9 for r in rates)
        assert rates == sorted(rates)  # 恢复阶段单调不降
        assert lim.rate == pytest.approx(5.0)
        assert lim.time_period == pytest.approx(1 / 5)

    @pytest.mark.asyncio(loop_scope="function")
    async def test_single_fast_response_does_not_recover(self):
        lim = _limiter(5.0)
        await lim.observe(None, 429)  # → 2.5
        _clock_of(lim).advance(1.0)
        await lim.observe(0.1, 200)  # streak=1 < 5
        assert lim.rate == pytest.approx(2.5)

    @pytest.mark.asyncio(loop_scope="function")
    async def test_unrelated_statuses_hold_rate(self):
        lim = _limiter(5.0)
        for status in (404, 403, 406, 301, 304):
            _clock_of(lim).advance(1.0)
            await lim.observe(0.2, status)
        assert lim.rate == pytest.approx(5.0)

    @pytest.mark.asyncio(loop_scope="function")
    async def test_adaptive_disabled_is_noop(self):
        lim = _limiter(5.0, adaptive=False)
        for status in (429, 503, None):
            await lim.observe(1.0, status)
        await lim.observe(4.0, 200)
        assert lim.rate == pytest.approx(5.0)
        assert lim.time_period == pytest.approx(1 / 5)

    @pytest.mark.asyncio(loop_scope="function")
    async def test_low_configured_rate_has_absolute_floor(self):
        lim = _limiter(0.1)  # ratio floor = 0.025 → 绝对下限 _ADAPT_MIN_RATE=0.05 生效
        for _ in range(10):
            await lim.observe(None, 429)
        assert lim.rate == pytest.approx(0.05)

    @pytest.mark.asyncio(loop_scope="function")
    async def test_concurrent_acquire_during_adaptation(self):
        lim = _limiter(50.0)

        async def worker() -> None:
            for _ in range(3):
                await lim.acquire()

        async def adapter() -> None:
            for i in range(8):
                await lim.observe(0.1, 429 if i % 2 else 200)

        await asyncio.gather(*(worker() for _ in range(5)), adapter())
        assert lim.floor_rate <= lim.rate <= 50.0


class TestRateLimitersAdaptive:
    def test_get_returns_stable_limiter(self):
        rl = RateLimiters(default_rate=5)
        assert rl.get("example.com") is rl.get("example.com")

    def test_default_rate_sets_period(self):
        rl = RateLimiters(default_rate=5)
        assert rl.get("never.example.com").time_period == pytest.approx(1 / 5)

    def test_localhost_floor_ratio(self):
        rl = RateLimiters(default_rate=5)
        assert rl.get("localhost").floor_rate == pytest.approx(75.0)

    @pytest.mark.asyncio(loop_scope="function")
    async def test_observe_unknown_host_is_noop(self):
        rl = RateLimiters(default_rate=5)
        await rl.observe("never-seen.example.com", 1.0, 429)
        assert "never-seen.example.com" not in rl._limiters

    @pytest.mark.asyncio(loop_scope="function")
    async def test_observe_none_host_is_noop(self):
        rl = RateLimiters(default_rate=5)
        await rl.observe(None, 1.0, 429)

    @pytest.mark.asyncio(loop_scope="function")
    async def test_set_rate_replaces_and_resets_adaptation(self):
        rl = RateLimiters(default_rate=5)
        old = rl.get("example.com")
        await old.observe(None, 429)  # 5 → 2.5
        assert old.rate == pytest.approx(2.5)
        rl.set_rate("example.com", 10.0)
        new = rl.get("example.com")
        assert new is not old
        assert new.rate == pytest.approx(10.0)
        assert new.time_period == pytest.approx(1 / 10)

    @pytest.mark.asyncio(loop_scope="function")
    async def test_from_config_adaptive_flag_propagates(self):
        rl = RateLimiters.from_config({"a.com": 4.0}, {}, {}, default_rate=5, adaptive=False)
        lim = rl.get("a.com")
        await lim.observe(None, 429)
        assert lim.rate == pytest.approx(4.0)  # 关闭自适应 → 无操作


class _StubResponse:
    def __init__(
        self,
        *,
        url: str = "",
        content: bytes = b"",
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.url = url
        self.content = content
        self.status_code = status
        self.headers = headers or {}


class _StubSession:
    """按调用次序返回应答 (用尽后重复最后一个), 记录出站参数."""

    def __init__(self, *responses: _StubResponse) -> None:
        self.calls: list[dict] = []
        self._responses = list(responses) or [_StubResponse()]

    async def request(self, method: str, url: str, **kwargs) -> _StubResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        index = min(len(self.calls) - 1, len(self._responses) - 1)
        return self._responses[index]


class _RaisingSession:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc
        self.calls = 0

    async def request(self, method: str, url: str, **kwargs):
        self.calls += 1
        raise self.exc


class TestWebClientAdaptiveWiring:
    """端到端: request() 的两种结局都驱动限速器调速."""

    @pytest.mark.asyncio(loop_scope="function")
    async def test_429_backs_off_host_limiter(self):
        client = WebClient(limiters=RateLimiters(default_rate=5), max_retries=0)
        client._session = _StubSession(_StubResponse(status=429))
        try:
            with pytest.raises(RequestError) as ei:
                await client.request("GET", "https://www.example.com/search?kw=abc")
            assert ei.value.http_status == 429
        finally:
            await client.close()
        limiter = client._limiters.get("www.example.com")
        assert limiter.rate == pytest.approx(2.5)
        assert limiter.time_period == pytest.approx(1 / 2.5)

    @pytest.mark.asyncio(loop_scope="function")
    async def test_success_never_exceeds_configured_rate(self):
        client = WebClient(limiters=RateLimiters(default_rate=5), max_retries=0)
        client._session = _StubSession(_StubResponse(status=200, content=b"ok"))
        try:
            for _ in range(10):
                await client.request("GET", "https://www.example.com/search")
        finally:
            await client.close()
        limiter = client._limiters.get("www.example.com")
        assert limiter.rate == pytest.approx(5.0)  # 恢复步进被配置速率钳住, 不会更快

    @pytest.mark.asyncio(loop_scope="function")
    async def test_curl_error_backs_off(self):
        client = WebClient(limiters=RateLimiters(default_rate=5), max_retries=0)
        client._session = _RaisingSession(CurlError("connection refused"))
        try:
            with pytest.raises(RequestError):
                await client.request("GET", "https://www.example.com/search")
        finally:
            await client.close()
        limiter = client._limiters.get("www.example.com")
        assert limiter.rate == pytest.approx(5 * 0.85)
