"""游客 token 鉴权: 免密钥上游用短期 JWT, 由 Amane 自行取得并在过期前刷新.

上游按 ``deviceId`` 发放有效期 30 分钟的 JWT, 同 deviceId 在有效期内复用同一 token,
因此 deviceId 必须持久化, 否则每次启动都要重新换取.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path

import httpx2

TOKEN_URL = "https://api2.immersivetranslate.com/free-model/get-token"
"""游客 token 端点; 响应的 ``data`` 字段直接是 JWT 字符串."""

DEVICE_ID_FILENAME = "llm_device_id"
"""deviceId 在 ``data_dir`` 中的文件名."""

GUEST_API_KEY = "guest-token"
"""OpenAI 客户端要求非空 ``api_key``; 该占位值被鉴权流程覆盖, 不进入请求."""

_REFRESH_SECONDS = 1_500.0
"""服务端 TTL 30 分钟; 提前 5 分钟刷新, 避免请求在途中过期."""


def load_or_create_device_id(path: Path | None) -> str:
    """读取持久化的 deviceId; 文件缺席时生成并写入.

    ``path`` 为 ``None`` (无 data_dir 的精简构造) 时返回进程级随机值, 不落盘.
    """
    if path is None:
        return str(uuid.uuid4())
    try:
        existing = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        existing = ""
    if existing:
        return existing
    device_id = str(uuid.uuid4())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(device_id, encoding="utf-8")
    return device_id


class GuestTokenAuth(httpx2.Auth):
    """向请求注入游客 JWT; 过期后下一次请求自动换新, 并发请求共享同一次刷新."""

    def __init__(self, device_id: str, *, proxy: str | None = None) -> None:
        self._device_id = device_id
        self._proxy = proxy
        self._token: str | None = None
        self._refresh_at = 0.0
        self._lock = asyncio.Lock()

    async def _current(self) -> str:
        if self._token is not None and time.monotonic() < self._refresh_at:
            return self._token
        async with self._lock:
            if self._token is not None and time.monotonic() < self._refresh_at:
                return self._token
            token = await self._fetch()
            self._token = token
            self._refresh_at = time.monotonic() + _REFRESH_SECONDS
            return token

    async def _fetch(self) -> str:
        """换 token 用独立客户端: 复用带鉴权的客户端会让取 token 的请求再次触发鉴权.

        不继承环境代理 —— 系统代理对上游端点常返回 404, 只有显式配置的 ``proxy`` 才走.
        """
        async with httpx2.AsyncClient(proxy=self._proxy, timeout=httpx2.Timeout(20.0), trust_env=False) as client:
            response = await client.get(TOKEN_URL, params={"deviceId": self._device_id})
        response.raise_for_status()
        token = response.json()["data"]
        if not isinstance(token, str) or not token:
            raise RuntimeError("游客 token 端点未返回字符串")
        return token

    async def async_auth_flow(self, request: httpx2.Request) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        request.headers["Authorization"] = f"Bearer {await self._current()}"
        yield request
