"""自动重排的预算与退避.

失败是否为环境性由 handler 判定 (``TaskResult.retryable``); 本模块只回答两件事: 还能不能重排
(次数预算), 以及这次要等多久 (指数退避 + 抖动).

退避是必要的, 不是修饰: 连接健康熔断的冷却时间以分钟计, 立即重排会在开路期内被短路空转, 白白耗尽
预算. 首次延迟必须大于代理熔断冷却 (120s), 否则首次重排必然再次撞在开路上.
"""

import random
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import HotSettings
    from ..db.models import Task
    from ..handlers.protocol import TaskResult

DEFAULT_MAX_ATTEMPTS = 8
DEFAULT_BASE_DELAY_S = 180.0
DEFAULT_MAX_DELAY_S = 3600.0
"""八次重排的累计等待约 4.5 小时, 与实测的故障窗口 (约 4 小时) 量级一致."""

_JITTER_RATIO = 0.2
"""±20% 抖动: 让同一批失败的任务不在同一秒到点, 避免重排涌入."""

_MAX_EXPONENT = 16
"""指数上限, 防止数值溢出; 结果本来就被 ``max_delay_s`` 截断."""


def _sample_jitter() -> float:
    return random.uniform(-_JITTER_RATIO, _JITTER_RATIO)


@dataclass(frozen=True)
class RetryBudget:
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    """允许的自动重排次数; <= 0 关闭自动重排."""
    base_delay_s: float = DEFAULT_BASE_DELAY_S
    max_delay_s: float = DEFAULT_MAX_DELAY_S
    jitter: Callable[[], float] = field(default=_sample_jitter)
    """返回抖动比例的采样函数, 取值区间 [-0.2, 0.2]; 测试可注入固定值."""

    @classmethod
    def from_config(cls, hot: HotSettings | None) -> RetryBudget:
        """按热配置构造; 无配置 (未注入 ``get_hot``) 时用默认值."""
        if hot is None:
            return cls()
        worker = hot.worker
        return cls(
            max_attempts=worker.retry_max_attempts,
            base_delay_s=worker.retry_base_delay_s,
            max_delay_s=worker.retry_max_delay_s,
        )

    def should_defer(self, result: TaskResult, task: Task) -> bool:
        """是否退避重排: handler 判定为环境性失败, 且已用掉的重排次数未达上限."""
        if self.max_attempts <= 0 or not result.retryable:
            return False
        return task.retries < self.max_attempts

    def delay_for(self, attempt: int) -> float:
        """第 ``attempt`` 次重排 (从 0 计数) 的等待秒数: 指数退避, 抖动后不低于 1 秒."""
        exponent = min(max(attempt, 0), _MAX_EXPONENT)
        ceiling = min(self.base_delay_s * (2.0**exponent), self.max_delay_s)
        return max(1.0, ceiling * (1.0 + self.jitter()))
