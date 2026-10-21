import json
from collections.abc import AsyncIterator
from dataclasses import asdict
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import ValidationError

from ...config import HotSettings
from ...events import Event, EventType
from ...probe import ProbeContext, Recorder, is_supported, run_probe, supported_sections
from ..deps import ConfigDep, PluginManagerDep, RuntimeDep
from ..models import ConfigProbeRequest

logger = structlog.get_logger()

router = APIRouter(prefix="/config", tags=["config"])

_PROBE_MEDIA_TYPE = "application/x-ndjson"
"""一行一个 JSON. 用 NDJSON 而不是 SSE: 单向、无重连语义, 前端一个 reader 循环就够."""


@router.get("")
async def get_config(config: ConfigDep) -> HotSettings:
    return config.hot


@router.patch("")
async def update_config(req: dict[str, Any], config: ConfigDep, runtime: RuntimeDep) -> HotSettings:
    """先用当前会话的插件目录校验新路由和插件配置, 再持久化."""
    changed_sections = list(req.keys())
    logger.info("config update requested", sections=changed_sections)

    try:
        preview = config.preview(req)
        if runtime.plugin_manager is not None:
            runtime.plugin_manager.validate_hot_settings(preview, require_available=False)
        config.update(req)
    except (KeyError, ValidationError, ValueError) as e:
        logger.warning("config update rejected", error=str(e), sections=changed_sections)
        raise HTTPException(status_code=422, detail=str(e)) from e

    await runtime.apply_rebuild()
    logger.info("runtime rebuilt after config update", sections=changed_sections)

    await runtime.event_bus.broadcast(Event(type=EventType.LOG, data={"type": "config.updated"}))

    return config.hot


@router.get("/schema")
async def get_config_schema(plugin_manager: PluginManagerDep) -> dict:
    return plugin_manager.augment_config_schema(HotSettings.model_json_schema())


def _line(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False) + "\n").encode()


@router.post("/probe")
async def probe_config(req: ConfigProbeRequest, config: ConfigDep, runtime: RuntimeDep) -> StreamingResponse:
    """对一个配置分区做自检, 边跑边把步骤推给前端.

    ``values`` 存在时按"未保存的表单值"测: ``preview`` 只做校验与合并, 不落盘也不触发 rebuild,
    所以点一次测试不会顺手改掉线上配置. 校验失败照常 422 —— 连 Pydantic 都过不了的配置没必要发请求.

    单步失败只体现在事件里, 流始终以 ``done`` 收尾: 一次测试的产出就是这份日志, 用 5xx 表达
    "代理不通"会让前端拿不到已经产生的那几步 (与 ``/api/network/check`` 同一取向).
    """
    if not is_supported(req.section):
        raise HTTPException(
            status_code=422,
            detail=f"分区 {req.section!r} 不支持测试; 可选: {', '.join(supported_sections())}",
        )

    hot = config.hot
    if req.values is not None:
        try:
            hot = config.preview({req.section: req.values})
        except (KeyError, ValidationError, ValueError) as e:
            logger.warning("config probe rejected", section=req.section, error=str(e))
            raise HTTPException(status_code=422, detail=f"{req.section}: {e}") from e

    recorder = Recorder()
    ctx = ProbeContext(
        hot=hot,
        saved=config.hot,
        runtime=runtime,
        data_dir=config.cold.data_dir,
        db_path=config.cold.db_path,
        rec=recorder,
    )
    logger.info("config probe started", section=req.section, overridden=req.values is not None)

    async def stream() -> AsyncIterator[bytes]:
        failed = False
        async for event in run_probe(req.section, ctx):
            failed = failed or event.failed
            yield _line({**asdict(event), "level": str(event.level), "done": False})
        logger.info(
            "config probe finished",
            section=req.section,
            ok=not failed,
            elapsed_ms=recorder.elapsed_ms,
        )
        yield _line({"done": True, "ok": not failed, "elapsed_ms": recorder.elapsed_ms})

    return StreamingResponse(
        stream(),
        media_type=_PROBE_MEDIA_TYPE,
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
