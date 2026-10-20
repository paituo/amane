"""Everything 索引查询: 参数拼装、输出解析与失败分类."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from amane.library.everything import (
    CommandResult,
    EverythingClient,
    EverythingError,
    EverythingHit,
    EverythingUnavailable,
    extension_query,
)

EXT_QUERY_CASES = [
    ([".mp4", ".mkv"], "ext:mkv;mp4"),
    (["MP4", ".MP4", "mp4"], "ext:mp4"),
    ([".strm"], "ext:strm"),
    ([], ""),
    (["."], ""),
]


@pytest.mark.parametrize(("extensions", "want"), EXT_QUERY_CASES)
def test_extension_query(extensions: list[str], want: str) -> None:
    assert extension_query(extensions) == want


def _client(
    tmp_path: Path,
    *,
    rows: list[dict[str, object]] | None = None,
    raw: str | None = None,
    exit_code: int = 0,
) -> EverythingClient:
    """构造一个由 ``rows`` / ``raw`` 决定导出内容的客户端."""

    def runner(args: Sequence[str], _timeout: float) -> CommandResult:
        out = Path(list(args)[list(args).index("-export-json") + 1])
        if exit_code:
            return CommandResult(returncode=exit_code, stdout="Error 8: Everything IPC not found.", stderr="")
        if raw is not None:
            out.write_text(raw, encoding="utf-8")
        else:
            out.write_text(json.dumps(rows or []), encoding="utf-8-sig")
        return CommandResult(returncode=0, stdout="", stderr="")

    exe = tmp_path / "es.exe"
    exe.write_bytes(b"")
    return EverythingClient(executable=exe, runner=runner)


def test_search_parses_rows(tmp_path: Path) -> None:
    root = tmp_path / "lib"
    client = _client(
        tmp_path,
        rows=[
            {"filename": str(root / "a.mp4"), "size": 123, "date_modified": "2026-10-18T22:55:09"},
            {"filename": str(root / "b.mkv"), "size": None},
        ],
    )
    assert client.search(root, {".mp4", ".mkv"}) == [
        EverythingHit(path=root / "a.mp4", size=123),
        EverythingHit(path=root / "b.mkv", size=None),
    ]


def test_search_filters_outside_root(tmp_path: Path) -> None:
    root = tmp_path / "lib"
    client = _client(
        tmp_path,
        rows=[{"filename": str(root / "a.mp4")}, {"filename": str(tmp_path / "other" / "b.mp4")}],
    )
    assert [hit.path.name for hit in client.search(root, {".mp4"})] == ["a.mp4"]


def test_search_non_recursive_keeps_direct_children(tmp_path: Path) -> None:
    root = tmp_path / "lib"
    client = _client(
        tmp_path,
        rows=[{"filename": str(root / "a.mp4")}, {"filename": str(root / "sub" / "b.mp4")}],
    )
    assert [hit.path.name for hit in client.search(root, {".mp4"}, recursive=False)] == ["a.mp4"]


def test_search_drops_directory_rows(tmp_path: Path) -> None:
    root = tmp_path / "lib"
    client = _client(tmp_path, rows=[{"filename": str(root / "sub") + "\\"}, {"filename": str(root / "a.mp4")}])
    assert [hit.path.name for hit in client.search(root, {".mp4"})] == ["a.mp4"]


def test_search_empty_extensions_skips_runner(tmp_path: Path) -> None:
    def runner(_args: Sequence[str], _timeout: float) -> CommandResult:
        raise AssertionError("空扩展名不应启动子进程")

    exe = tmp_path / "es.exe"
    exe.write_bytes(b"")
    client = EverythingClient(executable=exe, runner=runner)
    assert client.search(tmp_path, []) == []


def test_search_ipc_missing_is_unavailable(tmp_path: Path) -> None:
    with pytest.raises(EverythingUnavailable):
        _client(tmp_path, exit_code=8).search(tmp_path, {".mp4"})


def test_search_other_exit_code_is_error(tmp_path: Path) -> None:
    with pytest.raises(EverythingError):
        _client(tmp_path, exit_code=3).search(tmp_path, {".mp4"})


def test_search_invalid_json_is_error(tmp_path: Path) -> None:
    with pytest.raises(EverythingError):
        _client(tmp_path, raw="not json").search(tmp_path, {".mp4"})


def test_search_empty_output_is_empty_list(tmp_path: Path) -> None:
    assert _client(tmp_path, raw="").search(tmp_path, {".mp4"}) == []


def test_search_missing_executable_is_unavailable(tmp_path: Path) -> None:
    client = EverythingClient(executable=tmp_path / "absent.exe")
    with pytest.raises(EverythingUnavailable):
        client.search(tmp_path, {".mp4"})
