from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class PlaybackSubtitleItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    label: str
    language: str | None = None
    href: str


class PlaybackSourceOption(BaseModel):
    """一个可选的播放源. 只有名字, 不含探测结果: 切到它时才去问它有哪些流."""

    model_config = ConfigDict(extra="forbid")

    source_id: str
    name: str


class PlaybackStreamItem(BaseModel):
    """某个来源的一条流. ``name`` 是主机拼好的展示名 (来源名 · 流的展示名).

    ``key`` 是这条流的标识, 同时出现在 ``href`` 里; 来源整个不可用时为 ``None``.
    """

    model_config = ConfigDict(extra="forbid")

    source_id: str
    key: str | None = None
    name: str
    content_type: str
    seekable: bool
    available: bool
    detail: str | None = None
    href: str
    subtitles: list[PlaybackSubtitleItem] = Field(default_factory=list)


class PlaybackSourceListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[PlaybackSourceOption] = Field(default_factory=list)


class PlaybackStreamListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[PlaybackStreamItem] = Field(default_factory=list)


class PlaybackLocalPlayerResponse(BaseModel):
    """本机播放的当前设置. 前端据此决定封面按钮与播放卡片的行为, 不含任何文件信息."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool
    """外部播放开关. 关闭时走内置播放器."""

    mode: Literal["system", "custom"]
    player: str | None = None
    """自定义播放器的文件名; ``None`` 表示交给系统关联程序. 兼作"是否已配置"的判断."""


class PlaybackLocalOpenRequest(BaseModel):
    """按播放列表顺序拉起多个条目.

    只收条目 id: 路径一律由后端从索引里取, 前端给不了任意地址.
    """

    model_config = ConfigDict(extra="forbid")

    metadata_ids: list[int] = Field(min_length=1, max_length=200)
    """待播放的条目, 顺序即播放顺序. 重复 id 按首次出现保留."""

    file_id: int | None = None
    """只播某个条目下的这一份文件; 多条目时只作用于第一个条目."""


class PlaybackLocalOpenResponse(BaseModel):
    """拉起结果: 打开了哪些文件, 交给了哪个程序."""

    model_config = ConfigDict(extra="forbid")

    paths: list[str]
    """实际交给播放器的文件. 顺序与请求的条目顺序一致."""

    player: str | None = None
