"""本机播放: 把已入库的文件交给外部播放器.

两种取用方式由 ``playback.mode`` 决定:

- ``system``: 交给操作系统默认关联程序 — Windows ``os.startfile``, macOS ``open``, Linux ``xdg-open``.
  这条路只认单个路径, 给不了命令行参数, 因此一次只开一个文件.
- ``custom``: ``Popen`` 指定的可执行文件. 可以带多个文件, 播放器把它们排成播放列表 (VLC / mpv 都是
  多参数入列), 不必生成任何列表文件.

两者都只负责"拉起", 不等播放器退出: 播放器是长驻进程, 等它退出会把请求挂住.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from ..utils.threads import in_thread


class LocalPlayerError(RuntimeError):
    """拉起失败: 播放器或文件不存在, 系统没有关联程序."""


@dataclass(frozen=True)
class LaunchResult:
    """拉起结果. 回给前端用于提示"打开了什么、交给了谁"."""

    paths: list[Path]
    """实际交给播放器的文件. 系统关联只能带一个, 此时是挑出来的那一条."""

    player: str | None
    """播放器的文件名; ``None`` 表示交给系统关联程序, 文案由前端拼."""


def _spawn(argv: list[str]) -> None:
    """拉起后立刻返回. 三个流都断开, 避免子进程继承 Web 服务的管道."""
    subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )


@in_thread
def launch_local_player(paths: Sequence[Path], *, player: str = "") -> LaunchResult:
    """把 ``paths`` 交给 ``player`` (为空则交给系统关联程序).

    调用方负责挑出文件; 这里只复核它们仍在磁盘上 — 库里的路径可能已经被清理掉.
    """
    targets = [Path(item) for item in paths]
    if not targets:
        raise LocalPlayerError("没有可播放的文件")
    for target in targets:
        if not target.is_file():
            raise LocalPlayerError(f"文件不存在: {target}")

    if player:
        executable = Path(player).expanduser()
        if not executable.is_file():
            raise LocalPlayerError(f"播放器不存在: {executable}")
        _spawn([str(executable), *(str(item) for item in targets)])
        return LaunchResult(paths=targets, player=executable.name)

    # 系统关联只能带一个路径: 多文件在 `custom` 方式下才有意义.
    target = targets[0]
    try:
        if sys.platform == "win32":
            os.startfile(target)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            _spawn(["open", str(target)])
        else:
            _spawn(["xdg-open", str(target)])
    except OSError as exc:
        # Windows 无关联程序时是 winerror 1155, 原文是英文资源串, 换成可读原因.
        if getattr(exc, "winerror", None) == 1155:
            raise LocalPlayerError(f"系统没有与该格式关联的程序: {target.suffix or target.name}") from exc
        raise LocalPlayerError(f"无法用系统关联程序打开: {exc}") from exc
    return LaunchResult(paths=[target], player=None)
