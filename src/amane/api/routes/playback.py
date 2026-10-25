from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path as FsPath
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query, Request, Response
from fastapi.responses import JSONResponse

from ...config.manager import ExternalPlayerMode
from ...db.models import MediaFile
from ...library import MEDIA_EXTENSIONS
from ...net.errors import SourceError
from ...playback.factory import SOURCE_ID_MAX_LEN, ListedSource, PlaybackFactory
from ...playback.file_response import IndexedFileResponse
from ...playback.hls import PLAYLIST_CACHE_CONTROL, is_hls_content_type
from ...playback.href import playlist_href, stream_href, subtitle_href
from ...playback.local_player import LocalPlayerError, launch_local_player
from ...playback.proxy import NOSNIFF
from ...playback.query import playback_query
from ...plugins.api import PATH_SEGMENT_PATTERN, FilePlaybackTarget, PlaybackQuery, UpstreamPlaybackTarget
from ...utils.threads import path_exists
from ..deps import ConfigDep, RepoDep, RuntimeDep
from ..models.playback import (
    PlaybackLocalOpenResponse,
    PlaybackLocalPlayerResponse,
    PlaybackSourceListResponse,
    PlaybackSourceOption,
    PlaybackStreamItem,
    PlaybackStreamListResponse,
    PlaybackSubtitleItem,
)

router = APIRouter(prefix="/playback", tags=["playback"])

_TOKEN_PATTERN = r"^[0-9a-f]{32}$"
#: 进路径的标识 (流的 key, 字幕轨道 id) 的形状由插件契约声明, 路由不与它各写一份.
_PATH_SEGMENT_PATTERN = PATH_SEGMENT_PATTERN


def _source_href(source_id: str, metadata_id: int, key: str | None, content_type: str) -> str:
    if is_hls_content_type(content_type):
        return playlist_href(source_id, metadata_id, key)
    return stream_href(source_id, metadata_id, key)


async def _load_query(
    repo: RepoDep,
    metadata_id: int,
    selected_key: str | None,
) -> PlaybackQuery:
    """装配查询快照.

    ``selected_key`` 逐字交给插件: 主机不解释它, 也不核对它对应哪个文件 (那是插件的标识), 认不出来
    的 key 由插件以自身原因拒绝.
    """
    metadata = await repo.get_metadata(metadata_id)
    if metadata is None:
        raise HTTPException(status_code=404, detail="影片不存在")
    files = await repo.get_media_by_metadata_id(metadata_id)
    library_paths: dict[int, str] = {}
    for item in files:
        if item.library_id in library_paths:
            continue
        library = await repo.get_library(item.library_id)
        if library is not None:
            library_paths[item.library_id] = library.path
    return playback_query(metadata, files, selected_key=selected_key, library_paths=library_paths)


def _stream_item(row: ListedSource, metadata_id: int) -> PlaybackStreamItem:
    return PlaybackStreamItem(
        source_id=row.source_id,
        key=row.key,
        name=row.name,
        content_type=row.content_type,
        seekable=row.seekable,
        available=row.available,
        detail=row.detail,
        href=_source_href(row.source_id, metadata_id, row.key, row.content_type),
        subtitles=[
            PlaybackSubtitleItem(
                id=track.id,
                label=track.label,
                language=track.language,
                href=subtitle_href(row.source_id, metadata_id, row.key, track.id),
            )
            for track in row.subtitles
        ],
    )


def _playback_http_error(exc: SourceError) -> HTTPException:
    return HTTPException(status_code=502, detail=exc.detail or "播放来源失败")


@router.get("/sources", response_model=PlaybackSourceListResponse)
async def list_playback_sources(runtime: RuntimeDep) -> Response:
    """列出可选的播放源; 不调用插件, 因此不发任何上游请求."""
    factory = runtime.playback_factory
    if factory is None:
        raise HTTPException(status_code=503, detail="播放源未初始化")
    body = PlaybackSourceListResponse(
        items=[
            PlaybackSourceOption(source_id=option.source_id, name=option.name) for option in factory.source_options()
        ]
    )
    return JSONResponse(content=body.model_dump(mode="json"), headers={"Cache-Control": "private, no-cache"})


def _player_name(player: str) -> str | None:
    """配置的播放器路径取其文件名: 前端只用来拼文案, 不承载路径信息."""
    if not player:
        return None
    return FsPath(player).name or None


