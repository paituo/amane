from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

from pydantic import AfterValidator

from ..enums import ActorGender, LinkMode
from ..parsing.file_info import FileInfo
from ..utils.path import is_descendant
from .template import PLACEHOLDER_MAP_KEYS as PLACEHOLDER_MAP_KEYS
from .template import PLACEHOLDERS as PLACEHOLDERS
from .template import Parser, PathEngine, TemplateContext

if TYPE_CHECKING:
    from ..db.models import Library, Metadata


@dataclass
class ResolvedPaths:
    video: Path
    thumb: Path
    poster: Path
    fanart: Path
    extrafanart_dir: Path
    nfo: Path
    trailer: Path
    link: Path | None = None


# 默认值对齐 "云盘只读源" 的真实布局 (Everything 索引抽样 8325 文件 / 1628 含视频目录):
#   目录 `{prefix}/{number}/`, 视频 `{number}[-CDn][-C].{ext}`.
#   实测 1618/1628 目录名 == 视频名; 所有后缀 (CD 分集、`-C` 中字) 一律只进文件名,
#   目录只由 `{prefix}/{number}` 决定 (DVDMS-074-CD1.mkv 在 DVDMS-074/ 下).
# 与源布局同构的直接效果: 即便误开 auto_organize, 渲染出的路径就是文件原地,
# 整理退化为幂等操作, 不会在云盘上改名或搬家.
VIDEO_TEMPLATE_DEFAULT = "[{prefix}/]{number}/{number}[-CD{cd?}][-{sub?}].{ext}"
# 配套产物跟随视频名 (云盘实测: `-poster.jpg` 1454 / `-thumb.jpg` 1392 / `-fanart.jpg` 57,
# 固定名 `poster.jpg` 763 / `thumb.jpg` 431 是早期遗留). `{video_name}` 含 CD / 中字标记,
# 与 `DVDMS-074-CD1-poster.jpg` 这类带分集名的样本一致.
THUMB_TEMPLATE_DEFAULT = "{link_dir}/{video_name}-thumb.jpg"
POSTER_TEMPLATE_DEFAULT = "{link_dir}/{video_name}-poster.jpg"
FANART_TEMPLATE_DEFAULT = "{link_dir}/{video_name}-fanart.jpg"
EXTRAFANART_TEMPLATE_DEFAULT = "{link_dir}/extrafanart"
# 媒体服务器按视频文件名匹配 NFO, 所以默认跟随 {video_name} (含 CD / 中字段).
NFO_TEMPLATE_DEFAULT = "{link_dir}/{video_name}.nfo"
TRAILER_TEMPLATE_DEFAULT = "{link_dir}/trailer.mp4"
SUBTITLE_TEMPLATE_DEFAULT = "{link_dir}/{raw_srt_name}.{ext}"
# 链接 (STRM) 默认落在数据目录下按库名分子目录, 与源布局同构;
# 占位符不含盘符, 由 `link_template_default()` 拼上绝对前缀.
LINK_TEMPLATE_RELATIVE_DEFAULT = "{library}/[{prefix}/]{number}/{number}[-CD{cd?}][-{sub?}]"


def link_template_default(link_root: Path) -> str:
    """返回可用的绝对 `link_template` 默认值: `<link_root>/<库名>/<番号前缀>/<番号>/<文件名>`."""
    return f"{link_root.resolve().as_posix()}/{LINK_TEMPLATE_RELATIVE_DEFAULT}"


