"""跨任务 HTTP 响应缓存 (类 Scrapy HTTPCACHE, 进程内 TTL + LRU).

挂在进程级单例 :class:`~amane.net.http.WebClient` 上, 天然跨任务共享: 同一来源的搜索/详情页被多个
番号的搜刮任务反复请求时, 命中即直接返回, 不再触网、不进熔断统计. 内存态、不落库、不跨重启.

与 ``CacheKind.metadata`` (按 media_file 的 per-site 解析快照) 职责区分: 本层缓存的是**原始 HTTP
响应**, 更底层; 前者缓存的是**解析后的站点快照**. 两层叠加, 后者命中时前者根本不会被访问.

保守策略 (见 ``net/http.py`` 的 ``_response_cache_key`` / ``_store_response``):
- 只缓存 **GET**、无请求体、无 ``Range`` (分片下载) 的 **2xx** 响应;
- 只缓存**文本类**内容 (html/json/xml/纯文本), 图片/视频等二进制与大响应天然排除;
- 诊断探测 (``bypass_circuit``) 与强制刷新 (``bypass_cache``) 一律不走缓存;
- 短 TTL (默认 300s) 约束「站点 schema 变更」的可见延迟, 且可通过配置置 0 彻底关闭.
"""

from __future__ import annotations

import json
import time
from collections import OrderedDict
from collections.abc import Iterator, Mapping
from typing import Any


class _CaseInsensitiveHeaders(Mapping[str, str]):
    """大小写不敏感的响应头视图; 只读, 匹配 curl_cffi ``Response.headers`` 的 ``.get`` 语义."""

    __slots__ = ("_raw",)

    def __init__(self, raw: Mapping[str, str]) -> None:
        self._raw = {str(k): v for k, v in raw.items()}

    def get(self, key: object, default: Any = None) -> Any:
        """按大小写不敏感匹配取值; 非字符串键直接返回 ``default`` (Mapping 契约要求接受 object)."""
        if not isinstance(key, str):
            return default
        lowered = key.lower()
        for k, v in self._raw.items():
            if k.lower() == lowered:
                return v
        return default

    def __getitem__(self, key: str) -> str:
        sentinel = object()
        value = self.get(key, sentinel)
        if value is sentinel:
            raise KeyError(key)
        assert isinstance(value, str)
        return value

    def __iter__(self) -> Iterator[str]:
        return iter(self._raw)

    def __len__(self) -> int:
        return len(self._raw)

    def __repr__(self) -> str:
        return f"_CaseInsensitiveHeaders({self._raw!r})"


class CachedResponse:
    """从缓存还原的应答; 提供调用方实际用到的 ``Response`` 接口子集.

    curl_cffi 的 ``Response`` 绑定 curl easy handle, 无法离线构造/反序列化, 故用此鸭子类型在缓存命中
    时替代. 覆盖 ``WebClient`` 内部与各 crawler 的用法: ``status_code`` / ``headers.get`` /
    ``content`` / ``text`` / ``json()`` / ``url`` / ``encoding``.
    """

    __slots__ = ("content", "cookies", "encoding", "headers", "status_code", "url")

    def __init__(
        self,
        *,
        status_code: int,
        headers: Mapping[str, str],
        content: bytes,
        url: str,
        encoding: str = "utf-8",
    ) -> None:
        self.status_code = status_code
        self.headers = _CaseInsensitiveHeaders(headers)
        self.content = content
        self.url = url
        self.encoding = encoding
        self.cookies: dict[str, str] = {}

    @property
    def text(self) -> str:
        return self.content.decode(self.encoding or "utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.text)

    def __repr__(self) -> str:
        return f"CachedResponse(status={self.status_code}, url={self.url!r}, bytes={len(self.content)})"


class ResponseCache:
    """进程内 TTL + LRU 响应缓存. ``ttl_s <= 0`` 或 ``max_entries <= 0`` 时整体关闭."""

    def __init__(self, ttl_s: float = 300.0, max_entries: int = 256) -> None:
        self._ttl_s = ttl_s
        self._max_entries = max_entries
        self._store: OrderedDict[tuple, tuple[float, CachedResponse]] = OrderedDict()

    @property
    def enabled(self) -> bool:
        return self._ttl_s > 0 and self._max_entries > 0

    def __len__(self) -> int:
        return len(self._store)

    def get(self, key: tuple) -> CachedResponse | None:
        if not self.enabled:
            return None
        item = self._store.get(key)
        if item is None:
            return None
        stored_at, resp = item
        if time.monotonic() - stored_at >= self._ttl_s:
            self._store.pop(key, None)
            return None
        self._store.move_to_end(key)  # LRU: 命中即视为最近使用
        return resp

    def put(self, key: tuple, resp: CachedResponse) -> None:
        if not self.enabled:
            return
        self._store[key] = (time.monotonic(), resp)
        self._store.move_to_end(key)
        while len(self._store) > self._max_entries:
            self._store.popitem(last=False)  # 淘汰最久未用