def _pick_local_files(files: Sequence[MediaFile], *, file_id: int | None) -> list[str]:
    """挑出交给外部播放器的文件, 主文件排在最前.

    ``file_id`` 给了就只开那一条 (必须是该条目下的文件). 没给时按体积取主文件 — 条目下常混着预告与
    样片, 体积最大的那个才是正片; 其余视频按入库顺序 (id 升序) 跟在后面, 供支持多参数的播放器排成列表.
    """
    if file_id is not None:
        chosen = next((row for row in files if row.id == file_id), None)
        if chosen is None:
            raise HTTPException(status_code=400, detail="该文件不属于这个条目")
        return [chosen.path]

    videos = [row for row in files if FsPath(row.path).suffix.casefold() in MEDIA_EXTENSIONS]
    if not videos:
        raise HTTPException(status_code=404, detail="该条目没有视频文件")
    primary = max(videos, key=lambda row: row.size or 0)
    return [primary.path, *(row.path for row in videos if row.id != primary.id)]


@router.get("/local-player", response_model=PlaybackLocalPlayerResponse)
async def get_local_player(config: ConfigDep) -> Response:
    """本机播放的当前设置. 不读库, 打开详情页即可调用."""
    settings = config.hot.playback
    body = PlaybackLocalPlayerResponse(
        enabled=settings.external,
        mode=settings.mode.value,
        player=None if settings.mode is ExternalPlayerMode.SYSTEM else _player_name(settings.player),
    )
    return JSONResponse(content=body.model_dump(mode="json"), headers={"Cache-Control": "no-store"})


@router.post("/{metadata_id}/open-local", response_model=PlaybackLocalOpenResponse)
async def open_local_playback(
    metadata_id: Annotated[int, Path(ge=1)],
    repo: RepoDep,
    config: ConfigDep,
    file_id: Annotated[int | None, Query(ge=1)] = None,
) -> Response:
    """用本机外部播放器打开条目的文件.

    路径只取自条目索引, 不接受任意地址: 这个动作会在服务所在机器上执行程序, 收窄到"已入库的文件 +
    已配置的播放器"之后, 能触发的范围才是有界的.
    """
    settings = config.hot.playback
    if not settings.external:
        raise HTTPException(status_code=403, detail="外部播放未启用")
    if settings.mode is ExternalPlayerMode.CUSTOM and not settings.player:
        raise HTTPException(status_code=400, detail="未配置自定义播放器路径")
    if await repo.get_metadata(metadata_id) is None:
        raise HTTPException(status_code=404, detail="影片不存在")

    files = await repo.get_media_by_metadata_id(metadata_id)
    if not files:
        raise HTTPException(status_code=404, detail="该条目还没有索引到任何文件")

    candidates = _pick_local_files(files, file_id=file_id)
    present = [path for path in (FsPath(item) for item in candidates) if await path_exists(path)]
    if not present:
        raise HTTPException(status_code=404, detail=f"文件已不在磁盘上: {candidates[0]}")
    # 只有 `custom` 才把配置里的播放器透传下去: 切回 `system` 后旧路径仍留在配置里, 继续用它会让
    # "系统默认"名不副实; 路径失效时更会直接报错, 而不是回退到关联程序.
    player = settings.player if settings.mode is ExternalPlayerMode.CUSTOM else ""
    if settings.mode is ExternalPlayerMode.SYSTEM:
        # 系统关联只认一个路径, 多文件留给 `custom`.
        present = present[:1]

    try:
        result = await launch_local_player(present, player=player)
    except LocalPlayerError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    body = PlaybackLocalOpenResponse(
        paths=[str(path) for path in result.paths],
        player=result.player,
    )
    return JSONResponse(content=body.model_dump(mode="json"), headers={"Cache-Control": "no-store"})


@router.get("/{source_id}/{metadata_id}/streams", response_model=PlaybackStreamListResponse)
async def list_playback_streams(
    source_id: str,
    metadata_id: Annotated[int, Path(ge=1)],
    repo: RepoDep,
    runtime: RuntimeDep,
) -> Response:
    """探测一个来源在这个条目上的流.

    由前端在切到该来源时调用, 因此没有人工预算: 慢就慢在用户等待的那一次, 结果按来源与条目缓存
    一小段时间, 来回切换不重复探测. 探测失败也是一行不可用记录, 前端按 ``detail`` 显示原因.
    """
    factory = await _require_factory(source_id, runtime)
    query = await _load_query(repo, metadata_id, None)
    listed = await factory.list_streams(source_id, query)
    body = PlaybackStreamListResponse(items=[_stream_item(row, metadata_id) for row in listed])
    return JSONResponse(content=body.model_dump(mode="json"), headers={"Cache-Control": "no-store"})


@router.get("/{source_id}/{metadata_id}/index.m3u8")
async def play_metadata_playlist(
    request: Request,
    source_id: str,
    metadata_id: Annotated[int, Path(ge=1)],
    repo: RepoDep,
    runtime: RuntimeDep,
) -> Response:
    return await _playlist(request, source_id, metadata_id, None, repo, runtime)


