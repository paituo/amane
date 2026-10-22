#!/usr/bin/env python3
"""为 Orca 新建的工作区准备可运行的 amane 开发环境.

Orca 在新建工作树后自动调用本脚本 (见仓库根 ``orca.yaml`` 的 ``scripts.setup``).

工作内容:

1. 整体复制主仓库的 ``data/``: 配置、业务库、agent 会话历史、远程缓存一并带过去.
   ``data/`` 里不再有体积巨大的资源缓存与模型权重 —— 那两样已移到 ``data`` 之外,
   由 ``.env`` 的 ``AMANE_RESOURCES_DIR`` / ``AMANE_MODELS_DIR`` 指向共享目录.
2. 生成 ``.env``: 以主仓库的 ``.env`` (或 ``.env.dev``) 为模板, 把上述两个共享目录
   写成主仓库正在用的值 —— 于是每个工作区读同一批图片与同一份模型权重, 不必各存一份.
3. 同步 Python 与前端依赖 (``just setup``).

库文件要带过去: 新库虽然能自建出同样的表结构, 但没有数据, 刮削过的条目、收藏、
设置全要重来. 复制前需保证主仓库进程已退出, 否则 WAL 里可能有未落盘的事务.

设计约束:

* **不依赖 cwd**. Orca 调用 hook 时的工作目录未承诺, 路径由本文件的 ``__file__`` 反推.
* **不碰主仓库**. 全程只写当前工作区; 主仓库只读.
* **可重复执行**. 已有文件默认不覆盖, 避免开发者手改的配置被 setup 轮次冲掉.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
"""本脚本位于 ``<仓库根>/scripts/``, 因此仓库根是上两级.

不取 cwd: Orca 调用 hook 时的工作目录未承诺, 依赖 cwd 会把文件写到错误的位置.
"""

DATA_DIRNAME = "data"

SHARED_DIR_VARS = ("AMANE_RESOURCES_DIR", "AMANE_MODELS_DIR")
"""指向共享目录的配置项: 资源缓存与模型权重. 值从主仓库继承, 保证两边读到同一份."""

SKIP_COPY = ("resources", "models")
"""即使出现在 ``data/`` 里也不复制的子目录.

正常布局下这两样已在 ``data`` 之外, 列在这里是给"还没搬的旧检出"兜底:
照抄一份数 GB 缓存既慢又费盘, 而 ``.env`` 已经指向共享目录, 抄了也用不上.
"""


def log(message: str) -> None:
    print(f"[amane-setup] {message}", flush=True)  # noqa: T201


def warn(message: str) -> None:
    print(f"[amane-setup] 警告: {message}", file=sys.stderr, flush=True)  # noqa: T201


def is_main_checkout(path: Path) -> bool:
    """``path`` 是否是主仓库 (而非任一工作树)."""
    # 工作树的 .git 是文件 (内含 gitdir 指针), 主仓库的是目录.
    return (path / ".git").is_dir()


def find_main_checkout(start: Path) -> Path | None:
    """沿 git worktree 的 gitdir 指针回溯主仓库.

    工作树的 ``.git`` 文件形如 ``gitdir: <主仓库>/.git/worktrees/<name>``,
    从该路径向上找第一个含 ``.git`` 目录的祖先即可.
    """
    dot_git = start / ".git"
    if not dot_git.is_file():
        return None
    match = re.search(r"^gitdir:\s*(.+)$", dot_git.read_text(encoding="utf-8", errors="replace"), re.MULTILINE)
    if not match:
        return None
    for parent in Path(match.group(1).strip()).resolve().parents:
        if is_main_checkout(parent):
            return parent
    return None


def read_env_file(path: Path) -> dict[str, str]:
    """解析 KEY=VALUE 形式的 env 文件, 忽略空行与注释."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip()
    return values


def set_env_var(env_path: Path, key: str, value: str) -> None:
    """写入或替换 env 文件中的单个键, 保留其余行与顺序."""
    lines = env_path.read_text(encoding="utf-8", errors="replace").splitlines() if env_path.exists() else []
    kept = [line for line in lines if not re.match(rf"^\s*{re.escape(key)}\s*=", line)]
    kept.append(f"{key}={value}")
    env_path.write_text("\n".join(kept) + "\n", encoding="utf-8")


def resolve_shared_var(main: Path, key: str, fallback: Path) -> Path:
    """解析共享目录配置项: 环境变量 > 主仓库 ``.env`` > ``fallback``.

    ``fallback`` 是"该配置项没设时程序自己会用的位置", 因此没设时它就是真实位置,
    不会把工作区指到一个空目录.
    """
    override = os.environ.get(key)
    if override and override.strip():
        return Path(override.strip())
    configured = read_env_file(main / ".env").get(key)
    if configured:
        return Path(configured)
    return fallback


