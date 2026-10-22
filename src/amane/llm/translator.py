"""跨语系经缓存后经 pydantic-ai ``Model`` 翻译; 简繁经 ``zhconv``, 不经 LLM、不写入缓存.

已是目标语言返回 ``None``. ``build_translator`` 在 enabled=False、或密钥模式下缺 api_key 时返回 ``None``.
"""

import re
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import structlog
import zhconv
from aiolimiter import AsyncLimiter
from httpx2 import AsyncClient, Timeout, TransportError
from pydantic_ai.direct import model_request
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelRequest, SystemPromptPart, UserPromptPart
from pydantic_ai.models import Model

from ..enums import ApiType, Language, LLMAuthMode, LLMFailureKind, MetadataField
from ..utils.language import needs_llm_translation
from .cache import TranslationCache
from .guest_token import GUEST_API_KEY, GuestTokenAuth, load_or_create_device_id
from .model import build_model

logger = structlog.get_logger()

_TIMEOUT = 60.0
"""LLM 请求超时 (秒). 重试由 SDK 按其默认策略承担, 翻译器不配置次数."""

_CONTENT_POLICY_MARKERS: tuple[str, ...] = (
    "1301",
    "content_policy",
    "content_filter",
    "content management policy",
    "系统检测到输入或生成内容",
)
"""上游内容审核拒绝的正文特征. 命中即判定为确定性失败: 同样的文本重发必然还被拒."""

_RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
"""可恢复的状态码: 换个时机可能成功."""

_GATE_THRESHOLD = 3
"""连续多少次确定性失败后暂停翻译."""

_GATE_COOLDOWN_S = 600.0
"""暂停时长 (秒); 到期放行一个探针, 成功即复位."""


def _body_text(body: object | None) -> str:
    """把 ``ModelHTTPError.body`` 压成可检索的小写文本 (上游给的可能是 dict / str / 其它)."""
    if body is None:
        return ""
    if isinstance(body, str):
        return body.lower()
    if isinstance(body, Mapping):
        return " ".join(f"{k}={v}" for k, v in body.items()).lower()
    return str(body).lower()


def classify_llm_failure(exc: BaseException) -> LLMFailureKind:
    """按「再试一次有没有意义」给 LLM 异常分类.

    内容审核拒绝与鉴权/参数错误是确定性的: 同一 (端点, 模型, 文本) 重发只是重复等待,
    所以单独归类, 交给 ``_DeterministicFailureGate`` 累计并短路后续请求.
    """
    if isinstance(exc, ModelHTTPError):
        body = _body_text(exc.body)
        if any(marker in body for marker in _CONTENT_POLICY_MARKERS):
            return LLMFailureKind.CONTENT_POLICY
        if exc.status_code in (401, 403):
            return LLMFailureKind.AUTH
        if exc.status_code in _RETRYABLE_STATUSES:
            return LLMFailureKind.TRANSIENT
        if 400 <= exc.status_code < 500:
            return LLMFailureKind.BAD_REQUEST
        return LLMFailureKind.TRANSIENT
    if isinstance(exc, TimeoutError | TransportError):
        return LLMFailureKind.TRANSIENT
    return LLMFailureKind.UNKNOWN


class _DeterministicFailureGate:
    """连续确定性失败后暂停翻译一段时间 (与站点熔断同构).

    上游对同类文本恒定拒绝时, 每个任务都会各自白等一次失败 (实测单次 11.7 s, 51.6% 的任务命中),
    这里把这个「注定失败」信号升级成进程级的短期跳过; 冷却结束放行一个探针, 恢复即复位.
    """

    def __init__(
        self,
        threshold: int = _GATE_THRESHOLD,
        cooldown_s: float = _GATE_COOLDOWN_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._threshold = threshold
        self._cooldown_s = cooldown_s
        self._clock = clock
        self._failures = 0
        self._open_until = 0.0

    @property
    def open(self) -> bool:
        return self._clock() < self._open_until

    def record(self, kind: LLMFailureKind) -> None:
        if kind in (LLMFailureKind.TRANSIENT, LLMFailureKind.UNKNOWN):
            # 可恢复失败不能代表「这条路走不通」, 复位计数, 避免把偶发超时算成确定性拒绝.
            self._failures = 0
            return
        self._failures += 1
        if self._failures < self._threshold:
            return
        self._failures = 0
        self._open_until = self._clock() + self._cooldown_s
        logger.warning(
            "translation suspended after repeated deterministic failures",
            kind=str(kind),
            cooldown_s=self._cooldown_s,
        )

    def reset(self) -> None:
        self._failures = 0


_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)
"""去除提供商写进正文的思维链, 仅保留最终答案. ``ModelResponse.text`` 已排除 ``ThinkingPart``."""

