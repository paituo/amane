#!/usr/bin/env python3
"""为 Orca 新建的工作区准备可运行的 amane 开发环境.

Orca 在新建工作树后自动调用本脚本 (见仓库根 ``orca.yaml`` 的 ``scripts.setup``).
工作内容:

1. 从主仓库 ``data/`` 复制**配置类**文件 (``config.toml`` 等), 不复制资源与数据库,
   让每个工作区拥有独立、干净的数据目录.
2. 补齐 ``.env``: 主仓库存在则原样复制, 否则由 ``.env.dev`` 派生, 并写入本工作区专属端口.
3. 同步 Python 与前端依赖 (``just setup``).

设计约束:

* **不依赖 cwd**. Orca 调用 hook 时的工作目录未承诺, 路径一律由本文件的 ``__file__`` 反推.
* **不碰主仓库**. 全程只写当前工作区; 主仓库 ``D:/PythonProjects/amane`` 只读.
* **可重复执行**. 已有文件默认不覆盖, 避免开发者手改的配置被 setup 轮次冲掉.
"""

from __future__ import annotations

import argparse
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

WORKTREE_ROOT = Path(__file__).resolve().parent.parent
"""本脚本位于 ``<仓库根>/scripts/``, 因此仓库根是上两级.

不取 cwd: Orca 调用 hook 时的工作目录未承诺, 依赖 cwd 会把文件写到错误的位置.
"""

CONFIG_DIR = "data"
CONFIG_FILES = ("config.toml",)
"""从主仓库 ``data/`` 复制到工作区的配置文件.

只列配置: ``resources/`` ``models/`` ``*.db`` 属于数据资产, 由每个工作区自建, 复制它们
既慢又会制造多份互不相关的真实数据副本.
"""

DEFAULT_PORT = 8000
"""``Justfile`` 与 ``server.py`` 的默认端口."""

PORT_SCAN_LIMIT = 50
"""自动分配端口时最多向后探测的端口数."""


def log(message: str) -> None:
    print(f"[amane-setup] {message}", flush=True)  # noqa: T201


def warn(message: str) -> None:
    print(f"[amane-setup] 警告: {message}", file=sys.stderr, flush=True)  # noqa: T201


def is_main_checkout(path: Path) -> bool:
    """``path`` 是否是主仓库 (而非任一工作树)."""
    dot_git = path / ".git"
    # 工作树的 .git 是文件 (内含 gitdir 指针), 主仓库的是目录.
    return dot_git.is_dir()


def find_main_checkout(start: Path) -> Path | None:
    """沿 git worktree 的 gitdir 指针回溯到主仓库.

    工作树的 ``.git`` 文件形如 ``gitdir: <主仓库>/.git/worktrees/<name>``;
    向上查找包含 ``.git`` 目录的祖先即为主仓库.
    """
    dot_git = start / ".git"
    if dot_git.is_file():
        content = dot_git.read_text(encoding="utf-8", errors="replace")
        match = re.search(r"^gitdir:\s*(.+)$", content, re.MULTILINE)
        if match:
            gitdir = Path(match.group(1).strip()).resolve()
            for parent in gitdir.parents:
                if is_main_checkout(parent):
                    return parent
    return None


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return True
    return False


def pick_port(base: int) -> int:
    """从 ``base`` 起找第一个空闲端口; 全占用则回退 ``base`` 并告警."""
    for candidate in range(base, base + PORT_SCAN_LIMIT):
        if not port_in_use(candidate):
            return candidate
    warn(f"{base}..{base + PORT_SCAN_LIMIT} 均被占用, 仍使用 {base}")
    return base


def read_env_file(path: Path) -> dict[str, str]:
    """解析 KEY=VALUE 形式的 env 文件 (忽略空行与注释)."""
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip()
    return values


def copy_config_files(main: Path, *, force: bool) -> None:
    """复制主仓库 ``data/`` 下的配置文件, 不触碰数据资产."""
    source_dir = main / CONFIG_DIR
    if not source_dir.is_dir():
        warn(f"主仓库没有 {source_dir}, 跳过配置复制")
        return
    target_dir = WORKTREE_ROOT / CONFIG_DIR
    for name in CONFIG_FILES:
        source = source_dir / name
        if not source.is_file():
            warn(f"主仓库缺少 {source.name}, 跳过")
            continue
        target = target_dir / name
        if target.exists() and not force:
            log(f"{CONFIG_DIR}/{name} 已存在, 保留现有文件")
            continue
        target_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        log(f"已复制 {CONFIG_DIR}/{name}")


