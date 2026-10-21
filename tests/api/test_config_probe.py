"""``POST /config/probe``: 流式日志、未保存值只用于本次测试、非法入参在开流前被拒."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from httpx2 import AsyncClient


def _rows(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


class TestConfigProbeHttp:
    @pytest.mark.asyncio(loop_scope="function")
    async def test_stream_is_ndjson_and_ends_with_done(self, client: AsyncClient):
        resp = await client.post("config/probe", json={"section": "llm"})

        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/x-ndjson")
        rows = _rows(resp.text)
        assert rows, "至少要有一条事件"

        done = rows[-1]
        assert done == {"done": True, "ok": False, "elapsed_ms": done["elapsed_ms"]}
        assert all(row["done"] is False for row in rows[:-1])
        # seq 连续, 前端才能稳定排序
        assert [row["seq"] for row in rows[:-1]] == list(range(1, len(rows)))
        assert rows[0]["level"] == "info"
        assert rows[-2]["level"] == "error"

    @pytest.mark.asyncio(loop_scope="function")
    async def test_values_are_used_for_this_run_only(self, client: AsyncClient):
        before = (await client.get("config")).json()

        resp = await client.post(
            "config/probe",
            json={"section": "llm", "values": {"model": "probe-only-model"}},
        )

        assert resp.status_code == 200
        # 表单值生效: 行内日志应带这次填的模型名
        assert any("probe-only-model" in json.dumps(row, ensure_ascii=False) for row in _rows(resp.text))
        # 但没有落盘
        assert (await client.get("config")).json() == before

    @pytest.mark.asyncio(loop_scope="function")
    async def test_unsaved_values_can_change_the_verdict(self, client: AsyncClient):
        resp = await client.post(
            "config/probe",
            json={"section": "network", "values": {"proxy": "ftp://127.0.0.1:21"}},
        )

        rows = _rows(resp.text)
        assert rows[-1]["ok"] is False
        assert any("不受支持" in (row.get("detail") or "") for row in rows)

    @pytest.mark.asyncio(loop_scope="function")
    async def test_unknown_section_is_rejected_before_streaming(self, client: AsyncClient):
        resp = await client.post("config/probe", json={"section": "logging"})

        assert resp.status_code == 422
        assert "不支持测试" in resp.json()["detail"]

    @pytest.mark.asyncio(loop_scope="function")
    async def test_invalid_values_are_rejected(self, client: AsyncClient):
        resp = await client.post(
            "config/probe",
            json={"section": "search", "values": {"keyword_enabled": "not-a-bool"}},
        )

        assert resp.status_code == 422