# Language 枚举 → zhconv locale 代码 (仅中文变体).
_ZHCONV_LOCALE: dict[Language, str] = {
    Language.ZH_CN: "zh-cn",
    Language.ZH_TW: "zh-tw",
}

_LANG_NAME: dict[Language, str] = {
    Language.ZH_CN: "简体中文",
    Language.ZH_TW: "繁體中文",
    Language.JP: "日本語",
    Language.EN: "English",
}

_FIELD_HINT: dict[MetadataField, str] = {
    MetadataField.TITLE: "这是一部影片的标题, 翻译应简洁自然, 保留专有名词与番号.",
    MetadataField.PLOT: "这是一部影片的简介, 完整通顺地翻译全部内容.",
}

TARGET_LANG_PLACEHOLDER = "{target_lang}"
"""自定义提示词中引用目标语言名的占位符."""

_DEFAULT_SYSTEM_PROMPT = f"你是专业的影视元数据翻译. 将用户提供的文本翻译为{TARGET_LANG_PLACEHOLDER}."

_OUTPUT_CONSTRAINT = "只输出译文本身, 不要解释、不要引号、不要附加任何内容."


def build_system_prompt(
    target: Language,
    field: MetadataField,
    *,
    system_prompt: str | None = None,
    field_prompts: Mapping[MetadataField, str] | None = None,
) -> str:
    """组装 system 提示词 = 指令 + 字段说明 + 固定输出约束.

    ``system_prompt`` 覆盖内置指令, 空白时回退内置; ``field_prompts`` 逐字段覆盖内置说明,
    无覆盖或覆盖值为空白时回退内置. 输出约束不允许配置: 译文写入标量字段, 附加解释会污染元数据.
    占位符仅替换 ``{target_lang}``, 其余花括号按字面保留.
    """
    instruction = (system_prompt or "").strip() or _DEFAULT_SYSTEM_PROMPT
    override = (field_prompts or {}).get(field, "")
    hint = override.strip() or _FIELD_HINT.get(field, "")
    parts = [_render_placeholders(instruction, target)]
    if hint:
        parts.append(_render_placeholders(hint, target))
    parts.append(_OUTPUT_CONSTRAINT)
    return " ".join(parts)


def _render_placeholders(template: str, target: Language) -> str:
    """不使用 ``str.format``: 提示词允许出现 JSON 示例等花括号."""
    return template.replace(TARGET_LANG_PLACEHOLDER, _LANG_NAME[target])


