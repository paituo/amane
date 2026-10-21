from typing import Any

from pydantic import BaseModel, Field


class ConfigProbeRequest(BaseModel):
    """配置自检请求.

    ``values`` 传该 section 的**表单值**时, 用它覆盖已保存配置来测试 (不落盘、不触发 rebuild),
    这样改完还没保存也能先验证; 不传则按已保存的配置测.
    """

    section: str = Field(description="HotSettings 的 section 名: llm / agent / search / network")
    values: dict[str, Any] | None = Field(
        default=None,
        description="该 section 的字段值; 提供时覆盖已保存配置, 仅用于本次测试",
    )
