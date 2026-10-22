"""ingest=everything 的外挂字幕判定: 只读索引, 不 scandir 云盘目录."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from amane.config import EverythingConfig
from amane.db.repository import Repository
from amane.enums import LibraryIngest
from amane.events import EventBus
from amane.library.everything import CommandResult, EverythingClient
from amane.scheduler.service import WatcherService

_SUBTITLE_MARKERS = ("srt", "ass", "ssa", "vtt", "sub")


def _stub_client(
    tmp_path: Path,
    media_rows: list[dict[str, object]],
    subtitle_rows: list[dict[str, object]],
) -> EverythingClient:
    """按搜索词分流: 字幕扩展名查询返回 subtitle_rows, 其余返回 media_rows."""

    def runner(args: Sequence[str], _timeout: float) -> CommandResult:
        argv = list(args)
        query = str(argv[-1])
        is_subtitle = any(marker in query for marker in _SUBTITLE_MARKERS)
        rows = subtitle_rows if is_subtitle else media_rows
        out = Path(argv[argv.index("-export-json") + 1])
        out.write_text(json.dumps(rows), encoding="utf-8-sig")
        return CommandResult(returncode=0, stdout="", stderr="")

    exe = tmp_path / "es.exe"
    exe.write_bytes(b"")
    return EverythingClient(executable=exe, runner=runner)


def _service(repo: Repository, client: EverythingClient) -> WatcherService:
    return WatcherService(
        repo,
        EventBus(),
        use_polling=True,
        debounce_seconds=0,
        check_interval=0.05,
        everything_config=EverythingConfig(poll_interval=5),
        everything_client=client,
        everything_autostart=False,
    )


@pytest.mark.asyncio(loop_scope="function")
async def test_subtitle_flag_comes_from_index(repo: Repository, tmp_path: Path) -> None:
    await repo.create_library(name="es", path=str(tmp_path), ingest=LibraryIngest.EVERYTHING)
    with_sub = tmp_path / "AAA" / "AAA-001" / "AAA-001.mp4"
    without = tmp_path / "BBB" / "BBB-002" / "BBB-002.mp4"
    for video in (with_sub, without):
        video.parent.mkdir(parents=True)
        video.write_bytes(b"x" * 64)
    sub = with_sub.with_suffix(".srt")
    sub.write_bytes(b"1\n")

    client = _stub_client(
        tmp_path,
        media_rows=[{"filename": str(with_sub)}, {"filename": str(without)}],
        subtitle_rows=[{"filename": str(sub)}],
    )
    service = _service(repo, client)
    try:
        await service.start()
        await service._poll_everything()
        hit = await repo.get_media_file_by_path(str(with_sub))
        miss = await repo.get_media_file_by_path(str(without))
        assert hit is not None and miss is not None
        assert hit.has_external_subtitle is True
        assert miss.has_external_subtitle is False
    finally:
        await service.stop()


@pytest.mark.asyncio(loop_scope="function")
async def test_ingest_never_scandirs(repo: Repository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """入库全程不得调用 has_companion_subtitle (它会对目录做 os.scandir)."""
    await repo.create_library(name="es", path=str(tmp_path), ingest=LibraryIngest.EVERYTHING)
    video = tmp_path / "CCC" / "CCC-003" / "CCC-003.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"x" * 64)

    calls: list[tuple[object, ...]] = []

    async def _forbidden(*args: object, **_kwargs: object) -> bool:
        calls.append(args)
        raise AssertionError("everything 入库不应读目录")

    monkeypatch.setattr("amane.handlers._common.has_companion_subtitle", _forbidden)

    service = _service(repo, _stub_client(tmp_path, [{"filename": str(video)}], []))
    try:
        await service.start()
        await service._poll_everything()
        media = await repo.get_media_file_by_path(str(video))
        assert media is not None
        assert media.has_external_subtitle is False
        assert calls == []
    finally:
        await service.stop()