@router.head("/{source_id}/{metadata_id}/index.m3u8", include_in_schema=False)
async def play_metadata_playlist_head(
    request: Request,
    source_id: str,
    metadata_id: Annotated[int, Path(ge=1)],
    repo: RepoDep,
    runtime: RuntimeDep,
) -> Response:
    return await _playlist(request, source_id, metadata_id, None, repo, runtime)


@router.get("/{source_id}/{metadata_id}/streams/{key}/index.m3u8")
async def play_stream_playlist(
    request: Request,
    source_id: str,
    metadata_id: Annotated[int, Path(ge=1)],
    key: Annotated[str, Path(pattern=_PATH_SEGMENT_PATTERN)],
    repo: RepoDep,
    runtime: RuntimeDep,
) -> Response:
    return await _playlist(request, source_id, metadata_id, key, repo, runtime)


@router.head("/{source_id}/{metadata_id}/streams/{key}/index.m3u8", include_in_schema=False)
async def play_stream_playlist_head(
    request: Request,
    source_id: str,
    metadata_id: Annotated[int, Path(ge=1)],
    key: Annotated[str, Path(pattern=_PATH_SEGMENT_PATTERN)],
    repo: RepoDep,
    runtime: RuntimeDep,
) -> Response:
    return await _playlist(request, source_id, metadata_id, key, repo, runtime)


@router.get("/{source_id}/{metadata_id}/hls/{token}")
async def play_metadata_hls_part(
    request: Request,
    source_id: str,
    metadata_id: Annotated[int, Path(ge=1)],
    token: Annotated[str, Path(pattern=_TOKEN_PATTERN)],
    runtime: RuntimeDep,
) -> Response:
    return await _hls_part(request, source_id, metadata_id, None, token, runtime)


@router.head("/{source_id}/{metadata_id}/hls/{token}", include_in_schema=False)
async def play_metadata_hls_part_head(
    request: Request,
    source_id: str,
    metadata_id: Annotated[int, Path(ge=1)],
    token: Annotated[str, Path(pattern=_TOKEN_PATTERN)],
    runtime: RuntimeDep,
) -> Response:
    return await _hls_part(request, source_id, metadata_id, None, token, runtime)


@router.get("/{source_id}/{metadata_id}/streams/{key}/hls/{token}")
async def play_stream_hls_part(
    request: Request,
    source_id: str,
    metadata_id: Annotated[int, Path(ge=1)],
    key: Annotated[str, Path(pattern=_PATH_SEGMENT_PATTERN)],
    token: Annotated[str, Path(pattern=_TOKEN_PATTERN)],
    runtime: RuntimeDep,
) -> Response:
    return await _hls_part(request, source_id, metadata_id, key, token, runtime)


@router.head("/{source_id}/{metadata_id}/streams/{key}/hls/{token}", include_in_schema=False)
async def play_stream_hls_part_head(
    request: Request,
    source_id: str,
    metadata_id: Annotated[int, Path(ge=1)],
    key: Annotated[str, Path(pattern=_PATH_SEGMENT_PATTERN)],
    token: Annotated[str, Path(pattern=_TOKEN_PATTERN)],
    runtime: RuntimeDep,
) -> Response:
    return await _hls_part(request, source_id, metadata_id, key, token, runtime)


@router.get("/{source_id}/{metadata_id}/subtitles/{track_id}")
async def play_metadata_subtitle(
    request: Request,
    source_id: str,
    metadata_id: Annotated[int, Path(ge=1)],
    track_id: Annotated[str, Path(pattern=_PATH_SEGMENT_PATTERN)],
    repo: RepoDep,
    runtime: RuntimeDep,
) -> Response:
    return await _subtitle(request, source_id, metadata_id, None, track_id, repo, runtime)


@router.get("/{source_id}/{metadata_id}/streams/{key}/subtitles/{track_id}")
async def play_stream_subtitle(
    request: Request,
    source_id: str,
    metadata_id: Annotated[int, Path(ge=1)],
    key: Annotated[str, Path(pattern=_PATH_SEGMENT_PATTERN)],
    track_id: Annotated[str, Path(pattern=_PATH_SEGMENT_PATTERN)],
    repo: RepoDep,
    runtime: RuntimeDep,
) -> Response:
    return await _subtitle(request, source_id, metadata_id, key, track_id, repo, runtime)


@router.get("/{source_id}/{metadata_id}")
async def play_metadata(
    request: Request,
    source_id: str,
    metadata_id: Annotated[int, Path(ge=1)],
    repo: RepoDep,
    runtime: RuntimeDep,
) -> Response:
    return await _play(request, source_id, metadata_id, None, repo, runtime)


