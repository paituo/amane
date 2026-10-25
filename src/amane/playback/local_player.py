"""本机播放: 把已入库的文件交给外部播放器.

两种取用方式由 ``playback.mode`` 决定:

- ``system``: 交给操作系统默认关联程序 — Windows ``os.startfile`` (ShellExecute), macOS ``open``,
  Linux ``xdg-open``. 系统关联只认路径, 给不了命令行参数; 因此多个文件时先写一份临时 ``.m3u``
  再交给关联程序, 由播放器自己展开成列表.
- ``custom``: ``Popen`` 指定的可执行文件. 可以带多个文件, 播放器把它们排成播放列表 (VLC / mpv 都是
  多参数入列), 不必生成任何列表文件.

两者都只负责"拉起", 不等播放器退出: 播放器是长驻进程, 等它退出会把请求挂住.

**不经过 ``cmd``**: 早先为了给子进程注入干净环境曾用 ``cmd /c start``, 那等于每次播放都拉起一个命令
解释器, 杀软与用户都会把它当成可疑行为. 现在改为直接调系统 API, 环境问题用
``_sanitize_process_env`` 从本进程解决.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from ..utils.threads import in_thread

#: 临时播放列表的存放目录. 由系统关联打开, 播放器读完之后才能删, 因此不随请求清理,
#: 改为每次写入时顺手清掉过期的旧文件.
_PLAYLIST_DIR_NAME = "amane-playlists"

#: 临时播放列表的保留时长 (秒). 超过这个时间的视为上一次播放遗留.
_PLAYLIST_TTL_S = 6 * 3600

#: 让播放器回退到操作系统界面语言的变量. 由 Qt / GTK 系播放器读取.
_LOCALE_ENV_KEYS = ("LANG", "LC_ALL", "LC_MESSAGES", "LANGUAGE", "LC_CTYPE")


class LocalPlayerError(RuntimeError):
    """拉起失败: 播放器或文件不存在, 系统没有关联程序."""


@dataclass(frozen=True)
class LaunchResult:
    """拉起结果. 回给前端用于提示"打开了什么、交给了谁"."""

    paths: list[Path]
    """实际交给播放器的文件. 系统关联多文件时这是一份临时列表里的全部条目."""

    player: str | None
    """播放器的文件名; ``None`` 表示交给系统关联程序, 文案由前端拼."""


def _sanitize_process_env() -> None:
    """从本进程环境里去掉 POSIX 语言变量 (仅 Windows, 幂等).

    VLC 等基于 Qt 的播放器会读 ``LANG`` / ``LC_ALL`` / ``LANGUAGE`` 决定界面语言. amane 以控制台
    方式 (Git Bash / 终端) 启动时这些变量常是 ``en_US.UTF-8`` / ``C.UTF-8``, 子进程继承后播放器显示
    英文; 双击播放器时继承桌面环境, 没有这些变量, 显示系统语言.

    系统关联走 ShellExecute, **无法**为子进程单独注入环境, 只能在拉起前改本进程环境. 改一次即持久,
    之后 custom 模式的 ``Popen`` 与系统关联拿到的都是干净环境, 两种启动方式表现一致.

    非 Windows 不做: ``xdg-open`` 依赖 locale 选择本地化名称与文本编码.
    """

    if sys.platform != "win32":
        return
    for key in _LOCALE_ENV_KEYS:
        os.environ.pop(key, None)


def _clean_env() -> dict[str, str] | None:
    """``custom`` 模式的子进程环境: 非 Windows 保持父进程环境, 原样返回 ``None``."""
    if sys.platform != "win32":
        return None
    env = os.environ.copy()
    for key in _LOCALE_ENV_KEYS:
        env.pop(key, None)
    return env


def _spawn(argv: list[str]) -> None:
    """拉起后立刻返回. 三个流都断开, 避免子进程继承 Web 服务的管道."""
    subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        env=_clean_env(),
    )


def _playlist_dir() -> Path:
    directory = Path(tempfile.gettempdir()) / _PLAYLIST_DIR_NAME
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _write_playlist(targets: Sequence[Path]) -> Path:
    """为系统关联写一份 ``.m3u``: 关联程序只接受一个路径, 列表语义由文件本身承载.

    内容用 ``#EXTM3U`` + 一行一个绝对路径, UTF-8 编码 — 这是 VLC / mpv / PotPlayer 共通的读法.
    ``.m3u`` 而非 ``.m3u8``: 后者容易被当成 HLS 清单.
    """

    directory = _playlist_dir()
    now = time.time()
    for stale in directory.glob("*.m3u"):
        try:
            if now - stale.stat().st_mtime > _PLAYLIST_TTL_S:
                stale.unlink()
        except OSError:
            # 正被播放器占用的旧文件删不掉, 留着即可, 下次再试.
            continue

    target = directory / f"amane-{int(now)}-{os.getpid()}.m3u"
    target.write_text(
        "#EXTM3U\n" + "\n".join(str(item) for item in targets) + "\n",
        encoding="utf-8",
    )
    return target


def _open_with_association(target: Path) -> None:
    """交给操作系统的默认关联程序. 不走 ``cmd``, 也不解析命令行参数."""
    try:
        if sys.platform == "win32":
            # ShellExecute: 与资源管理器里双击同一个入口.
            os.startfile(target)
        elif sys.platform == "darwin":
            _spawn(["open", str(target)])
        else:
            _spawn(["xdg-open", str(target)])
    except OSError as exc:
        # Windows 无关联程序时是 winerror 1155, 原文是英文资源串, 换成可读原因.
        if getattr(exc, "winerror", None) == 1155:
            raise LocalPlayerError(f"系统没有与该格式关联的程序: {target.suffix or target.name}") from exc
        raise LocalPlayerError(f"无法用系统关联程序打开: {exc}") from exc


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

    _sanitize_process_env()

    if player:
        executable = Path(player).expanduser()
        if not executable.is_file():
            raise LocalPlayerError(f"播放器不存在: {executable}")
        _spawn([str(executable), *(str(item) for item in targets)])
        return LaunchResult(paths=targets, player=executable.name)

    # 系统关联只接受一个路径: 多文件时用一份临时列表承载, 否则只能播第一个.
    handle = targets[0] if len(targets) == 1 else _write_playlist(targets)
    _open_with_association(handle)
    return LaunchResult(paths=targets, player=None)
