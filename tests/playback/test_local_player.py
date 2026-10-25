"""本机播放的拉起: 只测拒绝路径, 真的启动播放器不在单测里做."""

from pathlib import Path

import pytest

from amane.playback.local_player import LocalPlayerError, launch_local_player


def test_launch_rejects_empty_file_list() -> None:
    with pytest.raises(LocalPlayerError, match="没有可播放的文件"):
        launch_local_player.sync([])


def test_launch_rejects_missing_file(tmp_path: Path) -> None:
    """库里的路径可能已经被清理掉: 拉起之前必须复核, 否则播放器会弹一个空窗口."""
    with pytest.raises(LocalPlayerError, match="文件不存在"):
        launch_local_player.sync([tmp_path / "gone.mp4"])


def test_launch_rejects_missing_player(tmp_path: Path) -> None:
    target = tmp_path / "clip.mp4"
    target.write_bytes(b"x")
    with pytest.raises(LocalPlayerError, match="播放器不存在"):
        launch_local_player.sync([target], player=str(tmp_path / "vlc.exe"))


def test_playlist_file_carries_every_path(tmp_path: Path) -> None:
    """系统关联只给一个路径, 队列靠临时的 m3u 承载: 必须有头, 且一行一个绝对路径."""
    from amane.playback.local_player import _write_playlist

    first = tmp_path / "a.mp4"
    second = tmp_path / "b.mp4"
    handle = _write_playlist([first, second])
    try:
        lines = handle.read_text(encoding="utf-8").splitlines()
        assert lines == ["#EXTM3U", str(first), str(second)]
        assert handle.suffix == ".m3u"
    finally:
        handle.unlink()
