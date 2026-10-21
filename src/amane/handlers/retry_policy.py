"""环境性失败判定: 决定一次失败是否值得自动重排.

只按站点结果 (``SiteOutcomeRecord``) 判定, 不解析 handler 的结果字段结构, 因此其它类型任务的
handler 也能复用同一套规则.

区分依据是「失败是否由可再生环境造成」: 熔断短路 / 连接失败 / 超时 / 5xx / 限流 / 空应答在一次运行
里可能纯属抖动, 重排后可恢复; 而站点给出了明确结论 (无此片 / 无可用字段 / 结构变更 / 反爬拦截 /
凭据缺失) 时, 重排只会得到同一结论.
"""

from collections.abc import Sequence

from ..net.errors import FailureReason
from ..observability.models import SiteOutcomeKind, SiteOutcomeRecord

# 连接健康熔断开路 (请求被短路), 连接层失败, 超时, 5xx, 其余 4xx/5xx, 429 限流, 空应答.
# 未列入的原因 (无此片 / 无可用字段 / 结构变更 / 反爬拦截 / 凭据缺失 / 地理限制 / 未归类异常)
# 重试不改变结果, 反复重排只会掩盖配置与反爬问题.
RETRYABLE_REASONS: frozenset[FailureReason] = frozenset(
    {
        FailureReason.CIRCUIT_OPEN,
        FailureReason.NETWORK,
        FailureReason.TIMEOUT,
        FailureReason.SERVER_ERROR,
        FailureReason.HTTP_ERROR,
        FailureReason.RATE_LIMITED,
        FailureReason.EMPTY_RESPONSE,
    }
)


def is_environmental_failure(outcomes: Sequence[SiteOutcomeRecord]) -> bool:
    """判定一次刮削的站点结果是否属于环境性失败.

    规则:

    1. 无站点结果 (参数校验失败 / 无可用来源 / 处理器缺失) 判否: 属于配置或代码问题.
    2. 存在 ``failed`` 且原因为可重排原因判是.
    3. 结果为「全部跳过」(批次级可用性探测把来源判为不可用, 一个请求都没发) 时判是: 这是
       「环境未验证」而非「确认无数据」; 重排时 handler 会丢弃批次级禁用来源, 下轮真正发请求.
    4. 其余判否.
    """
    if not outcomes:
        return False
    if any(row.outcome is SiteOutcomeKind.FAILED and row.reason in RETRYABLE_REASONS for row in outcomes):
        return True
    return all(row.outcome is SiteOutcomeKind.SKIPPED for row in outcomes)