@router.head("/{source_id}/{metadata_id}", include_in_schema=False)
async def play_metadata_head(
    request: Request,
    source_id: str,
    metadata_id: Annotated[int, Path(ge=1)],
    repo: RepoDep,
    runtime: RuntimeDep,
) -> Response:
    return await _play(request, source_id, metadata_id, None, repo, runtime)


@router.get("/{source_id}/{metadata_id}/streams/{key}")
async def play_stream(
    request: Request,
    source_id: str,
    metadata_id: Annotated[int, Path(ge=1)],
    key: Annotated[str, Path(pattern=_PATH_SEGMENT_PATTERN)],
    repo: RepoDep,
    runtime: RuntimeDep,
) -> Response:
    return await _play(request, source_id, metadata_id, key, repo, runtime)


@router.head("/{source_id}/{metadata_id}/streams/{key}", include_in_schema=False)
async def play_stream_head(
    request: Request,
    source_id: str,
    metadata_id: Annotated[int, Path(ge=1)],
    key: Annotated[str, Path(pattern=_PATH_SEGMENT_PATTERN)],
    repo: RepoDep,
    runtime: RuntimeDep,
) -> Response:
    return await _play(request, source_id, metadata_id, key, repo, runtime)


async def _require_factory(source_id: str, runtime: RuntimeDep) -> PlaybackFactory:
    if len(source_id) > SOURCE_ID_MAX_LEN:
        raise HTTPException(status_code=404, detail="播放源不存在")
    factory = runtime.playback_factory
    if factory is None:
        raise HTTPException(status_code=503, detail="播放源未初始化")
    if not factory.known_source(source_id) or not factory.enabled(source_id):
        raise HTTPException(status_code=404, detail="播放源不存在")
    return factory


async def _playlist(
    request: Request,
    source_id: str,
    metadata_id: int,
    selected_key: str | None,
    repo: RepoDep,
    runtime: RuntimeDep,
) -> Response:
    factory = await _require_factory(source_id, runtime)
    query = await _load_query(repo, metadata_id, selected_key)
    try:
        text = await factory.hls_playlist_text(source_id, query)
    except LookupError:
        raise HTTPException(status_code=404, detail="没有可播放的流") from None
    except SourceError as exc:
        raise _playback_http_error(exc) from exc
    return factory.playlist_response(text, head=request.method == "HEAD")


async def _hls_part(
    request: Request,
    source_id: str,
    metadata_id: int,
    selected_key: str | None,
    token: str,
    runtime: RuntimeDep,
) -> Response:
    factory = await _require_factory(source_id, runtime)
    try:
        return await factory.serve_hls_part(
            request,
            source_id=source_id,
            metadata_id=metadata_id,
            selected_key=selected_key,
            token=token,
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="播放片段不存在") from None
    except SourceError as exc:
        raise _playback_http_error(exc) from exc


async def _subtitle(
    request: Request,
    source_id: str,
    metadata_id: int,
    selected_key: str | None,
    track_id: str,
    repo: RepoDep,
    runtime: RuntimeDep,
) -> Response:
    factory = await _require_factory(source_id, runtime)
    query = await _load_query(repo, metadata_id, selected_key)
    try:
        result = await factory.load_subtitle(source_id, query, track_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="字幕不存在") from None
    except SourceError as exc:
        raise _playback_http_error(exc) from exc
    if isinstance(result, str):
        return Response(
            content=result.encode("utf-8"),
            media_type="text/vtt; charset=utf-8",
            headers={**NOSNIFF, "Cache-Control": PLAYLIST_CACHE_CONTROL},
        )
    return await factory.stream.proxy(request, source_id=source_id, target=result, allow="subtitle")


async def _play(
    request: Request,
    source_id: str,
    metadata_id: int,
    selected_key: str | None,
    repo: RepoDep,
    runtime: RuntimeDep,
) -> Response:
    factory = await _require_factory(source_id, runtime)
    query = await _load_query(repo, metadata_id, selected_key)
    try:
        target = await factory.resolve(source_id, query)
    except LookupError:
        raise HTTPException(status_code=404, detail="没有可播放的流") from None
    except SourceError as exc:
        raise _playback_http_error(exc) from exc
    if isinstance(target, FilePlaybackTarget):
        # 目标路径已由 ``PlaybackFactory.resolve`` 核对为条目索引内的文件.
        return IndexedFileResponse(
            request,
            target.path,
            media_type=target.content_type,
            headers={**NOSNIFF, "Cache-Control": "private"},
        )
    if isinstance(target, UpstreamPlaybackTarget):
        return await factory.stream.proxy(request, source_id=source_id, target=target)
    try:
        text = await factory.hls_playlist_text(source_id, query, target)
    except SourceError as exc:
        raise _playback_http_error(exc) from exc
    return factory.playlist_response(text, head=request.method == "HEAD")
