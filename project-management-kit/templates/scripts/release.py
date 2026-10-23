#!/usr/bin/env python3
"""发版管线的预检、白名单提交与 CHANGELOG 抽取.

依赖 `uv version` 读取与递增版本; 换语言栈时只需替换 `read_version` 与 `next_version`.
对外入口由 Justfile 的 bump recipe 编排, 不要在本地手改版本或手打 tag.

用法::

    python scripts/release.py precheck patch [--dry-run]
    python scripts/release.py commit
    python scripts/release.py extract 1.2.3
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Literal

ROOT = Path(__file__).resolve().parents[1]
BumpKind = Literal["major", "minor", "patch"]
BUMP_CHOICES: tuple[BumpKind, ...] = ("major", "minor", "patch")

# 整段 bump 结束后只允许这些相对路径出现在变更里; 以 / 结尾的是目录前缀.
ALLOWED_PATHS: frozenset[str] = frozenset(
    {
        "CHANGELOG.md",
        "pyproject.toml",
        "uv.lock",
        "openapi.json",
        "web/src/client/",
    }
)

# precheck 允许已写好但未提交的 changelog; 其它脏文件仍拒绝.
PREBUMP_ALLOWED_PATHS: frozenset[str] = frozenset({"CHANGELOG.md"})

_H2 = re.compile(r"^##[ \t]+(\S.*?)\s*$")
_VERSION_TITLE = re.compile(r"^\[?v?(\d+\.\d+\.\d+)\]?(?:\s+.*)?$", re.IGNORECASE)


class ReleaseError(Exception):
    """用户可见的失败, `main` 打印到 stderr 并以 1 退出."""


def run(cmd: list[str], cwd: Path) -> str:
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip() or f"exit {result.returncode}"
        raise ReleaseError(f"{' '.join(cmd)} 失败: {detail}")
    return result.stdout


def git(*args: str) -> str:
    return run(["git", *args], cwd=ROOT)


def changed_paths() -> frozenset[str]:
    diff = git("diff", "--name-only", "HEAD")
    untracked = git("ls-files", "--others", "--exclude-standard")
    paths: set[str] = set()
    for chunk in (diff, untracked):
        paths.update(line.strip() for line in chunk.splitlines() if line.strip())
    return frozenset(paths)


def path_allowed(path: str, allowed: frozenset[str]) -> bool:
    if path in allowed:
        return True
    return any(path.startswith(prefix) for prefix in allowed if prefix.endswith("/"))


def extra_paths(changed: frozenset[str], allowed: frozenset[str]) -> frozenset[str]:
    return frozenset(path for path in changed if not path_allowed(path, allowed))


def read_version(*, bump: BumpKind | None = None, dry_run: bool = False) -> str:
    cmd = ["uv", "version", "--output-format", "json", "--no-sync"]
    if bump is not None:
        cmd.extend(["--bump", bump])
    if dry_run:
        cmd.append("--dry-run")
    raw = run(cmd, cwd=ROOT)
    try:
        data: object = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ReleaseError(f"uv version 不是 JSON: {raw!r}") from exc
    if not isinstance(data, dict):
        raise ReleaseError(f"uv version JSON 应为 object: {raw!r}")
    version = data.get("version")
    if not isinstance(version, str) or not version:
        raise ReleaseError(f"uv version 缺少 version: {raw!r}")
    return version


def tag_name(version: str) -> str:
    return f"v{version}"


def tag_exists(name: str) -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/tags/{name}"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def normalize_version(raw: str) -> str:
    text = raw.strip()
    return text[1:] if text.startswith(("v", "V")) else text


def split_sections(text: str) -> list[tuple[str, str]]:
    """返回 (标题不含 ##, 整节含标题行)."""
    sections: list[tuple[str, str]] = []
    title: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        nonlocal title, buffer
        if title is not None:
            sections.append((title, "".join(buffer)))
        title = None
        buffer = []

    for line in text.splitlines(keepends=True):
        match = _H2.match(line.rstrip("\r\n"))
        if match is not None:
            flush()
            title = match.group(1).strip()
            buffer = [line]
        elif title is not None:
            buffer.append(line)
    flush()
    return sections


def extract_section(version: str) -> str:
    path = ROOT / "CHANGELOG.md"
    if not path.is_file():
        raise ReleaseError("缺少 CHANGELOG.md")
    want = normalize_version(version)
    for title, section in split_sections(path.read_text(encoding="utf-8")):
        match = _VERSION_TITLE.match(title)
        if match is None or match.group(1) != want:
            continue
        stripped = section.strip()
        lines = stripped.split("\n", 1)
        if len(lines) < 2 or not lines[1].strip():
            raise ReleaseError(f"CHANGELOG 中 v{want} 节为空")
        return stripped + "\n"
    raise ReleaseError(f"CHANGELOG 中没有 v{want} 节")


def precheck(kind: BumpKind, *, dry_run: bool) -> None:
    changed = changed_paths()
    unexpected = extra_paths(changed, PREBUMP_ALLOWED_PATHS)
    if unexpected:
        raise ReleaseError(f"工作区不干净, 拒绝 bump: {', '.join(sorted(unexpected))}")

    current = read_version()
    new = read_version(bump=kind, dry_run=True)
    if new == current:
        raise ReleaseError(f"版本未变化: {current}")
    if tag_exists(tag_name(new)):
        raise ReleaseError(f"tag 已存在: {tag_name(new)}")
    extract_section(new)

    print(f"{current} => {new}")
    print(f"commit: release: {new}")
    print(f"tag: {tag_name(new)}")
    if dry_run:
        print("dry-run: 未写入")


def commit_release() -> None:
    version = read_version()
    tag = tag_name(version)
    if tag_exists(tag):
        raise ReleaseError(f"tag 已存在: {tag}")
    extract_section(version)

    changed = changed_paths()
    unexpected = extra_paths(changed, ALLOWED_PATHS)
    if unexpected:
        raise ReleaseError(f"bump 改动了白名单以外的文件, 已中止提交: {', '.join(sorted(unexpected))}")

    to_add = sorted(path for path in changed if path_allowed(path, ALLOWED_PATHS))
    if "pyproject.toml" not in to_add:
        raise ReleaseError("版本文件未变化")
    git("add", "--", *to_add)
    git("commit", "-m", f"release: {version}")
    git("tag", "-a", tag, "-m", tag)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="step", required=True)

    pre = sub.add_parser("precheck", help="工作区干净、下一版本节存在、tag 不冲突")
    pre.add_argument("kind", choices=BUMP_CHOICES)
    pre.add_argument("--dry-run", action="store_true", help="只预览, 不写文件")

    sub.add_parser("commit", help="按白名单 git add / commit / tag")

    extract = sub.add_parser("extract", help="打印指定版本的 CHANGELOG 节")
    extract.add_argument("version")

    args = parser.parse_args(argv)
    try:
        if args.step == "precheck":
            precheck(args.kind, dry_run=args.dry_run)
        elif args.step == "commit":
            commit_release()
        else:
            sys.stdout.write(extract_section(args.version))
    except ReleaseError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
