"""Everything (es.exe) 索引查询: 把索引里的文件列表变成库入库候选.

只做"列出某库根下有哪些媒体文件", 不做删除/移动判定; 那两类仍以磁盘或其它通道为准.
``es.exe`` 的输出编码与退出码是这里唯一的耦合点, 都收敛在 ``EverythingClient``.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from pathlib import Path

#: ``es.exe`` 找不到 Everything IPC 时的退出码 (实测 "Error 8: Everything IPC not found").
_EXIT_IPC_NOT_FOUND = 8

#: 常见安装位置; 仅在未显式配置 ``executable`` 时探测.
_CANDIDATE_EXECUTABLES = (
    Path(r"C:\Program Files\Everything\es.exe"),
    Path(r"C:\Program Files (x86)\Everything\es.exe"),
    Path(r"C:\Program Files\Everything 1.5a\es.exe"),
)

#: CREATE_NO_WINDOW; 不引用 subprocess 的同名属性以便跨平台导入.
_CREATE_NO_WINDOW = 0x08000000


class EverythingUnavailable(RuntimeError):
    """来源不可用: 非 Windows、``es.exe`` 缺失、Everything 未运行或未建索引."""


class EverythingError(RuntimeError):
    """``es.exe`` 以非预期状态结束, 或输出无法解析."""


@dataclass(frozen=True, slots=True)
class EverythingHit:
    """索引里的一个文件. ``size`` 未知时为 ``None`` (索引尚未取到属性)."""

    path: Path
    size: int | None = None


@dataclass(frozen=True, slots=True)
class CommandResult:
    """子进程结果. stdout/stderr 已解码, 供错误消息使用."""

    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[Sequence[str], float], CommandResult]


def _default_runner(args: Sequence[str], timeout: float) -> CommandResult:
    completed = subprocess.run(
        list(args),
        capture_output=True,
        timeout=timeout,
        check=False,
        creationflags=_CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    return CommandResult(
        returncode=completed.returncode,
        stdout=completed.stdout.decode(errors="replace"),
        stderr=completed.stderr.decode(errors="replace"),
    )


def extension_query(extensions: Collection[str]) -> str:
    """把扩展名集合变成 Everything 的 ``ext:`` 查询.

    空集合返回空串; 调用方据此跳过枚举, 避免把非媒体文件当成候选.
    """
    normalized = sorted({ext.lower().lstrip(".") for ext in extensions if ext.strip(".")})
    if not normalized:
        return ""
    return "ext:" + ";".join(normalized)


class EverythingClient:
    """一次性查询封装. 无状态, 可被任意线程调用 (每次调用独立子进程)."""

    def __init__(
        self,
        executable: str | Path | None = None,
        *,
        instance: str = "",
        timeout_ms: int = 5000,
        wall_timeout: float = 120.0,
        runner: Runner | None = None,
    ) -> None:
        self._executable = Path(executable) if executable else None
        self._instance = instance
        self._timeout_ms = timeout_ms
        self._wall_timeout = wall_timeout
        self._runner = runner or _default_runner

    @property
    def executable(self) -> Path | None:
        """已配置或探测到的 ``es.exe``; 不可用时为 ``None``."""
        if self._executable is not None:
            return self._executable if self._executable.is_file() else None
        if os.name != "nt":
            return None
        for candidate in _CANDIDATE_EXECUTABLES:
            if candidate.is_file():
                return candidate
        return None

    def search(
        self,
        root: Path,
        extensions: Collection[str],
        *,
        recursive: bool = True,
        limit: int | None = None,
    ) -> list[EverythingHit]:
        """列出 ``root`` 下的媒体文件.

        ``recursive=False`` 时只留直接子文件. 索引滞后于磁盘时可能返回已不存在的路径,
        由调用方 ``stat`` 或后续清理兜底.
        """
        executable = self.executable
        if executable is None:
            raise EverythingUnavailable("未找到 es.exe, 或当前平台不支持 Everything 来源")

        query = extension_query(extensions)
        if not query:
            return []

        with tempfile.TemporaryDirectory(prefix="amane-everything-") as tmp:
            out = Path(tmp) / "result.json"
            result = self._runner(self._build_args(executable, out, root, query, limit), self._wall_timeout)
            if result.returncode == _EXIT_IPC_NOT_FOUND:
                raise EverythingUnavailable(
                    "Everything 未运行或 IPC 不可达: " + (result.stdout or result.stderr).strip()
                )
            if result.returncode != 0:
                raise EverythingError(f"es.exe 退出码 {result.returncode}: " + (result.stderr or result.stdout).strip())
            return self._parse_output(out, root, recursive=recursive)

    def _build_args(self, executable: Path, out: Path, root: Path, query: str, limit: int | None) -> list[str]:
        args = [
            str(executable),
            "-export-json",
            str(out),
            "-utf8-bom",
            "-full-path-and-name",
            "-size",
            "-path",
            str(root),
            "/a-d",
            "-timeout",
            str(self._timeout_ms),
        ]
        if self._instance:
            args.extend(["-instance", self._instance])
        if limit is not None and limit > 0:
            args.extend(["-n", str(limit)])
        # 搜索词必须是最后一个参数; 放在选项之前会被当作选项值吞掉.
        args.append(query)
        return args

    def _parse_output(self, out: Path, root: Path, *, recursive: bool) -> list[EverythingHit]:
        if not out.is_file():
            return []
        # 导出固定带 BOM; 无 BOM 的旧版本也能读.
        raw = out.read_text(encoding="utf-8-sig").strip()
        if not raw:
            return []
        try:
            rows = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise EverythingError(f"es.exe 输出不是合法 JSON: {exc}") from exc
        if not isinstance(rows, list):
            raise EverythingError("es.exe 输出不是 JSON 数组")

        hits: list[EverythingHit] = []
        for row in rows:
            hit = _hit_from_row(row)
            if hit is None:
                continue
            if not _is_under(hit.path, root):
                continue
            if not recursive and not _is_direct_child(hit.path, root):
                continue
            hits.append(hit)
        return hits


def _hit_from_row(row: object) -> EverythingHit | None:
    if not isinstance(row, dict):
        return None
    filename = row.get("filename")
    if not isinstance(filename, str) or not filename.strip():
        return None
    # 目录在 es 输出里以分隔符结尾; /a-d 已排除, 这里兜底防止配置漂移.
    if filename != filename.rstrip("\\/"):
        return None
    trimmed = filename.strip()
    size = row.get("size")
    return EverythingHit(path=Path(trimmed), size=int(size) if isinstance(size, int | float) else None)


def _norm(value: Path) -> str:
    return os.path.normcase(os.path.normpath(str(value)))


def _is_under(path: Path, root: Path) -> bool:
    candidate = _norm(path)
    base = _norm(root)
    return candidate == base or candidate.startswith(base + os.sep)


def _is_direct_child(path: Path, root: Path) -> bool:
    return _norm(path.parent) == _norm(root)
