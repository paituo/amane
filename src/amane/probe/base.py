"""配置自检的基座: 一步一条事件, 结果只回给点按钮的人.

与 ``crawlers.connectivity`` 分属两层: 那边探测**站点**可达性并据此影响调度决策, 这边只回答
「刚填的这段配置对不对」. 因此不进任务队列、不写库、不改配置 —— 探针可以在保存之前跑,
拿的是 ``ConfigManager.preview`` 合并出来的临时配置.

一条事件 = 一次 ``yield``: 流式输出才能让"模型正在下载"和"卡住了"在界面上有区别.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..app.runtime import AppRuntime
    from ..config import HotSettings


class ProbeLevel(StrEnum):
    """事件级别. 只有 ``ERROR`` 会把整次测试判为失败; ``WARN`` 表示"能用但有代价"."""

    INFO = "info"
    OK = "ok"
    WARN = "warn"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class ProbeEvent:
    """一步的结果.

    ``elapsed_ms`` 自探针开始计, ``step_ms`` 自上一事件计: 前者回答"一共等了多久",
    后者回答"这一步本身花了多久". 加载模型那一步两者相差很大, 分开记才看得出钱花在哪.
    """

    seq: int
    level: ProbeLevel
    message: str
    detail: str | None = None
    elapsed_ms: int = 0
    step_ms: int = 0

    @property
    def failed(self) -> bool:
        return self.level is ProbeLevel.ERROR


class Recorder:
    """串行发号: 前端按 ``seq`` 排序, 不依赖流里事件的到达顺序."""

    def __init__(self) -> None:
        now = time.perf_counter()
        self._start = now
        self._last = now
        self._seq = 0

    @property
    def elapsed_ms(self) -> int:
        return int((time.perf_counter() - self._start) * 1000)

    def emit(self, level: ProbeLevel, message: str, detail: str | None = None) -> ProbeEvent:
        now = time.perf_counter()
        self._seq += 1
        event = ProbeEvent(
            seq=self._seq,
            level=level,
            message=message,
            detail=detail,
            elapsed_ms=int((now - self._start) * 1000),
            step_ms=int((now - self._last) * 1000),
        )
        self._last = now
        return event

    def info(self, message: str, detail: str | None = None) -> ProbeEvent:
        return self.emit(ProbeLevel.INFO, message, detail)

    def ok(self, message: str, detail: str | None = None) -> ProbeEvent:
        return self.emit(ProbeLevel.OK, message, detail)

    def warn(self, message: str, detail: str | None = None) -> ProbeEvent:
        return self.emit(ProbeLevel.WARN, message, detail)

    def error(self, message: str, detail: str | None = None) -> ProbeEvent:
        return self.emit(ProbeLevel.ERROR, message, detail)


@dataclass(slots=True)
class ProbeContext:
    """一次测试的全部输入. 探针不得回写这些字段, 也不得持久化任何东西."""

    hot: HotSettings
    """已合并未保存改动的配置; 探针一律以它为准."""

    saved: HotSettings
    """已落盘的配置. 只用来判断运行时里已有的实例能否直接复用 (避免重复加载模型)."""

    runtime: AppRuntime | None
    """运行中的装配; 进程未起完整运行时为 ``None``, 探针退化为"只按配置检查"."""

    data_dir: Path
    """模型缓存与业务库所在目录."""

    db_path: Path
    """业务库路径; 词索引与向量表都在这个文件 (向量另开 ``search_vec.db``)."""

    rec: Recorder


ProbeFn = Callable[[ProbeContext], AsyncIterator[ProbeEvent]]


def describe_error(exc: BaseException, *, limit: int = 800) -> str:
    """异常 → 可读细节. 上游 SDK 常把响应体塞进 ``str(exc)``, 那正是用户要看的 401/404 原因."""
    text = str(exc).strip() or exc.__class__.__name__
    if len(text) > limit:
        text = f"{text[:limit]}… (共 {len(text)} 字符)"
    return f"{type(exc).__name__}: {text}"
