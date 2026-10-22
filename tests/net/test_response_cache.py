"""跨任务 HTTP 响应缓存 (P2-⑤): ResponseCache 单元 + WebClient 接入行为.

覆盖: 命中免二次触网 / 缓存字段还原 / POST·Range·大响应·二进制不缓存 / bypass_circuit·bypass_cache 旁路 /
熔断开路时缓存兜底 / TTL 过期 / LRU 淘汰 / 关闭开关 / get_text·get_json 命中.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from amane.net import http
from amane.net.connectivity_breaker import ConnectivityBreaker
from amane.net.http import RateLimiters, WebClient, _response_cache_key
from amane.net.response_cache import CachedResponse, ResponseCache, _CaseInsensitiveHeaders

if TYPE_CHECKING:
    pass


class _StubResponse:
    """最小应答替身; 默认带文本 content-type 使命中缓存路径. 含 ``text``/``json()`` 供 helper 两条路径通用."""

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
        self.headers = headers if headers is not None else {"Content-Type": "text/html"}
        self.encoding = "utf-8"

    @property
    def text(self) -> str:
        return self.content.decode(self.encoding or "utf-8", errors="replace")

    def json(self) -> Any:
        import json

        return json.loads(self.text)


class _StubSession:
    """替换 ``WebClient._session``: 记录出站调用, 按序返回应答."""

    def __init__(self, *responses: _StubResponse) -> None:
        self.calls: list[dict[str, Any]] = []
        self._responses = list(responses) or [_StubResponse()]

    async def request(self, method: str, url: str, **kwargs: Any) -> _StubResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        index = min(len(self.calls) - 1, len(self._responses) - 1)
        return self._responses[index]


def _client(
    *,
    response_cache: ResponseCache | None,
    connectivity: ConnectivityBreaker | None = None,
) -> WebClient:
    return WebClient(
        limiters=RateLimiters(default_rate=100),
        connectivity=connectivity,
        response_cache=response_cache,
    )


def _cache_of(client: WebClient) -> ResponseCache:
    """取客户端上的缓存实例, 兼作非空断言 (WebClient 允许不启用缓存)."""
    cache = client._response_cache
    assert cache is not None, "该用例要求客户端启用响应缓存"
    return cache


# --- ResponseCache 单元 -----------------------------------------------------


class TestResponseCacheUnit:
    def test_put_get_roundtrip(self):
        cache = ResponseCache(ttl_s=300.0, max_entries=8)
        assert cache.enabled is True
        resp = CachedResponse(status_code=200, headers={"Content-Type": "text/html"}, content=b"x", url="u")
        cache.put(("k",), resp)
        assert cache.get(("k",)) is resp

    def test_miss_returns_none(self):
        cache = ResponseCache(ttl_s=300.0, max_entries=8)
        assert cache.get(("absent",)) is None

    def test_expired_returns_none_and_evicts(self):
        cache = ResponseCache(ttl_s=300.0, max_entries=8)
        cache.put(("k",), CachedResponse(status_code=200, headers={}, content=b"x", url="u"))
        assert cache.get(("k",)) is not None
        # 手工把入库时间提前到超过 TTL (免 sleep).
        stored_at, resp = cache._store[("k",)]
        cache._store[("k",)] = (stored_at - 301.0, resp)
        assert cache.get(("k",)) is None
        assert len(cache) == 0  # 过期即从存储清除

    def test_lru_evicts_least_recently_used(self):
        cache = ResponseCache(ttl_s=300.0, max_entries=2)

        def _r(tag: str) -> CachedResponse:
            return CachedResponse(status_code=200, headers={}, content=tag.encode(), url=tag)

        cache.put(("a",), _r("a"))
        cache.put(("b",), _r("b"))
        assert cache.get(("a",)) is not None  # touch a → b 成为最久未用
        cache.put(("c",), _r("c"))  # 淘汰 b
        assert cache.get(("a",)) is not None
        assert cache.get(("b",)) is None
        assert cache.get(("c",)) is not None

    def test_disabled_when_ttl_zero(self):
        cache = ResponseCache(ttl_s=0.0, max_entries=8)
        assert cache.enabled is False
        cache.put(("k",), CachedResponse(status_code=200, headers={}, content=b"x", url="u"))
        assert cache.get(("k",)) is None
        assert len(cache) == 0

    def test_disabled_when_max_entries_zero(self):
        cache = ResponseCache(ttl_s=300.0, max_entries=0)
        assert cache.enabled is False


class TestCaseInsensitiveHeaders:
    def test_get_is_case_insensitive(self):
        h = _CaseInsensitiveHeaders({"Content-Type": "text/html"})
        assert h.get("content-type") == "text/html"
        assert h.get("CONTENT-TYPE") == "text/html"
        assert h.get("x-missing", "d") == "d"

    def test_getitem_and_keyerror(self):
        h = _CaseInsensitiveHeaders({"Content-Type": "text/html"})
        assert h["content-type"] == "text/html"
        with pytest.raises(KeyError):
            h["missing"]

    def test_iter_and_len(self):
        h = _CaseInsensitiveHeaders({"A": "1", "B": "2"})
        assert len(h) == 2
        assert set(h) == {"A", "B"}


class TestResponseCacheKey:
    def test_none_for_post(self):
        assert _response_cache_key("POST", "u", None, None, None, {"a": "1"}, None) is None

    def test_none_for_json_body(self):
        assert _response_cache_key("GET", "u", None, None, None, None, {"a": 1}) is None

    def test_none_for_range(self):
        assert _response_cache_key("GET", "u", None, None, {"Range": "bytes=0-1"}, None, None) is None

    def test_key_stable_and_discriminating(self):
        k1 = _response_cache_key("GET", "u", "p", "fp", {"A": "1"}, None, None)
        k2 = _response_cache_key("GET", "u", "p", "fp", {"A": "1"}, None, None)
        assert k1 == k2
        # 代理 / 指纹 / 请求头任一不同 → 键不同.
        assert k1 != _response_cache_key("GET", "u", "p2", "fp", {"A": "1"}, None, None)
        assert k1 != _response_cache_key("GET", "u", "p", "fp2", {"A": "1"}, None, None)
        assert k1 != _response_cache_key("GET", "u", "p", "fp", {"A": "2"}, None, None)


# --- WebClient 接入行为 -----------------------------------------------------


class TestWebClientResponseCache:
    @pytest.mark.asyncio(loop_scope="function")
    async def test_hit_avoids_second_fetch(self, monkeypatch: pytest.MonkeyPatch):
        client = _client(response_cache=ResponseCache(300.0, 16))
        session = _StubSession(_StubResponse(url="https://example.com/x", content=b"page-v1"))
        monkeypatch.setattr(client, "_session", session)

        r1 = await client.request("GET", "https://example.com/x")
        r2 = await client.request("GET", "https://example.com/x")

        assert r1.content == r2.content == b"page-v1"
        assert isinstance(r2, CachedResponse)
        assert len(session.calls) == 1  # 第二次命中缓存, 未触网

    @pytest.mark.asyncio(loop_scope="function")
    async def test_hit_restores_fields(self, monkeypatch: pytest.MonkeyPatch):
        client = _client(response_cache=ResponseCache(300.0, 16))
        session = _StubSession(
            _StubResponse(
                url="https://example.com/final",
                content=b"<html>hi</html>",
                status=200,
                headers={"Content-Type": "text/html; charset=utf-8"},
            )
        )
        monkeypatch.setattr(client, "_session", session)

        await client.request("GET", "https://example.com/x")
        cached = await client.request("GET", "https://example.com/x")

        assert isinstance(cached, CachedResponse)
        assert cached.status_code == 200
        assert cached.content == b"<html>hi</html>"
        assert str(cached.url) == "https://example.com/final"  # 还原重定向终址
        assert cached.headers.get("content-type") == "text/html; charset=utf-8"  # 大小写不敏感

    @pytest.mark.asyncio(loop_scope="function")
    async def test_miss_then_store(self, monkeypatch: pytest.MonkeyPatch):
        client = _client(response_cache=ResponseCache(300.0, 16))
        session = _StubSession(_StubResponse(content=b"x"))
        monkeypatch.setattr(client, "_session", session)

        assert len(_cache_of(client)) == 0
        await client.request("GET", "https://example.com/x")
        assert len(_cache_of(client)) == 1  # 2xx 文本响应已写入

    @pytest.mark.asyncio(loop_scope="function")
    async def test_post_not_cached(self, monkeypatch: pytest.MonkeyPatch):
        client = _client(response_cache=ResponseCache(300.0, 16))
        session = _StubSession(_StubResponse(content=b"ok"))
        monkeypatch.setattr(client, "_session", session)

        await client.request("POST", "https://example.com/x", data={"a": "1"})
        await client.request("POST", "https://example.com/x", data={"a": "1"})

        assert len(session.calls) == 2
        assert len(_cache_of(client)) == 0

    @pytest.mark.asyncio(loop_scope="function")
    async def test_range_not_cached(self, monkeypatch: pytest.MonkeyPatch):
        client = _client(response_cache=ResponseCache(300.0, 16))
        session = _StubSession(_StubResponse(content=b"chunk", status=206))
        monkeypatch.setattr(client, "_session", session)

        await client.request("GET", "https://example.com/x", headers={"Range": "bytes=0-4"})
        await client.request("GET", "https://example.com/x", headers={"Range": "bytes=0-4"})

        assert len(session.calls) == 2
        assert len(_cache_of(client)) == 0

    @pytest.mark.asyncio(loop_scope="function")
    async def test_large_body_not_cached(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(http, "_RESPONSE_CACHE_MAX_BODY", 16)
        client = _client(response_cache=ResponseCache(300.0, 16))
        session = _StubSession(_StubResponse(content=b"x" * 100))
        monkeypatch.setattr(client, "_session", session)

        await client.request("GET", "https://example.com/x")
        await client.request("GET", "https://example.com/x")

        assert len(_cache_of(client)) == 0
        assert len(session.calls) == 2

    @pytest.mark.asyncio(loop_scope="function")
    async def test_binary_content_type_not_cached(self, monkeypatch: pytest.MonkeyPatch):
        client = _client(response_cache=ResponseCache(300.0, 16))
        session = _StubSession(_StubResponse(content=b"\x89PNG\r\n", headers={"Content-Type": "image/jpeg"}))
        monkeypatch.setattr(client, "_session", session)

        await client.request("GET", "https://example.com/cover.jpg")
        await client.request("GET", "https://example.com/cover.jpg")

        assert len(_cache_of(client)) == 0
        assert len(session.calls) == 2

    @pytest.mark.asyncio(loop_scope="function")
    async def test_error_status_not_cached(self, monkeypatch: pytest.MonkeyPatch):
        client = _client(response_cache=ResponseCache(300.0, 16))
        session = _StubSession(_StubResponse(content=b"nope", status=404))
        monkeypatch.setattr(client, "_session", session)

        with pytest.raises(http.RequestError):
            await client.request("GET", "https://example.com/x")

        assert len(_cache_of(client)) == 0

    @pytest.mark.asyncio(loop_scope="function")
    async def test_bypass_circuit_skips_cache(self, monkeypatch: pytest.MonkeyPatch):
        client = _client(response_cache=ResponseCache(300.0, 16))
        session = _StubSession(_StubResponse(content=b"x"))
        monkeypatch.setattr(client, "_session", session)

        await client.request("GET", "https://example.com/x", bypass_circuit=True)
        await client.request("GET", "https://example.com/x", bypass_circuit=True)

        assert len(session.calls) == 2
        assert len(_cache_of(client)) == 0

    @pytest.mark.asyncio(loop_scope="function")
    async def test_bypass_cache_force_refresh(self, monkeypatch: pytest.MonkeyPatch):
        client = _client(response_cache=ResponseCache(300.0, 16))
        session = _StubSession(_StubResponse(content=b"v1"))
        monkeypatch.setattr(client, "_session", session)

        await client.request("GET", "https://example.com/x")  # 写入缓存
        assert len(_cache_of(client)) == 1
        r2 = await client.request("GET", "https://example.com/x", bypass_cache=True)  # 强制刷新, 跳过缓存

        assert r2.content == b"v1"
        assert len(session.calls) == 2  # 绕过缓存再次触网

    @pytest.mark.asyncio(loop_scope="function")
    async def test_cache_serves_when_host_circuit_open(self, monkeypatch: pytest.MonkeyPatch):
        """韧性增益: 熔断开路 ≠ 无可用数据; 缓存的热页仍应服务, 不触网也不抛错."""
        breaker = ConnectivityBreaker(host_fail_max=1, host_cooldown_s=300.0)
        client = _client(response_cache=ResponseCache(300.0, 16), connectivity=breaker)
        session = _StubSession(_StubResponse(url="https://example.com/x", content=b"<html>hi</html>"))
        monkeypatch.setattr(client, "_session", session)

        r1 = await client.request("GET", "https://example.com/x")
        assert r1.content == b"<html>hi</html>"
        assert len(session.calls) == 1

        await breaker.record_failure("host:example.com")
        assert breaker.is_open("host:example.com") is True

        r2 = await client.request("GET", "https://example.com/x")  # 熔断开路, 但缓存命中
        assert r2.content == b"<html>hi</html>"
        assert len(session.calls) == 1  # 关键: 未再触网

    @pytest.mark.asyncio(loop_scope="function")
    async def test_no_cache_object_never_hits(self, monkeypatch: pytest.MonkeyPatch):
        client = _client(response_cache=None)
        session = _StubSession(_StubResponse(content=b"x"))
        monkeypatch.setattr(client, "_session", session)

        await client.request("GET", "https://example.com/x")
        await client.request("GET", "https://example.com/x")

        assert len(session.calls) == 2  # 无缓存 → 每次触网 (向后兼容)

    @pytest.mark.asyncio(loop_scope="function")
    async def test_get_text_and_get_json_on_cached(self, monkeypatch: pytest.MonkeyPatch):
        client = _client(response_cache=ResponseCache(300.0, 16))
        html = _StubSession(_StubResponse(content=b"<html>hi</html>", headers={"Content-Type": "text/html"}))
        monkeypatch.setattr(client, "_session", html)
        t1 = await client.get_text("https://example.com/x")
        t2 = await client.get_text("https://example.com/x")
        assert t1 == t2 == "<html>hi</html>"
        assert len(html.calls) == 1

        js = _StubSession(_StubResponse(content=b'{"a": 1}', headers={"Content-Type": "application/json"}))
        monkeypatch.setattr(client, "_session", js)
        j1 = await client.get_json("https://example.com/data")
        j2 = await client.get_json("https://example.com/data")
        assert j1 == j2 == {"a": 1}
        assert len(js.calls) == 1