def copy_data_dir(main: Path, *, force: bool) -> None:
    """整体复制主仓库的 ``data/``, 跳过共享目录与库的临时旁文件."""
    source_dir = main / DATA_DIRNAME
    if not source_dir.is_dir():
        warn(f"主仓库没有 {source_dir}, 跳过数据复制")
        return
    target_dir = ROOT / DATA_DIRNAME
    target_dir.mkdir(parents=True, exist_ok=True)

    copied = 0
    skipped = 0
    for entry in sorted(source_dir.iterdir()):
        if entry.name in SKIP_COPY:
            log(f"跳过 {DATA_DIRNAME}/{entry.name} (已移到 data 之外共享)")
            continue
        target = target_dir / entry.name
        if target.exists() and not force:
            skipped += 1
            continue
        if entry.is_dir():
            shutil.copytree(entry, target, dirs_exist_ok=True)
        else:
            shutil.copy2(entry, target)
        copied += 1

    if copied:
        log(f"已复制 {DATA_DIRNAME}/ 下 {copied} 项")
    if skipped:
        log(f"{DATA_DIRNAME}/ 下 {skipped} 项已存在, 保留现有文件")


def check_wal_checkpoint_free(db_path: Path) -> bool:
    """主库旁是否残留未落盘的 WAL.

    直接用 ``sqlite3`` 读文件头判断不够可靠, 这里只用文件大小做保守提示:
    ``-wal`` 非空意味着主进程可能没来得及 checkpoint, 此时复制到的库里少了那部分事务.
    """
    wal = db_path.with_name(db_path.name + "-wal")
    try:
        return wal.stat().st_size == 0
    except FileNotFoundError:
        return True


def warn_if_db_busy(main: Path) -> None:
    """复制前提示主仓库可能仍在运行 —— 运行中的库拷出来可能是半截状态."""
    db_path = main / DATA_DIRNAME / "amane.db"
    if not db_path.is_file():
        return
    if check_wal_checkpoint_free(db_path):
        return
    warn("主仓库 amane.db 的 WAL 非空, 可能有进程正在写入; 复制到的库或许缺少最近的事务")
    warn("建议先退出主仓库的服务再建工作区, 或事后在工作区里重新迁移")


def verify_sqlite_copy() -> None:
    """用 SQLite 的完整性检查把复制结果验一遍.

    只对能打开且确实是 SQLite 的文件做: 复制期间被截断的文件会在这里报错,
    而不是等到新工作区启动时才崩.
    """
    checked = 0
    for db_path in sorted((ROOT / DATA_DIRNAME).glob("*.db")):
        checked += 1
        try:
            with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
                result = conn.execute("PRAGMA quick_check").fetchone()
        except sqlite3.DatabaseError as exc:
            warn(f"{db_path.name} 无法打开: {exc}; 建议删除后由程序自建, 或重新复制")
            continue
        if result and result[0] != "ok":
            warn(f"{db_path.name} 完整性检查未通过: {result[0]}")
    if checked:
        log(f"已校验 {DATA_DIRNAME}/ 下的 {checked} 个 SQLite 文件")


def ensure_env_file(main: Path, *, force: bool) -> None:
    """生成工作区 ``.env``: 以主仓库为模板, 再写入两个共享目录配置."""
    target = ROOT / ".env"
    if not target.exists() or force:
        for candidate in (main / ".env", main / ".env.dev"):
            if candidate.is_file():
                shutil.copy2(candidate, target)
                log(f"已由 {candidate.name} 生成 .env")
                break
        else:
            target.write_text("", encoding="utf-8")
            log("主仓库无 .env/.env.dev, 已创建空 .env")

    fallbacks = {
        "AMANE_RESOURCES_DIR": main / DATA_DIRNAME / "resources",
        "AMANE_MODELS_DIR": main / DATA_DIRNAME / "models",
    }
    for key in SHARED_DIR_VARS:
        shared = resolve_shared_var(main, key, fallbacks[key])
        if not shared.is_dir():
            warn(f"共享目录不存在: {shared}; 首次使用时会自动创建")
        set_env_var(target, key, shared.as_posix())
        log(f".env {key}={shared.as_posix()}")


def install_dependencies(*, enabled: bool) -> None:
    if not enabled:
        log("按参数跳过依赖安装")
        return
    if not (ROOT / "Justfile").is_file():
        warn("未找到 Justfile, 跳过依赖安装")
        return
    try:
        result = subprocess.run(["just", "setup"], cwd=ROOT, check=False)
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

    log(f"工作区: {ROOT}")
    if not (ROOT / "Justfile").is_file():
        warn(f"{ROOT} 不像 amane 仓库根 (缺 Justfile), 退出")
        return 1
    if is_main_checkout(ROOT):
        log("当前就是主仓库, 无需准备; 退出")
        return 0

    main_checkout = find_main_checkout(ROOT)
    if main_checkout is None:
        warn("无法定位主仓库, 仅生成本地 .env")
        ensure_env_file(ROOT, force=args.force)
        return 0

    # 保护: 任何情况下都不写主仓库, 主仓库只读.
    if main_checkout.resolve() == ROOT.resolve():
        warn("工作区与主仓库同路径, 拒绝写入")
        return 1

    log(f"主仓库: {main_checkout}")
    warn_if_db_busy(main_checkout)
    copy_data_dir(main_checkout, force=args.force)
    verify_sqlite_copy()
    ensure_env_file(main_checkout, force=args.force)
    install_dependencies(enabled=not args.skip_install)
    log("完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