class LLMTranslator:
    def __init__(
        self,
        model: Model,
        cache: TranslationCache | None = None,
        *,
        rate_limit: float = 2.0,
        system_prompt: str | None = None,
        field_prompts: Mapping[MetadataField, str] | None = None,
        skip_keywords: Sequence[str] = (),
        gate: _DeterministicFailureGate | None = None,
    ) -> None:
        self._model = model
        self._cache = cache
        # LLM 端点独立限速, 与站点 host 限速隔离: 桶容量 1, 严格平滑.
        # 包住一次翻译调用; 该调用内部的重试由 SDK 客户端承担.
        self._limiter = AsyncLimiter(1, 1 / rate_limit)
        self._system_prompt = system_prompt
        self._field_prompts: Mapping[MetadataField, str] = field_prompts or {}
        # 小写化后比较: 关键词多为中日文, 但混入的英文缩写按不区分大小写更稳妥.
        self._skip_keywords: tuple[str, ...] = tuple(k.lower() for k in skip_keywords if k.strip())
        self._gate = gate or _DeterministicFailureGate()

    async def translate(
        self, text: str, target: Language, field: MetadataField, *, use_cache: bool = True
    ) -> str | None:
        text = text.strip()
        if not text:
            return None

        # 限定词黑名单: 命中即保留原文, 不读缓存也不发请求. 这类文本在上游内容审核处必然被拒,
        # 省下的是「必败请求 + 单次约 12 秒等待」.
        if self._skip_keywords:
            lowered = text.lower()
            hit = next((k for k in self._skip_keywords if k in lowered), None)
            if hit is not None:
                logger.debug("translation skipped by keyword", field=str(field), keyword=hit)
                return None

        if needs_llm_translation(text, target):
            system = build_system_prompt(
                target,
                field,
                system_prompt=self._system_prompt,
                field_prompts=self._field_prompts,
            )
            # 缓存命中即跳过 LLM: 全缓存重刮, 配置不变时不重复翻译, 也避免 temperature 漂移.
            # 键含 system 提示词, 改提示词后旧译文不再命中. use_cache=False 时跳过读取
            # (强制重译), 但仍回写以刷新缓存.
            if self._cache is not None and use_cache:
                cached = await self._cache.get(text, target, field, system)
                if cached is not None:
                    return cached
            result = await self._ask(system_prompt=system, user_prompt=text)
            if not result:
                return None
            if self._cache is not None:
                await self._cache.put(text, target, field, system, result)
            return result

        # 中文文本: 简繁字形转换 (幂等); 共用字/已是目标变体则结果等于原文 → 返回 None 省去写回.
        locale = _ZHCONV_LOCALE.get(target)
        if locale is not None:
            converted = zhconv.convert(text, locale)
            return converted if converted != text else None

        return None

    async def _ask(self, *, system_prompt: str, user_prompt: str) -> str | None:
        """单轮请求. 无内容或抛异常时返回 ``None``, 不抛异常.

        失败按类别记账: 确定性失败 (内容审核/鉴权/参数) 连续累计到阈值后暂停翻译一段时间,
        避免每个任务都重新踩同一个必然失败的坑; 可恢复失败只记日志.
        """
        if self._gate.open:
            logger.debug("translation skipped, failure gate open")
            return None
        messages: list[ModelMessage] = [
            ModelRequest(parts=[SystemPromptPart(content=system_prompt), UserPromptPart(content=user_prompt)])
        ]
        async with self._limiter:
            try:
                response = await model_request(self._model, messages)
            except Exception as e:
                kind = classify_llm_failure(e)
                self._gate.record(kind)
                logger.warning(
                    "LLM request failed",
                    kind=str(kind),
                    retryable=kind is LLMFailureKind.TRANSIENT,
                    error=str(e),
                )
                return None
        self._gate.reset()
        text = response.text
        return _THINK.sub("", text).strip() if text else None


def build_translator(
    *,
    enabled: bool,
    api_type: ApiType,
    auth_mode: LLMAuthMode,
    api_key: str | None,
    base_url: str,
    model: str,
    rate_limit: float,
    proxy: str | None = None,
    device_id_path: Path | None = None,
    system_prompt: str | None = None,
    field_prompts: Mapping[MetadataField, str] | None = None,
    skip_keywords: Sequence[str] = (),
    cache: TranslationCache | None = None,
) -> LLMTranslator | None:
    """未启用、或密钥模式下缺密钥时返回 ``None``. ``cache`` 热重载时复用同一实例.

    ``guest_token`` 模式不读 ``api_key``: 鉴权由 ``GuestTokenAuth`` 在每次请求前注入,
    ``build_model`` 收到的占位密钥被其覆盖. ``device_id_path`` 为 deviceId 的持久化位置.
    """
    if not enabled:
        return None
    if auth_mode is LLMAuthMode.API_KEY and not api_key:
        return None

    auth: GuestTokenAuth | None = None
    effective_key = api_key
    if auth_mode is LLMAuthMode.GUEST_TOKEN:
        auth = GuestTokenAuth(load_or_create_device_id(device_id_path), proxy=proxy)
        effective_key = GUEST_API_KEY
    # 翻译路径自建传输客户端 (proxy + 超时) 并交给 provider; 助理不传, 由 pydantic-ai 构造默认客户端.
    http_client = AsyncClient(auth=auth, proxy=proxy, timeout=Timeout(_TIMEOUT), follow_redirects=True)
    return LLMTranslator(
        build_model(api_type, base_url=base_url, api_key=effective_key, model=model, http_client=http_client),
        cache,
        rate_limit=rate_limit,
        system_prompt=system_prompt,
        field_prompts=field_prompts,
        skip_keywords=skip_keywords,
    )
