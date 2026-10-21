"""环境性失败判定: 哪些站点结论值得自动重排."""

from amane.handlers.retry_policy import RETRYABLE_REASONS, is_environmental_failure
from amane.net.errors import FailureReason
from amane.observability.models import SiteOutcomeKind, SiteOutcomeRecord


def _row(reason: FailureReason | None, outcome: SiteOutcomeKind = SiteOutcomeKind.FAILED) -> SiteOutcomeRecord:
    return SiteOutcomeRecord(site="javdb", outcome=outcome, reason=reason)


def test_no_outcomes_is_terminal() -> None:
    """无站点结论 (无可用来源 / 参数无效) 属配置问题, 不重排."""
    assert is_environmental_failure([]) is False


def test_circuit_open_only_is_retryable() -> None:
    """四个来源全被熔断短路: 一个结论都没拿到, 必须重排."""
    rows = [_row(FailureReason.CIRCUIT_OPEN) for _ in range(4)]
    assert is_environmental_failure(rows) is True


def test_circuit_open_mixed_with_no_metadata_is_retryable() -> None:
    """混合时只要有一个环境性原因即可重排: 未触网的来源没有发言权."""
    rows = [_row(FailureReason.CIRCUIT_OPEN), _row(FailureReason.NO_USABLE_METADATA)]
    assert is_environmental_failure(rows) is True


def test_all_no_usable_metadata_is_terminal() -> None:
    """全部来源可达但无数据: 业务性结论, 重排必然复现."""
    rows = [_row(FailureReason.NO_USABLE_METADATA) for _ in range(4)]
    assert is_environmental_failure(rows) is False


def test_all_skipped_is_retryable() -> None:
    """全部跳过 = 环境未验证 (可用性探测判死), 与「确认无数据」不同."""
    rows = [_row(FailureReason.CIRCUIT_OPEN, SiteOutcomeKind.SKIPPED) for _ in range(3)]
    assert is_environmental_failure(rows) is True


def test_skipped_without_failure_is_retryable() -> None:
    """跳过行即使不带原因也应视为未验证."""
    rows = [SiteOutcomeRecord(site="official", outcome=SiteOutcomeKind.SKIPPED)]
    assert is_environmental_failure(rows) is True


def test_ok_with_skip_is_terminal() -> None:
    """存在已成功的来源说明链路正常, 不因个别跳过而重排."""
    rows = [
        SiteOutcomeRecord(site="javdb", outcome=SiteOutcomeKind.OK),
        SiteOutcomeRecord(site="official", outcome=SiteOutcomeKind.SKIPPED),
    ]
    assert is_environmental_failure(rows) is False


def test_rate_limited_and_server_error_are_retryable() -> None:
    """限流与 5xx 是站点侧应激, 退避后可恢复."""
    assert is_environmental_failure([_row(FailureReason.RATE_LIMITED)]) is True
    assert is_environmental_failure([_row(FailureReason.SERVER_ERROR)]) is True


def test_blocked_reasons_are_terminal() -> None:
    """反爬拦截与凭据/地理限制需要改配置或换出口, 重排只会掩盖问题."""
    for reason in (
        FailureReason.CLOUDFLARE_CHALLENGE,
        FailureReason.CLOUDFLARE_BLOCKED,
        FailureReason.IP_BANNED,
        FailureReason.GEO_RESTRICTED,
        FailureReason.LOGIN_REQUIRED,
        FailureReason.PARSE_ERROR,
        FailureReason.API_ERROR,
        FailureReason.NOT_FOUND,
        FailureReason.UNEXPECTED,
        FailureReason.CRAWLER_UNAVAILABLE,
    ):
        assert is_environmental_failure([_row(reason)]) is False, reason


def test_retryable_reasons_are_disjoint_from_terminal_examples() -> None:
    """可重排集合不得包含需要人工介入的原因 (防后续误加)."""
    assert FailureReason.CLOUDFLARE_CHALLENGE not in RETRYABLE_REASONS
    assert FailureReason.NO_USABLE_METADATA not in RETRYABLE_REASONS
    assert FailureReason.CIRCUIT_OPEN in RETRYABLE_REASONS
