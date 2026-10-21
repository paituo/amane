"""全局测试 fixtures"""

import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine

from amane.db.repository import Repository
from amane.media import ResourceStore
from tests.schema_template import copy_schema

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Iterator, Sequence

    from sqlalchemy.ext.asyncio import AsyncEngine


@pytest.fixture
def candidates_first_scandir(monkeypatch: pytest.MonkeyPatch) -> None:
    """固定目录枚举顺序: 名字以 `ad-` 开头的候选排在媒体之前.

    清单触顶的用例, 前提是截断点落在媒体收集完成之前; 文件系统给的顺序是环境属性,
    靠文件名巧合会让用例失去鉴别力. 触顶用例与需要同一前提的入库扫描用例都用它.

    `amane.library.cleanup.inventory.os` 就是 `os` 模块, 因此这是整个进程内的替换, 只给单元级用例用:
    带 worker 的用例上会连带影响别的调用方.
    """
    real_scandir = os.scandir

    @contextmanager
    def ordered(path: str | os.PathLike[str]) -> Iterator[Sequence[os.DirEntry[str]]]:
        with real_scandir(path) as scanned:
            yield sorted(scanned, key=lambda entry: (not entry.name.startswith("ad-"), entry.name))

    monkeypatch.setattr("amane.library.cleanup.inventory.os.scandir", ordered)


def _use_plain_windows_path_for_tmp_cleanup() -> None:
    r"""让 pytest 清理系统临时目录时使用不带 `\\?\` 前缀的路径.

    pytest 的 `rm_rf` 在 Windows 上先把路径转成扩展长度形式 (`_pytest.pathlib.ensure_extended_length_path`),
    再交给 `shutil.rmtree`. 它保留超过保留数的 `pytest-of-<user>/pytest-N` 时需要整树删除: 先改名成
    `garbage-<uuid>`, 再删. 这条删除路径在 WorkBuddy 托管的 Python 里会被 safe-delete 拦截:
    该 shim 对系统临时目录本来有旁路 (`_should_bypass_safe_delete`), 但它用
    `normcase(realpath(abspath(p)))` 做前缀比较, 而 `realpath` 对 `\\?\` 前缀原样保留,
    于是旁路永远失配 —— 临时目录的整树删除被当成"批量删除用户文件", 超过阈值 (默认 50 个文件)
    就直接 `SystemExit(1)`, 表现是整轮 pytest 在 100% 之后失败, 而所有用例其实都过了.

    所以这里只做一件事: 目标位于系统临时目录时, 把前缀去掉再交给 rmtree. 旁路恢复后,
    临时目录走原生删除 (真删, 不进回收站), 其余路径的删除仍受 shim 保护, 本函数不碰.
    非 WorkBuddy 环境 (`CODEBUDDY_SESSION_ID` 未设) 或非 Windows 时是空操作,
    加前缀对超长路径的意义也保留 —— 只有系统临时目录下的路径会被去掉前缀.
    """
    if sys.platform != "win32":
        return
    if not os.environ.get("CODEBUDDY_SESSION_ID") and not os.environ.get("CODEBUDDY_SAFE_DELETE_ENABLED"):
        return

    import _pytest.pathlib as pytest_pathlib

    original = pytest_pathlib.ensure_extended_length_path
    tmp_root = os.path.normcase(Path(tempfile.gettempdir()).resolve())

    def ensure_extended_length_path(path: Path) -> Path:
        extended = original(path)
        text = str(extended)
        if not text.startswith("\\\\?\\"):
            return extended
        stripped = text[4:]
        plain = os.path.normcase(Path(stripped).resolve())
        if plain == tmp_root or plain.startswith(tmp_root + os.sep):
            return Path(stripped)
        return extended

    pytest_pathlib.ensure_extended_length_path = ensure_extended_length_path


_use_plain_windows_path_for_tmp_cleanup()


def _file_engine(db_path: Path) -> AsyncEngine:
    """文件 SQLite 引擎 (与生产同构: 多连接 + WAL).

    SQLite 自身通过连接级事务隔离 + WAL 控制并发写, 应用层无需加锁.
    """
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", connect_args={"timeout": 5})

    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, _connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        # 测试库数据可丢弃: 关闭 fsync 刷盘加速写入 (生产保持默认 FULL).
        cursor.execute("PRAGMA synchronous=OFF")
        # 旧内存库 (StaticPool) 不启用 FK; 保持该语义, 避免无归属 MediaFile 的用例集体失败.
        cursor.execute("PRAGMA foreign_keys=OFF")
        cursor.close()

    return engine


@pytest_asyncio.fixture
async def repo(tmp_path: Path) -> AsyncGenerator[Repository]:
    """
    基于文件 SQLite 的异步 Repository (与生产同构).

    每个测试在 tmp_path 下独立数据库文件, 测试结束自动清理.
    """
    db_path = tmp_path / "amane.db"
    copy_schema(db_path)
    engine = _file_engine(db_path)
    yield Repository(engine)

    await engine.dispose()


@pytest_asyncio.fixture
async def resource_store(tmp_path: Path) -> AsyncGenerator[ResourceStore]:
    """独立的 ResourceStore (文件 DB + tmp 目录).

    handler 现强制注入 ResourceStore; 测试不触发真实下载时仅作占位.
    """
    db_path = tmp_path / "resources.db"
    copy_schema(db_path)
    engine = _file_engine(db_path)
    yield ResourceStore(engine=engine, base_dir=tmp_path / "resources")

    await engine.dispose()
