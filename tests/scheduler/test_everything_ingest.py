"""ingest=everything: 注册、轮询入库与来源不可用时的降级."""

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


def _stub_client(tmp_path: Path, rows: list[dict[str, object]] | None = None, exit_code: int = 0) -> EverythingClient:
    def runner(args: Sequence[str], _timeout: float) -> CommandResult:
        out = Path(list(args)[list(args).index("-export-json") + 1])
        if exit_code:
            return CommandResult(returncode=exit_code, stdout="Error 8: Everything IPC not found.", stderr="")
        out.write_text(json.dumps(rows or []), encoding="utf-8-sig")
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
    )


@pytest.mark.asyncio(loop_scope="function")
async def test_start_registers_everything_without_observer(repo: Repository, tmp_path: Path) -> None:
    lib = await repo.create_library(name="es", path=str(tmp_path), ingest=LibraryIngest.EVERYTHING)
    service = _service(repo, _stub_client(tmp_path))
    try:
        await service.start()
        assert service._watcher is None
        assert lib.id in service._everything_routes
        assert service._everything_task is not None
    finally:
        await service.stop()


@pytest.mark.asyncio(loop_scope="function")
async def test_poll_registers_media_file(repo: Repository, tmp_path: Path) -> None:
    lib = await repo.create_library(name="es", path=str(tmp_path), ingest=LibraryIngest.EVERYTHING)
    video = tmp_path / "ABC-123.mp4"
    video.write_bytes(b"x" * 64)
    service = _service(repo, _stub_client(tmp_path, [{"filename": str(video), "size": 64}]))
    try:
        await service.start()
        await service._poll_everything()
        media = await repo.get_media_file_by_path(str(video))
        assert media is not None
        assert media.library_id == lib.id
    finally:
        await service.stop()


@pytest.mark.asyncio(loop_scope="function")
async def test_poll_skips_non_media(repo: Repository, tmp_path: Path) -> None:
    await repo.create_library(name="es", path=str(tmp_path), ingest=LibraryIngest.EVERYTHING)
    note = tmp_path / "note.txt"
    note.write_bytes(b"x")
    service = _service(repo, _stub_client(tmp_path, [{"filename": str(note), "size": 1}]))
    try:
        await service.start()
        await service._poll_everything()
        assert await repo.get_media_file_by_path(str(note)) is None
    finally:
        await service.stop()


@pytest.mark.asyncio(loop_scope="function")
async def test_poll_unavailable_source_does_not_raise(repo: Repository, tmp_path: Path) -> None:
    await repo.create_library(name="es", path=str(tmp_path), ingest=LibraryIngest.EVERYTHING)
    service = _service(repo, _stub_client(tmp_path, exit_code=8))
    try:
        await service.start()
        await service._poll_everything()
        assert await repo.list_media_files(limit=None) == []
    finally:
        await service.stop()


@pytest.mark.asyncio(loop_scope="function")
async def test_poll_is_idempotent_for_repeated_paths(repo: Repository, tmp_path: Path) -> None:
    lib = await repo.create_library(name="es", path=str(tmp_path), ingest=LibraryIngest.EVERYTHING)
    video = tmp_path / "ABC-124.mp4"
    video.write_bytes(b"x" * 64)
    service = _service(repo, _stub_client(tmp_path, [{"filename": str(video)}, {"filename": str(video)}]))
    try:
        await service.start()
        await service._poll_everything()
        await service._poll_everything()
        files = await repo.list_media_files(library_id=lib.id, limit=None)
        assert [media.path for media in files] == [str(video)]
    finally:
        await service.stop()


@pytest.mark.asyncio(loop_scope="function")
async def test_sync_library_switches_to_everything(repo: Repository, tmp_path: Path) -> None:
    lib = await repo.create_library(name="es", path=str(tmp_path), ingest=LibraryIngest.NATIVE)
    service = _service(repo, _stub_client(tmp_path))
    try:
        await service.start()
        assert lib.id not in service._everything_routes

        updated = await repo.update_library(lib.id, ingest=LibraryIngest.EVERYTHING)
        assert updated is not None
        service.sync_library(updated)
        assert lib.id in service._everything_routes
    finally:
        await service.stop()


@pytest.mark.asyncio(loop_scope="function")
async def test_ingest_switch_swaps_channel(repo: Repository, tmp_path: Path) -> None:
    """三态往返: 每次切换都要摘掉上一通道的路由, 否则旧通道会继续往同一个库写."""
    lib = await repo.create_library(name="es", path=str(tmp_path), ingest=LibraryIngest.NATIVE)
    assert lib.id is not None
    service = _service(repo, _stub_client(tmp_path))

    async def switch(ingest: LibraryIngest, cloud_path: str | None = None) -> None:
        updated = await repo.update_library(lib.id, ingest=ingest, cloud_path=cloud_path)
        assert updated is not None
        service.sync_library(updated)
        assert (lib.id in service._everything_routes) is (ingest is LibraryIngest.EVERYTHING)
        assert (lib.id in service._cloud_routes) is (ingest is LibraryIngest.CLOUDDRIVE)

    try:
        await service.start()
        await switch(LibraryIngest.EVERYTHING)
        await switch(LibraryIngest.NATIVE)
        await switch(LibraryIngest.CLOUDDRIVE, cloud_path="/115open/lib")
        await switch(LibraryIngest.EVERYTHING)
        assert service._everything_task is not None
    finally:
        await service.stop()