def ensure_env_file(main: Path, *, force: bool) -> None:
    """生成工作区 ``.env``, 并保证其中带有一个可用的 ``AMANE_PORT``."""
    target = WORKTREE_ROOT / ".env"
    if target.exists() and not force:
        log(".env 已存在, 仅校正端口")
        write_port(target, force=False)
        return

    for candidate in (main / ".env", main / ".env.dev"):
        if candidate.is_file():
            shutil.copy2(candidate, target)
            log(f"已由 {candidate.name} 生成 .env")
            write_port(target, force=True)
            return

    target.write_text("", encoding="utf-8")
    log("主仓库无 .env/.env.dev, 已创建空 .env")
    write_port(target, force=True)


def write_port(env_path: Path, *, force: bool) -> None:
    """确保 ``.env`` 里有一个未被占用的 ``AMANE_PORT``.

    ``force`` 为真时重新分配并覆盖; 否则仅在缺失时补写, 避免每次 setup 都改端口.
    """
    values = read_env_file(env_path) if env_path.exists() else {}
    existing = values.get("AMANE_PORT")
    if existing and not force:
        if not existing.isdigit():
            warn(f".env 中 AMANE_PORT={existing} 不是合法端口, 重新分配")
        elif port_in_use(int(existing)):
            warn(f".env 中 AMANE_PORT={existing} 已被占用, 重新分配")
        else:
            log(f".env AMANE_PORT={existing} 可用")
            return

    port = pick_port(DEFAULT_PORT)
    lines = env_path.read_text(encoding="utf-8", errors="replace").splitlines() if env_path.exists() else []
    rewritten = [line for line in lines if not re.match(r"^\s*AMANE_PORT\s*=", line)]
    rewritten.append(f"AMANE_PORT={port}")
    env_path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")
    log(f".env AMANE_PORT={port}")


def install_dependencies(*, enabled: bool) -> None:
    script = WORKTREE_ROOT / "Justfile"
    if not enabled:
        log("按参数跳过依赖安装")
        return
    if not script.is_file():
        warn("未找到 Justfile, 跳过依赖安装")
        return
    try:
        result = subprocess.run(["just", "setup"], cwd=WORKTREE_ROOT, check=False)
    except FileNotFoundError:
        warn("未找到 just, 跳过依赖安装; 请手动执行 just setup")
        return
    if result.returncode != 0:
        warn(f"just setup 退出码 {result.returncode}, 请手动检查")
    else:
        log("依赖安装完成")


def main() -> int:
    parser = argparse.ArgumentParser(description="准备工作区级的 amane 开发环境")
    parser.add_argument("--force", action="store_true", help="覆盖已存在的配置与 .env")
    parser.add_argument("--skip-install", action="store_true", help="跳过 just setup")
    args = parser.parse_args()

    log(f"工作区: {WORKTREE_ROOT}")
    if not (WORKTREE_ROOT / "Justfile").is_file():
        warn(f"{WORKTREE_ROOT} 不像 amane 仓库根 (缺 Justfile), 退出")
        return 1
    if is_main_checkout(WORKTREE_ROOT):
        log("当前就是主仓库, 无需准备; 退出")
        return 0

    main_checkout = find_main_checkout(WORKTREE_ROOT)
    if main_checkout is None:
        warn("无法定位主仓库, 仅生成本地 .env")
        ensure_env_file(WORKTREE_ROOT, force=args.force)
        return 0

    # 保护: 任何情况下都不写主仓库, 主仓库只读.
    if main_checkout.resolve() == WORKTREE_ROOT.resolve():
        warn("工作区与主仓库同路径, 拒绝写入")
        return 1

    log(f"主仓库: {main_checkout}")
    copy_config_files(main_checkout, force=args.force)
    ensure_env_file(main_checkout, force=args.force)
    install_dependencies(enabled=not args.skip_install)
    log("完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