def normalize_link_template(value: str | None) -> str | None:
    """空白 link_template 视为未设置 (不创建链接)."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def validate_path_template(value: str) -> str:
    """空串合法."""
    Parser(value).parse()
    return value


PathTemplate = Annotated[str, AfterValidator(validate_path_template)]


def render_path_template(template: str, variables: dict[str, str]) -> str:
    return PathEngine(template).render(TemplateContext.from_mapping(variables))


def resolve_paths(
    library: Library,
    metadata: Metadata,
    ext: str = "",
    cd: int | None = None,
    source_path: Path | None = None,
    file_info: FileInfo | None = None,
    safe_dirs: Sequence[Path] | None = (),
    actor_genders: Mapping[str, ActorGender] | None = None,
) -> ResolvedPaths:
    """``safe_dirs is None`` 时不限制绝对模板写出的路径; 相对模板仍须在 base_path 下."""
    base_path = Path(library.path)
    ctx = TemplateContext.from_metadata(
        metadata,
        ext=ext,
        source_path=source_path,
        file_info=file_info,
        cd=cd,
        actor_genders=actor_genders,
    )
    ctx.apply_library(library.name)

    # 先渲染视频, 注入 `{video_*}` 后再渲染链接与刮削产物.
    video = PathEngine(library.video_template).resolve(ctx, base_path, safe_dirs)
    ctx.apply_video(video, base_path)
    link = _resolve_link_path(library, ctx, base_path, safe_dirs)
    ctx.apply_link(link)

    thumb = PathEngine(library.thumb_template or THUMB_TEMPLATE_DEFAULT).resolve(ctx, base_path, safe_dirs)
    poster = PathEngine(library.poster_template or POSTER_TEMPLATE_DEFAULT).resolve(ctx, base_path, safe_dirs)
    fanart = PathEngine(library.fanart_template or FANART_TEMPLATE_DEFAULT).resolve(ctx, base_path, safe_dirs)
    extrafanart_dir = PathEngine(library.extrafanart_template or EXTRAFANART_TEMPLATE_DEFAULT).resolve(
        ctx, base_path, safe_dirs
    )
    nfo = PathEngine(library.nfo_template or NFO_TEMPLATE_DEFAULT).resolve(ctx, base_path, safe_dirs)
    trailer = PathEngine(library.trailer_template or TRAILER_TEMPLATE_DEFAULT).resolve(ctx, base_path, safe_dirs)

    return ResolvedPaths(
        video=video,
        thumb=thumb,
        poster=poster,
        fanart=fanart,
        extrafanart_dir=extrafanart_dir,
        nfo=nfo,
        trailer=trailer,
        link=link,
    )


def _resolve_link_path(
    library: Library,
    ctx: TemplateContext,
    base_path: Path,
    safe_dirs: Sequence[Path] | None,
) -> Path | None:
    """结果必须在库外: 库外指链接文件自身所在的位置, 判定只解析父目录链.

    链接文件已指向库内视频时, 判定的是文件落在哪里, 不是它指向的视频, 因此仍然通过校验.
    此时 `{video_dir}` / `{video_name}` / `{video_relpath}` 已注入, `{link_dir}` / `{link_name}` 尚未注入.
    """
    template = normalize_link_template(library.link_template)
    if template is None:
        return None
    link = PathEngine(template).resolve(ctx, base_path, safe_dirs)
    if library.link_mode == LinkMode.STRM:
        link = link.with_suffix(".strm")
    if is_descendant(link.parent, base_path):
        raise ValueError(f"链接模板必须解析到库根之外: {link} 位于 {base_path} 之下")
    return link


def resolve_subtitle_path(
    library: Library,
    metadata: Metadata,
    subtitle_source: Path,
    *,
    video_dir: Path,
    link_dir: Path | None = None,
    video_name: str = "",
    video_dest: Path | None = None,
    link_name: str | None = None,
    source_path: Path | None = None,
    file_info: FileInfo | None = None,
    safe_dirs: Sequence[Path] | None = (),
    actor_genders: Mapping[str, ActorGender] | None = None,
) -> Path:
    """`{ext}` / `{raw_srt_name}` 取自该字幕源文件; `{raw_name}` / `{raw_dir}` 仍是视频源.
    `{video_dir}` / `{video_name}` 为整理后视频父目录与文件名 (不含扩展名);
    `{video_relpath}` 为整理后视频相对库根目录的路径 (`video_dest` 未传时为空);
    `{link_dir}` / `{link_name}` 为链接父目录与文件名 (未设置链接时分别与视频侧相同).
    默认模板保持原文件名与扩展名.
    """
    base_path = Path(library.path)
    ctx = TemplateContext.from_metadata(
        metadata, source_path=source_path, file_info=file_info, actor_genders=actor_genders
    )
    ctx.apply_library(library.name)
    if video_dest is not None:
        ctx.apply_video(video_dest, base_path)
    else:
        ctx.variables["video_dir"] = str(video_dir)
        ctx.variables["video_name"] = video_name
        ctx.variables["video_relpath"] = ""
    ctx.apply_link(None)
    if link_dir is not None:
        ctx.variables["link_dir"] = str(link_dir)
    if link_name is not None:
        ctx.variables["link_name"] = link_name
    ctx.apply_subtitle(subtitle_source)
    template = library.subtitle_template or SUBTITLE_TEMPLATE_DEFAULT
    return PathEngine(template).resolve(ctx, base_path, safe_dirs)
